"""A cloud network enslaves a host NIC to a lab bridge, and not every named
interface can be one. Binding a bridge to a bridge fails deep in netlink with
ELOOP — "Too many levels of symbolic links" — which tells the user nothing
about what they picked, so both the listing and the attach have to be honest
about it up front.
"""

from __future__ import annotations

from typing import Any

import pytest

from labtris_netd.net import NetdFault, PyrouteNet, _map_nl


class _Addr:
    def __init__(self, index: int, ip: str, prefixlen: int) -> None:
        self._ip = ip
        self._prefix = prefixlen
        self._index = index

    def __getitem__(self, key: str) -> Any:
        return {"prefixlen": self._prefix, "index": self._index}[key]

    def get_attr(self, key: str) -> Any:
        return {"IFA_ADDRESS": self._ip}.get(key)


class _Link(dict[str, Any]):
    """The shape pyroute2 hands back: flags in the mapping, everything else
    behind get_attr()."""

    def __init__(self, name: str, index: int, kind: str | None, master: int | None) -> None:
        super().__init__(index=index, flags=1)
        self._attrs = {"IFLA_IFNAME": name, "IFLA_MASTER": master}
        self._kind = kind

    def get_attr(self, key: str) -> Any:
        if key == "IFLA_LINKINFO":
            return None if self._kind is None else _Info(self._kind)
        return self._attrs.get(key)


class _Info:
    def __init__(self, kind: str) -> None:
        self._kind = kind

    def get_attr(self, key: str) -> Any:
        return self._kind if key == "IFLA_INFO_KIND" else None


class _FakeIPRoute:
    def __init__(self, links: list[_Link], addrs: list[_Addr] | None = None) -> None:
        self._links = links
        self._addrs = addrs or []

    def __enter__(self) -> _FakeIPRoute:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_addr(self, index: int | None = None) -> list[Any]:
        if index is None:
            return list(self._addrs)
        return [a for a in self._addrs if a["index"] == index]

    def link_lookup(self, ifname: str) -> list[int]:
        return [link["index"] for link in self._links if link.get_attr("IFLA_IFNAME") == ifname]

    def get_links(self, index: int | None = None) -> list[_Link]:
        if index is None:
            return self._links
        return [link for link in self._links if link["index"] == index]

    def get_routes(self, family: int) -> list[Any]:
        return []


LINKS = [
    _Link("enp39s0", 2, None, None),  # a real NIC
    _Link("docker0", 3, "bridge", None),  # Docker's — Labtris never touches it
    _Link("veth9", 4, "veth", 3),  # already held by that bridge
    _Link("sharkA", 5, "veth", None),  # free, and a legal uplink
    _Link("br0", 6, "bridge", None),  # netplan / EVE-style pre-bridge
    _Link("ens160", 7, None, 6),  # the NIC now inside br0
]
ADDRS = [
    _Addr(2, "192.0.2.10", 24),  # address on the bare NIC
    _Addr(6, "10.124.133.91", 24),  # address on the reusable bridge
]


@pytest.fixture()
def net(monkeypatch: pytest.MonkeyPatch) -> PyrouteNet:
    monkeypatch.setattr("labtris_netd.net.IPRoute", lambda: _FakeIPRoute(LINKS, ADDRS))
    return PyrouteNet()


def test_listing_says_which_interfaces_can_back_a_cloud(net: PyrouteNet) -> None:
    by_name = {i["name"]: i for i in net.host_interfaces()["interfaces"]}

    assert by_name["enp39s0"]["usable"] is True
    assert by_name["sharkA"]["usable"] is True

    # docker0 is the one bridge Labtris must never touch.
    assert by_name["docker0"]["usable"] is False
    assert "Docker" in by_name["docker0"]["unusable_reason"]

    # A general host bridge is reusable — this is the EVE-NG-style pnet, and
    # netplan's br0. Labtris attaches lab veths to it without touching the
    # NIC underneath.
    assert by_name["br0"]["usable"] is True
    assert by_name["br0"]["reusable"] is True

    # A NIC enslaved to a non-Labtris bridge is unusable on its own — pick
    # the bridge holding it instead.
    assert by_name["ens160"]["usable"] is False
    assert "pick that bridge" in by_name["ens160"]["unusable_reason"]

    assert by_name["veth9"]["usable"] is False


def test_inspect_bridge_returns_a_bridge(net: PyrouteNet) -> None:
    r = net.inspect_bridge("br0")
    assert r["exists"] and r["kind"] == "bridge" and r["member_count"] == 1
    assert "10.124.133.91/24" in r["addresses"]


def test_inspect_bridge_refuses_a_nic(net: PyrouteNet) -> None:
    r = net.inspect_bridge("enp39s0")
    assert r["exists"] and r["kind"] is None  # not a bridge


def test_inspect_bridge_reports_missing(net: PyrouteNet) -> None:
    r = net.inspect_bridge("nonesuch")
    assert r["exists"] is False


def test_attaching_a_bridge_is_refused_before_netlink_sees_it(net: PyrouteNet) -> None:
    with pytest.raises(NetdFault) as exc:
        net.cloud_attach("docker0", "br-lab", force=False)

    assert exc.value.code == "EINVAL"
    assert "is itself a bridge" in exc.value.message


def test_force_does_not_make_a_nested_bridge_possible(net: PyrouteNet) -> None:
    # force exists for "this will cut the host off the network, I mean it".
    # It cannot talk the kernel into nesting bridges, so it must not pretend to.
    with pytest.raises(NetdFault) as exc:
        net.cloud_attach("docker0", "br-lab", force=True)

    assert exc.value.code == "EINVAL"


def test_eloop_reads_as_something_about_interfaces() -> None:
    class _NlError(Exception):
        code = 40

    fault = _map_nl(_NlError(), "br-13b48a89612a")

    assert fault.code == "ELOOP"
    assert "symbolic link" not in fault.message
    assert "bridge" in fault.message
