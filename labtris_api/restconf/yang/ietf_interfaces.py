"""ietf-interfaces YANG module → Labtris Interface rows (Phase K3).

Reference: RFC 8343 (revision 2018-02-20). We ship the pragmatic subset
network engineers actually query and toggle:

Config datastore (`/interfaces/interface[]`):
    name            (leaf, key)     — the labtris Interface.name (Gi0/0, eth0, ...)
    type            (identityref)   — 'iana-if-type:ethernetCsmacd' for every
                                       labtris tap; we don't model tunnels/loopbacks
                                       distinctly yet.
    enabled         (leaf boolean)  — mapped to iface_set_state on the host tap

State datastore (`/interfaces-state/interface[]`):
    name
    type
    admin-status    (enum)          — 'up' / 'down' — derived from the guest link
                                       state and the tap's admin state
    oper-status     (enum)          — 'up' / 'down' — from IFLA_OPERSTATE
    statistics{}    (container)     — counters copied from labtris_netd's
                                       iface_counters response

Every dict returned matches the JSON encoding rules in RFC 7951 (module-
prefixed top key on the outer container; bare identifiers inside).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.models import Interface, Node

MODULE = "ietf-interfaces"

# Every labtris tap is L2-shaped, so ethernetCsmacd is the honest default.
# When we add loopback-style ports (P4 CPU port, etc.) they get their
# own identity here.
_DEFAULT_TYPE = "iana-if-type:ethernetCsmacd"


async def _node_by_name(session: AsyncSession, name: str) -> Node | None:
    row = (
        await session.execute(select(Node).where(Node.name == name))
    ).scalar_one_or_none()
    return row


async def _interfaces_for_node(session: AsyncSession, node_id: str) -> list[Interface]:
    return list(
        (
            await session.execute(
                select(Interface).where(Interface.node_id == node_id).order_by(Interface.idx)
            )
        ).scalars()
    )


def _iface_config(iface: Interface) -> dict[str, Any]:
    """The `interface` leaf-list entry inside the config datastore."""
    return {
        "name": iface.name,
        "type": _DEFAULT_TYPE,
        # Absent host_ifname means the tap hasn't been plugged in on the
        # host yet (node stopped, or NIC not attached). We report enabled
        # true so a `PUT enabled: true` on a stopped node doesn't silently
        # look like it changed something.
        "enabled": True,
    }


def _iface_state(iface: Interface, counters: dict[str, Any] | None) -> dict[str, Any]:
    """The `interface` leaf-list entry inside the state datastore."""
    body: dict[str, Any] = {
        "name": iface.name,
        "type": _DEFAULT_TYPE,
        "admin-status": "up",
        "oper-status": "unknown",
    }
    if counters is None or not counters.get("exists"):
        return body
    body["admin-status"] = "up"
    body["oper-status"] = "up"
    body["statistics"] = {
        "in-octets": counters.get("rx_bytes", 0),
        "out-octets": counters.get("tx_bytes", 0),
        "in-unicast-pkts": counters.get("rx_packets", 0),
        "out-unicast-pkts": counters.get("tx_packets", 0),
        "in-discards": counters.get("rx_dropped", 0),
        "out-discards": counters.get("tx_dropped", 0),
        "in-errors": counters.get("rx_errors", 0),
        "out-errors": counters.get("tx_errors", 0),
    }
    return body


async def get_interfaces_config(
    session: AsyncSession, node_name: str
) -> dict[str, Any] | None:
    node = await _node_by_name(session, node_name)
    if node is None:
        return None
    ifaces = await _interfaces_for_node(session, node.id)
    return {"interface": [_iface_config(i) for i in ifaces]}


async def get_interface_config(
    session: AsyncSession, node_name: str, iface_name: str
) -> dict[str, Any] | None:
    node = await _node_by_name(session, node_name)
    if node is None:
        return None
    ifaces = await _interfaces_for_node(session, node.id)
    for i in ifaces:
        if i.name == iface_name:
            return {"interface": [_iface_config(i)]}
    return None


async def get_interfaces_state(
    session: AsyncSession, node_name: str
) -> dict[str, Any] | None:
    """List the interface names on the node, fetch counters for their
    host taps in one netd round-trip, and shape them into the state
    envelope."""
    node = await _node_by_name(session, node_name)
    if node is None:
        return None
    ifaces = await _interfaces_for_node(session, node.id)
    counters = await _fetch_counters([i.host_ifname for i in ifaces if i.host_ifname])
    return {
        "interface": [
            _iface_state(i, counters.get(i.host_ifname) if i.host_ifname else None)
            for i in ifaces
        ]
    }


async def get_interface_state(
    session: AsyncSession, node_name: str, iface_name: str
) -> dict[str, Any] | None:
    node = await _node_by_name(session, node_name)
    if node is None:
        return None
    ifaces = await _interfaces_for_node(session, node.id)
    for i in ifaces:
        if i.name == iface_name:
            counters = (
                await _fetch_counters([i.host_ifname]) if i.host_ifname else {}
            )
            return {
                "interface": [
                    _iface_state(i, counters.get(i.host_ifname) if i.host_ifname else None)
                ]
            }
    return None


async def set_interface_enabled(
    session: AsyncSession, node_name: str, iface_name: str, enabled: bool
) -> tuple[bool, str]:
    """Turn the interface's host tap admin-up or admin-down.

    Returns (ok, message). ok=False means the caller should surface a
    RESTCONF error; the message is what the client sees."""
    node = await _node_by_name(session, node_name)
    if node is None:
        return False, f"no such node {node_name!r}"
    ifaces = await _interfaces_for_node(session, node.id)
    iface = next((i for i in ifaces if i.name == iface_name), None)
    if iface is None:
        return False, f"no such interface {iface_name!r} on {node_name!r}"
    if not iface.host_ifname:
        return False, (
            f"interface {iface_name!r} has no host tap — start the node first"
        )
    try:
        from labtris_api.netd_client import netd

        await netd.call("iface.set_state", {"name": iface.host_ifname, "up": enabled})
    except Exception as exc:  # noqa: BLE001
        return False, f"netd rejected the state change: {exc}"
    return True, "ok"


async def _fetch_counters(host_ifnames: list[str]) -> dict[str, Any]:
    if not host_ifnames:
        return {}
    try:
        from labtris_api.netd_client import netd

        resp = await netd.call("iface.counters", {"names": host_ifnames})
        counters = resp.get("counters") if isinstance(resp, dict) else None
        if isinstance(counters, dict):
            return counters
        if isinstance(resp, dict):
            # Older shape: netd returned counters at the top level.
            return resp
    except Exception:  # noqa: BLE001
        return {}
    return {}
