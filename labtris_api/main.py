from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from labtris_api.config import settings
from labtris_api.version import __version__
from labtris_api.errors import ApiError, api_error_handler, unhandled_error_handler
from labtris_api.routers import (
    ai,
    auth,
    backup,
    capture,
    events,
    feedback,
    health,
    hooks,
    hosts,
    images,
    impair,
    labs,
    links,
    networks,
    nodes,
    p4,
    pods,
    system,
    tasks,
    templates,
    wireshark,
)
from labtris_api.routers import (
    settings as settings_router,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Stored overrides have to be live before the first request, or the first
    # AI call after a restart quietly uses the env default.
    # Correct anything that drifted while the process was not running — most
    # of all a host reboot, which takes every VM with it while the nodes go on
    # claiming they are up.
    try:
        from labtris_api.db import SessionLocal as _Session
        from labtris_api.reconcile import reconcile

        async with _Session() as _session:
            await reconcile(_session)
    except Exception:  # noqa: BLE001 - never let housekeeping stop the API booting
        pass

    try:
        from labtris_api.db import SessionLocal
        from labtris_api.routers.settings import apply_overrides

        async with SessionLocal() as session:
            await apply_overrides(session)
    except Exception:  # noqa: BLE001 - a missing table must not stop the API booting
        pass
    # Every lab that has hooks_source gets a fresh auto-fire watcher on
    # startup. Without this, a restart would leave hooks defined but never
    # firing until the user re-PUT the same spec.
    try:
        from labtris_api.routers.hooks import resume_watchers

        await resume_watchers()
    except Exception:  # noqa: BLE001 — hooks are opt-in; don't fail boot
        pass
    yield
    # Wireshark sessions are deliberately started in their own process session
    # so they survive a reload — which is exactly what makes them leak if
    # nothing reaps them on the way out.
    from labtris_api import wireshark as ws

    await ws.stop_all()

    # Cancel every long-lived background task we spawned via
    # asyncio.create_task from a request handler. Uvicorn's shutdown
    # hangs on any survivor until systemd's TimeoutStopSec (90s default)
    # fires and SIGKILLs the process — during which nginx returns 502 to
    # every new request because uvicorn stopped accept()ing. Naming the
    # tasks explicitly here keeps shutdown in seconds, not minutes.
    import asyncio
    import contextlib

    from labtris_api.routers.images import _pulls
    from labtris_api.runtime.bootstrap import _running as _bs_running
    from labtris_api.runtime.hooks import _running as _hooks_running
    from labtris_api.runtime.qemu import (
        _qmp_conns,
        _sessions as _serial_sessions,
    )

    for name, tasks in (
        ("image-pull", list(_pulls.values())),
        ("bootstrap", list(_bs_running.values())),
        ("hooks-watch", list(_hooks_running.values())),
    ):
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Serial-console pumps run one asyncio task per running VM and hold
    # a unix socket open. Cancel them so uvicorn's shutdown doesn't wait
    # for read() to return.
    for sess in list(_serial_sessions.values()):
        with contextlib.suppress(Exception):
            await sess.close()

    # QMP connection cache — just close the sockets; no task to await.
    for _, writer, _ in list(_qmp_conns.values()):
        with contextlib.suppress(Exception):
            writer.close()
    _qmp_conns.clear()

    # RFB framebuffer cache — same treatment. drop_cached_client cancels
    # the read loop and closes the socket for each entry.
    from labtris_api.runtime.vnc_cache import _clients as _rfb_clients, drop_cached_client

    for vm_dir in list(_rfb_clients.keys()):
        with contextlib.suppress(Exception):
            await drop_cached_client(vm_dir)


def create_app() -> FastAPI:
    app = FastAPI(title="labtris", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(_announce_topology_on_mutation)
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    # Without this, an unhandled exception returns Starlette's plain-text
    # default and every JSON client chokes on the response rather than the bug.
    app.add_exception_handler(Exception, unhandled_error_handler)

    prefix = "/api/v1"
    app.include_router(auth.router, prefix=prefix)
    app.include_router(health.router, prefix=prefix)
    app.include_router(labs.router, prefix=prefix)
    app.include_router(nodes.router, prefix=prefix)
    app.include_router(networks.router, prefix=prefix)
    app.include_router(links.router, prefix=prefix)
    app.include_router(impair.router, prefix=prefix)
    app.include_router(capture.router, prefix=prefix)
    app.include_router(system.router, prefix=prefix)
    app.include_router(ai.router, prefix=prefix)
    app.include_router(events.router, prefix=prefix)
    app.include_router(templates.router, prefix=prefix)
    app.include_router(images.router, prefix=prefix)
    app.include_router(tasks.router, prefix=prefix)
    app.include_router(hosts.router, prefix=prefix)
    app.include_router(wireshark.router, prefix=prefix)
    app.include_router(settings_router.router, prefix=prefix)
    app.include_router(backup.router, prefix=prefix)
    app.include_router(feedback.router, prefix=prefix)
    app.include_router(hooks.router, prefix=prefix)
    app.include_router(pods.router, prefix=prefix)
    app.include_router(p4.router, prefix=prefix)

    web_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/", _WebStatic(directory=web_dist, html=True), name="web")

    return app


_MUTATING = {"POST", "PATCH", "PUT", "DELETE"}


async def _announce_topology_on_mutation(request, call_next):
    """Publish a "topology" event on every successful mutation so the
    browser's lab WebSocket picks up shape changes made by the AI, the
    MCP client, or a script — not just node state changes.

    Without this, the assistant would add a node or draw a link and the
    canvas would sit stale until either (a) something eventually triggered
    a state change (start/stop), which publishes via lifecycle.announce(),
    or (b) the user reloaded. Since the assistant's whole job is to
    modify the topology, a stale canvas is the failure mode people notice
    first.

    The lab_id comes from whichever path param the matched route carries;
    if none of the topology-adjacent ids are present the mutation is not
    lab-scoped and we don't publish. When the id is a nested resource
    (node/network/link/interface/template) we look up its lab in one
    small query — cheap, and covers every current and future route
    without touching them one at a time.
    """
    # For DELETE, the row is gone by the time the response comes back, so
    # look up the owning lab_id *before* calling the downstream handler.
    # For every other mutation we defer the lookup until after — cheaper
    # (skipped on non-2xx) and reads the row post-write, which matters
    # for creates that only populate the id inside the handler.
    is_mutation = request.method in _MUTATING
    lab_id: str | None = None
    if is_mutation and request.method == "DELETE":
        lab_id = await _resolve_lab_id(request)

    response = await call_next(request)

    if not is_mutation:
        return response
    if not (200 <= response.status_code < 300):
        return response
    if lab_id is None:
        lab_id = await _resolve_lab_id(request)
    if not lab_id:
        return response
    from labtris_api.routers.events import announce_topology

    await announce_topology(lab_id)
    return response


async def _resolve_lab_id(request) -> str | None:
    """Find which lab a mutation touches from the request's path params.
    Direct `{lab_id}` wins; otherwise a nested id is looked up in the DB.
    None means "not a lab-scoped mutation" — the caller doesn't publish."""
    params = request.path_params or {}
    if lab_id := params.get("lab_id"):
        return lab_id
    for key in ("node_id", "network_id", "link_id", "iface_id", "template_id"):
        if other_id := params.get(key):
            resolved = await _lab_id_for(key, other_id)
            if resolved:
                return resolved
    return None


async def _lab_id_for(kind: str, obj_id: str) -> str | None:
    """Cheap lookup: the lab a nested resource belongs to. One indexed
    SELECT per mutation, executed only after the request has already
    succeeded — so a slow or missing DB never blocks the response the
    caller was going to get. Returns None on any error (best effort;
    the caller just skips the publish)."""
    try:
        from sqlalchemy import select

        from labtris_api.db import SessionLocal
        from labtris_api.models import Interface, Link, Network, Node, Template

        async with SessionLocal() as session:
            if kind == "node_id":
                row = await session.get(Node, obj_id)
                return row.lab_id if row else None
            if kind == "network_id":
                row = await session.get(Network, obj_id)
                return row.lab_id if row else None
            if kind == "link_id":
                row = await session.get(Link, obj_id)
                return row.lab_id if row else None
            if kind == "iface_id":
                row = await session.get(Interface, obj_id)
                if row is None:
                    return None
                node = await session.get(Node, row.node_id)
                return node.lab_id if node else None
            if kind == "template_id":
                # A template edit is not tied to a specific lab, but every
                # lab that has a node built from this template needs to
                # refetch — the palette shows the new spec, and the
                # inspector on any node using this image should reflect
                # the new sizing. Broadcast to every affected lab.
                tmpl = await session.get(Template, obj_id)
                if tmpl is None:
                    return None
                nodes = (
                    await session.execute(
                        select(Node.lab_id).where(Node.image == tmpl.image).distinct()
                    )
                ).scalars().all()
                from labtris_api.routers.events import announce_topology

                for lid in nodes:
                    await announce_topology(lid)
                # Return None so the caller doesn't publish again for one
                # of them.
                return None
    except Exception:  # noqa: BLE001 - best-effort notify
        return None
    return None


class _WebStatic(StaticFiles):
    """StaticFiles that gets the caching right for a hashed-asset SPA.

    Vite fingerprints every file under assets/ (index-<hash>.js), so those
    are safe to cache forever — the name changes when the bytes do. But
    index.html is a fixed name that points at whichever hash is current;
    if the browser caches it (which it does by default, since StaticFiles
    sends no Cache-Control), a deploy is invisible until a manual
    hard-refresh. Mark the fingerprinted assets immutable and everything
    else no-cache so index.html is revalidated on every load.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("labtris_api.main:app", host="0.0.0.0", port=8080, reload=False)


_ = settings
