from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any, Literal

import ulid
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from labtris_api.errors import conflict, runtime_error, unprocessable
from labtris_api.models import (
    Geometry,
    Host,
    IfnameRegistry,
    Interface,
    Lab,
    Link,
    MacRegistry,
    Network,
    NetworkHost,
    Node,
)
from labtris_api.naming import (
    DEFAULT_IFACE_SCHEME,
    device_hint,
    guest_iface_name,
    host_ifname,
)
from labtris_api.netd_client import NetdError, client_for_host, netd
from labtris_api.runtime.base import IfaceSpec, NodeSpec, RuntimeHandle, StopMode
from labtris_api.runtime.containers import profile_for as container_profile
from labtris_api.runtime.qemu import QEMU_CATALOG
from labtris_api.runtime.registry import get_runtime


def new_id() -> str:
    return ulid.new().str


#: Locally-administered prefix. Configurable so an operator running several
#: instances on one L2 domain can keep them from ever meeting.
MAC_PREFIX = (0x02, 0x00)


def random_mac() -> str:
    """An unreserved address. Prefer allocate_mac(), which also records it."""
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] | 0x02) & 0xFE
    return ":".join(f"{b:02x}" for b in raw)


def _candidate_mac() -> str:
    tail = secrets.token_bytes(4)
    return ":".join(f"{b:02x}" for b in (*MAC_PREFIX, *tail))


async def allocate_mac(session: AsyncSession, owner_id: str) -> str:
    """Reserve a MAC for an interface.

    Mirrors try_allocate_ifname: the uniqueness is the database's, not ours,
    so two requests racing for the same address cannot both win. Falls back to
    an unreserved random address rather than failing a node creation outright
    — a lab that starts is worth more than a perfect registry."""
    existing = await session.execute(
        select(MacRegistry).where(MacRegistry.owner_id == owner_id)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return str(row.mac)
    for _ in range(24):
        mac = _candidate_mac()
        nested = await session.begin_nested()
        session.add(MacRegistry(mac=mac, owner_id=owner_id))
        try:
            await session.flush()
            await nested.commit()
            return mac
        except IntegrityError:
            await nested.rollback()
    return random_mac()


async def release_macs(session: AsyncSession, owner_ids: list[str]) -> None:
    """Give addresses back when their interfaces go away."""
    if not owner_ids:
        return
    await session.execute(sql_delete(MacRegistry).where(MacRegistry.owner_id.in_(owner_ids)))


def peer_owner(iface_id: str) -> str:
    return ("P" + iface_id)[:26].ljust(26, "0")


async def get_lab(session: AsyncSession, lab_id: str) -> Lab:
    from labtris_api.errors import not_found

    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise not_found(f"lab {lab_id} not found")
    return lab


def _same_id(a: str | None, b: str | None) -> bool:
    """Compare ids from a CHAR column, where storage pads with spaces."""
    return a is not None and b is not None and a.strip() == b.strip()


async def require_lab_owner(session: AsyncSession, lab_id: str, user: Any) -> Lab:
    """The lab, if this person is allowed to destroy part of it.

    The rule is narrower than it looks. Everyone can see and open every lab,
    and everyone can work in one — adding a node, drawing a link, moving
    things around — because two people in the same lab at once is a thing
    this is meant to support, and a shared workshop where only the owner may
    touch anything is not shared.

    What is reserved is destruction: deleting a lab, deleting a node, wiping
    a disk. Those are the actions nobody can undo for you, and until now any
    signed-in user could perform them on anybody's work.

    Admins override, because someone has to be able to clear up after a
    student who left, and because an instructor is an admin.
    """
    from labtris_api.errors import ApiError

    lab = await get_lab(session, lab_id)
    if getattr(user, "is_admin", False):
        return lab
    # A lab from before users existed has no owner. Refusing everyone would
    # make those labs undeletable; letting everyone through would be the hole
    # this closes. Whoever is signed in may claim responsibility for them.
    # CHAR(26) is fixed width, so Postgres pads anything shorter with spaces
    # and a plain == against the id fails. Real ULIDs are exactly 26 so this
    # does not bite in production, which is precisely what would have made it
    # a nasty surprise the first time an id was not.
    if lab.owner_id is None or _same_id(lab.owner_id, getattr(user, "id", None)):
        return lab
    raise ApiError(
        "forbidden",
        f"{lab.name!r} belongs to someone else — you can open and work in it, "
        "but only its owner or an administrator can delete from it",
        403,
    )


async def get_unlocked_lab(session: AsyncSession, lab_id: str) -> Lab:
    """Like get_lab but rejects topology edits on a locked lab (EVE-NG lock semantics)."""
    from labtris_api.errors import conflict

    lab = await get_lab(session, lab_id)
    if lab.locked:
        raise conflict("lab is locked — unlock it before editing the topology")
    return lab


async def get_node(session: AsyncSession, node_id: str) -> Node:
    from labtris_api.errors import not_found

    result = await session.execute(
        select(Node).options(selectinload(Node.interfaces)).where(Node.id == node_id)
    )
    node = result.scalar_one_or_none()
    if node is None:
        raise not_found(f"node {node_id} not found")
    return node


async def try_allocate_ifname(
    session: AsyncSession,
    kind: Literal["tap", "bridge", "veth", "vxlan"],
    owner_id: str,
    hint: str = "",
) -> str:
    existing = await session.execute(
        select(IfnameRegistry).where(
            IfnameRegistry.owner_id == owner_id, IfnameRegistry.kind == kind
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row.host_ifname
    for salt in range(16):
        name = host_ifname(kind, owner_id, salt, hint)
        nested = await session.begin_nested()
        session.add(IfnameRegistry(host_ifname=name, kind=kind, owner_id=owner_id))
        try:
            await session.flush()
            await nested.commit()
            return name
        except IntegrityError:
            await nested.rollback()
    from labtris_api.errors import internal

    raise internal("ifname allocation exhausted")


def next_iface_idx(node: Node) -> int:
    used = {i.idx for i in node.interfaces}
    idx = 0
    while idx in used:
        idx += 1
    return idx


def next_free_iface_slot(node: Node, scheme: str | None) -> int:
    """Advance idx until both the idx and the scheme-derived name are free.

    Prior interfaces may have off-scheme names (imports, hand-created rows,
    the old auto-link helper that named idx=0 as "eth1") — so idx-uniqueness
    on its own doesn't guarantee name-uniqueness, and the unique index on
    (node_id, name) will refuse the insert if we don't check both."""
    taken_idx = {i.idx for i in node.interfaces}
    taken_names = {i.name for i in node.interfaces}
    idx = 0
    while idx in taken_idx or guest_iface_name(scheme, idx) in taken_names:
        idx += 1
    return idx


def iface_scheme_for(runtime: str, image: str) -> str:
    """Which naming scheme a node's guest will use for its ports.

    Both catalogs declare one; anything bring-your-own falls back to eth0,
    which is right for a container and the safest guess for anything else."""
    if runtime == "docker":
        profile = container_profile(image)
        return profile.iface_scheme if profile else DEFAULT_IFACE_SCHEME
    spec = QEMU_CATALOG.get(image)
    return spec.iface_scheme if spec else DEFAULT_IFACE_SCHEME


def guest_name(idx: int, requested: str | None, scheme: str | None = None) -> str:
    """What the guest will call this port, unless the caller overrode it."""
    return requested if requested else guest_iface_name(scheme, idx)


async def lookup_peer(session: AsyncSession, iface_id: str) -> str:
    result = await session.execute(
        select(IfnameRegistry).where(IfnameRegistry.owner_id == peer_owner(iface_id))
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise runtime_error("missing peer veth allocation")
    return row.host_ifname


async def node_spec(session: AsyncSession, node: Node) -> NodeSpec:
    ifaces: list[IfaceSpec] = []
    for iface in node.interfaces:
        peer = None
        try:
            peer = await lookup_peer(session, iface.id)
        except Exception:
            peer = None
        ifaces.append(
            IfaceSpec(
                iface_id=iface.id,
                idx=iface.idx,
                guest_name=iface.name,
                host_ifname=iface.host_ifname or "",
                mac=str(iface.mac),
                peer_ifname=peer,
            )
        )
    env = {str(k): str(v) for k, v in (node.env or {}).items()}
    cpu = float(node.cpu_limit) if node.cpu_limit is not None else None
    # For a saved image the template's spec is the source of truth for
    # disk_bus and graphical — the built-in catalog has no entry to fall
    # back on. Without this NX-OSv (which needs sata) or a desktop image
    # (which needs graphical=true) boot-looped because create() hardcoded
    # virtio and False.
    from labtris_api.models import Template
    from labtris_api.runtime.qemu import CUSTOM_PREFIX

    tspec: dict[str, Any] = {}
    if node.image.startswith(CUSTOM_PREFIX):
        row = (
            await session.execute(
                select(Template).where(Template.image == node.image)
            )
        ).scalars().first()
        if row is not None:
            tspec = dict(row.spec or {})

    return NodeSpec(
        qemu_opts=dict(node.qemu_opts or {}),
        node_id=node.id,
        lab_id=node.lab_id,
        name=node.name,
        image=node.image,
        env=env,
        cmd=list(node.cmd) if node.cmd else None,
        cpu_limit=cpu,
        nic_model=node.nic_model or tspec.get("nic_model"),
        ram_mb=node.ram_mb or tspec.get("ram_mb"),
        disk_bus=tspec.get("disk_bus"),
        graphical=tspec.get("graphical"),
        # Companion files + template extras — set once at template
        # registration, applied at every boot. Existing templates have
        # none of these and behave as they did.
        bios=tspec.get("bios"),
        cdrom=tspec.get("cdrom"),
        extra_args=list(tspec.get("qemu_extra_args") or []),
        interfaces=ifaces,
    )


async def _drop_iface(name: str) -> None:
    try:
        await netd.call("iface.delete", {"name": name})
    except NetdError:
        pass


async def _ensure_bridge(name: str) -> None:
    # A host-owned bridge (`br0`, `pnet0`) never matches the Labtris naming
    # scheme netd enforces on bridge.create, so calling it would return EINVAL
    # not EEXIST. On the reuse path the bridge is already there — this is a
    # no-op.
    from labtris_netd.protocol import IFNAME_RE

    if not IFNAME_RE.match(name):
        return
    try:
        await netd.call("bridge.create", {"name": name})
    except NetdError as exc:
        if exc.code != "EEXIST":
            raise


def _vni_for(network_id: str) -> int:
    """Deterministic VNI from the network id — stays in the 24-bit VXLAN VNI
    space and away from 0."""
    return (int(network_id[-8:], 36) % (2**24 - 100)) + 1


async def _require_vxlan_support(hosts: list[Host]) -> None:
    """Ask each netd whether its kernel really has the vxlan device type
    before we start allocating bridges and interface names across the mesh.

    Without this the first `vxlan.create` fails with ENOTSUP partway through,
    leaving half-built state behind — and the error reads like a bug in the
    overlay rather than what it is: a kernel that was built without
    CONFIG_VXLAN (common on minimal cloud and sandbox kernels)."""
    missing = []
    for host in hosts:
        try:
            caps = await client_for_host(host).call("host.capabilities")
        except NetdError:
            continue  # netd predates the verb — let the create attempt answer
        vxlan = (caps.get("links") or {}).get("vxlan") or {}
        if not vxlan.get("supported"):
            missing.append(f"{host.name} ({vxlan.get('error') or 'no vxlan device type'})")
    if missing:
        raise unprocessable(
            "these hosts' kernels cannot create vxlan devices: "
            + "; ".join(missing)
            + ". Load the vxlan module (`modprobe vxlan`) or run netd on a "
            "host with a kernel built with CONFIG_VXLAN."
        )


async def setup_vxlan_mesh(session: AsyncSession, net: Network, hosts: list[Host]) -> None:
    """Full-mesh unicast VXLAN: one bridge per host, one vxlan device per
    peer on that bridge, all sharing one VNI — extends `net`'s L2 segment
    across every host in `hosts`. F6's overlay primitive
    (`docs/04-scaling.md`; containerlab's `tools vxlan`).

    Requires the vxlan kernel device type; hosts whose kernel lacks it (e.g.
    some minimal/sandboxed kernels) are rejected up front rather than halfway
    through building the mesh — that's a host capability gap, not a bug here.
    """
    if not net.host_ifname:
        net.host_ifname = await try_allocate_ifname(
            session, "bridge", net.id, device_hint(net.name)
        )
    if net.vni is None:
        net.vni = _vni_for(net.id)
    await session.commit()

    for host in hosts:
        if not host.underlay_ip:
            raise runtime_error(f"host {host.name!r} has no underlay_ip set for vxlan")

    await _require_vxlan_support(hosts)

    for host in hosts:
        client = client_for_host(host)
        try:
            await client.call("bridge.create", {"name": net.host_ifname})
        except NetdError as exc:
            if exc.code != "EEXIST":
                raise runtime_error(f"vxlan setup on {host.name!r}: {exc.message}") from exc
        await client.call("iface.set_state", {"name": net.host_ifname, "up": True})
        for peer in hosts:
            if peer.id == host.id:
                continue
            # ifname_registry.owner_id is CHAR(26) (sized for one ULID); hash
            # the (network, host, peer) triple down to fit.
            digest = hashlib.blake2b(
                f"{net.id}:{host.id}:{peer.id}".encode(), digest_size=13
            ).hexdigest()
            vxlan_ifname = await try_allocate_ifname(
                session, "vxlan", digest, device_hint(net.name, "vx")
            )
            await session.commit()
            try:
                await client.call(
                    "vxlan.create",
                    {
                        "name": vxlan_ifname,
                        "vni": net.vni,
                        "remote": peer.underlay_ip,
                        "local": host.underlay_ip,
                        "dstport": 4789,
                    },
                )
                await client.call("iface.attach", {"name": vxlan_ifname, "bridge": net.host_ifname})
                await client.call("iface.set_state", {"name": vxlan_ifname, "up": True})
            except NetdError as exc:
                if exc.code == "ENOTSUP":
                    raise unprocessable(
                        f"vxlan {host.name!r} -> {peer.name!r}: {exc.message}"
                    ) from exc
                raise runtime_error(
                    f"vxlan {host.name!r} -> {peer.name!r} failed: {exc.message}"
                ) from exc
            session.add(NetworkHost(network_id=net.id, host_id=host.id, vxlan_ifname=vxlan_ifname))
    await session.commit()


async def teardown_vxlan_mesh(session: AsyncSession, net: Network) -> None:
    result = await session.execute(select(NetworkHost).where(NetworkHost.network_id == net.id))
    rows = list(result.scalars())
    for row in rows:
        host = await session.get(Host, row.host_id)
        if host is None:
            continue
        client = client_for_host(host)
        if row.vxlan_ifname:
            try:
                await client.call("vxlan.delete", {"name": row.vxlan_ifname})
            except NetdError:
                pass
        try:
            await client.call("bridge.delete", {"name": net.host_ifname})
        except NetdError:
            pass
        if row.vxlan_ifname:
            await _release_ifname(session, row.vxlan_ifname)
        await session.delete(row)
    await session.commit()


async def release_ifnames(session: AsyncSession, node: Node) -> None:
    """Give a node's device names back to the pool.

    destroy_node_runtime deletes the devices but leaves the reservations, so
    every node deletion shrank the namespace permanently. Called from deletion
    only — a wiped node keeps its identity, and keeping its names is what makes
    a wipe reproduce the same dataplane."""
    owners = [i.id for i in node.interfaces]
    owners += [peer_owner(i.id) for i in node.interfaces]
    owners.append(node.id)
    if owners:
        await session.execute(
            sql_delete(IfnameRegistry).where(IfnameRegistry.owner_id.in_(owners))
        )


async def _release_ifname(session: AsyncSession, host_ifname: str) -> None:
    """Hand a name back to the registry. vxlan endpoints are registered under
    a hash of the (network, host, peer) triple rather than any object's id, so
    nothing else will ever match them by owner."""
    await session.execute(
        sql_delete(IfnameRegistry).where(IfnameRegistry.host_ifname == host_ifname)
    )


async def announce(node: Node, state: str, error: str | None = None) -> None:
    """Tell whoever is watching this lab that a node changed state.

    Only a *successful* start used to publish, so the UI heard nothing when a
    node stopped or failed and learned about it by refetching on a timer. A
    state change that is not announced is one nobody can react to."""
    try:
        from labtris_api.routers.events import hub

        msg: dict[str, Any] = {
            "type": "node",
            "id": node.id,
            "name": node.name,
            "state": state,
        }
        if error:
            msg["error"] = error
        await hub.publish(node.lab_id, msg)
    except Exception:  # noqa: BLE001 - the state change is the news, not this
        pass


async def start_node(session: AsyncSession, node: Node) -> Node:
    if node.state == "running":
        return node
    if node.state == "starting":
        # A first-use qemu image is a multi-GB fetch, so a start can legitimately
        # sit here for fifteen minutes and look dead. Clicking Start again used
        # to launch a second create against the same overlay, and the two raced
        # for qemu-img's write lock.
        raise conflict(
            f"node {node.name!r} is already starting — a first-time qemu image is "
            "downloaded and converted before boot, which can take several minutes; "
            "watch progress in the catalog or the Pull images task"
        )
    runtime = get_runtime(node.runtime)
    node.state = "starting"
    node.last_error = None
    await session.commit()
    node = await get_node(session, node.id)

    created_bridges: list[str] = []
    handle: RuntimeHandle | None = None
    try:
        for iface in node.interfaces:
            if not iface.host_ifname:
                iface.host_ifname = await try_allocate_ifname(
                    session, "veth", iface.id, device_hint(node.name, iface.name)
                )
            await try_allocate_ifname(
                session, "veth", peer_owner(iface.id), device_hint(node.name, iface.name + "p")
            )
            # Create the tap now — unbridged for the moment even if wired,
            # so QEMU can open it by name at launch. Bridge attach happens
            # after the guest is running (see the post-boot loop below). For
            # runtimes without host-side taps (Docker) this is a no-op on
            # the wire — the netd call is idempotent per name.
            if node.runtime == "qemu":
                try:
                    await netd.call("iface.delete", {"name": iface.host_ifname})
                except NetdError:
                    pass
                await netd.call(
                    "tap.create",
                    {"name": iface.host_ifname, "owner_uid": os.getuid()},
                )
            if not iface.network_id:
                continue
            net = await session.get(Network, iface.network_id)
            if net is None:
                continue
            if not net.host_ifname:
                net.host_ifname = await try_allocate_ifname(
                    session, "bridge", net.id, net.name
                )
                created_bridges.append(net.host_ifname)
            await _ensure_bridge(net.host_ifname)

        await session.commit()
        node = await get_node(session, node.id)
        spec = await node_spec(session, node)

        if node.runtime_ref:
            existing = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
            observed = await runtime.observe(existing)
            if observed.exists:
                handle = existing
                if not observed.running:
                    # Refresh the persisted config from the current spec
                    # so a template edit — new disk_bus for NX-OS, more
                    # RAM for PAN-OS, a wider NIC set — takes effect on
                    # this start. Also picks up interfaces added while
                    # stopped for cold-plug. Cheap; QEMU-only (Docker
                    # implementation is a no-op).
                    if hasattr(runtime, "sync_from_spec"):
                        await runtime.sync_from_spec(handle, spec)
                    else:
                        await runtime.sync_interfaces(handle, spec.interfaces)
                    await runtime.start(handle)

        if handle is None:
            handle = await runtime.create(spec)
            node.runtime_ref = handle.ref
            await session.commit()
            await runtime.start(handle)

        observed = await runtime.observe(handle)
        handle = RuntimeHandle(node_id=node.id, ref=handle.ref, pid=observed.pid)

        for iface in node.interfaces:
            if not iface.network_id or not iface.host_ifname:
                continue
            net = await session.get(Network, iface.network_id)
            if net is None or not net.host_ifname:
                continue
            await _ensure_bridge(net.host_ifname)
            if node.runtime == "qemu":
                # Cold-plug did the guest-side attach; the pre-boot loop did
                # the tap.create. All that remains is joining the tap to the
                # bridge and bringing it up — the wire. Docker still goes
                # through the full attach_iface (veth pair, netns move).
                await netd.call(
                    "iface.attach",
                    {"name": iface.host_ifname, "bridge": net.host_ifname},
                )
                await netd.call("iface.set_state", {"name": iface.host_ifname, "up": True})
            else:
                peer = await lookup_peer(session, iface.id)
                i_spec = IfaceSpec(
                    iface_id=iface.id,
                    idx=iface.idx,
                    guest_name=iface.name,
                    host_ifname=iface.host_ifname,
                    mac=str(iface.mac),
                    peer_ifname=peer,
                )
                await runtime.attach_iface(handle, i_spec, net.host_ifname)
            # Bringing a host-owned bridge up would hit netd's IFNAME_RE guard.
            # See the matching skip in realize_membership.
            from labtris_netd.protocol import IFNAME_RE as _RE

            if _RE.match(net.host_ifname or ""):
                await netd.call("iface.set_state", {"name": net.host_ifname, "up": True})

        from labtris_api.impair import apply_link_qos

        try:
            links = (
                await session.execute(select(Link).where(Link.lab_id == node.lab_id))
            ).scalars()
            for link in links:
                a = await session.get(Interface, link.a_iface_id)
                b = await session.get(Interface, link.b_iface_id)
                if a and b and node.id in {a.node_id, b.node_id}:
                    await apply_link_qos(link, a, b, session=session)
        except Exception:
            pass

        node.state = "running"
        await session.commit()
        await announce(node, "running")
        return await get_node(session, node.id)
    except Exception as exc:
        node.state = "failed"
        node.last_error = str(exc)
        await session.commit()
        await announce(node, "failed", str(exc))
        if handle is not None and node.runtime_ref != handle.ref:
            try:
                await runtime.destroy(handle)
            except Exception:
                pass
        for br in created_bridges:
            await _drop_iface(br)
        if isinstance(exc, NetdError):
            raise runtime_error(exc.message) from exc
        raise


async def stop_node(session: AsyncSession, node: Node, mode: StopMode) -> Node:
    runtime = get_runtime(node.runtime)
    if node.runtime_ref:
        handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
        try:
            await runtime.stop(handle, mode)
        except Exception:
            try:
                await runtime.destroy(handle)
            except Exception:
                pass
        for iface in node.interfaces:
            if iface.host_ifname:
                await _drop_iface(iface.host_ifname)
                # _drop_iface removes the device and its registry row, but the
                # interface row kept naming it — so a stopped node still
                # claimed host devices that no longer existed, and the next
                # start reused the dead name instead of allocating a live one.
                iface.host_ifname = None
    node.state = "stopped"
    await session.commit()
    await announce(node, "stopped")
    return await get_node(session, node.id)


async def destroy_node_runtime(session: AsyncSession, node: Node) -> None:
    runtime = get_runtime(node.runtime)
    if node.runtime_ref:
        handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
        try:
            await runtime.destroy(handle)
        except Exception:
            pass
    for iface in list(node.interfaces):
        if iface.host_ifname:
            await _drop_iface(iface.host_ifname)
        result = await session.execute(
            select(IfnameRegistry).where(IfnameRegistry.owner_id == peer_owner(iface.id))
        )
        row = result.scalar_one_or_none()
        if row is not None:
            await _drop_iface(row.host_ifname)
    node.runtime_ref = None
    node.state = "stopped"


async def realize_link_if_running(session: AsyncSession, link: Link) -> None:
    a = await session.get(Interface, link.a_iface_id)
    b = await session.get(Interface, link.b_iface_id)
    net = await session.get(Network, link.network_id)
    if a is None or b is None or net is None:
        return
    node_a = await get_node(session, a.node_id)
    node_b = await get_node(session, b.node_id)
    if node_a.state != "running" or node_b.state != "running":
        return
    if not net.host_ifname:
        net.host_ifname = await try_allocate_ifname(
            session, "bridge", net.id, device_hint(net.name)
        )
        await netd.call("bridge.create", {"name": net.host_ifname})
        await session.commit()
    for iface, node in ((a, node_a), (b, node_b)):
        if not iface.host_ifname:
            iface.host_ifname = await try_allocate_ifname(
                session, "veth", iface.id, device_hint(node.name, iface.name)
            )
        await try_allocate_ifname(
            session, "veth", peer_owner(iface.id), device_hint(node.name, iface.name + "p")
        )
        await session.commit()
        if not node.runtime_ref or not net.host_ifname:
            continue
        runtime = get_runtime(node.runtime)
        handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
        spec = IfaceSpec(
            iface_id=iface.id,
            idx=iface.idx,
            guest_name=iface.name,
            host_ifname=iface.host_ifname or "",
            mac=str(iface.mac),
            peer_ifname=await lookup_peer(session, iface.id),
        )
        await runtime.attach_iface(handle, spec, net.host_ifname)
    if net.host_ifname:
        await netd.call("iface.set_state", {"name": net.host_ifname, "up": True})
    from labtris_api.impair import apply_link_qos

    await apply_link_qos(link, a, b, session=session)


async def realize_membership(session: AsyncSession, iface: Interface) -> None:
    """Put one interface onto its network's bridge, now, if its node is running.

    Joining a node to a shared segment has to work on a live lab — that is the
    whole point of a canvas you can rewire while things are running — so this
    is the single-interface counterpart of realize_link_if_running()."""
    if not iface.network_id:
        return
    net = await session.get(Network, iface.network_id)
    if net is None:
        return
    node = await get_node(session, iface.node_id)
    if not net.host_ifname:
        net.host_ifname = await try_allocate_ifname(
            session, "bridge", net.id, device_hint(net.name)
        )
        await session.commit()
    await _ensure_bridge(net.host_ifname)
    await bind_cloud(net)
    if node.state != "running" or not node.runtime_ref:
        return
    if not iface.host_ifname:
        iface.host_ifname = await try_allocate_ifname(
            session, "veth", iface.id, device_hint(node.name, iface.name)
        )
    await try_allocate_ifname(
        session, "veth", peer_owner(iface.id), device_hint(node.name, iface.name + "p")
    )
    await session.commit()
    runtime = get_runtime(node.runtime)
    spec = IfaceSpec(
        iface_id=iface.id,
        idx=iface.idx,
        guest_name=iface.name,
        host_ifname=iface.host_ifname or "",
        mac=str(iface.mac),
        peer_ifname=await lookup_peer(session, iface.id),
    )
    await runtime.attach_iface(
        RuntimeHandle(node_id=node.id, ref=node.runtime_ref), spec, net.host_ifname or ""
    )
    # Bringing an OS-owned bridge "up" via iface.set_state would hit the
    # IFNAME_RE guard (br0 is not a Labtris name). Reused bridges are already
    # up — that's a precondition of picking them.
    from labtris_netd.protocol import IFNAME_RE as _RE

    if _RE.match(net.host_ifname or ""):
        await netd.call("iface.set_state", {"name": net.host_ifname, "up": True})
    await apply_port_vlan(net, iface)


async def apply_port_vlan(net: Network, iface: Interface) -> None:
    """Re-apply a port's VLAN membership to the bridge it has just joined.

    VLAN membership lives in the kernel on the bridge port, and a port is a
    fresh veth every time its node starts — so without this the access port you
    configured yesterday is a plain untagged port today, and the lab quietly
    stops demonstrating the thing it was built to demonstrate.
    """
    if not net.vlan_aware or not iface.vlan_mode or not iface.host_ifname:
        return
    tagged = [int(v) for v in (iface.trunk_vids or "").split(",") if v]
    try:
        await netd.call(
            "bridge.port_vlan",
            {
                "name": iface.host_ifname,
                "pvid": iface.vlan_id,
                "tagged": tagged,
                "untagged": [iface.vlan_id] if iface.vlan_id else [],
            },
        )
    except NetdError:
        # Reported by the diagnostics rather than fatal here: the port is
        # attached and passing traffic, it is just on the wrong VLAN.
        return


async def bind_cloud(net: Network, force: bool = False) -> None:
    """A `cloud` network is a lab bridge with one of the host's own NICs
    enslaved to it, which is how a lab reaches anything outside itself
    (EVE-NG's pnet). Enslaving a NIC moves its L2 traffic to the bridge, so
    this deliberately refuses the interface netd itself is reachable over —
    doing that to the management NIC takes the host off the network unless the
    caller explicitly overrides."""
    if net.kind != "cloud" or not net.cloud_ref or not net.host_ifname:
        return
    # Reuse path: the cloud is backed by an OS-owned bridge that Labtris found
    # rather than created. The bridge already exists and its NIC (if any) is
    # already enslaved by netplan — there is nothing to attach. This is called
    # from realize_membership on every node start, so the guard has to live
    # here rather than only on the create path.
    if net.cloud_ref == net.host_ifname:
        return
    try:
        await netd.call(
            "cloud.attach",
            {"name": net.cloud_ref, "bridge": net.host_ifname, "force": force},
        )
    except NetdError as exc:
        if exc.code == "EEXIST":
            return
        if exc.code == "EPERM":
            raise unprocessable(exc.message) from exc
        raise runtime_error(
            f"binding host interface {net.cloud_ref!r} to {net.name!r} failed: {exc.message}"
        ) from exc


async def delete_lab(session: AsyncSession, lab: Lab) -> None:
    result = await session.execute(
        select(Node).options(selectinload(Node.interfaces)).where(Node.lab_id == lab.id)
    )
    nodes = list(result.scalars().unique())
    owner_ids: list[str] = [lab.id]
    for node in nodes:
        await destroy_node_runtime(session, node)
        owner_ids.append(node.id)
        for iface in node.interfaces:
            owner_ids.append(iface.id)
            owner_ids.append(peer_owner(iface.id))
    nets = list((await session.execute(select(Network).where(Network.lab_id == lab.id))).scalars())
    for net in nets:
        owner_ids.append(net.id)
        if net.kind == "vxlan":
            # A stretched network has a bridge and a vxlan endpoint on every
            # host it spans. Dropping the local interface leaves all the
            # remote ones behind forever — nothing else ever revisits them.
            await teardown_vxlan_mesh(session, net)
        elif net.kind == "cloud" and net.host_ifname == net.cloud_ref:
            # Reuse path: the bridge is OS-owned. Deleting the lab must not
            # take the host's uplink with it.
            pass
        elif net.host_ifname:
            await _drop_iface(net.host_ifname)
        await session.delete(net)
    rows = await session.execute(
        select(IfnameRegistry).where(IfnameRegistry.owner_id.in_(owner_ids))
    )
    for row in rows.scalars():
        await _drop_iface(row.host_ifname)
        await session.delete(row)
    await release_macs(session, owner_ids)
    from labtris_api.runtime.qemu import remove_data_volume

    for node in nodes:
        remove_data_volume(node.id)
    geom = await session.get(Geometry, lab.id)
    if geom is not None:
        await session.delete(geom)
    await session.execute(sql_delete(Lab).where(Lab.id == lab.id))
    await session.commit()
