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

from typing import Any

import structlog
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from labtris_api.models import Interface, Lab, MacRegistry, Network, Node

logger = structlog.get_logger(__name__)


async def _stale_running_nodes(session: AsyncSession) -> list[Node]:
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
    for node in rows:
        if not node.runtime_ref:
            stale.append(node)
            continue
        try:
            runtime = get_runtime(node.runtime)
            observed = await runtime.observe(
                RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
            )
        except Exception:  # noqa: BLE001 - an unreachable runtime is not proof either way
            continue
        if not observed.running:
            stale.append(node)
    return stale


async def reconcile(session: AsyncSession) -> dict[str, Any]:
    """Correct what drifted. Returns what it changed, for the log and tests."""
    result: dict[str, Any] = {
        "marked_stopped": [],
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
    # Marking it stopped is the honest correction. Deliberately NOT restarting
    # it — silently booting VMs because a host rebooted would be a surprising
    # amount of work to start on its own, and the user may not want it.
    for node in await _stale_running_nodes(session):
        node.state = "stopped"
        for iface in node.interfaces:
            iface.host_ifname = None
        result["marked_stopped"].append(node.name)

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
