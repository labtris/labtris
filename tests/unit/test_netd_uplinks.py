"""Uplink-bridges netplan render — the pure step of `configure_uplinks`.

The write-and-apply half lives in `_apply_netplan_write`, which is shared
with the management-network path and is covered by acceptance-level
verification on a real host. This file covers the yaml shape, which is
the part that becomes a persistent config bug if it's wrong.
"""

from __future__ import annotations

import pytest
import yaml

from labtris_netd.net import _render_uplink_bridges_netplan


def _parse(text: str) -> dict:
    return yaml.safe_load(text)


def test_empty_list_produces_an_inert_but_valid_file() -> None:
    """Zero uplinks is legal — it's how an admin removes the last one. The
    file must still be valid netplan, so `netplan apply` doesn't refuse
    the whole system config."""
    out = _render_uplink_bridges_netplan([])
    doc = _parse(out)
    assert doc["network"]["version"] == 2
    # No `ethernets` or `bridges` keys — the file is a shell.
    assert set(doc["network"].keys()) == {"version"}


def test_one_pair_renders_ethernet_and_bridge_blocks() -> None:
    out = _render_uplink_bridges_netplan([("ens192", "br1")])
    doc = _parse(out)
    net = doc["network"]
    assert net["ethernets"]["ens192"] == {"dhcp4": False, "dhcp6": False}
    br = net["bridges"]["br1"]
    assert br["interfaces"] == ["ens192"]
    assert br["dhcp4"] is False
    assert br["dhcp6"] is False
    assert br["parameters"] == {"stp": False, "forward-delay": 0}


def test_two_pairs_render_both_and_only_their_bridges() -> None:
    out = _render_uplink_bridges_netplan([("ens192", "br1"), ("ens224", "br2")])
    doc = _parse(out)
    assert set(doc["network"]["ethernets"].keys()) == {"ens192", "ens224"}
    assert set(doc["network"]["bridges"].keys()) == {"br1", "br2"}
    # Each bridge holds only its own NIC — no cross-wiring.
    assert doc["network"]["bridges"]["br1"]["interfaces"] == ["ens192"]
    assert doc["network"]["bridges"]["br2"]["interfaces"] == ["ens224"]


def test_duplicate_nic_is_refused() -> None:
    """A NIC can only be enslaved to one bridge. Two entries pointing at
    the same NIC is a mistake, not a shape the renderer should accept
    silently."""
    with pytest.raises(ValueError, match="more than once"):
        _render_uplink_bridges_netplan([("ens192", "br1"), ("ens192", "br2")])


def test_bridges_are_unaddressed() -> None:
    """Uplink bridges do not carry an IP — the management pane's job is
    to put one on a bridge if the admin wants that, and it uses its own
    netplan file."""
    out = _render_uplink_bridges_netplan([("ens192", "br1")])
    doc = _parse(out)
    br = doc["network"]["bridges"]["br1"]
    assert "addresses" not in br
    assert "routes" not in br
