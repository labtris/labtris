from __future__ import annotations

from labtris_api.naming import IFNAME_RE, base36, device_hint, host_ifname


def test_host_ifname_deterministic() -> None:
    a = host_ifname("tap", "01JAAAAAAAAAAAAAAAAAAAAA", 0)
    b = host_ifname("tap", "01JAAAAAAAAAAAAAAAAAAAAA", 0)
    assert a == b
    assert a != host_ifname("tap", "01JAAAAAAAAAAAAAAAAAAAAA", 1)


def test_ifname_re_and_length() -> None:
    for kind in ("tap", "bridge", "veth"):
        for salt in range(16):
            name = host_ifname(kind, "01JCOLLISIONTESTOWNER0001", salt)  # type: ignore[arg-type]
            assert IFNAME_RE.match(name)
            assert len(name) <= 15
            assert len(name) == 9


def test_collision_salt_retry_changes_name() -> None:
    owner = "01JSALTRETRYOWNER00000001"
    names = {host_ifname("bridge", owner, salt) for salt in range(16)}
    assert len(names) == 16
    assert all(n.startswith("b") for n in names)


def test_base36_alphabet() -> None:
    assert base36(0) == "0"
    assert base36(35) == "z"
    assert base36(36) == "10"


def test_a_readable_name_says_what_the_device_is_for() -> None:
    """vjyo3kffo told you nothing. Reading `ip link` or a capture should not
    require cross-referencing the database to learn which node you are on."""
    name = host_ifname("veth", "01IFACE0000000000000000001", 0, "ub1-eth0")

    assert name.startswith("v")
    assert "ub1eth0" in name
    assert len(name) <= 15


def test_names_still_fit_what_linux_accepts() -> None:
    """IFNAMSIZ is 16 including the terminator, so 15 is the hard ceiling —
    and a hint is user-supplied, so it cannot be trusted to be short."""
    for hint in ("", "a", "core-router-01-ethernet3", "x" * 200, "!!!???"):
        for kind in ("tap", "bridge", "veth", "vxlan"):
            name = host_ifname(kind, "01OWNER00000000000000001", 0, hint)  # type: ignore[arg-type]
            assert len(name) <= 15, f"{name} is too long for {hint!r}"
            assert IFNAME_RE.match(name), f"{name} fails the ownership gate"


def test_the_gate_still_excludes_devices_we_do_not_own() -> None:
    """This pattern is what stops netd deleting docker0 or a host NIC, so
    making names readable must not make it looser in a way that matters."""
    for real in (
        "docker0", "veth48dff4c", "br-13b48a89612a", "eth0", "ens3", "enp39s0",
        "lo", "tap0", "virbr0", "tun0", "bond0", "team0", "veth0", "wlan0",
    ):
        assert not IFNAME_RE.match(real), f"{real} would be treated as ours"


def test_old_names_are_still_recognised() -> None:
    """Live labs have devices from the previous scheme; the gate has to keep
    accepting them or netd stops being able to clean them up."""
    for legacy in ("vjyo3kffo", "b299nt0hm", "t12345678", "xabcdefgh"):
        assert IFNAME_RE.match(legacy)


def test_a_hint_does_not_weaken_uniqueness() -> None:
    """Two interfaces can easily share a hint — every lab has an eth0. The
    digest, not the hint, is what keeps them apart."""
    a = host_ifname("veth", "01IFACE000000000000000001", 0, "r1-eth0")
    b = host_ifname("veth", "01IFACE000000000000000002", 0, "r1-eth0")

    assert a != b


def test_names_stay_deterministic_so_a_restart_reproduces_the_dataplane() -> None:
    args = ("veth", "01IFACE000000000000000001", 0, "r1-eth0")
    assert host_ifname(*args) == host_ifname(*args)  # type: ignore[arg-type]


def test_composing_a_name_keeps_both_halves_recognisable() -> None:
    """Concatenating then truncating mangled both: core-rtr + eth0 produced
    "rertreth0", less use than the hash it replaced. The port is short and
    fully distinguishing, so it survives whole and the owner takes the rest."""
    assert device_hint("core-rtr", "eth0") == "corereth0"
    assert device_hint("ub1", "eth0") == "ub1eth0"
    assert device_hint("really-long-node-name-here", "eth3") == "realleth3"


def test_a_composed_name_never_exceeds_the_budget() -> None:
    for owner in ("", "a", "x" * 100, "core-router-01"):
        for port in ("", "eth0", "eth10", "y" * 40):
            name = host_ifname("veth", "01OWNER0000000000000000001", 0,
                               device_hint(owner, port))
            assert len(name) <= 15
            assert IFNAME_RE.match(name)


def test_ip_brief_parsing_survives_busybox_output() -> None:
    """`ip -br -4 addr` is parsed rather than `ip -j` requested, because
    busybox has no JSON output and Alpine is the commonest node image."""
    from labtris_api.routers.nodes import _parse_ip_brief

    out = "\n".join(
        [
            "lo               UNKNOWN        127.0.0.1/8",
            "eth1             UP             10.0.1.1/24",
            "eth2             UP             10.0.2.1/24 172.16.0.1/16",
            "eth3             DOWN           ",
        ]
    )
    parsed = _parse_ip_brief(out)

    # Loopback is never interesting, and a port with no address is absent
    # rather than present-and-empty: "unknown" and "none" are different.
    assert "lo" not in parsed
    assert "eth3" not in parsed
    assert parsed["eth1"] == ["10.0.1.1/24"]
    assert parsed["eth2"] == ["10.0.2.1/24", "172.16.0.1/16"]
