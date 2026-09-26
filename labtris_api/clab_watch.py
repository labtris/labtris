"""Notice labs that containerlab deployed, without being told.

You keep running `containerlab deploy` the way you already do. Labtris
watches the Docker event stream, sees the containers appear, and builds
the lab so it is on the canvas when you next look at the UI.

Docker events rather than eBPF, deliberately. containerlab labels every
container it creates — `containerlab=<lab>`, `clab-node-name`,
`clab-node-kind`, `clab-topo-file` — so the daemon already publishes, as
structured data, everything an execve trace would have to reconstruct by
parsing argv and resolving a relative path against a traced cwd. Tracing
would also miss `clab` under a different name, a Makefile wrapper, or a
deploy that happened while the API was down; the event stream plus a
sweep at boot misses none of those.

This sees only topologies deployed against the same daemon Labtris
drives. On a source or ISO install that is the host's Docker, so running
containerlab on the box is enough. On the container install it means
running it inside the instance — which is also what makes clab's netlink
wiring land somewhere netd can see it. See
docs/design/containerlab-in-namespace.mdx.

The dataplane is read but never touched: clab built it, and adoption
keeps host_ifname=None so netd is never asked about a device it must not
manage. Reading matters because the .clab.yml is often unreachable from
the API process, and the veths themselves say how the lab is wired — see
_plan_from_dataplane. Everything this module writes is rows.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: containerlab stamps the lab name on every container it owns.
LAB_LABEL = "containerlab"
#: ...and the topology file it was deployed from. The only place the links
#: are written down; the containers themselves carry no wiring information.
TOPO_LABEL = "clab-topo-file"

#: A deploy creates its containers in a burst. Waiting a moment after the
#: first event means one reconcile per lab rather than one per node.
SETTLE_SECONDS = 4.0

#: Reconnect delay after the event stream drops (a daemon restart).
RETRY_SECONDS = 5.0

_task: asyncio.Task[None] | None = None
_pending: dict[str, asyncio.Task[None]] = {}


def _docker() -> Any:
    import aiodocker

    from labtris_api.config import settings

    return aiodocker.Docker(settings.docker_host)


async def _containers_for(lab: str) -> list[dict[str, Any]]:
    """Every running container clab owns for this lab, fully inspected."""
    docker = _docker()
    try:
        out = []
        for c in await docker.containers.list():
            info = await c.show()
            labels = (info.get("Config") or {}).get("Labels") or {}
            if labels.get(LAB_LABEL) == lab:
                out.append(info)
        return out
    finally:
        await docker.close()


def _topology_from(containers: list[dict[str, Any]]) -> str:
    """Read the .clab.yml the deploy was given, if we can reach it.

    Every node carries the same path, so the first readable one wins. It is
    absolute and relative to whatever filesystem clab ran on: the same one
    as the API for a source install, and for the container install the same
    one again, because clab has to run inside the instance for its wiring to
    be usable at all. A bind-mounted working directory is the case where
    this fails, and that is reported rather than guessed at.
    """
    for info in containers:
        path = ((info.get("Config") or {}).get("Labels") or {}).get(TOPO_LABEL)
        if not path:
            continue
        with contextlib.suppress(OSError):
            p = Path(path)
            if p.is_file():
                return p.read_text()
    return ""


#: containerlab's own management interface. Its peer lands on clab's
#: management bridge rather than in another node, so it never pairs and is
#: excluded anyway — this is belt and braces, and documents the intent.
MGMT_IFACE = "eth0"


def pair_links(
    nodes: dict[str, Any],
) -> tuple[list[tuple[str, str, str, str]], dict[str, list[str]], list[str]]:
    """Pair up veth ends across namespaces into links.

    `nodes` is the `netns.links` reply: node name -> {"interfaces": [...]}.
    Returns (links, interfaces-per-node, ambiguous-ends), where a link is
    (a_node, a_iface, b_node, b_iface).

    Each veth end reports its peer's ifindex, as numbered in the peer's own
    namespace. So two interfaces in two DIFFERENT nodes that name each
    other's ifindex are the two ends of one cable. An end whose peer is not
    in any node — containerlab's eth0, whose other half sits on the
    management bridge — matches nothing and is left out, which is how the
    management network stays out of the topology without being special-cased.

    Split out from the netd call so it can be tested against captured
    output; it is the only part of the discovery with anything to get wrong.
    """
    # (node, ifindex) -> (ifname, peer_ifindex)
    ends: dict[tuple[str, int], tuple[str, int]] = {}
    for node, data in nodes.items():
        if data.get("error"):
            continue
        for row in data.get("interfaces") or []:
            name, peer = row.get("name"), row.get("peer_index")
            if not name or name == "lo" or not peer:
                continue
            if row.get("kind") not in ("veth", None):
                continue
            ends[(node, int(row["index"]))] = (name, int(peer))

    # Candidate peers for every end, computed up front. Doing this in one
    # pass and pairing in a second is what makes ambiguity symmetric: if a
    # has two possible peers then b, which sees only a, is no less stuck —
    # deciding end-by-end would let b claim a while a was being dropped.
    candidates: dict[tuple[str, int], list[tuple[str, int]]] = {
        (node, idx): [
            (other, other_idx)
            for (other, other_idx), (_, other_peer) in ends.items()
            if other != node and other_idx == peer_idx and other_peer == idx
        ]
        for (node, idx), (_, peer_idx) in ends.items()
    }

    links: list[tuple[str, str, str, str]] = []
    ifaces: dict[str, list[str]] = {}
    ambiguous: list[str] = []
    seen: set[tuple[str, int]] = set()
    for key in sorted(ends):
        if key in seen:
            continue
        node, _ = key
        name = ends[key][0]
        cands = candidates[key]
        if not cands:
            # The other half is not in any node — containerlab's eth0, whose
            # peer sits on the management bridge. Not a lab link, and this
            # is how the management network stays out without a special case.
            continue
        peer_key = cands[0]
        if len(cands) > 1 or candidates.get(peer_key) != [key]:
            ambiguous.append(f"{node}:{name}")
            continue
        other, other_idx = peer_key
        other_name = ends[peer_key][0]
        seen.add(key)
        seen.add(peer_key)
        ifaces.setdefault(node, []).append(name)
        ifaces.setdefault(other, []).append(other_name)
        links.append((node, name, other, other_name))
    return links, ifaces, ambiguous


async def _plan_from_dataplane(clab_lab: str, containers: list[dict[str, Any]]) -> Any:
    """Work out the topology from the veths containerlab actually built.

    The fallback for when the .clab.yml cannot be read, which is the normal
    case on the container install: a deploy run inside an ephemeral clab
    container leaves `clab-topo-file` pointing at a path on a filesystem
    that died with it.

    The wiring is recoverable without the file because each veth end reports
    its peer's ifindex, so the pairing in pair_links() has no guesswork in
    it — and it describes what is running rather than what was requested.
    Where the file and the dataplane disagree, the dataplane is right.
    """
    from labtris_api.migrate import Plan, PlannedLink, PlannedNode
    from labtris_api.netd_client import netd

    pids: dict[str, int] = {}
    images: dict[str, str] = {}
    for info in containers:
        labels = (info.get("Config") or {}).get("Labels") or {}
        node = labels.get("clab-node-name")
        pid = (info.get("State") or {}).get("Pid") or 0
        if not node or not pid:
            continue
        pids[node] = int(pid)
        images[node] = (info.get("Config") or {}).get("Image") or "unknown"

    if not pids:
        return None

    reply = await netd.call("netns.links", {"pids": pids})
    links, ifaces, ambiguous = pair_links(reply.get("nodes") or {})

    plan = Plan(name=clab_lab)
    plan.links = [
        PlannedLink(a_node=a, a_iface=ai, b_node=b, b_iface=bi) for a, ai, b, bi in links
    ]
    for node in sorted(pids):
        plan.nodes.append(
            PlannedNode(
                name=node,
                runtime="docker",
                image=images.get(node, "unknown"),
                # The interfaces that carry a lab link, in the order found.
                # MGMT_IFACE is deliberately absent: it is clab's management
                # network, not part of the topology being studied.
                ifaces=[i for i in ifaces.get(node, []) if i != MGMT_IFACE],
            )
        )
    plan.warnings.append(
        "topology read from the running veths because clab-topo-file was not "
        "readable; node images and links are real, but anything the .clab.yml "
        "said and the dataplane does not show (startup config, kinds, labels) "
        "is not here"
    )
    if ambiguous:
        plan.warnings.append(
            "left these interfaces unwired because more than one peer matched: "
            + ", ".join(sorted(ambiguous))
        )
    return plan


async def _adopted_lab_id(session: Any, clab_lab: str) -> str | None:
    """The Labtris lab already bound to this clab deployment, if any.

    Looked up through the adoption marker on the nodes rather than by lab
    name, because realize_plan() de-duplicates names — a clab lab called
    `spike` can legitimately have become the Labtris lab `spike-1`, and
    matching on the name would then find somebody else's lab and overwrite
    its nodes.
    """
    from sqlalchemy import select

    from labtris_api.clab_adopt import ADOPTED_ENV
    from labtris_api.models import Node

    row = (
        await session.execute(
            select(Node.lab_id).where(Node.env[ADOPTED_ENV].as_string() == clab_lab).limit(1)
        )
    ).scalars().first()
    return row


async def _mark_gone(session: Any, clab_lab: str) -> int:
    """A destroy took the containers away; stop claiming the nodes run.

    The rows stay. `clab destroy` is not a request to delete a Labtris lab,
    and silently dropping a topology someone may have annotated would be a
    worse surprise than a lab full of stopped nodes.
    """
    from sqlalchemy import select

    from labtris_api.clab_adopt import ADOPTED_ENV
    from labtris_api.lifecycle import announce
    from labtris_api.models import Node

    rows = (
        await session.execute(
            select(Node).where(Node.env[ADOPTED_ENV].as_string() == clab_lab)
        )
    ).scalars().all()
    changed = [n for n in rows if n.state != "stopped" or n.runtime_ref is not None]
    for node in changed:
        node.state = "stopped"
        # A stale id would make console/exec dial a container that is gone.
        node.runtime_ref = None
    if changed:
        await session.commit()
        for node in changed:
            await announce(node, "stopped")
    return len(changed)


async def _reconcile(clab_lab: str) -> None:
    """Make the Labtris rows agree with what containerlab has running."""
    from labtris_api.clab_adopt import adopt
    from labtris_api.db import SessionLocal
    from labtris_api.migrate import parse_clab
    from labtris_api.routers.events import announce_topology
    from labtris_api.routers.labs import realize_plan

    containers = await _containers_for(clab_lab)

    async with SessionLocal() as session:
        lab_id = await _adopted_lab_id(session, clab_lab)

        if not containers:
            if lab_id is not None:
                stopped = await _mark_gone(session, clab_lab)
                log.info("clab.watch.destroyed", clab=clab_lab, nodes=stopped)
            return

        if lab_id is not None:
            # A redeploy: same topology, new container ids. Re-adopt so the
            # rows point at the live containers instead of dead ones.
            result = await adopt(session, lab_id, clab_lab)
            await announce_topology(lab_id)
            log.info("clab.watch.readopted", clab=clab_lab,
                     lab=lab_id, adopted=len(result["adopted"]))
            return

        topo = _topology_from(containers)
        if topo:
            plan = parse_clab(topo)
            plan.name = clab_lab
            source = "topology-file"
        else:
            # No file to read — normal on the container install, where the
            # deploy ran inside an ephemeral clab container. Recover the
            # topology from the veths instead of giving up: the links are in
            # the dataplane whether or not the file survived.
            plan = await _plan_from_dataplane(clab_lab, containers)
            source = "dataplane"
            if plan is None:
                log.warning("clab.watch.undiscoverable", clab=clab_lab,
                            nodes=len(containers),
                            hint="no clab-node-name labels and no readable "
                                 "clab-topo-file; nothing to build a lab from")
                return
        lab, warnings = await realize_plan(session, plan)
        result = await adopt(session, lab.id, clab_lab)
        log.info("clab.watch.imported", clab=clab_lab, lab=lab.id, name=lab.name,
                 source=source, nodes=len(plan.nodes), links=len(plan.links),
                 adopted=len(result["adopted"]), missing=result["missing"],
                 warnings=warnings)


def _schedule(clab_lab: str) -> None:
    """Debounce per lab: the last event in a burst wins, one reconcile runs."""
    old = _pending.pop(clab_lab, None)
    if old is not None:
        old.cancel()

    async def run() -> None:
        try:
            await asyncio.sleep(SETTLE_SECONDS)
            await _reconcile(clab_lab)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad lab must not stop the watch
            log.warning("clab.watch.reconcile_failed", clab=clab_lab, error=str(exc))
        finally:
            if _pending.get(clab_lab) is asyncio.current_task():
                _pending.pop(clab_lab, None)

    _pending[clab_lab] = asyncio.create_task(run())


async def _sweep() -> None:
    """Catch labs that were deployed while nothing was watching."""
    docker = _docker()
    try:
        labs = set()
        for c in await docker.containers.list():
            info = await c.show()
            name = ((info.get("Config") or {}).get("Labels") or {}).get(LAB_LABEL)
            if name:
                labs.add(name)
    finally:
        await docker.close()
    for lab in labs:
        _schedule(lab)
    if labs:
        log.info("clab.watch.sweep", labs=sorted(labs))


async def _loop() -> None:
    with contextlib.suppress(Exception):
        await _sweep()
    while True:
        docker = _docker()
        try:
            subscriber = docker.events.subscribe()
            while True:
                event = await subscriber.get()
                if event is None:
                    # aiodocker publishes None when the stream ends, which
                    # is also how a daemon restart arrives. Reconnect.
                    raise ConnectionError("docker event stream ended")
                if event.get("Type") != "container":
                    continue
                if event.get("Action") not in ("start", "die", "destroy"):
                    continue
                attrs = (event.get("Actor") or {}).get("Attributes") or {}
                clab_lab = attrs.get(LAB_LABEL)
                if clab_lab:
                    _schedule(clab_lab)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Never let a dropped stream leave the feature quietly dead for
            # the rest of the process lifetime.
            log.warning("clab.watch.stream_lost", error=str(exc))
        finally:
            with contextlib.suppress(Exception):
                await docker.close()
        await asyncio.sleep(RETRY_SECONDS)


async def start() -> None:
    """Begin watching. Safe to call twice; the second call is a no-op."""
    global _task
    from labtris_api.config import settings

    if not settings.clab_watch:
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    log.info("clab.watch.started")


async def stop() -> None:
    global _task
    pending = list(_pending.values())
    for t in pending:
        t.cancel()
    _pending.clear()
    if _task is not None:
        _task.cancel()
        pending.append(_task)
    # CancelledError derives from BaseException, not Exception, so awaiting a
    # cancelled task inside suppress(Exception) re-raises out of the lifespan
    # and uvicorn reports "Application shutdown failed. Exiting." return_
    # exceptions keeps every cancellation contained here.
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _task = None
