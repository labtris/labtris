"""A generated EVPN fabric has to advertise something.

Every config builder wrote `interface lo` with a /32, BGP neighbours and an
l2vpn evpn block, and none of them had `address-family ipv4 unicast`. The
sessions still came up — `frr defaults datacenter` activates peers for ipv4
unicast — so a 3267-node fat tree reported 22 established peers per switch and
looked healthy while every single one said:

    BGP table version 0
    RIB entries 0, using 0 bytes of memory
    ... State/PfxRcd 0   PfxSnt 0

Nothing was ever originated. `show ip route summary` was one connected route
and nothing else, on all 605 switches. Spine-leaf and rail-optimised had the
identical bug.
"""

from __future__ import annotations

import pytest

import labtris_api.topology_gen as tg

BUILDERS = [
    ("fattree-core", tg._fattree_core_frr_conf, (1, 22)),
    ("fattree-agg", tg._fattree_agg_frr_conf, (1, 1, 22)),
    ("fattree-edge", tg._fattree_edge_frr_conf, (1, 1, 22)),
    ("spine", tg._spine_frr_conf, (1, 4)),
    ("leaf", tg._leaf_frr_conf, (1, 2)),
    ("rail-spine", tg._rail_spine_frr_conf, (1, 4)),
    ("rail-host", tg._rail_host_frr_conf, (1, 1)),
    # The north-south and inter-site roles. These had peer-groups with no
    # members and no loopback until 2026-10: a config that parsed, peered
    # with nobody and advertised nothing.
    ("front-end-border", tg._border_frr_conf, (1, 1, 3)),
    ("site-border", tg._site_border_frr_conf, (1, 2, 2, 1)),
    ("uplink-leaf", tg._uplink_leaf_frr_conf, ("site1-leaf-1", 65120, "10.254.1.1")),
    ("transit", tg._transit_frr_conf, (1, 4)),
    ("region-border", tg._region_border_frr_conf, (1, 2, 2)),
    ("edge-core", tg._edge_core_frr_conf, (4,)),
    ("edge-site", tg._edge_site_frr_conf, (1,)),
]


def test_peer_groups_have_members() -> None:
    """A `neighbor X peer-group` with no `neighbor ethN interface peer-group X`
    is the bug the site border shipped with. Every builder that declares a
    group must put at least one interface in it."""
    import re

    for label, fn, args in BUILDERS:
        out = fn(*args)
        groups = re.findall(r"^ neighbor (\S+) peer-group$", out, re.M)
        for g in groups:  # noqa: PLW2901
            assert re.search(rf"^ neighbor eth\d+ interface peer-group {g}$", out, re.M), (
                f"{label}: peer-group {g} has no members"
            )


def test_scale_above_interface_order_matches_wiring() -> None:
    """region border: transits first, then leaves; transit: mesh then regions."""
    out = tg._region_border_frr_conf(1, transits=2, leaves=3)
    assert " neighbor eth0 interface peer-group transit" in out
    assert " neighbor eth1 interface peer-group transit" in out
    assert " neighbor eth2 interface peer-group fabric" in out
    assert " neighbor eth4 interface peer-group fabric" in out
    assert " neighbor eth5 " not in out
    out = tg._transit_frr_conf(1, ports=1 + 3)  # 2 transits, 3 regions
    assert " neighbor eth3 interface peer-group peers" in out
    assert " neighbor eth4 " not in out


def test_scale_edge_core_skips_the_egress_port() -> None:
    out = tg._edge_core_frr_conf(2)
    assert " neighbor eth0 interface peer-group fabric" in out
    assert " neighbor eth1 interface peer-group edges" in out
    assert " neighbor eth2 interface peer-group edges" in out
    assert " neighbor eth3 " not in out


@pytest.mark.parametrize(("label", "fn", "args"), BUILDERS, ids=[b[0] for b in BUILDERS])
def test_every_builder_originates_its_loopback(label, fn, args) -> None:  # noqa: ANN001
    out = fn(*args)

    assert "address-family ipv4 unicast" in out, f"{label} has no ipv4 unicast family"
    assert "redistribute connected" in out, f"{label} advertises nothing"
    assert "exit-address-family" in out


@pytest.mark.parametrize(("label", "fn", "args"), BUILDERS, ids=[b[0] for b in BUILDERS])
def test_the_loopback_it_advertises_actually_exists(label, fn, args) -> None:  # noqa: ANN001
    """redistribute connected is only useful if there is a connected route to
    redistribute — the /32 on lo is what gets carried."""
    out = fn(*args)

    assert "interface lo" in out, label
    assert "/32" in out, label


@pytest.mark.parametrize(("label", "fn", "args"), BUILDERS, ids=[b[0] for b in BUILDERS])
def test_the_address_families_are_closed_in_order(label, fn, args) -> None:  # noqa: ANN001
    """An unclosed family swallows the block after it, so the evpn section
    would silently become part of ipv4 unicast."""
    out = fn(*args)
    opens = [i for i, l in enumerate(out.splitlines()) if l.strip().startswith("address-family")]
    closes = [i for i, l in enumerate(out.splitlines()) if l.strip() == "exit-address-family"]

    assert len(opens) == len(closes), f"{label}: {len(opens)} opened, {len(closes)} closed"
    for o, c in zip(opens, closes, strict=True):
        assert c > o, f"{label}: a family closes before it opens"


def test_ipv4_unicast_comes_before_the_evpn_family() -> None:
    """Not required by FRR, but the underlay has to be up before EVPN routes
    have anything to resolve their next hops against — and keeping the order
    consistent is what makes the generated configs diffable."""
    out = tg._fattree_core_frr_conf(1, 22)

    assert out.index("address-family ipv4 unicast") < out.index("address-family l2vpn evpn")
