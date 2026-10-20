"""Make the database agree with the machine, at startup.

Two things drift apart while nobody is looking. Rows outlive the things they
describe — a node deleted while its name reservation stays, so the pool only
shrinks. And rows describe things that stopped existing — a host reboot takes
every QEMU process with it while the nodes go on claiming they are running,
which is a lab that reports healthy and cannot pass a packet.

Neither is detectable from inside a request; both are obvious once, at start,
when nothing else is happening. This runs then.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from labtris_api.models import Interface, Lab, MacRegistry, Network, Node

logger = structlog.get_logger(__name__)


async def _stale_running_nodes(session: AsyncSession, wait_s: float = 0.0) -> list[Node]:
    """Nodes the database calls running whose runtime says otherwise."""
    from labtris_api.runtime.base import RuntimeHandle
    from labtris_api.runtime.registry import get_runtime

    # Eager-load the interfaces: they are cleared below, and touching a lazy
    # relationship here raises MissingGreenlet rather than loading it.
    rows = (
        await session.execute(
            select(Node)
            .options(selectinload(Node.interfaces))
            .where(Node.state == "running")
        )
    ).scalars()
    stale: list[Node] = []
    # One budget for the whole pass. dind restarts alongside the api and
    # takes a few seconds to answer; asking once and skipping on an error
    # left every container node marked running with nothing behind it after
    # an upgrade (seen on the Hetzner install, 2026-10-20: 13 nodes).
    deadline = time.monotonic() + wait_s
    for node in rows:
        if not node.runtime_ref:
            stale.append(node)
            continue
        runtime = get_runtime(node.runtime)
        handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
        while True:
            try:
                observed = await runtime.observe(handle)
                break
            except Exception:  # noqa: BLE001 - not answering yet is not proof either way
                if time.monotonic() >= deadline:
                    observed = None
                    break
                await asyncio.sleep(2)
        if observed is None:
            logger.warning("reconcile.runtime_unreachable", node=node.name,
                           runtime=node.runtime)
            continue
        if not observed.running:
            stale.append(node)
    return stale


async def reconcile(session: AsyncSession, wait_s: float = 0.0) -> dict[str, Any]:
    """Correct what drifted. Returns what it changed, for the log and tests.

    `restore` lists the ids of nodes it found dead while marked running: the
    ones that were up when the API went down. Startup starts them again
    (restore_nodes) unless settings.restore_running_nodes is off."""
    result: dict[str, Any] = {
        "marked_stopped": [],
        "restore": [],
        "ifnames_released": 0,
        "macs_released": 0,
        "nat_restored": [],
    }

    # A NAT network's plumbing does not survive a reboot: the address, the
    # masquerade rule and the DHCP server all live in the kernel and in a
    # process, not in the database. Unlike a stopped node, restoring these is
    # not a surprise — the network object exists and the user expects it to
    # route, and nothing boots as a side effect.
    nat_nets = (
        await session.execute(select(Network).where(Network.kind == "nat"))
    ).scalars()
    for net in nat_nets:
        if not net.host_ifname or not net.subnet:
            continue
        try:
            from labtris_api.nat import bring_up
            from labtris_api.netd_client import netd

            await netd.call("bridge.create", {"name": net.host_ifname})
        except Exception:
            # Already there is the normal case; bring_up is what matters.
            pass
        try:
            await bring_up(net, dhcp=bool(net.dhcp_first))
            result["nat_restored"].append(net.name)
        except Exception as exc:  # noqa: BLE001 - one broken network must not
            # stop the rest of the instance coming back.
            logger.warning("nat_restore_failed", network=net.name, error=str(exc))

    # A node that claims to be running with nothing behind it is worse than a
    # stopped one: every console, capture and validation built on it is wrong.
    # Marking it stopped is the honest correction; whether it then starts
    # again is restore_nodes' business, after commit, so a failed start
    # leaves an honest "stopped" behind rather than a stale "running".
    for node in await _stale_running_nodes(session, wait_s):
        node.state = "stopped"
        for iface in node.interfaces:
            iface.host_ifname = None
        result["marked_stopped"].append(node.name)
        result["restore"].append(node.id)

    # Reservations whose owner is gone. Every id in the registry is a lab,
    # node, network, interface, or an interface's peer — anything matching
    # none of those describes something that no longer exists.
    live: set[str] = set()
    for model in (Lab, Node, Network, Interface):
        live |= {row for row in (await session.execute(select(model.id))).scalars()}
    from labtris_api.lifecycle import peer_owner

    peers = {peer_owner(i) for i in live}

    from labtris_api.models import IfnameRegistry

    reservations = list((await session.execute(select(IfnameRegistry))).scalars())
    orphans = [r.host_ifname for r in reservations if r.owner_id not in live | peers]
    if orphans:
        await session.execute(
            sql_delete(IfnameRegistry).where(IfnameRegistry.host_ifname.in_(orphans))
        )
        result["ifnames_released"] = len(orphans)

    mac_rows = list((await session.execute(select(MacRegistry))).scalars())
    # Delete by owner, not by address: the column is Postgres MACADDR and
    # comparing it to a list of strings needs an explicit cast the ORM will
    # not add ("operator does not exist: macaddr = character varying").
    mac_orphans = [r.owner_id for r in mac_rows if r.owner_id not in live]
    if mac_orphans:
        await session.execute(
            sql_delete(MacRegistry).where(MacRegistry.owner_id.in_(mac_orphans))
        )
        result["macs_released"] = len(mac_orphans)

    await session.commit()
    if result["marked_stopped"] or result["ifnames_released"] or result["macs_released"]:
        logger.info("reconcile", **result)
    return result


async def restore_nodes(
    session_factory: Any, node_ids: list[str], concurrency: int = 4
) -> dict[str, str]:
    """Start the nodes reconcile found dead, a few at a time.

    Bounded because a lab of thirty VMs all cold-booting at once is the
    storm that makes some of them time out; the order is the order they
    were found, which is per lab. Each start gets its own session: they run
    concurrently and an AsyncSession must not cross tasks. Returns
    name -> "running" or the error, for the log."""
    from labtris_api.lifecycle import get_node, start_node

    sem = asyncio.Semaphore(concurrency)
    out: dict[str, str] = {}

    async def one(node_id: str) -> None:
        async with sem, session_factory() as s:
            try:
                node = await get_node(s, node_id)
                node = await start_node(s, node)
                out[node.name] = node.state
            except Exception as exc:  # noqa: BLE001 - one bad node must not stop the rest
                out[node_id] = f"failed: {exc}"

    await asyncio.gather(*(one(i) for i in node_ids))
    logger.info("reconcile.restored", nodes=out)
    return out
