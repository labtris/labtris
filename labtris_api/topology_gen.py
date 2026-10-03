"""AI-fabric topology generators.

Draw a 2-spine 4-leaf 8-host-per-leaf spine-leaf by hand and you spend
20 minutes on it and get half the wires wrong. Feed the same shape to
this module and get a working 42-node lab in one call — nodes,
interfaces, links, per-link networks, geometry, and (optionally) FRR
startup-configs that come up as a BGP EVPN underlay.

Three patterns today:

- **spine-leaf** — the standard 2-tier CLOS: N spines × M leaves, every
  spine wires to every leaf. Hosts hang off each leaf; each host has
  one uplink. Useful for RoCEv2 and Ultra Ethernet prototyping.
- **rail-optimised** — the shape a GPU cluster uses. Hosts group by
  "rail" (GPU index) and each rail attaches to a dedicated spine. Rail
  1 hosts talk rail-1-to-rail-1 across all leaves. Not a spine-leaf
  variant; a different topology with different failure modes.
- **fat-tree** — full 3-tier k-ary fat tree (core, aggregation, edge).
  Text-book. Included for parity with the DC-networking literature.

Everything the generator produces goes through the same Node/Link/
Network/Geometry rows a hand-drawn lab uses, so a generated lab is
indistinguishable from a manually-built one after commit. No hidden
"generated" flag; delete-a-node works, edit-a-link works.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.errors import bad_request

#: Both live in frr_templates so the demo-pod builder can load them by
#: path without importing the application. Re-exported under the names
#: this module has always used.
from labtris_api.frr_templates import _FRR_DAEMONS_EVPN, _frr_installer
from labtris_api.lifecycle import (
    allocate_mac,
    get_lab,
    iface_scheme_for,
    new_id,
)
from labtris_api.models import Geometry, Interface, Lab, Link, Network, Node
from labtris_api.naming import guest_iface_name

# ---- kinds -----------------------------------------------------------

# Well-known short names the generator accepts, mapped to what actually
# goes into Node.runtime + Node.image. Small on purpose — the point of
# these patterns is a working fabric out-of-the-box; more esoteric node
# kinds are for the user to substitute after generation.
_KINDS: dict[str, tuple[str, str]] = {
    # docker
    "alpine":  ("docker", "alpine:3.20"),
    "frr":     ("docker", "frrouting/frr:v8.4.0"),
    "bmv2":    ("docker", "p4lang/behavioral-model:latest"),
    "ubuntu":  ("docker", "ubuntu:24.04"),
    "srlinux": ("docker", "ghcr.io/nokia/srlinux:latest"),
    # AI-fabric endpoints. Both are local builds (see packaging/
    # dockerfiles/), so a generated fabric using them needs the image
    # built first — the generator does not pull them.
    "uet-ref":   ("docker", "labtris/uet-ref:latest"),
    "rdma-host": ("docker", "labtris/rdma-host:latest"),
}


def _resolve_kind(kind: str) -> tuple[str, str]:
    if kind not in _KINDS:
        raise bad_request(
            f"unknown node kind {kind!r} — try {', '.join(sorted(_KINDS))}"
        )
    return _KINDS[kind]


# ---- request schema --------------------------------------------------


class GenerateIn(BaseModel):
    pattern: Literal[
        "spine-leaf", "rail-optimised", "fat-tree", "front-end", "scale-across"
    ]
    #: Sizes — meaning depends on pattern.
    spines: int = Field(2, ge=1, le=32)
    leaves: int = Field(4, ge=1, le=64)
    hosts_per_leaf: int = Field(2, ge=0, le=64)
    #: rail-optimised only
    rails: int = Field(4, ge=1, le=32)
    hosts_per_rail: int = Field(2, ge=0, le=32)
    #: fat-tree only. k must be even; the standard formula is k^3/4
    #: hosts, 5k^2/4 switches — so k grows the node count cubically:
    #:
    #:   k=8    128 hosts +  80 switches =   208 nodes
    #:   k=12   432 hosts + 180 switches =   612 nodes
    #:   k=16  1024 hosts + 320 switches =  1344 nodes
    #:   k=20  2000 hosts + 500 switches =  2500 nodes
    #:   k=22  2662 hosts + 605 switches =  3267 nodes
    #:   k=24  3456 hosts + 720 switches =  4176 nodes
    #:
    #: The old ceiling was 8, "kept modest for a browser-usable lab", which
    #: made the largest fat-tree 208 nodes and sent anyone wanting a big
    #: fabric to spine-leaf instead — where the limits already allow
    #: 32 x 64 x 64 = 4192. 16 matches that intent and reaches the ~1300-node
    #: shape people compare against.
    #:
    #: Creating one is cheap: a 1352-node spine-leaf generated in 48s,
    #: database rows and all. STARTING one is not — measured at ~5.3s per
    #: node, dominated by per-node netlink work, so a k=16 fat-tree is an
    #: hour of starting. The cap is not what makes that slow, and raising it
    #: does not pretend otherwise.
    #:
    #: Then 16 turned out to be the ceiling again the moment someone wanted
    #: 3000 nodes: k=16 is 1344 and the next even k is the only way up,
    #: because the host count is k^3/4 and nothing else in a fat-tree is
    #: adjustable. 24 is the new cap, which reaches 4176 — chosen because it
    #: is the first value that clears 4000 and because 3k^3/4 links at k=24
    #: is 10368, within what the generator has been run at. It is a ceiling
    #: on what you may ASK for, not a promise the host can start it: measure
    #: the per-node cost of the kind you are using before committing to a
    #: big k. For FRR with zebra+staticd+bgpd+ospfd that was ~20 MB a node,
    #: so a k=22 all-FRR fabric wants 65 GB before any routes exist.
    k: int = Field(4, ge=2, le=24)
    #: scale-across only: how many sites, and the one-way delay between
    #: them. Defaults describe a metro pair — far enough that a collective
    #: notices, close enough that people really do run them this way.
    sites: int = Field(2, ge=2, le=8)
    dci_delay_ms: int = Field(12, ge=0, le=500)
    #: A DCI is rarely symmetric: different paths, different queues. The
    #: return delay defaults slightly higher so a generated lab exercises
    #: per-direction impairment rather than quietly ignoring it.
    dci_return_delay_ms: int = Field(14, ge=0, le=500)
    #: front-end only: how many service hosts hang off the access switch.
    services: int = Field(3, ge=0, le=32)
    #: front-end only: give the border pair a NAT segment for egress, so
    #: the lab can actually reach the internet rather than only looking as
    #: though it could.
    with_egress: bool = True
    #: What to run each role as. Free choice — a spine can be a bmv2
    #: switch (programmable data plane) or a plain FRR router.
    spine_kind: str = "bmv2"
    leaf_kind: str = "frr"
    host_kind: str = "alpine"
    #: When spine_kind == "bmv2".
    p4_program: str = "basic_switch"
    #: Generate FRR daemons + frr.conf on every leaf and spine.
    with_bgp_evpn: bool = False
    #: Placeholder — Phase E does not ship rxe autoload yet; the flag
    #: is accepted so a caller's request shape stays stable when it
    #: does. Ignored for now.
    with_rxe: bool = False


class GenerateOut(BaseModel):
    lab_id: str
    pattern: str
    node_count: int
    link_count: int
    network_count: int


# ---- geometry helpers ------------------------------------------------

_GRID = 200        # px between nodes on either axis
_ROW_STEP = 200    # px between rows
_ROW_OFFSET = 100  # px inset from the top


def _grid_pos(row: int, col: int, wide_count: int) -> dict[str, int]:
    """Center-align a row of `wide_count` nodes at column `col`."""
    center = wide_count * _GRID / 2
    x = int(col * _GRID - center + _GRID / 2)
    y = int(_ROW_OFFSET + row * _ROW_STEP)
    return {"x": x, "y": y}


# ---- node + link materialisation ------------------------------------


class _Builder:
    """Small helper that batches session.add + tracks id maps.

    Keeps the top-level generator functions readable: no session
    plumbing, no repeated Interface(...) constructor calls."""

    def __init__(self, session: AsyncSession, lab: Lab) -> None:
        self.session = session
        self.lab = lab
        self.nodes_created: list[Node] = []
        self.links_created: list[Link] = []
        self.networks_created: list[Network] = []
        self.geometry_nodes: dict[str, dict[str, int]] = {}

    async def add_node(
        self,
        name: str,
        kind: str,
        row: int,
        col: int,
        row_wide: int,
        opts: dict[str, Any] | None = None,
        startup_config: str | None = None,
        at: tuple[int, int] | None = None,
    ) -> Node:
        """`at` is an explicit (col, row) in grid cells, for a layout that a
        single centred row cannot express — see the fat tree, which places
        each pod as a block rather than stretching one row of hosts across
        half a million pixels."""
        runtime, image = _resolve_kind(kind)
        node = Node(
            id=new_id(),
            lab_id=self.lab.id,
            name=name,
            runtime=runtime,
            image=image,
            env={},
            cmd=None,
            state="defined",
            opts=opts,
            startup_config=startup_config,
        )
        self.session.add(node)
        self.nodes_created.append(node)
        self.geometry_nodes[node.id] = (
            {"x": at[0] * _GRID, "y": at[1] * _GRID}
            if at is not None
            else _grid_pos(row, col, row_wide)
        )
        return node

    async def add_interface(self, node: Node) -> Interface:
        # Count existing interfaces via a real query — never touch the
        # lazy `node.interfaces` relationship in async code (SQLAlchemy's
        # MissingGreenlet fires the moment you do). One SELECT per
        # interface add is fine for the sizes this generator hands out.
        from sqlalchemy import func as _func

        idx = (
            await self.session.execute(
                select(_func.count(Interface.id)).where(Interface.node_id == node.id)
            )
        ).scalar_one()
        scheme = iface_scheme_for(node.runtime, node.image)
        iface_id = new_id()
        iface = Interface(
            id=iface_id,
            node_id=node.id,
            idx=int(idx),
            name=guest_iface_name(scheme, int(idx)),
            mac=await allocate_mac(self.session, iface_id),
        )
        self.session.add(iface)
        await self.session.flush()
        return iface

    async def wire(self, a: Interface, b: Interface) -> Link:
        """Materialise the Network + Link pair that a p2p wire is
        made of, matching what create_link in routers/links.py does.

        Returns the Link so a caller can impair it — scale-across needs a
        inter-site hop that is slow, and slow in both directions separately.
        """
        net = Network(
            id=new_id(),
            lab_id=self.lab.id,
            name=f"lnk-{new_id()[-8:].lower()}",
            kind="bridge",
        )
        self.session.add(net)
        await self.session.flush()
        a.network_id = net.id
        b.network_id = net.id
        link = Link(
            id=new_id(),
            lab_id=self.lab.id,
            a_iface_id=a.id,
            b_iface_id=b.id,
            network_id=net.id,
            admin_up=True,
        )
        self.session.add(link)
        self.links_created.append(link)
        self.networks_created.append(net)
        return link

    async def flush(self) -> None:
        await self.session.flush()


# ---- FRR config helpers ---------------------------------------------


def _spine_frr_conf(idx: int, leaves: int) -> str:
    """FRR config for a spine in a spine-leaf EVPN underlay.

    Every spine gets AS 65000; every leaf gets a unique AS starting at
    65001. The spine peers unnumbered eBGP with every leaf across
    directly-attached interfaces (eth0..ethN-1 in the container's
    naming scheme). L2VPN EVPN address family carries the overlay.
    """
    router_id = f"10.0.0.{idx}"
    ifaces = "\n".join(
        f" neighbor eth{i} interface remote-as 6500{i + 1}"
        for i in range(leaves)
    )
    ifaces_evpn = "\n".join(
        f"  neighbor eth{i} activate" for i in range(leaves)
    )
    return f"""\
frr version 8.4
frr defaults datacenter
hostname spine-{idx}
!
interface lo
 ip address {router_id}/32
!
router bgp 65000
 bgp router-id {router_id}
 bgp bestpath as-path multipath-relax
{ifaces}
 !
{_IPV4_UNDERLAY}
 !
 address-family l2vpn evpn
{ifaces_evpn}
  advertise-all-vni
 exit-address-family
!
line vty
!
"""


def _rail_spine_frr_conf(rail: int, hosts: int) -> str:
    """FRR config for the spine of one rail in a rail-optimised fabric.

    Each rail is its own iBGP island — same AS across the rail so
    intra-rail path selection is straightforward. Router-id keyed on
    rail index so a multi-rail lab reads clearly. Hosts attach on
    eth0..eth(hosts-1); the spine peers unnumbered iBGP with each,
    ready to carry EVPN if the operator turns on advertise-all-vni
    later.
    """
    asn = 64700 + rail
    router_id = f"10.{rail}.0.1"
    ifaces = "\n".join(
        f" neighbor eth{i} interface remote-as {asn}" for i in range(hosts)
    )
    # A neighbour declared under `router bgp` exchanges nothing for an
    # address family until it is activated inside it. Without these the
    # EVPN block is inert and `show bgp l2vpn evpn summary` reports no
    # neighbours at all, which looks like a session that never came up.
    ifaces_evpn = "\n".join(f"  neighbor eth{i} activate" for i in range(hosts))
    return f"""\
frr version 8.4
frr defaults datacenter
hostname spine-r{rail}
!
interface lo
 ip address {router_id}/32
!
router bgp {asn}
 bgp router-id {router_id}
{ifaces}
 !
{_IPV4_UNDERLAY}
 !
 address-family l2vpn evpn
{ifaces_evpn}
  advertise-all-vni
 exit-address-family
!
line vty
!
"""


def _rail_host_frr_conf(rail: int, host: int) -> str:
    """FRR config for a host in a rail-optimised fabric — iBGP peer to
    its rail's spine, single uplink. The host still gets bgpd so it can
    advertise its own /32 into the rail; not strictly needed for a
    ping test but present so the researcher can inject routes without
    touching a second daemon."""
    asn = 64700 + rail
    router_id = f"10.{rail}.{host}.1"
    return f"""\
frr version 8.4
frr defaults datacenter
hostname h-r{rail}-{host}
!
interface lo
 ip address {router_id}/32
!
router bgp {asn}
 bgp router-id {router_id}
 neighbor eth0 interface remote-as {asn}
 !
{_IPV4_UNDERLAY}
 !
 address-family l2vpn evpn
  neighbor eth0 activate
 exit-address-family
!
line vty
!
"""


#: Default VNI for the generated overlay. One L2 segment spanning every edge
#: switch is the smallest thing that actually exercises EVPN: a host in pod 1
#: and a host in pod 22 land in the same broadcast domain, so reaching each
#: other requires MAC routes to have crossed the whole fabric.
_OVERLAY_VNI = 10
_OVERLAY_PREFIX = 16  # 10.100.0.0/16 across the fabric


def _fattree_edge_overlay(pod: int, idx: int, k: int, vni: int = _OVERLAY_VNI) -> str:
    """Kernel-side VXLAN and bridge setup for an edge switch.

    `advertise-all-vni` on its own advertises nothing. FRR learns VNIs from
    the kernel, so with no vxlan device in a bridge there is no VNI to
    advertise, and `show bgp l2vpn evpn summary` shows an empty table while
    every peer sits happily Established. That is exactly what this fabric
    did: 0 VNIs, 0 vxlan interfaces, 0 bridges, on all 242 edges.

    `nolearning` is the flag that matters — it stops the kernel learning and
    flooding MACs itself and leaves that to EVPN, which is the whole point.
    Host-facing ports go into the bridge; the agg uplinks must stay out, or
    the underlay gets bridged into the tenant segment.
    """
    half = k // 2
    local = f"10.{pod}.{100 + idx}.1"
    enslave = "\n".join(
        f"ip link set eth{i} master br{vni} && ip link set eth{i} up"
        for i in range(half, k)
    )
    return (
        f"# ---- EVPN overlay, VNI {vni} ----\n"
        "# No MTU change on the uplinks, deliberately. VXLAN adds 50 bytes, and\n"
        "# the hosts are set to 1450, so an encapsulated frame is at most 1500 —\n"
        "# which the default already carries. An earlier version raised the\n"
        "# uplinks to 1600 and was wrong twice over: unnecessary, because\n"
        "# 1450+50 fits; and applied to the edge's end only, leaving every\n"
        "# agg-edge link with 1600 on one side and 1500 on the other. Keep the\n"
        "# tenant MTU below the fabric MTU minus overhead and there is nothing\n"
        "# to configure here.\n"
        f"ip link add br{vni} type bridge 2>/dev/null || true\n"
        f"ip link set br{vni} up\n"
        f"ip link add vxlan{vni} type vxlan id {vni} dstport 4789 "
        f"local {local} nolearning 2>/dev/null || true\n"
        f"ip link set vxlan{vni} master br{vni}\n"
        f"ip link set vxlan{vni} up\n"
        f"{enslave}\n"
        "# EVPN already carries the MAC-IP bindings, so flooding ARP across the\n"
        "# overlay is pure waste where the kernel supports suppressing it.\n"
        f"bridge link set dev vxlan{vni} neigh_suppress on 2>/dev/null || true\n"
    )


def _fattree_host_setup(pod: int, edge: int, h: int, k: int,
                        vni: int = _OVERLAY_VNI) -> str:
    """A host in the overlay: one address in the tenant segment.

    Hosts were generated with no config at all, so 2662 of them had no
    address and nothing to send. An EVPN fabric with no tenants carries no
    MAC routes, which is how the overlay could look fine while being idle.
    """
    half = k // 2
    pos = (edge - 1) * half + h
    addr = f"10.100.{pod}.{pos}"
    return (
        "#!/bin/sh\n"
        "# Generated by `labtris lab generate --with-bgp-evpn`.\n"
        "set -e\n"
        "# 1450 + VXLAN's 50 = 1500, exactly the fabric MTU, so nothing on the\n"
        "# path has to be widened. Raise this and every uplink needs raising\n"
        "# too, on both ends of every link.\n"
        "ip link set eth0 mtu 1450 2>/dev/null || true\n"
        f"ip addr replace {addr}/{_OVERLAY_PREFIX} dev eth0\n"
        "ip link set eth0 up\n"
        f"echo \"host {addr}/{_OVERLAY_PREFIX} on VNI {vni}\"\n"
    )


#: Advertise the loopback into the underlay.
#:
#: Every tier had `interface lo` with a /32, BGP neighbours and an l2vpn evpn
#: block — and no `address-family ipv4 unicast` at all. Sessions still came up,
#: because `frr defaults datacenter` activates peers for ipv4 unicast, so a
#: 3267-node fabric reported 22 established peers per switch and looked
#: healthy while every one of them said:
#:
#:   BGP table version 0
#:   RIB entries 0, using 0 bytes of memory
#:   ... State/PfxRcd 0   PfxSnt 0
#:
#: Nothing was ever originated, so the underlay carried no routes at all and
#: `show ip route summary` was one connected route and nothing else.
#:
#: redistribute connected rather than a `network` statement: the loopback
#: address differs per node and is already in the config above, so there is no
#: second place to keep in step.
_IPV4_UNDERLAY = """\
 address-family ipv4 unicast
  redistribute connected
 exit-address-family"""


def _fattree_core_frr_conf(idx: int, k: int) -> str:
    """Core switch in a k-ary fat tree. AS 65000 across all cores;
    router-id 10.255.0.<idx>. Unnumbered eBGP to every aggregation
    switch it wires to (half*k ports, one per pod)."""
    router_id = f"10.255.0.{idx}"
    # Each core port lands in a different pod, and a pod's AS is
    # 65100+pod with pods numbered from 1 — so the peer on eth{i} is
    # 65101+i, not a flat 65100. Declaring one AS for all of them meant
    # the eBGP OPEN carried the wrong AS and no session ever came up,
    # which reads downstream as "No BGP neighbors found".
    ifaces = "\n".join(
        f" neighbor eth{i} interface remote-as {65101 + i}" for i in range(k)
    )
    ifaces_evpn = "\n".join(f"  neighbor eth{i} activate" for i in range(k))
    return f"""\
frr version 8.4
frr defaults datacenter
hostname core-{idx}
!
interface lo
 ip address {router_id}/32
!
router bgp 65000
 bgp router-id {router_id}
 bgp bestpath as-path multipath-relax
{ifaces}
 !
{_IPV4_UNDERLAY}
 !
 address-family l2vpn evpn
{ifaces_evpn}
  advertise-all-vni
 exit-address-family
!
line vty
!
"""


def _fattree_agg_frr_conf(pod: int, idx: int, k: int) -> str:
    """Aggregation switch in pod `pod`, position `idx` within pod. Pod
    AS is 65100+pod; peers upward to cores (half interfaces) and
    downward to edges (half interfaces)."""
    half = k // 2
    asn = 65100 + pod
    router_id = f"10.{pod}.{idx}.1"
    # Interface order follows the order links are created, and the
    # builder wires agg↔edge before agg↔core — so the low interfaces
    # face DOWN to the edges and the high ones face UP to the cores.
    # Having these the other way round pointed the core-facing port at
    # the pod's own AS, and the core's OPEN came back rejected with
    # "OPEN Message Error/Bad Peer AS": correct AS numbering on both
    # ends, wired to the wrong ports.
    down = "\n".join(
        f" neighbor eth{i} interface remote-as {asn}" for i in range(half)
    )
    up = "\n".join(
        f" neighbor eth{i} interface remote-as 65000" for i in range(half, k)
    )
    ifaces_evpn = "\n".join(f"  neighbor eth{i} activate" for i in range(k))
    return f"""\
frr version 8.4
frr defaults datacenter
hostname agg-{pod}-{idx}
!
interface lo
 ip address {router_id}/32
!
router bgp {asn}
 bgp router-id {router_id}
 bgp bestpath as-path multipath-relax
{up}
{down}
 !
{_IPV4_UNDERLAY}
 !
 address-family l2vpn evpn
{ifaces_evpn}
  advertise-all-vni
 exit-address-family
!
line vty
!
"""


def _fattree_edge_frr_conf(pod: int, idx: int, k: int) -> str:
    """Edge switch in pod `pod`, position `idx`. Same pod AS as
    aggregations; peers upward to aggs (half interfaces) and downward
    to k/2 hosts."""
    half = k // 2
    asn = 65100 + pod
    router_id = f"10.{pod}.{100 + idx}.1"
    up = "\n".join(f" neighbor eth{i} interface remote-as {asn}" for i in range(half))
    # Same omission the core and agg had: without an EVPN family and an
    # activate per uplink, the session comes up carrying ipv4 unicast
    # only and `show bgp l2vpn evpn summary` reports nothing — which
    # looks identical to a peer that never connected. Hosts hang off the
    # remaining ports and do not speak BGP, so only the agg uplinks are
    # activated.
    up_evpn = "\n".join(f"  neighbor eth{i} activate" for i in range(half))
    return f"""\
frr version 8.4
frr defaults datacenter
hostname edge-{pod}-{idx}
!
interface lo
 ip address {router_id}/32
!
router bgp {asn}
 bgp router-id {router_id}
{up}
 !
{_IPV4_UNDERLAY}
 !
 address-family l2vpn evpn
{up_evpn}
  advertise-all-vni
 exit-address-family
!
line vty
!
"""


def _leaf_frr_conf(idx: int, spines: int) -> str:
    """FRR config for a leaf in a spine-leaf EVPN underlay.

    Leaf N has AS 6500N, peers eBGP unnumbered to every spine on its
    first N interfaces (eth0..ethN-1 spine uplinks). Overlay VNIs are
    left to the operator — advertise-all-vni turns on discovery so any
    bridge with a VXLAN VNI configured on this leaf is auto-advertised.
    """
    asn = 65000 + idx
    router_id = f"10.0.0.{100 + idx}"
    ifaces = "\n".join(
        f" neighbor eth{i} interface remote-as 65000"
        for i in range(spines)
    )
    ifaces_evpn = "\n".join(
        f"  neighbor eth{i} activate" for i in range(spines)
    )
    return f"""\
frr version 8.4
frr defaults datacenter
hostname leaf-{idx}
!
interface lo
 ip address {router_id}/32
!
router bgp {asn}
 bgp router-id {router_id}
 bgp bestpath as-path multipath-relax
{ifaces}
 !
{_IPV4_UNDERLAY}
 !
 address-family l2vpn evpn
{ifaces_evpn}
  advertise-all-vni
 exit-address-family
!
line vty
!
"""


# ---- pattern generators ----------------------------------------------


async def _spine_leaf(b: _Builder, body: GenerateIn) -> None:
    """N spines × M leaves × K hosts-per-leaf.

    Rows on the canvas: spines top, leaves middle, hosts bottom.
    Every spine wires to every leaf (full CLOS); each host has one
    uplink to its leaf. Leaves multi-home hosts; a real fabric would
    also dual-home each host to two leaves — Phase E does not (needs
    multipath, deferred to Phase F).
    """
    spines: list[Node] = []
    leaves: list[Node] = []

    for i in range(body.spines):
        cfg = None
        if body.with_bgp_evpn and body.spine_kind == "frr":
            cfg = _frr_installer(_FRR_DAEMONS_EVPN, _spine_frr_conf(i + 1, body.leaves))
        n = await b.add_node(
            f"spine-{i+1}", body.spine_kind, row=0, col=i, row_wide=body.spines,
            opts={"p4_program": body.p4_program} if body.spine_kind == "bmv2" else None,
            startup_config=cfg,
        )
        spines.append(n)

    for j in range(body.leaves):
        cfg = None
        if body.with_bgp_evpn and body.leaf_kind == "frr":
            cfg = _frr_installer(_FRR_DAEMONS_EVPN, _leaf_frr_conf(j + 1, body.spines))
        n = await b.add_node(
            f"leaf-{j+1}", body.leaf_kind, row=1, col=j, row_wide=body.leaves,
            startup_config=cfg,
        )
        leaves.append(n)

    await b.flush()

    for s in spines:
        for l in leaves:
            a = await b.add_interface(s)
            c = await b.add_interface(l)
            await b.wire(a, c)

    if body.hosts_per_leaf:
        for j, leaf in enumerate(leaves):
            for h in range(body.hosts_per_leaf):
                host = await b.add_node(
                    f"h-{j+1}-{h+1}",
                    body.host_kind,
                    row=2,
                    col=j * body.hosts_per_leaf + h,
                    row_wide=body.leaves * body.hosts_per_leaf,
                )
                a = await b.add_interface(leaf)
                c = await b.add_interface(host)
                await b.wire(a, c)


async def _rail_optimised(b: _Builder, body: GenerateIn) -> None:
    """Rail-optimised: R rails × H hosts-per-rail, each rail on a
    dedicated spine. Hosts are only connected within their rail.

    Real GPU clusters group NICs by 'rail' (GPU index): NIC-0 on
    every host attaches to rail-0's spine, NIC-1 to rail-1's, and
    so on. Traffic within a rail can be all-to-all with no leaf
    hop; cross-rail traffic uses a separate (usually less-fat) fabric
    the generator does NOT emit — cross-rail is the case Ultra
    Ethernet's UET argues for improving, and modelling only one
    rail keeps this generator honest.
    """
    for r in range(body.rails):
        spine_cfg = (
            _frr_installer(_FRR_DAEMONS_EVPN, _rail_spine_frr_conf(r + 1, body.hosts_per_rail))
            if body.with_bgp_evpn and body.spine_kind == "frr" else None
        )
        spine = await b.add_node(
            f"spine-r{r+1}", body.spine_kind, row=0, col=r, row_wide=body.rails,
            opts={"p4_program": body.p4_program} if body.spine_kind == "bmv2" else None,
            startup_config=spine_cfg,
        )
        await b.flush()
        for h in range(body.hosts_per_rail):
            host_cfg = (
                _frr_installer(_FRR_DAEMONS_EVPN, _rail_host_frr_conf(r + 1, h + 1))
                if body.with_bgp_evpn and body.host_kind == "frr" else None
            )
            host = await b.add_node(
                f"h-r{r+1}-{h+1}",
                body.host_kind,
                row=1,
                col=r * body.hosts_per_rail + h,
                row_wide=body.rails * body.hosts_per_rail,
                startup_config=host_cfg,
            )
            a = await b.add_interface(spine)
            c = await b.add_interface(host)
            await b.wire(a, c)


async def _fat_tree(b: _Builder, body: GenerateIn) -> None:
    """k-ary fat tree: (k/2)^2 core, k*k/2 aggregation, k*k/2 edge,
    k^3/4 hosts. Textbook (Al-Fares 2008).

    k must be even, and the node count grows with k^3 — k=8 is 208 nodes,
    k=16 is 1344. The schema caps it at 16; the docstring used to say
    {2,4,6,8} "so the result fits on a laptop screen", which confused a
    rendering preference with a capability and left the largest fat-tree at
    208 nodes while spine-leaf already allowed 4192.
    """
    if body.k % 2:
        raise bad_request("fat-tree requires k to be even")
    k = body.k
    half = k // 2

    def _core_cfg(i: int) -> str | None:
        if body.with_bgp_evpn and body.spine_kind == "frr":
            return _frr_installer(_FRR_DAEMONS_EVPN, _fattree_core_frr_conf(i + 1, k))
        return None

    def _agg_cfg(pod: int, i: int) -> str | None:
        if body.with_bgp_evpn and body.leaf_kind == "frr":
            return _frr_installer(_FRR_DAEMONS_EVPN, _fattree_agg_frr_conf(pod + 1, i + 1, k))
        return None

    def _edge_cfg(pod: int, i: int) -> str | None:
        if body.with_bgp_evpn and body.leaf_kind == "frr":
            # The overlay runs BEFORE frrinit: zebra discovers VNIs from the
            # kernel at startup, so a vxlan device created afterwards is not
            # advertised until something restarts FRR again.
            return _frr_installer(
                _FRR_DAEMONS_EVPN,
                _fattree_edge_frr_conf(pod + 1, i + 1, k),
                pre=_fattree_edge_overlay(pod + 1, i + 1, k),
            )
        return None

    # ---- layout ------------------------------------------------------
    #
    # A fat tree used to be laid out as four centred rows, which at k=22
    # means 2662 hosts across one row: 532,400 px wide against 800 px tall,
    # a 665:1 aspect ratio. Fit that on a screen and every node is smaller
    # than a pixel, nothing can be clicked, and 7986 links cross everything.
    #
    # A fat tree is not four rows, it is k pods. So each pod is a block:
    # its k/2 aggregation switches on one line, its k/2 edge switches on the
    # next, and each edge's k/2 hosts stacked in the column directly beneath
    # it. Pods are tiled in a grid, with the cores wrapped into a band above
    # rather than stretched across the whole width.
    #
    # k=22 goes from 532,400 x 800 to roughly 12,000 x 14,800 — 44x narrower
    # and actually navigable. It also makes the 2662 edge-to-host links,
    # a third of every link in the lab, short vertical segments that cross
    # nothing, because a host sits under its own switch. The core-to-
    # aggregation crossings remain, because in a fat tree every aggregation
    # switch really does reach half the cores; that is the topology, not the
    # drawing.
    pod_cols = half                      # a pod is k/2 columns wide
    pod_rows = 2 + half                  # agg, edge, then k/2 host rows
    pods_across = max(1, math.ceil(math.sqrt(k)))
    span = pods_across * (pod_cols + 1)  # +1 column of air between pods
    core_rows = max(1, math.ceil((half * half) / span))

    def _pod_origin(pod: int) -> tuple[int, int]:
        pr, pc = divmod(pod, pods_across)
        return pc * (pod_cols + 1), core_rows + 1 + pr * (pod_rows + 1)

    cores = [
        await b.add_node(
            f"core-{i+1}", body.spine_kind, row=0, col=i, row_wide=half * half,
            opts={"p4_program": body.p4_program} if body.spine_kind == "bmv2" else None,
            startup_config=_core_cfg(i),
            at=(i % span, i // span),
        )
        for i in range(half * half)
    ]
    aggs_per_pod = half
    edges_per_pod = half
    aggs: list[list[Node]] = []
    edges: list[list[Node]] = []
    for pod in range(k):
        ox, oy = _pod_origin(pod)
        pod_aggs = [
            await b.add_node(
                f"agg-{pod+1}-{a+1}", body.leaf_kind, row=1,
                col=pod * aggs_per_pod + a, row_wide=k * aggs_per_pod,
                startup_config=_agg_cfg(pod, a),
                at=(ox + a, oy),
            )
            for a in range(aggs_per_pod)
        ]
        pod_edges = [
            await b.add_node(
                f"edge-{pod+1}-{e+1}", body.leaf_kind, row=2,
                col=pod * edges_per_pod + e, row_wide=k * edges_per_pod,
                startup_config=_edge_cfg(pod, e),
                at=(ox + e, oy + 1),
            )
            for e in range(edges_per_pod)
        ]
        aggs.append(pod_aggs)
        edges.append(pod_edges)
    await b.flush()

    # Aggregation ↔ edge within a pod (full mesh).
    for pod in range(k):
        for a_node in aggs[pod]:
            for e_node in edges[pod]:
                a = await b.add_interface(a_node)
                c = await b.add_interface(e_node)
                await b.wire(a, c)

    # Aggregation ↔ core (the Al-Fares wiring).
    for pod in range(k):
        for i, a_node in enumerate(aggs[pod]):
            for j in range(half):
                core_idx = i * half + j
                a = await b.add_interface(a_node)
                c = await b.add_interface(cores[core_idx])
                await b.wire(a, c)

    # Hosts under each edge: k/2 per edge.
    for pod in range(k):
        ox, oy = _pod_origin(pod)
        for e_i, e_node in enumerate(edges[pod]):
            for h in range(half):
                host = await b.add_node(
                    f"h-{pod+1}-{e_i+1}-{h+1}",
                    body.host_kind,
                    row=3,
                    col=(pod * edges_per_pod + e_i) * half + h,
                    row_wide=k * edges_per_pod * half,
                    # An EVPN fabric with no tenants carries no MAC routes,
                    # so the overlay looks healthy while being entirely idle.
                    startup_config=(
                        _fattree_host_setup(pod + 1, e_i + 1, h + 1, k)
                        if body.with_bgp_evpn
                        else None
                    ),
                    # Stacked straight down from its own edge switch, so this
                    # link is a short vertical line rather than a wire across
                    # the whole lab.
                    at=(ox + e_i, oy + 2 + h),
                )
                a = await b.add_interface(e_node)
                c = await b.add_interface(host)
                await b.wire(a, c)


# ---- top-level entry -------------------------------------------------


# ---- front-end -------------------------------------------------------
#
# The ordinary north-south network: users, storage, the internet, inference
# requests arriving. /ai-fabrics calls this tier real, and it was the one
# tier called real with no generator behind it — three of the four patterns
# here are scale-out shapes.


def _border_frr_conf(idx: int, peers: int, services: int) -> str:
    """A border router: eBGP to its pair, and a default handed south."""
    neighbours = "\n".join(
        f" neighbor eth{p} interface remote-as external"
        for p in range(peers)
    )
    return f"""frr defaults datacenter
hostname border-{idx}
!
router bgp 6500{idx}
 bgp router-id 10.255.0.{idx}
 bgp bestpath as-path multipath-relax
 no bgp ebgp-requires-policy
{neighbours}
 !
 address-family ipv4 unicast
  redistribute connected
  neighbor eth0 activate
 exit-address-family
!
"""


async def _front_end(b: _Builder, body: GenerateIn) -> None:
    """A redundant border pair, an access switch, and the services behind it.

    Deliberately small. The point of this tier is not scale — it is that the
    cluster has a front door, and that the front door is a real one you can
    curl through rather than a box on a diagram.
    """
    borders: list[Node] = []
    for i in range(2):
        cfg = None
        if body.leaf_kind == "frr":
            cfg = _frr_installer(
                _FRR_DAEMONS_EVPN, _border_frr_conf(i + 1, 1, body.services)
            )
        borders.append(
            await b.add_node(
                f"border-{i+1}", body.leaf_kind, row=0, col=i, row_wide=2,
                startup_config=cfg,
            )
        )

    access = await b.add_node("access-1", body.leaf_kind, row=1, col=0, row_wide=1)
    await b.flush()

    # Border pair to each other, so one can fail.
    a, c = await b.add_interface(borders[0]), await b.add_interface(borders[1])
    await b.wire(a, c)

    for border in borders:
        x = await b.add_interface(border)
        y = await b.add_interface(access)
        await b.wire(x, y)

    #: Named for what they are rather than host-1..n: the tier exists to
    #: carry storage reads and inference requests, and a lab that says so
    #: teaches more than one that does not.
    roles = ["storage", "inference-api", "registry", "monitoring", "jump"]
    for h in range(body.services):
        name = roles[h] if h < len(roles) else f"svc-{h+1}"
        host = await b.add_node(
            name, body.host_kind, row=2, col=h, row_wide=max(1, body.services)
        )
        await b.flush()
        hi = await b.add_interface(host)
        ai = await b.add_interface(access)
        await b.wire(hi, ai)

    if body.with_egress:
        # A NAT segment both borders sit on. Written with a subnet from the
        # start because the row's own CHECK constraint requires one.
        from labtris_api.nat import pick_subnet, pool_for

        subnet = await pick_subnet(b.session, None)
        gateway, first, last = pool_for(subnet)
        egress = Network(
            id=new_id(), lab_id=b.lab.id, name="egress", kind="nat",
            subnet=str(subnet), gateway=gateway,
            dhcp_first=first, dhcp_last=last,
        )
        b.session.add(egress)
        await b.session.flush()
        b.networks_created.append(egress)
        for border in borders:
            iface = await b.add_interface(border)
            iface.network_id = egress.id


# ---- scale-across ----------------------------------------------------
#
# Connectivity between data-centre clusters, so one workload can span sites.
# The interesting part is not the topology, which is two small fabrics — it
# is the hop between them, which is slow, and slow by different amounts each
# way.


def _site_border_frr_conf(site: int, sites: int, leaves: int) -> str:
    return f"""frr defaults datacenter
hostname site{site}-border
!
router bgp 6510{site}
 bgp router-id 10.254.0.{site}
 bgp bestpath as-path multipath-relax
 no bgp ebgp-requires-policy
 neighbor fabric peer-group
 neighbor fabric remote-as external
 neighbor dci peer-group
 neighbor dci remote-as external
 !
 address-family ipv4 unicast
  redistribute connected
  neighbor fabric activate
  neighbor dci activate
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor dci activate
  advertise-all-vni
 exit-address-family
!
"""


async def _scale_across(b: _Builder, body: GenerateIn) -> None:
    """N sites, each a small fabric, joined border-to-border.

    The DCI carries per-direction impairment because a real one does: the
    two directions take different paths through different queues, and a
    collective that assumes symmetry is exactly the thing this tier exists
    to let you break on purpose.
    """
    site_borders: list[Node] = []

    for site in range(body.sites):
        base_col = site * (body.leaves + 1)
        cfg = None
        if body.leaf_kind == "frr":
            cfg = _frr_installer(
                _FRR_DAEMONS_EVPN,
                _site_border_frr_conf(site + 1, body.sites, body.leaves),
            )
        border = await b.add_node(
            f"site{site+1}-border", body.leaf_kind,
            row=0, col=base_col, row_wide=body.sites * (body.leaves + 1),
            startup_config=cfg,
        )
        site_borders.append(border)

        leaves: list[Node] = []
        for j in range(body.leaves):
            leaf = await b.add_node(
                f"site{site+1}-leaf-{j+1}", body.leaf_kind,
                row=1, col=base_col + j,
                row_wide=body.sites * (body.leaves + 1),
            )
            leaves.append(leaf)
        await b.flush()

        for leaf in leaves:
            x = await b.add_interface(border)
            y = await b.add_interface(leaf)
            await b.wire(x, y)

        for j, leaf in enumerate(leaves):
            for h in range(body.hosts_per_leaf):
                host = await b.add_node(
                    f"site{site+1}-h-{j+1}-{h+1}", body.host_kind,
                    row=2, col=base_col + j * max(1, body.hosts_per_leaf) + h,
                    row_wide=body.sites * (body.leaves + 1)
                             * max(1, body.hosts_per_leaf),
                )
                await b.flush()
                hi = await b.add_interface(host)
                li = await b.add_interface(leaf)
                await b.wire(hi, li)

    # The DCI itself: a ring, so three or more sites stay connected when one
    # hop goes down rather than partitioning on the first failure.
    hops = body.sites if body.sites > 2 else 1
    for i in range(hops):
        a_node = site_borders[i]
        b_node = site_borders[(i + 1) % body.sites]
        x = await b.add_interface(a_node)
        y = await b.add_interface(b_node)
        link = await b.wire(x, y)
        link.impair_ab = {"delay_ms": body.dci_delay_ms}
        link.impair_ba = {"delay_ms": body.dci_return_delay_ms}


async def generate(session: AsyncSession, lab_id: str, body: GenerateIn) -> GenerateOut:
    lab = await get_lab(session, lab_id)
    b = _Builder(session, lab)

    if body.pattern == "spine-leaf":
        await _spine_leaf(b, body)
    elif body.pattern == "rail-optimised":
        await _rail_optimised(b, body)
    elif body.pattern == "fat-tree":
        await _fat_tree(b, body)
    elif body.pattern == "front-end":
        await _front_end(b, body)
    elif body.pattern == "scale-across":
        await _scale_across(b, body)
    else:  # pragma: no cover - Pydantic Literal blocks the else path
        raise bad_request(f"unknown pattern {body.pattern!r}")

    # Geometry — one row of gathered positions from _Builder.geometry_nodes.
    geom = await session.get(Geometry, lab.id)
    if geom is None:
        geom = Geometry(lab_id=lab.id, data={
            "nodes": b.geometry_nodes,
            "links": {},
            "view": {"x": 80, "y": 40, "k": 0.7},
        })
        session.add(geom)
    else:
        data = dict(geom.data or {})
        data["nodes"] = {**(data.get("nodes") or {}), **b.geometry_nodes}
        geom.data = data

    await session.commit()
    return GenerateOut(
        lab_id=lab.id,
        pattern=body.pattern,
        node_count=len(b.nodes_created),
        link_count=len(b.links_created),
        network_count=len(b.networks_created),
    )
