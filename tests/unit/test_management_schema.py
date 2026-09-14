"""ManagementNetworkIn's validators — the last line before a malformed
address reaches netd.

Netd repeats the checks, so a bad payload doesn't touch /etc/netplan even
if this layer is bypassed. Testing them here catches the errors that the
API layer will surface with a helpful message rather than netd's terse
`EINVAL` string.
"""

from __future__ import annotations

import pytest

from labtris_api.schemas import ManagementNetworkIn


def test_dhcp_needs_nothing_else() -> None:
    m = ManagementNetworkIn(mode="dhcp")
    assert m.mode == "dhcp"
    assert m.interface == "ens160"  # default
    assert m.address is None
    assert m.dns == []


def test_manual_requires_address_and_gateway() -> None:
    with pytest.raises(ValueError, match="requires address and gateway"):
        ManagementNetworkIn(mode="manual")


def test_manual_accepts_valid_ipv4() -> None:
    m = ManagementNetworkIn(
        mode="manual",
        interface="ens160",
        address="10.0.0.5/24",
        gateway="10.0.0.1",
        dns=["8.8.8.8", "1.1.1.1"],
    )
    assert m.address == "10.0.0.5/24"
    assert m.dns == ["8.8.8.8", "1.1.1.1"]


def test_gateway_outside_subnet_is_refused() -> None:
    # A gateway that isn't in the address's subnet gives an unreachable
    # route on the box, which is worse than a rejected form.
    with pytest.raises(ValueError, match="not inside"):
        ManagementNetworkIn(
            mode="manual",
            address="10.0.0.5/24",
            gateway="192.168.1.1",  # different network
        )


def test_malformed_address_refused() -> None:
    with pytest.raises(ValueError, match="malformed address"):
        ManagementNetworkIn(mode="manual", address="not-an-address", gateway="10.0.0.1")


def test_malformed_gateway_refused() -> None:
    with pytest.raises(ValueError, match="malformed gateway"):
        ManagementNetworkIn(mode="manual", address="10.0.0.5/24", gateway="also-not")


def test_malformed_dns_refused() -> None:
    with pytest.raises(ValueError, match="malformed DNS"):
        ManagementNetworkIn(
            mode="manual",
            address="10.0.0.5/24",
            gateway="10.0.0.1",
            dns=["8.8.8.8", "not-an-ip"],
        )


def test_unknown_mode_refused() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        ManagementNetworkIn(mode="static")  # not one of dhcp/manual
