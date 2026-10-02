from __future__ import annotations

import asyncio
import time

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
    audit,
    auth,
    backup,
    capture,
    events,
    feedback,
    health,
    hooks,
    hosts,
    images,
    impair,    labs,
    link_groups,
    links,
    networks,
    nodes,
    p4,
    pods,
    ssh_endpoints,
    ssh_keys,
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
    # Phase K1 SSH proxy — off unless LABTRIS_SSH_PROXY_ENABLED. Failures
    # log and skip; the port belongs to an optional feature and must not
    # take the API down with it.
    try:
        from labtris_api import ssh_proxy

        await ssh_proxy.start()
    except Exception:  # noqa: BLE001
        pass
    # Watch for labs deployed by containerlab so they appear in the UI
    # without an import step. Includes a sweep, so a lab deployed while the
    # API was down is picked up here rather than never.
    try:
        from labtris_api import clab_watch

        await clab_watch.start()
    except Exception:  # noqa: BLE001 — observing clab is a convenience
        pass

    # Prune the audit log on a timer. It records every mutation, and on a host
    # running thousands of nodes that grows fast — a 931-node start is 931
    # entries on its own. Pruning on write would put a DELETE in the path of
    # every operation; once an hour costs nothing and keeps the window honest.
    _pruner = asyncio.create_task(_prune_audit_forever(), name="audit-prune")

    yield
    _pruner.cancel()
    # Wireshark sessions are deliberately started in their own process session
    # so they survive a reload — which is exactly what makes them leak if
    # nothing reaps them on the way out.
    from labtris_api import wireshark as ws

    await ws.stop_all()

    # The clab watcher holds an open Docker /events stream, which uvicorn
    # will otherwise wait on for the full shutdown timeout.
    try:
        from labtris_api import clab_watch

        await clab_watch.stop()
    except Exception:  # noqa: BLE001
        pass

    # Cancel every long-lived background task we spawned via
    # asyncio.create_task from a request handler. Uvicorn's shutdown
    # hangs on any survivor until systemd's TimeoutStopSec (90s default)
    # fires and SIGKILLs the process — during which nginx returns 502 to
    # every new request because uvicorn stopped accept()ing. Naming the
    # tasks explicitly here keeps shutdown in seconds, not minutes.
    #
    # asyncio is NOT re-imported here. A function-local import makes the name
    # local for the WHOLE function, so the create_task above — which runs
    # before this line — became an UnboundLocalError and the app refused to
    # start. It is imported at module scope.
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

    # SSH proxy — close the listener before uvicorn tears down the loop
    # or the asyncssh.SSHAcceptor's own cleanup races the shutdown.
    with contextlib.suppress(Exception):
        from labtris_api import ssh_proxy

        await ssh_proxy.stop()


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
    app.include_router(audit.router, prefix=prefix)
    app.include_router(feedback.router, prefix=prefix)
    app.include_router(hooks.router, prefix=prefix)
    app.include_router(pods.router, prefix=prefix)
    app.include_router(p4.router, prefix=prefix)
    app.include_router(link_groups.router, prefix=prefix)
    app.include_router(ssh_keys.router, prefix=prefix)
    app.include_router(ssh_endpoints.router, prefix=prefix)

    # Phase K3 — RESTCONF (RFC 8040) sub-app. Distinct top-level URL
    # (`/restconf/...`) because the spec pins its own path scheme; not
    # nested under /api/v1. Off with LABTRIS_RESTCONF_ENABLED=false.
    if settings.restconf_enabled:
        from labtris_api.restconf.router import router as restconf_router

        app.include_router(restconf_router)

    # Third-party plugins (Phase L cloud-compat and future) attach here,
    # BEFORE the SPA static mount below — otherwise a plugin route that
    # collides with an SPA path would be shadowed by index.html.
    from labtris_api.plugins import load_plugins

    load_plugins(app)

    web_dist = (
        Path(settings.web_dist).expanduser()
        if settings.web_dist
        else Path(__file__).resolve().parent.parent / "web" / "dist"
    )
    if web_dist.is_dir():
        app.mount("/", _WebStatic(directory=web_dist, html=True), name="web")
    elif settings.web_dist:
        # Skipping the mount is fine when nobody asked for a UI, but if a
        # path was configured and is not there, every page is a 404 with
        # nothing in the log to say why.
        import structlog

        structlog.get_logger(__name__).warning(
            "web.dist_missing", path=str(web_dist),
            hint="LABTRIS_WEB_DIST is set but that directory does not exist; serving API only",
        )

    return app


_MUTATING = {"POST", "PATCH", "PUT", "DELETE"}


async def _audit(request, response, lab_id, duration_ms) -> None:
    """Record one mutation.

    Wrapped so it can never fail the request it is describing: the operation
    has already happened by the time we get here, and raising now would report
    a success as an error.
    """
    from labtris_api import audit as audit_mod
    from labtris_api.db import SessionLocal
    from labtris_api.lifecycle import new_id

    try:
        if audit_mod._SKIP.match(request.url.path):  # noqa: SLF001
            return

        # The actor is read from the token here rather than taken from
        # request.state alone.
        #
        # request.state is set by get_current_user, which means the audit
        # would silently lose the actor on any path that authenticates
        # differently — a websocket, a route using another dependency, or a
        # test that overrides it. An audit log whose "who" column depends on
        # which dependency happened to run is not one to rely on.
        #
        # The claims carry the username and the act, so this costs no query.
        # An unauthenticated mutation (there should be none) records as nobody
        # rather than being dropped, because a write with no actor is exactly
        # the thing someone would want to find.
        user = getattr(request.state, "user", None)
        if user is None or not getattr(user, "username", ""):
            user = _actor_from_token(request) or user
        route = request.scope.get("route")
        template = getattr(route, "path", "") or ""

        # The announce resolves lab_id for DELETE *before* calling the
        # handler, which is before the router has matched and therefore before
        # path_params exist — so it arrives here as None on exactly the
        # requests where the id is sitting in the path. Resolve it again now
        # that the route has matched.
        if lab_id is None:
            params = request.scope.get("path_params") or {}
            lab_id = params.get("lab_id")

        # lab_id is null on a create. The row did not exist when the request
        # arrived, so there is no path param to read, and its id is only in the
        # response body — which would mean buffering every response to catch a
        # handful of creates. Route, actor and time still answer "who made a
        # lab, and when"; which one is then one query away.
        body = getattr(request.state, "audit_body", None)

        async with SessionLocal() as session:
            await audit_mod.record(
                session,
                id=new_id(),
                actor_id=getattr(user, "id", None),
                actor_name=getattr(user, "username", "") or "",
                via=getattr(user, "via", "human"),
                method=request.method,
                path=request.url.path,
                route=template,
                status=response.status_code,
                lab_id=lab_id,
                target_kind=_target_kind(template),
                target_id=_target_id(request),
                summary=audit_mod.summarise(request.method, template, response.status_code),
                detail=audit_mod.redact(body) if body else {},
                duration_ms=duration_ms,
            )
    except Exception:  # noqa: BLE001 - see docstring
        return


async def _prune_audit_forever() -> None:
    """Drop audit entries past the retention window, hourly.

    Runs once at startup as well, so a host that was down over the weekend
    does not keep a fortnight of entries until the first hour elapses.
    """
    from labtris_api import audit
    from labtris_api.db import SessionLocal

    while True:
        try:
            async with SessionLocal() as session:
                await audit.prune(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed prune must not end the loop
            pass
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return


def _actor_from_token(request):
    """Actor straight from the signed token, with no database round-trip.

    Returns something shaped like a User — id, username, via — or None when
    there is no readable token.
    """
    from types import SimpleNamespace

    from labtris_api.auth import SESSION_COOKIE, read_token

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()
    if not token:
        return None
    claims = read_token(token)
    if not claims:
        return None
    return SimpleNamespace(
        id=claims.get("sub"),
        username=claims.get("username") or "",
        via="assistant" if claims.get("act") == "assistant" else "human",
    )


def _target_kind(template: str) -> str | None:
    """Which kind of thing was acted on, from the route template."""
    for kind in ("nodes", "links", "networks", "interfaces", "labs", "users",
                 "templates", "images", "hosts", "tasks", "pods"):
        if f"/{kind}" in template:
            return kind.rstrip("s")
    return None


def _target_id(request) -> str | None:
    """The most specific id in the path — the node rather than its lab."""
    params = request.scope.get("path_params") or {}
    for key in ("node_id", "link_id", "network_id", "iface_id", "user_id",
                "task_id", "template_id", "host_id", "lab_id"):
        if params.get(key):
            return str(params[key])
    return None


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

    started = time.monotonic()
    response = await call_next(request)

    if not is_mutation:
        return response

    # Audited before the success check, deliberately: a refused operation is
    # often the one worth looking at, and a log that only records what worked
    # cannot answer "why did nothing happen when I clicked that".
    await _audit(request, response, lab_id, int((time.monotonic() - started) * 1000))

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
