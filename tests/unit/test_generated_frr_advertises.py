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
]


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
