"""The EVPN overlay has to exist in the kernel, or advertise-all-vni is a no-op.

FRR learns VNIs from the kernel. With no vxlan device in a bridge there is no
VNI to advertise, so `show bgp l2vpn evpn summary` reports an empty table while
every peer sits Established — indistinguishable, from the summary, from a
working overlay. The 3267-node fabric ran that way: 0 VNIs, 0 vxlan
interfaces, 0 bridges, on all 242 edges.
"""

from __future__ import annotations

import re
import subprocess

import labtris_api.topology_gen as tg


def test_the_edge_creates_a_vxlan_device_in_a_bridge() -> None:
    out = tg._fattree_edge_overlay(3, 5, 22)

    assert "ip link add br10 type bridge" in out
    assert re.search(r"ip link add vxlan10 type vxlan id 10 dstport 4789", out)
    assert "ip link set vxlan10 master br10" in out


def test_the_vxlan_source_is_the_edge_s_own_loopback() -> None:
    """VXLAN needs a reachable source address, and the loopback is the only
    thing the underlay advertises."""
    out = tg._fattree_edge_overlay(3, 5, 22)

    assert "local 10.3.105.1" in out, out
    assert tg._fattree_edge_frr_conf(3, 5, 22).count("10.3.105.1") >= 1


def test_nolearning_is_set_so_evpn_owns_mac_learning() -> None:
    """Without it the kernel floods and learns MACs itself, which defeats the
    entire purpose of running EVPN."""
    assert "nolearning" in tg._fattree_edge_overlay(1, 1, 22)


def test_only_host_facing_ports_join_the_bridge() -> None:
    """Bridging an agg uplink would put the underlay into the tenant segment."""
    k = 22
    half = k // 2
    out = tg._fattree_edge_overlay(1, 1, k)
    enslaved = {int(m) for m in re.findall(r"ip link set eth(\d+) master br10", out)}

    assert enslaved == set(range(half, k)), enslaved
    for up in range(half):
        assert f"ip link set eth{up} master" not in out


def test_the_uplinks_are_left_alone_and_the_budget_still_works() -> None:
    """An earlier version raised the uplinks to 1600 and was wrong twice:
    unnecessary, because 1450 + VXLAN's 50 is exactly 1500 and the default
    already carries that; and applied to the edge's end only, leaving every
    agg-edge link with 1600 one side and 1500 the other.
    """
    out = tg._fattree_edge_overlay(1, 1, 22)

    assert "mtu 1600" not in out, "one-sided MTU change is back"
    assert "mtu" not in out.replace("# ", ""), "the overlay should set no MTU at all"

    host = tg._fattree_host_setup(1, 1, 1, 22)
    tenant = int(re.search(r"mtu (\d+)", host).group(1))
    assert tenant + 50 <= 1500, f"{tenant} + 50 exceeds the fabric MTU"


def test_the_host_gets_an_address_and_a_reduced_mtu() -> None:
    out = tg._fattree_host_setup(7, 2, 3, 22)

    assert "ip addr replace 10.100.7." in out
    assert "/16 dev eth0" in out
    assert "mtu 1450" in out


def test_host_addresses_are_unique_within_a_pod() -> None:
    k, half = 22, 11
    seen = set()
    for edge in range(1, half + 1):
        for h in range(1, half + 1):
            out = tg._fattree_host_setup(1, edge, h, k)
            addr = re.search(r"ip addr replace (\S+)/", out).group(1)
            assert addr not in seen, f"{addr} duplicated at edge {edge} host {h}"
            seen.add(addr)
    assert len(seen) == half * half


def test_both_scripts_are_valid_shell() -> None:
    for label, body in (
        ("edge overlay", "set -e\n" + tg._fattree_edge_overlay(1, 1, 22)),
        ("host setup", tg._fattree_host_setup(1, 1, 1, 22)),
    ):
        r = subprocess.run(["sh", "-n"], input=body, capture_output=True, text=True)
        assert r.returncode == 0, f"{label}: {r.stderr}"


def test_the_overlay_runs_before_frr_starts() -> None:
    """zebra discovers VNIs at startup, so a vxlan device created after
    frrinit is not advertised until something restarts FRR again."""
    script = tg._frr_installer(tg._FRR_DAEMONS_EVPN, "hostname x",
                               pre=tg._fattree_edge_overlay(1, 1, 22))

    assert script.index("ip link add vxlan10") < script.index("frrinit.sh restart")


def test_the_installer_without_a_pre_section_is_unchanged() -> None:
    """Cores and aggs pass no overlay and must not gain a stray blank step."""
    script = tg._frr_installer(tg._FRR_DAEMONS_EVPN, "hostname core-1")

    assert "vxlan" not in script
    r = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
