"""The netplan rendering step for the management-network verb.

The full `configure_management` writes to /etc/netplan and shells out to
`netplan apply`; that side of it is exercised in an acceptance test where a
real writable /etc is available. This file covers only the pure rendering:
the yaml goes on disk verbatim, so any mistake here becomes a persistent
config bug rather than an in-memory one.
"""

from __future__ import annotations

import pytest
import yaml

from labtris_netd.net import _render_management_netplan


def _parse(text: str) -> dict:
    """Round-trip through yaml so a test asserts against a shape rather
    than exact whitespace."""
    return yaml.safe_load(text)


def test_dhcp_renders_a_minimal_netplan() -> None:
    out = _render_management_netplan("dhcp", "ens160", None, None, [])
    doc = _parse(out)
    assert doc["network"]["version"] == 2
    eth = doc["network"]["ethernets"]["ens160"]
    assert eth == {"dhcp4": True, "dhcp6": False}


def test_manual_carries_address_gateway_dns() -> None:
    out = _render_management_netplan(
        "manual", "ens160", "10.0.0.5/24", "10.0.0.1", ["8.8.8.8", "1.1.1.1"]
    )
    doc = _parse(out)
    eth = doc["network"]["ethernets"]["ens160"]
    assert eth["dhcp4"] is False
    assert eth["addresses"] == ["10.0.0.5/24"]
    assert eth["routes"] == [{"to": "default", "via": "10.0.0.1"}]
    assert eth["nameservers"] == {"addresses": ["8.8.8.8", "1.1.1.1"]}


def test_manual_without_dns_omits_the_nameservers_key() -> None:
    """An empty DNS list should not render an empty `addresses: []`, which
    netplan treats as "clear DNS" — different behaviour."""
    out = _render_management_netplan("manual", "ens160", "10.0.0.5/24", "10.0.0.1", [])
    doc = _parse(out)
    assert "nameservers" not in doc["network"]["ethernets"]["ens160"]


def test_manual_without_address_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires address and gateway"):
        _render_management_netplan("manual", "ens160", None, "10.0.0.1", [])


def test_manual_without_gateway_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires address and gateway"):
        _render_management_netplan("manual", "ens160", "10.0.0.5/24", None, [])


def test_interface_name_flows_through() -> None:
    """Whatever interface the caller names is what the yaml holds — the
    validator that says "ens160 is a real NIC" lives one layer up."""
    out = _render_management_netplan("dhcp", "enp3s0", None, None, [])
    doc = _parse(out)
    assert "enp3s0" in doc["network"]["ethernets"]


def test_generated_yaml_is_valid_netplan_v2_shape() -> None:
    """Cheap structural gate — a v2 netplan needs `version: 2` under
    `network`, and interfaces under one of the known top-level kinds."""
    out = _render_management_netplan("manual", "ens160", "10.0.0.5/24", "10.0.0.1", [])
    doc = _parse(out)
    assert set(doc.keys()) == {"network"}
    net = doc["network"]
    assert net["version"] == 2
    assert set(net.keys()) - {"version", "ethernets"} == set()
