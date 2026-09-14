from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import (
    bad_request,
    conflict,
    not_found,
    runtime_error,
    unprocessable,
)
from labtris_api.lifecycle import (
    bind_cloud,
    get_lab,
    new_id,
    realize_membership,
    require_lab_owner,
    setup_vxlan_mesh,
    teardown_vxlan_mesh,
    try_allocate_ifname,
)
from labtris_api.models import Host, Interface, Link, Network, NetworkHost, Node
from labtris_api.naming import device_hint
from labtris_api.nat import (
    apply_vlan_awareness,
    bring_up,
    dhcp_key,
    pick_subnet,
    pool_for,
    reservations_for,
    tear_down,
)
from labtris_api.netd_client import NetdError, netd
from labtris_api.schemas import (
    CaptureIn,
    InterfaceOut,
    InterfacePatch,
    NetworkCreate,
    NetworkHostOut,
    NetworkOut,
    NetworkPatch,
    PortReservation,
    PortVlan,
)

router = APIRouter(tags=["networks"])


@router.post("/labs/{lab_id}/networks", response_model=NetworkOut, status_code=201)
async def create_network(
    lab_id: str,
    body: NetworkCreate,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Network:
    await get_lab(session, lab_id)
    if body.kind == "vxlan" and len(body.host_ids) < 2:
        raise bad_request("a vxlan network needs at least 2 host_ids to span")
    net = Network(
        id=new_id(),
        lab_id=lab_id,
        name=body.name,
        kind=body.kind,
        cloud_ref=body.cloud_ref,
        vni=body.vni,
    )
    # Addressing is decided before the first commit, not after it: a NAT row
    # with no subnet violates its own CHECK constraint, and the row has to be
    # valid the first time it is written.
    if body.kind == "nat":
        subnet = await pick_subnet(session, body.subnet)
        gateway, first, last = pool_for(subnet)
        net.subnet = str(subnet)
        net.gateway = gateway
        net.dns_server = body.dns_server
        if body.dhcp:
            net.dhcp_first, net.dhcp_last = first, last

    session.add(net)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Reporting every integrity error as a duplicate name sent me looking
        # for a network that did not exist. Only claim that when it is what
        # actually happened.
        detail = str(getattr(exc, "orig", exc))
        if "networks_lab_id_name_key" in detail or "duplicate key" in detail:
            raise conflict(f"network name {body.name!r} already exists") from exc
        raise bad_request(f"that network cannot be created: {detail[:200]}") from exc
    await session.refresh(net)

    is_reuse = False
    if body.kind == "cloud":
        if not body.cloud_ref:
            raise bad_request("a cloud network needs cloud_ref, the host NIC to bind")
        # Ask netd whether the picker chose a bare NIC or an OS-owned bridge.
        # The answer decides both the uniqueness rule and whether Labtris will
        # create+enslave or just reuse what's there.
        try:
            probe = await netd.call("host.inspect_bridge", {"name": body.cloud_ref})
        except NetdError as exc:
            await session.delete(net)
            await session.commit()
            raise bad_request(f"inspecting {body.cloud_ref!r}: {exc.message}") from exc
        is_reuse = bool(probe.get("exists") and probe.get("kind") == "bridge")
        # Same guard host_interfaces() applies in the picker: docker0 is off
        # limits, and nothing should be able to bind lab traffic through it.
        # Enforcing here as well means a client that talks to the API directly
        # (MCP, curl, tests) can't sneak past the UI's check.
        if is_reuse and body.cloud_ref == "docker0":
            await session.delete(net)
            await session.commit()
            raise bad_request(
                "docker0 is Docker's bridge — Labtris will not attach lab "
                "traffic to it. Pick a different host bridge or NIC."
            )
        # Uniqueness — but only for the enslave path. There, a second cloud
        # would move the NIC (Linux: one master per interface) and leave the
        # first cloud with a dead bridge. On the reuse path there's no
        # enslavement — Labtris just attaches lab veths as extra bridge ports
        # — so multiple cloud networks can share the same host bridge. They
        # end up as separate icons on the canvas but the same L2 segment on
        # the wire, which is what you want when a single uplink needs to
        # appear as multiple lab connections.
        if not is_reuse:
            taken = await session.execute(
                select(Network).where(
                    Network.kind == "cloud",
                    Network.cloud_ref == body.cloud_ref,
                    Network.id != net.id,
                )
            )
            holder = taken.scalars().first()
            if holder is not None:
                await session.delete(net)
                await session.commit()
                raise conflict(
                    f"host interface {body.cloud_ref!r} is already bound to network "
                    f"{holder.name!r}. One NIC can only be on one bridge — delete that "
                    "cloud first, or pick another interface."
                )
        if is_reuse:
            if probe.get("has_default_route_addr") and not body.allow_default_route:
                await session.delete(net)
                await session.commit()
                raise unprocessable(
                    f"{body.cloud_ref!r} holds this host's default-route address. "
                    "Reusing it as a cloud is safe (the address stays put), but "
                    "confirm by setting allow_default_route."
                )
            net.host_ifname = body.cloud_ref
            await session.commit()

    if body.kind in ("bridge", "nat") or (body.kind == "cloud" and not is_reuse):
        # Build the bridge up front so the object on the canvas corresponds to
        # something real, and a cloud is actually wired to its host NIC before
        # any node joins it.
        net.host_ifname = await try_allocate_ifname(
            session, "bridge", net.id, device_hint(net.name)
        )
        await session.commit()
        try:
            await netd.call("bridge.create", {"name": net.host_ifname})
        except NetdError as exc:
            if exc.code != "EEXIST":
                await session.delete(net)
                await session.commit()
                raise runtime_error(f"creating bridge for {net.name!r}: {exc.message}") from exc
        await netd.call("iface.set_state", {"name": net.host_ifname, "up": True})
        try:
            await bind_cloud(net, force=body.allow_default_route)
            if body.kind == "nat":
                await bring_up(net, dhcp=body.dhcp, hosts=await reservations_for(session, net))
            if body.vlan_aware and body.kind in ("bridge", "nat"):
                net.vlan_aware = True
                net.vlan_proto = body.vlan_proto
                await apply_vlan_awareness(net)
        except Exception:
            # Anything half-built goes with the row. A NAT network that exists
            # in the database but has no masquerade rule is worse than none:
            # it looks like it should work.
            await tear_down(net)
            try:
                await netd.call("bridge.delete", {"name": net.host_ifname})
            except NetdError:
                pass
            await session.delete(net)
            await session.commit()
            raise
        await session.commit()
        await session.refresh(net)

    if body.kind == "vxlan":
        hosts = [await session.get(Host, hid) for hid in body.host_ids]
        missing = [hid for hid, h in zip(body.host_ids, hosts, strict=False) if h is None]
        if missing:
            await session.delete(net)
            await session.commit()
            raise bad_request(f"unknown host_ids: {missing}")
        try:
            await setup_vxlan_mesh(session, net, [h for h in hosts if h is not None])
        except Exception:
            # The row is already committed (setup_vxlan_mesh needs its id to
            # allocate interface names); a mesh that couldn't be built must not
            # leave a network behind that looks usable.
            await session.rollback()
            await teardown_vxlan_mesh(session, net)
            await session.delete(net)
            await session.commit()
            raise
        await session.refresh(net)

    return net


@router.patch("/interfaces/{iface_id}", response_model=InterfaceOut)
async def set_interface_network(
    iface_id: str,
    body: InterfacePatch,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Interface:
    """Join an interface to a network, or detach it.

    Networks could only be chosen when an interface was created, which made a
    shared segment unusable from a canvas: you could not take a node that
    already exists and put it on a bridge. Takes effect immediately on a
    running node."""
    iface = await session.get(Interface, iface_id)
    if iface is None:
        raise not_found(f"interface {iface_id} not found")

    linked = await session.execute(
        select(Link).where(or_(Link.a_iface_id == iface_id, Link.b_iface_id == iface_id))
    )
    if linked.scalars().first() is not None:
        raise conflict(
            "interface is part of a point-to-point link — delete the link before "
            "moving it onto a network"
        )

    data = body.model_dump(exclude_unset=True)
    if "network_id" in data:
        target = data["network_id"]
        if target is not None:
            net = await session.get(Network, target)
            if net is None:
                raise not_found(f"network {target} not found")
            owner = await session.get(Node, iface.node_id)
            if owner is None or net.lab_id != owner.lab_id:
                raise bad_request("interface and network belong to different labs")
        iface.network_id = target
    await session.commit()
    await session.refresh(iface)
    if iface.network_id:
        await realize_membership(session, iface)
        await session.refresh(iface)
    return iface


@router.get("/system/host-interfaces")
async def host_interfaces(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    """The host NICs a `cloud` network can bind to."""
    try:
        return await netd.call("host.interfaces")
    except NetdError as exc:
        raise runtime_error(f"listing host interfaces failed: {exc.message}") from exc


@router.get("/networks/{network_id}/hosts", response_model=list[NetworkHostOut])
async def network_hosts(
    network_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> list[NetworkHostOut]:
    """Which hosts a stretched network reaches, and the vxlan device carrying
    it on each — the mesh the one POST above built, made inspectable."""
    net = await session.get(Network, network_id)
    if net is None:
        raise not_found(f"network {network_id} not found")
    rows = (
        (
            await session.execute(
                select(NetworkHost, Host)
                .join(Host, Host.id == NetworkHost.host_id)
                .where(NetworkHost.network_id == network_id)
                .order_by(Host.name)
            )
        )
        .tuples()
        .all()
    )
    return [
        NetworkHostOut(
            host_id=host.id,
            host_name=host.name,
            underlay_ip=host.underlay_ip,
            vxlan_ifname=row.vxlan_ifname,
        )
        for row, host in rows
    ]


@router.post("/networks/{network_id}/capture/start")
async def start_network_capture(
    network_id: str,
    body: CaptureIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Capture everything crossing a segment, rather than one node's view of it.

    A bridge sees every frame its ports flood, so this is the closest thing to
    a mirror port — and it works on an empty bridge, which is how you watch a
    cloud's uplink or prove that nothing is arriving at all."""
    net = await _capturable(session, network_id)
    return await netd.call("capture.start", {"name": net.host_ifname, "bpf": body.bpf})


@router.get("/networks/{network_id}/capture")
async def read_network_capture(
    network_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    net = await _capturable(session, network_id)
    return await netd.call("capture.read", {"name": net.host_ifname})


@router.post("/networks/{network_id}/capture/stop")
async def stop_network_capture(
    network_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    net = await _capturable(session, network_id)
    return await netd.call("capture.stop", {"name": net.host_ifname})


async def _capturable(session: AsyncSession, network_id: str) -> Network:
    net = await session.get(Network, network_id)
    if net is None:
        raise not_found(f"network {network_id} not found")
    if not net.host_ifname:
        raise unprocessable(
            f"network {net.name!r} has no bridge on this host yet — a vxlan segment "
            "is only built once its mesh is set up"
        )
    return net


@router.delete("/networks/{network_id}", status_code=204)
async def delete_network(
    network_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    net = await session.get(Network, network_id)
    if net is None:
        raise not_found(f"network {network_id} not found")
    await require_lab_owner(session, net.lab_id, user)
    in_use = await session.execute(select(Interface).where(Interface.network_id == network_id))
    if in_use.scalars().first() is not None:
        raise conflict("network is in use")
    if net.kind == "nat":
        # Before the bridge disappears: the rules and the DHCP server are
        # attached to it by name, and cleaning up after it is gone means
        # matching on text alone.
        await tear_down(net)
    if net.kind == "vxlan":
        await teardown_vxlan_mesh(session, net)
    elif net.kind == "cloud" and net.cloud_ref:
        if net.host_ifname and net.host_ifname == net.cloud_ref:
            # Reuse path: the bridge is OS-owned (`br0`, `pnet0`). Labtris
            # never created or enslaved anything for it, so there is nothing
            # to detach or delete. Just drop the DB row.
            pass
        else:
            # Give the host its interface back before the bridge disappears.
            try:
                await netd.call("cloud.detach", {"name": net.cloud_ref})
            except NetdError:
                pass
            if net.host_ifname:
                try:
                    await netd.call("bridge.delete", {"name": net.host_ifname})
                except NetdError:
                    pass
    elif net.host_ifname:
        try:
            await netd.call("bridge.delete", {"name": net.host_ifname})
        except NetdError:
            pass
    await session.delete(net)
    await session.commit()
    return Response(status_code=204)


@router.patch("/networks/{network_id}", response_model=NetworkOut)
async def update_network(
    network_id: str,
    body: NetworkPatch,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Network:
    """Edit a network in place.

    Changing the subnet of a NAT network is the interesting case: the old
    masquerade rule and DHCP server are for a prefix that is about to stop
    existing, so the plumbing comes down before the row changes and goes back
    up after. Guests keep their old addresses until they renew, which is
    exactly what would happen on real equipment.
    """
    net = await session.get(Network, network_id)
    if net is None:
        raise not_found(f"network {network_id} not found")
    await require_lab_owner(session, net.lab_id, user)

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return net

    rebuild = net.kind == "nat" and (
        "subnet" in fields or "dhcp" in fields or "dns_server" in fields
    )
    if rebuild:
        await tear_down(net)

    if "name" in fields and fields["name"]:
        net.name = fields["name"]
    if "dns_server" in fields:
        net.dns_server = fields["dns_server"]
    if "subnet" in fields and fields["subnet"] and net.kind == "nat":
        subnet = await pick_subnet(session, fields["subnet"])
        gateway, first, last = pool_for(subnet)
        net.subnet, net.gateway = str(subnet), gateway
        if net.dhcp_first:
            net.dhcp_first, net.dhcp_last = first, last
    if "dhcp" in fields and net.kind == "nat":
        if fields["dhcp"]:
            _, first, last = pool_for(ipaddress.ip_network(net.subnet, strict=False))
            net.dhcp_first, net.dhcp_last = first, last
        else:
            net.dhcp_first = net.dhcp_last = None

    vlan_changed = False
    if "vlan_aware" in fields and net.kind in ("bridge", "nat"):
        net.vlan_aware = bool(fields["vlan_aware"])
        vlan_changed = True
    if "vlan_proto" in fields and fields["vlan_proto"]:
        net.vlan_proto = fields["vlan_proto"]
        vlan_changed = True

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"network name {body.name!r} already exists") from exc
    await session.refresh(net)

    if rebuild:
        await bring_up(net, dhcp=bool(net.dhcp_first), hosts=await reservations_for(session, net))
    if vlan_changed:
        await apply_vlan_awareness(net)
    return net


@router.get("/networks/{network_id}/leases")
async def network_leases(
    network_id: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """What the network's DHCP server has actually handed out.

    Read from dnsmasq's own lease file rather than tracked separately, because
    the server is the thing that knows — anything we recorded alongside it
    would be a second answer that drifts.
    """
    net = await session.get(Network, network_id)
    if net is None:
        raise not_found(f"network {network_id} not found")
    if net.kind != "nat" or not net.dhcp_first:
        return {"leases": []}
    try:
        return await netd.call("dhcp.leases", {"key": dhcp_key(net.id)})
    except NetdError:
        return {"leases": []}


@router.patch("/interfaces/{iface_id}/vlan", response_model=InterfaceOut)
async def set_interface_vlan(
    iface_id: str,
    body: PortVlan,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Interface:
    """Put a port on a VLAN, as access or trunk.

    Refused on a bridge that is not VLAN-filtering, rather than accepted and
    silently ignored: the kernel takes the configuration either way and does
    nothing with it, which is the worst possible answer for someone learning
    what a trunk is.
    """
    iface = await session.get(Interface, iface_id)
    if iface is None:
        raise not_found(f"interface {iface_id} not found")
    node = await session.get(Node, iface.node_id)
    if node is None:
        raise not_found("interface has no node")
    await require_lab_owner(session, node.lab_id, user)

    net = await session.get(Network, iface.network_id) if iface.network_id else None
    if body.vlan_mode is not None:
        if net is None:
            raise bad_request("that port is not on a network, so it has no VLAN to be on")
        if not net.vlan_aware:
            raise bad_request(
                f"{net.name!r} is a plain bridge — it forwards tagged frames without "
                "reading them, so an access port on it would not be enforced. Turn on "
                "VLAN filtering for that network first."
            )
    if body.vlan_mode == "access" and not body.vlan_id:
        raise bad_request("an access port needs a vlan_id")
    if body.vlan_mode == "trunk" and not body.trunk_vids:
        raise bad_request("a trunk needs at least one VLAN in trunk_vids")

    iface.vlan_mode = body.vlan_mode
    iface.vlan_id = body.vlan_id if body.vlan_mode == "access" else None
    iface.trunk_vids = (
        ",".join(str(v) for v in body.trunk_vids) if body.vlan_mode == "trunk" else None
    )
    await session.commit()
    await session.refresh(iface)

    # Only a port that is actually attached to the bridge can be configured on
    # it; a defined-but-not-running node has nothing there yet, and the
    # membership is applied when it joins.
    if iface.host_ifname and net is not None and net.vlan_aware:
        try:
            await netd.call(
                "bridge.port_vlan",
                {
                    "name": iface.host_ifname,
                    "pvid": iface.vlan_id,
                    "tagged": [int(v) for v in (iface.trunk_vids or "").split(",") if v],
                    "untagged": [iface.vlan_id] if iface.vlan_id else [],
                },
            )
        except NetdError as exc:
            raise runtime_error(f"setting VLAN on {iface.name}: {exc.message}") from exc
    return iface


@router.get("/networks/{network_id}/sessions")
async def network_sessions(
    network_id: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Flows currently tracked through this network's NAT.

    The one view a capture inside the lab cannot give you: what the outside
    sees each flow as after the source is rewritten. Read from conntrack, which
    is the kernel's own record — anything we counted ourselves would be a
    second opinion that drifts.
    """
    net = await session.get(Network, network_id)
    if net is None:
        raise not_found(f"network {network_id} not found")
    if net.kind != "nat" or not net.subnet:
        return {"sessions": [], "note": "only a NAT network translates anything"}
    try:
        return await netd.call("nat.sessions", {"subnet": net.subnet})
    except NetdError as exc:
        return {"sessions": [], "note": exc.message}


@router.patch("/interfaces/{iface_id}/reservation", response_model=InterfaceOut)
async def set_interface_reservation(
    iface_id: str,
    body: PortReservation,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Interface:
    """Pin this port to an address, so it gets the same one every boot.

    Refused outside the network's own subnet and inside its DHCP pool: a
    reservation the server would never hand out, or one that collides with what
    it hands out dynamically, produces a guest that sometimes has the address
    and sometimes does not — the hardest kind of lab fault to see.
    """
    iface = await session.get(Interface, iface_id)
    if iface is None:
        raise not_found(f"interface {iface_id} not found")
    node = await session.get(Node, iface.node_id)
    if node is None:
        raise not_found("interface has no node")
    await require_lab_owner(session, node.lab_id, user)

    net = await session.get(Network, iface.network_id) if iface.network_id else None
    wanted = (body.reserved_ip or "").strip() or None

    if wanted:
        if net is None or net.kind != "nat" or not net.subnet:
            raise bad_request("reservations only apply to a port on a NAT network")
        try:
            addr = ipaddress.ip_address(wanted)
        except ValueError as exc:
            raise bad_request(f"{wanted!r} is not an address") from exc
        subnet = ipaddress.ip_network(net.subnet, strict=False)
        if addr not in subnet:
            raise bad_request(f"{wanted} is not inside {net.subnet}")
        if net.gateway and str(addr) == net.gateway:
            raise bad_request(f"{wanted} is the gateway")
        if net.dhcp_first and net.dhcp_last:
            first = ipaddress.ip_address(net.dhcp_first)
            last = ipaddress.ip_address(net.dhcp_last)
            if first <= addr <= last:
                raise bad_request(
                    f"{wanted} is inside the DHCP pool ({net.dhcp_first}–{net.dhcp_last}). "
                    "Reserve an address below it, or shrink the pool."
                )
        clash = await session.execute(
            select(Interface).where(
                Interface.network_id == net.id,
                Interface.reserved_ip == wanted,
                Interface.id != iface.id,
            )
        )
        if clash.scalars().first() is not None:
            raise conflict(f"{wanted} is already reserved on {net.name!r}")

    iface.reserved_ip = wanted
    await session.commit()
    await session.refresh(iface)

    # dnsmasq takes reservations as arguments, so it is restarted with the new
    # set. Existing leases survive in its lease file; a guest holding the old
    # address keeps it until it renews, which is what real equipment does too.
    if net is not None and net.kind == "nat" and net.dhcp_first:
        await tear_down(net)
        await bring_up(net, dhcp=True, hosts=await reservations_for(session, net))
    return iface
