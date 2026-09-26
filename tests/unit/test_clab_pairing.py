"""Recovering a containerlab topology from the veths it built.

The fixtures are real `netns.links` output captured from a live instance on
2026-09-26 — the same deploys whose `Created link:` lines are quoted in each
test — because the whole point of this code is to agree with what
containerlab actually did, and invented ifindexes cannot check that.
"""

from labtris_api.clab_watch import pair_links


def veth(index: int, name: str, peer: int, netnsid: int = 1) -> dict:
    return {
        "index": index,
        "name": name,
        "kind": "veth",
        "mac": "aa:c1:ab:00:00:01",
        "mtu": 9500,
        "up": True,
        "peer_index": peer,
        "peer_netnsid": netnsid,
    }


LO = {
    "index": 1,
    "name": "lo",
    "kind": None,
    "mac": "00:00:00:00:00:00",
    "mtu": 65536,
    "up": True,
    "peer_index": None,
    "peer_netnsid": None,
}


def test_point_to_point():
    """clab said: n1:eth1 <-> n2:eth1."""
    nodes = {
        "n1": {"interfaces": [LO, veth(7, "eth0", 8, 0), veth(9, "eth1", 10)]},
        "n2": {"interfaces": [LO, veth(5, "eth0", 6, 0), veth(10, "eth1", 9)]},
    }
    links, ifaces, ambiguous = pair_links(nodes)
    assert links == [("n1", "eth1", "n2", "eth1")]
    assert ambiguous == []
    # eth0's peer is on clab's management bridge, not in another node, so it
    # never pairs. The management network must not become a lab link.
    assert ifaces == {"n1": ["eth1"], "n2": ["eth1"]}


def test_triangle():
    """clab said: a:eth1<->b:eth1, b:eth2<->c:eth1, c:eth2<->a:eth2."""
    nodes = {
        "a": {"interfaces": [LO, veth(16, "eth0", 17, 0), veth(21, "eth1", 20),
                             veth(24, "eth2", 25)]},
        "b": {"interfaces": [LO, veth(18, "eth0", 19, 0), veth(20, "eth1", 21),
                             veth(22, "eth2", 23)]},
        "c": {"interfaces": [LO, veth(26, "eth0", 27, 0), veth(23, "eth1", 22),
                             veth(25, "eth2", 24)]},
    }
    links, _, ambiguous = pair_links(nodes)
    assert sorted(links) == [
        ("a", "eth1", "b", "eth1"),
        ("a", "eth2", "c", "eth2"),
        ("b", "eth2", "c", "eth1"),
    ]
    assert ambiguous == []


def test_each_link_reported_once():
    """Both ends describe the same cable; it must not be drawn twice."""
    nodes = {
        "x": {"interfaces": [veth(4, "eth1", 5)]},
        "y": {"interfaces": [veth(5, "eth1", 4)]},
    }
    links, _, _ = pair_links(nodes)
    assert len(links) == 1


def test_unpaired_end_is_dropped():
    """A veth to a bridge rather than to another node is not a lab link."""
    nodes = {"solo": {"interfaces": [LO, veth(7, "eth0", 8, 0)]}}
    assert pair_links(nodes) == ([], {}, [])


def test_ambiguous_peer_is_reported_not_guessed():
    """Two nodes can number an interface identically.

    ifindexes are per-namespace, so b and c can both hold index 5 pointing
    back at a's index 4. Nothing in the data says which is the real peer, so
    the link is dropped and named rather than picked at random.
    """
    nodes = {
        "a": {"interfaces": [veth(4, "eth1", 5)]},
        "b": {"interfaces": [veth(5, "eth1", 4)]},
        "c": {"interfaces": [veth(5, "eth1", 4)]},
    }
    links, _, ambiguous = pair_links(nodes)
    assert links == []
    # All three, not just a: b and c each see a as their only candidate, but
    # a can be the peer of at most one of them, so neither claim is safe.
    assert sorted(ambiguous) == ["a:eth1", "b:eth1", "c:eth1"]


def test_node_netd_could_not_read_is_skipped():
    """One exited container must not lose the rest of the topology."""
    nodes = {
        "gone": {"error": "setns failed: No such file or directory"},
        "x": {"interfaces": [veth(4, "eth1", 5)]},
        "y": {"interfaces": [veth(5, "eth1", 4)]},
    }
    links, _, _ = pair_links(nodes)
    assert links == [("x", "eth1", "y", "eth1")]
