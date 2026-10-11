"""The two newest patterns wire what their FRR configs say they wire.

Each border config names its peers by interface index, in the order the
generator is supposed to add them. If the generator adds them in another
order the config still parses, FRR still starts, and the lab peers with
the wrong port — or nothing. So the test checks the order against the
config, not just the node count.
"""

from __future__ import annotations

from labtris_api import topology_gen as tg


class _Iface:
    def __init__(self, node: _Node, idx: int) -> None:
        self.node = node
        self.idx = idx
        self.network_id = None


class _Node:
    def __init__(self, name: str, kind: str, startup_config: str | None) -> None:
        self.id = name
        self.name = name
        self.kind = kind
        self.startup_config = startup_config
        self.ifaces: list[_Iface] = []


class _Link:
    def __init__(self, a: _Iface, b: _Iface) -> None:
        self.a, self.b = a, b
        self.impair_ab: dict | None = None
        self.impair_ba: dict | None = None


class _Builder:
    def __init__(self) -> None:
        self.nodes: dict[str, _Node] = {}
        self.links: list[_Link] = []
        self.networks_created: list = []
        self.geometry_nodes: dict = {}
        self.lab = type("Lab", (), {"id": "lab"})()
        self.session = None

    async def add_node(self, name, kind, row, col, row_wide, opts=None,
                       startup_config=None, at=None):  # noqa: ANN001, ANN201
        n = _Node(name, kind, startup_config)
        self.nodes[name] = n
        self.geometry_nodes[name] = tg._grid_pos(row, col, row_wide)  # noqa: SLF001
        return n

    async def add_interface(self, node):  # noqa: ANN001, ANN201
        i = _Iface(node, len(node.ifaces))
        node.ifaces.append(i)
        return i

    async def wire(self, a, b):  # noqa: ANN001, ANN201
        link = _Link(a, b)
        self.links.append(link)
        return link

    async def flush(self):  # noqa: ANN201
        return None

    # helpers
    def peer_of(self, node: str, idx: int) -> str:
        for lk in self.links:
            if lk.a.node.name == node and lk.a.idx == idx:
                return lk.b.node.name
            if lk.b.node.name == node and lk.b.idx == idx:
                return lk.a.node.name
        raise AssertionError(f"{node} eth{idx} is not wired")

    def link_between(self, x: str, y: str) -> _Link:
        for lk in self.links:
            names = {lk.a.node.name, lk.b.node.name}
            if names == {x, y}:
                return lk
        raise AssertionError(f"no link {x}–{y}")


async def test_scale_above_regions_reach_every_transit_with_distinct_delays() -> None:
    b = _Builder()
    body = tg.GenerateIn(pattern="scale-above", regions=3, transits=2, leaves=2,
                         hosts_per_leaf=1, leaf_kind="frr")
    await tg._scale_above(b, body)  # noqa: SLF001

    assert len(b.nodes) == 2 + 3 * (1 + 2 + 2)
    # Transit mesh: one link for two transits, and it is the first port on both.
    assert b.peer_of("transit-1", 0) == "transit-2"
    # Region border: eth0, eth1 go to the transits, eth2.. to the leaves —
    # exactly what _region_border_frr_conf(1, 2, 2) names.
    assert b.peer_of("region1-border", 0) == "transit-1"
    assert b.peer_of("region1-border", 1) == "transit-2"
    assert b.peer_of("region1-border", 2) == "region1-leaf-1"
    assert b.peer_of("region1-border", 3) == "region1-leaf-2"
    cfg = b.nodes["region1-border"].startup_config
    assert "neighbor eth1 interface peer-group transit" in cfg
    assert "neighbor eth3 interface peer-group fabric" in cfg
    # The leaf's uplink is its eth0, which its config peers on.
    assert b.peer_of("region2-leaf-1", 0) == "region2-border"
    assert "neighbor eth0 interface remote-as external" in b.nodes["region2-leaf-1"].startup_config
    # Delays: transit-2 is one step further than transit-1, and the return
    # leg is one step more than the outbound on each.
    l1 = b.link_between("region1-border", "transit-1")
    l2 = b.link_between("region1-border", "transit-2")
    assert l1.impair_ab == {"delay_ms": 25} and l1.impair_ba == {"delay_ms": 33}
    assert l2.impair_ab == {"delay_ms": 33} and l2.impair_ba == {"delay_ms": 41}
    # Transit ports: mesh first, then one per region, all peers.
    t1 = b.nodes["transit-1"]
    assert len(t1.ifaces) == 1 + 3
    assert "neighbor eth3 interface peer-group peers" in t1.startup_config
    assert "neighbor eth4 " not in t1.startup_config


async def test_scale_edge_hands_out_a_different_last_mile_per_edge() -> None:
    from labtris_api.impair import PRESETS

    b = _Builder()
    body = tg.GenerateIn(pattern="scale-edge", edges=5, hosts_per_edge=1,
                         services=2, leaf_kind="frr", with_egress=False)
    await tg._scale_edge(b, body)  # noqa: SLF001

    assert b.peer_of("core-border", 0) == "core-access"
    for e in range(5):
        assert b.peer_of("core-border", e + 1) == f"edge-{e+1}"
        assert b.peer_of(f"edge-{e+1}", 0) == "core-border"
    presets = [b.link_between("core-border", f"edge-{e+1}").impair_ab for e in range(5)]
    assert presets[:4] == [PRESETS[p] for p in ("satellite", "3g", "wifi", "lossy-wan")]
    assert presets[4] == PRESETS["satellite"], "the cycle wraps"
    assert b.nodes["inference-api"].kind == "alpine"
    assert b.peer_of("inference-api", 0) == "core-access"
    cfg = b.nodes["core-border"].startup_config
    assert "neighbor eth5 interface peer-group edges" in cfg
    assert "neighbor eth6 " not in cfg


async def test_scale_edge_single_preset_and_rejects_unknown() -> None:
    import pytest

    b = _Builder()
    body = tg.GenerateIn(pattern="scale-edge", edges=2, edge_preset="3g",
                         leaf_kind="frr", with_egress=False, services=0)
    await tg._scale_edge(b, body)  # noqa: SLF001
    from labtris_api.impair import PRESETS
    assert b.link_between("core-border", "edge-2").impair_ba == PRESETS["3g"]

    body = tg.GenerateIn(pattern="scale-edge", edge_preset="fibre-to-the-moon",
                         leaf_kind="frr", with_egress=False)
    with pytest.raises(Exception, match="unknown edge_preset"):
        await tg._scale_edge(_Builder(), body)  # noqa: SLF001


async def test_scale_across_border_names_the_ports_it_has() -> None:
    """The border config declared fabric and dci peer-groups with no members.
    Now it names leaves eth0.. and the DCI after them, in wiring order."""
    b = _Builder()
    body = tg.GenerateIn(pattern="scale-across", sites=3, leaves=2, hosts_per_leaf=0,
                         leaf_kind="frr")
    await tg._scale_across(b, body)  # noqa: SLF001

    assert b.peer_of("site1-border", 0) == "site1-leaf-1"
    assert b.peer_of("site1-border", 1) == "site1-leaf-2"
    # three sites → a ring → two DCI ports per border
    assert b.peer_of("site1-border", 2) in {"site2-border", "site3-border"}
    assert b.peer_of("site1-border", 3) in {"site2-border", "site3-border"}
    cfg = b.nodes["site1-border"].startup_config
    assert "neighbor eth1 interface peer-group fabric" in cfg
    assert "neighbor eth2 interface peer-group dci" in cfg
    assert "neighbor eth3 interface peer-group dci" in cfg
    assert "neighbor eth4 " not in cfg
    assert b.nodes["site2-leaf-1"].startup_config is not None
