"""Validation of the NAT and VLAN verbs.

netd is the only process on the box that touches netlink and the firewall, and
it runs as root. Every one of these parameters ends up in an nftables rule, a
dnsmasq argument list, or a filename under /run — so the interesting tests are
the ones that check what it refuses, not what it accepts.
"""

from __future__ import annotations

from typing import Any

from labtris_netd.verbs import dispatch


class RecordingNet:
    """Accepts every call and records it. Anything that reaches here has
    already passed validation, which is the thing under test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):  # noqa: ANN202
        def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, {"args": args, "kwargs": kwargs}))
            return {}

        return record


def _call(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    return dispatch({"id": 1, "verb": verb, "params": params}, RecordingNet())


def test_a_subnet_that_is_not_a_subnet_is_refused() -> None:
    """This string is concatenated into an nft rule. "10.0.0.0/24; drop" must
    not be treated as a subnet."""
    for bad in ("10.0.0.0", "10.0.0.0/33", "10.0.0.0/24 ; nft flush ruleset",
                "999.1.1.0/24", "", "$(id)"):
        reply = _call("nat.enable", {"subnet": bad, "bridge": "bnat0-7f3a"})
        assert reply["ok"] is False, f"{bad!r} was accepted"
        assert reply["error"]["code"] == "EINVAL"


def test_a_valid_subnet_reaches_the_operation() -> None:
    net = RecordingNet()
    reply = dispatch(
        {"id": 1, "verb": "nat.enable", "params": {"subnet": "10.200.3.0/24", "bridge": "bnat0-7f3a"}},
        net,
    )

    assert reply["ok"] is True
    assert net.calls[0][0] == "nat_enable"


def test_a_dhcp_key_cannot_walk_out_of_run() -> None:
    """The key becomes a lease filename. A key containing a slash would write
    wherever it liked, as root."""
    for bad in ("../../etc/passwd", "net/../..", "a b", "x" * 65, ""):
        reply = _call("dhcp.start", {
            "key": bad, "bridge": "bnat0-7f3a", "gateway": "10.0.0.1",
            "prefix": 24, "first": "10.0.0.50", "last": "10.0.0.250",
        })
        assert reply["ok"] is False, f"{bad!r} was accepted"


def test_reserved_vlan_ids_are_refused_here_rather_than_by_the_kernel() -> None:
    """0 and 4095 are reserved. The kernel rejects them with an error that does
    not say which VLAN it disliked."""
    for bad in (0, 4095, -1, 70000, "20"):
        reply = _call("bridge.port_vlan", {"name": "vp-n1e1", "pvid": bad})
        assert reply["ok"] is False, f"{bad!r} was accepted"

    ok = _call("bridge.port_vlan", {"name": "vp-n1e1", "pvid": 20, "untagged": [20]})
    assert ok["ok"] is True


def test_only_the_two_real_tag_protocols_are_accepted() -> None:
    assert _call("bridge.vlan_aware", {"name": "bnat0-7f3a", "on": True, "proto": "802.1Q"})["ok"]
    assert _call("bridge.vlan_aware", {"name": "bnat0-7f3a", "on": True, "proto": "802.1ad"})["ok"]
    assert not _call("bridge.vlan_aware", {"name": "bnat0-7f3a", "on": True, "proto": "802.1x"})["ok"]


def test_an_address_must_be_an_address() -> None:
    for bad in ("10.0.0.256", "10.0.0", "not-an-ip", "10.0.0.1/24"):
        assert not _call("addr.replace", {"name": "bnat0-7f3a", "address": bad, "prefix": 24})["ok"]
    assert _call("addr.replace", {"name": "bnat0-7f3a", "address": "10.0.0.1", "prefix": 24})["ok"]


def test_a_prefix_outside_zero_to_thirtytwo_is_refused() -> None:
    for bad in (-1, 33, 64, "24", None):
        assert not _call("addr.replace", {"name": "bnat0-7f3a", "address": "10.0.0.1", "prefix": bad})["ok"]


def test_the_tag_protocol_reaches_the_kernel_as_an_ethertype() -> None:
    """The kernel wants the number, not the name. Passing "802.1Q" straight
    through got "required argument is not an integer" from netlink — an error
    naming neither the argument nor what it wanted, and which only showed up
    with a real bridge on the other end."""
    net = RecordingNet()
    dispatch(
        {"id": 1, "verb": "bridge.vlan_aware",
         "params": {"name": "bnat0-7f3a", "on": True, "proto": "802.1ad"}},
        net,
    )
    assert net.calls[0][1]["args"][2] == 0x88A8

    net = RecordingNet()
    dispatch(
        {"id": 1, "verb": "bridge.vlan_aware",
         "params": {"name": "bnat0-7f3a", "on": True, "proto": "802.1Q"}},
        net,
    )
    assert net.calls[0][1]["args"][2] == 0x8100
