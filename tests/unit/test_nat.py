"""Subnet allocation and the pool maths for NAT networks.

The interesting failures here are silent: a subnet that overlaps another lab's
routes traffic to the wrong place, and a pool that includes the gateway hands a
guest the router's own address and the segment stops working in a way that
looks like a DHCP problem.
"""

from __future__ import annotations

import ipaddress

import pytest

from labtris_api.errors import ApiError
from labtris_api.nat import AUTO_POOL, dhcp_key, pool_for


def test_the_gateway_is_never_inside_the_pool() -> None:
    """Handing a guest the gateway's own address breaks the segment in a way
    that looks like anything other than what it is."""
    net = ipaddress.ip_network("10.200.4.0/24")
    gateway, first, last = pool_for(net)

    assert ipaddress.ip_address(gateway) < ipaddress.ip_address(first)
    assert ipaddress.ip_address(first) <= ipaddress.ip_address(last)


def test_the_pool_leaves_room_for_hand_addressed_nodes() -> None:
    """A lab usually gives its routers fixed addresses. Starting the pool at
    .50 means nobody has to check whether DHCP will collide with the .1 to .10
    everyone reaches for."""
    _, first, _ = pool_for(ipaddress.ip_network("10.200.4.0/24"))

    assert first.endswith(".50")


@pytest.mark.parametrize("cidr", ["10.10.0.0/28", "172.16.0.0/29", "192.168.1.0/30"])
def test_small_subnets_still_produce_a_usable_pool(cidr: str) -> None:
    """The .50 rule cannot apply to a /29. It falls back to halfway rather than
    producing a pool that starts past the end of the subnet — or, on a /30,
    one whose first address is the gateway."""
    net = ipaddress.ip_network(cidr)
    gateway, first, last = pool_for(net)

    for addr in (gateway, first, last):
        assert ipaddress.ip_address(addr) in net
    assert ipaddress.ip_address(gateway) < ipaddress.ip_address(first)
    assert ipaddress.ip_address(first) <= ipaddress.ip_address(last)


def test_a_subnet_with_no_room_for_a_guest_is_refused() -> None:
    """A /32 is one address, which is the gateway; there is no second one to
    hand out. Anything smaller than a /30 is refused earlier, by pick_subnet,
    which is where a caller-supplied subnet arrives."""
    with pytest.raises(ApiError):
        pool_for(ipaddress.ip_network("10.0.0.0/32"))


def test_the_automatic_pool_avoids_the_addresses_labs_use() -> None:
    """10.0.x and 10.1.x are where nearly every tutorial puts its own
    addressing, so an automatic NAT subnet landing there would collide with
    the topology it was added to."""
    assert AUTO_POOL.supernet_of(ipaddress.ip_network("10.200.0.0/24"))
    assert not AUTO_POOL.overlaps(ipaddress.ip_network("10.0.0.0/16"))
    assert not AUTO_POOL.overlaps(ipaddress.ip_network("10.1.0.0/16"))


def test_the_dhcp_key_cannot_escape_its_directory() -> None:
    """The key names a file under /run. netd validates it too, but a key built
    from an id should not be the thing relying on that."""
    key = dhcp_key("01ARZ3NDEKTSV4RRFFQ69G5FAV")

    assert "/" not in key and ".." not in key


def test_conntrack_parses_both_the_proc_and_tool_formats() -> None:
    """The proc file leads with "ipv4 2" columns and the CLI does not, so the
    protocol cannot be read from a fixed index. Getting this wrong is silent:
    every row parses, and every one reports the wrong protocol."""
    import importlib.util
    import pathlib as _p
    import sys

    # net.py imports pyroute2, which only exists on a host with netlink. Load
    # just the parser rather than the module.
    src = _p.Path(__file__).resolve().parents[2] / "labtris_netd" / "net.py"
    text = src.read_text()
    start = text.index("def _in_subnet")
    end = text.index('_NFT_TABLE = "labtris"')
    ns: dict = {"Any": object}
    exec(compile(text[start:end], "netparse", "exec"), ns)  # noqa: S102
    del importlib, sys

    proc_line = (
        "ipv4     2 tcp      6 431994 ESTABLISHED src=10.200.0.50 dst=140.82.121.4 "
        "sport=54321 dport=443 src=140.82.121.4 dst=10.0.2.15 sport=443 dport=54321 "
        "[ASSURED] mark=0 use=1"
    )
    tool_line = (
        "tcp      6 431994 ESTABLISHED src=10.200.0.50 dst=140.82.121.4 "
        "sport=54321 dport=443 src=140.82.121.4 dst=10.0.2.15 sport=443 dport=54321 "
        "[ASSURED] mark=0 use=1"
    )

    for raw in (proc_line, tool_line):
        rows = ns["_conntrack_for"](raw, "10.200.0.0/24")
        assert len(rows) == 1, raw[:20]
        row = rows[0]
        assert row["proto"] == "tcp"
        assert row["src"] == "10.200.0.50"
        assert row["dport"] == "443"
        # The whole point of the view: what the outside sees it as.
        assert row["translated"] == "10.0.2.15"


def test_a_flow_from_another_subnet_is_not_this_network_s_business() -> None:
    import pathlib as _p

    src = _p.Path(__file__).resolve().parents[2] / "labtris_netd" / "net.py"
    text = src.read_text()
    ns: dict = {"Any": object}
    exec(  # noqa: S102
        compile(text[text.index("def _in_subnet") : text.index('_NFT_TABLE = "labtris"')],
                "netparse", "exec"),
        ns,
    )
    line = ("tcp 6 431999 ESTABLISHED src=192.168.9.9 dst=8.8.8.8 sport=1 dport=443 "
            "src=8.8.8.8 dst=192.168.9.9 sport=443 dport=1 mark=0 use=1")

    assert ns["_conntrack_for"](line, "10.200.0.0/24") == []
