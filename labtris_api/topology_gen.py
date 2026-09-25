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

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.errors import bad_request
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
}


def _resolve_kind(kind: str) -> tuple[str, str]:
    if kind not in _KINDS:
        raise bad_request(
            f"unknown node kind {kind!r} — try {', '.join(sorted(_KINDS))}"
        )
    return _KINDS[kind]


# ---- request schema --------------------------------------------------


class GenerateIn(BaseModel):
    pattern: Literal["spine-leaf", "rail-optimised", "fat-tree"]
    #: Sizes — meaning depends on pattern.
    spines: int = Field(2, ge=1, le=32)
    leaves: int = Field(4, ge=1, le=64)
    hosts_per_leaf: int = Field(2, ge=0, le=64)
    #: rail-optimised only
    rails: int = Field(4, ge=1, le=32)
    hosts_per_rail: int = Field(2, ge=0, le=32)
    #: fat-tree only. k must be even; the standard formula is k^3/4
    #: hosts, 5k^2/4 switches. Kept modest for a browser-usable lab.
    k: int = Field(4, ge=2, le=8)
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
    ) -> Node:
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
        self.geometry_nodes[node.id] = _grid_pos(row, col, row_wide)
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

    async def wire(self, a: Interface, b: Interface) -> None:
        """Materialise the Network + Link pair that a p2p wire is
        made of, matching what create_link in routers/links.py does."""
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

    async def flush(self) -> None:
        await self.session.flush()


# ---- FRR config helpers ---------------------------------------------


def _frr_installer(daemons: str, frr_conf: str) -> str:
    """Wrap the FRR daemons + frr.conf as a self-installing shell script.

    push_config already writes startup_config to /config/startup-config
    inside the container; running `sh /config/startup-config` there
    then applies the config in place. Two-step from the user:

        labtris node push <id>
        labtris node exec <id> -- sh /config/startup-config

    Kept as one file rather than two writes because the container has
    exactly one startup_config slot today. When per-node bind mounts on
    the FRR profile land (Phase F5+), this becomes native — the
    generator writes straight to `/etc/frr/` and reload is automatic.
    """
    return f"""#!/bin/sh
# Generated by `labtris lab generate --with-bgp-evpn`.
# Push with: labtris node push <this-node>
# Apply with: labtris node exec <this-node> -- sh /config/startup-config
set -e
mkdir -p /etc/frr
cat > /etc/frr/daemons <<'EOF_DAEMONS'
{daemons}
EOF_DAEMONS
cat > /etc/frr/frr.conf <<'EOF_FRR'
{frr_conf}
EOF_FRR
chown -R frr:frr /etc/frr 2>/dev/null || true
# Restart rather than reload, and unconditionally. The frrouting/frr image
# runs `frrinit.sh start` as its entrypoint, so by the time this script
# lands zebra is already up — started from a daemons file that still said
# bgpd=no. Against that half-running FRR `start` is a no-op and bgpd never
# launches, which looks like a fabric that generates correct config and
# then never converges. There is no watchfrr in this image to HUP either.
# Only restart re-reads the daemons file.
/usr/lib/frr/frrinit.sh restart
"""


_FRR_DAEMONS_EVPN = """\
bgpd=yes
ospfd=no
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
pathd=no
zebra_options=  "  -A 127.0.0.1 -s 90000000"
bgpd_options=   "  -A 127.0.0.1"
"""


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
 address-family l2vpn evpn
  neighbor eth0 activate
 exit-address-family
!
line vty
!
"""


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

    k must be even. Kept to k in {2,4,6,8} in this generator so the
    result fits on a laptop screen — k=8 already produces 80 nodes.
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
            return _frr_installer(_FRR_DAEMONS_EVPN, _fattree_edge_frr_conf(pod + 1, i + 1, k))
        return None

    cores = [
        await b.add_node(
            f"core-{i+1}", body.spine_kind, row=0, col=i, row_wide=half * half,
            opts={"p4_program": body.p4_program} if body.spine_kind == "bmv2" else None,
            startup_config=_core_cfg(i),
        )
        for i in range(half * half)
    ]
    aggs_per_pod = half
    edges_per_pod = half
    aggs: list[list[Node]] = []
    edges: list[list[Node]] = []
    for pod in range(k):
        pod_aggs = [
            await b.add_node(
                f"agg-{pod+1}-{a+1}", body.leaf_kind, row=1,
                col=pod * aggs_per_pod + a, row_wide=k * aggs_per_pod,
                startup_config=_agg_cfg(pod, a),
            )
            for a in range(aggs_per_pod)
        ]
        pod_edges = [
            await b.add_node(
                f"edge-{pod+1}-{e+1}", body.leaf_kind, row=2,
                col=pod * edges_per_pod + e, row_wide=k * edges_per_pod,
                startup_config=_edge_cfg(pod, e),
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
        for e_i, e_node in enumerate(edges[pod]):
            for h in range(half):
                host = await b.add_node(
                    f"h-{pod+1}-{e_i+1}-{h+1}",
                    body.host_kind,
                    row=3,
                    col=(pod * edges_per_pod + e_i) * half + h,
                    row_wide=k * edges_per_pod * half,
                )
                a = await b.add_interface(e_node)
                c = await b.add_interface(host)
                await b.wire(a, c)


# ---- top-level entry -------------------------------------------------


async def generate(session: AsyncSession, lab_id: str, body: GenerateIn) -> GenerateOut:
    lab = await get_lab(session, lab_id)
    b = _Builder(session, lab)

    if body.pattern == "spine-leaf":
        await _spine_leaf(b, body)
    elif body.pattern == "rail-optimised":
        await _rail_optimised(b, body)
    elif body.pattern == "fat-tree":
        await _fat_tree(b, body)
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
