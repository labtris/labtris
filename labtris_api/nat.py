"""NAT networks: a lab segment with a way out that is not the host's own LAN.

A `cloud` network puts the lab on a real interface, which is powerful and
dangerous — it is the same broadcast domain as everything else on that wire,
and binding the wrong NIC takes the host off the network. Most of the time
nobody wanted that. They wanted `apt-get` to work.

That is what this is: a bridge, an address on it that the host answers as the
gateway, a masquerade rule so replies find their way back, and a DHCP server so
a node dropped onto the segment simply works.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.errors import bad_request, runtime_error
from labtris_api.models import Network
from labtris_api.netd_client import NetdError, netd

if TYPE_CHECKING:  # pragma: no cover
    pass

#: Where automatic subnets come from. Deliberately inside 10/8 but well away
#: from 10.0.0.0/16 and 10.1.0.0/16, which is where nearly every tutorial —
#: and therefore nearly every lab built by hand — puts its own addressing.
#: Colliding with the lab's own topology would be a strange first experience.
AUTO_POOL = ipaddress.ip_network("10.200.0.0/16")
AUTO_PREFIX = 24


async def pick_subnet(session: AsyncSession, wanted: str | None) -> ipaddress.IPv4Network:
    """Validate the caller's subnet, or find a free one.

    Overlap is checked against every NAT network on the instance rather than
    just this lab: they all masquerade through the same host, and two labs
    sharing 10.200.5.0/24 would have the host routing for a prefix that means
    two different things.
    """
    existing: list[ipaddress.IPv4Network] = []
    rows = await session.execute(select(Network).where(Network.subnet.is_not(None)))
    for row in rows.scalars():
        try:
            existing.append(ipaddress.ip_network(row.subnet, strict=False))  # type: ignore[arg-type]
        except ValueError:
            continue

    if wanted:
        try:
            net = ipaddress.ip_network(wanted, strict=False)
        except ValueError as exc:
            raise bad_request(f"{wanted!r} is not a valid subnet") from exc
        if not isinstance(net, ipaddress.IPv4Network):
            raise bad_request("NAT networks are IPv4 for now")
        if net.prefixlen > 30:
            raise bad_request(
                f"{wanted} has no room for a gateway and a guest — use /30 or larger"
            )
        clash = next((e for e in existing if e.overlaps(net)), None)
        if clash is not None:
            raise bad_request(
                f"{wanted} overlaps {clash}, which another NAT network already uses. "
                "They all masquerade through this host, so the prefix has to mean one thing."
            )
        return net

    for candidate in AUTO_POOL.subnets(new_prefix=AUTO_PREFIX):
        if not any(e.overlaps(candidate) for e in existing):
            return candidate
    raise bad_request(f"no free /{AUTO_PREFIX} left in {AUTO_POOL}")


def pool_for(net: ipaddress.IPv4Network) -> tuple[str, str, str]:
    """Gateway, first and last DHCP address for a subnet.

    The gateway takes the first usable address and the pool starts well above
    it, leaving room for the static addresses people give routers in a lab
    without having to think about whether DHCP will hand the same one out.
    """
    hosts = list(net.hosts())
    if len(hosts) < 2:
        raise bad_request(f"{net} has no room for a gateway and a guest")
    gateway = hosts[0]
    # Start at .50 where there is room, so the .1-.10 that everyone gives their
    # routers by hand stays out of the pool. On a small subnet that rule would
    # push the pool past the end — or, worse, onto the gateway itself — so it
    # falls back to halfway, and never below the first address after the
    # gateway.
    start = max(1, min(49, len(hosts) // 2))
    first = hosts[start]
    last = hosts[-1]
    return str(gateway), str(first), str(last)


def dhcp_key(network_id: str) -> str:
    """Names the dnsmasq instance and its lease file. netd restricts the
    character set; ULIDs are already within it."""
    return f"net-{network_id}"


async def reservations_for(session: AsyncSession, net: Network) -> list[dict[str, str]]:
    """The MAC/address pairs dnsmasq should pin on this network.

    Read at start time rather than stored alongside the server: the interfaces
    are the truth, and a second copy would drift the first time someone moved a
    node onto a different segment.
    """
    from labtris_api.models import Interface

    rows = await session.execute(
        select(Interface).where(
            Interface.network_id == net.id, Interface.reserved_ip.is_not(None)
        )
    )
    return [{"mac": str(i.mac), "ip": str(i.reserved_ip)} for i in rows.scalars()]


async def bring_up(net: Network, dhcp: bool, hosts: list[dict[str, str]] | None = None) -> None:
    """Address the bridge, masquerade behind it, and start DHCP if asked.

    Ordered so the segment is never half-built in a way that leaks: the address
    goes on first because masquerading a subnet the host cannot reach would
    install a rule for traffic that never arrives.
    """
    if not net.host_ifname or not net.subnet or not net.gateway:
        raise runtime_error(f"network {net.name!r} is not ready to bring up")
    prefix = int(net.subnet.split("/")[1])
    try:
        await netd.call(
            "addr.replace",
            {"name": net.host_ifname, "address": net.gateway, "prefix": prefix},
        )
        await netd.call("nat.enable", {"subnet": net.subnet, "bridge": net.host_ifname})
    except NetdError as exc:
        raise runtime_error(f"setting up NAT for {net.name!r}: {exc.message}") from exc

    if dhcp and net.dhcp_first and net.dhcp_last:
        try:
            await netd.call(
                "dhcp.start",
                {
                    "key": dhcp_key(net.id),
                    "bridge": net.host_ifname,
                    "gateway": net.gateway,
                    "prefix": prefix,
                    "first": net.dhcp_first,
                    "last": net.dhcp_last,
                    "dns": net.dns_server,
                    "hosts": hosts or [],
                },
            )
        except NetdError as exc:
            # The network still routes without DHCP, so this is reported rather
            # than fatal — but it is reported, because a guest that sits there
            # not getting an address is otherwise a mystery.
            raise runtime_error(
                f"{net.name!r} is up and routing, but its DHCP server would not "
                f"start: {exc.message}"
            ) from exc


async def tear_down(net: Network) -> None:
    """Undo bring_up, in the reverse order, tolerating anything already gone.

    Every step is best-effort: this runs while deleting a network, and refusing
    to finish because a rule was already removed would leave the row behind and
    the bridge with it.
    """
    if not net.host_ifname:
        return
    for verb, params in (
        ("dhcp.stop", {"key": dhcp_key(net.id)}),
        ("nat.disable", {"subnet": net.subnet, "bridge": net.host_ifname}),
        ("addr.flush", {"name": net.host_ifname}),
    ):
        if verb == "nat.disable" and not net.subnet:
            continue
        try:
            await netd.call(verb, params)
        except NetdError:
            continue


async def apply_vlan_awareness(net: Network) -> None:
    """Turn VLAN filtering on or off for a bridge that already exists."""
    if not net.host_ifname:
        return
    try:
        await netd.call(
            "bridge.vlan_aware",
            {"name": net.host_ifname, "on": bool(net.vlan_aware), "proto": net.vlan_proto},
        )
    except NetdError as exc:
        raise runtime_error(
            f"setting VLAN filtering on {net.name!r}: {exc.message}"
        ) from exc
