"""What a guest calls its own ports, and why we only claim to predict it.

The kernel names interfaces from the PCI slot they land in, so none of this is
something Labtris decides. It is a promise about what someone will see once
they log in — which is what a netplan file has to match, and what makes the
difference between a working lab and a puzzled ten minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from labtris_api.lifecycle import (
    guest_name,
    iface_scheme_for,
    next_free_iface_slot,
    next_iface_idx,
)
from labtris_api.naming import IFACE_SCHEMES, guest_iface_name


@dataclass
class _StubIface:
    idx: int
    name: str


@dataclass
class _StubNode:
    interfaces: list[_StubIface] = field(default_factory=list)


def test_containers_still_get_eth0() -> None:
    """The default has to stay the one that was always right for a netns."""
    assert [guest_iface_name("eth", i) for i in range(3)] == ["eth0", "eth1", "eth2"]


def test_vmware_ports_are_32_slots_apart() -> None:
    """ens192, ens224, ens256 — the numbers jump because VMXNET3 adapters sit
    32 PCI slots apart. Consecutive numbering here would be a plausible-looking
    lie, and the one people actually get caught by."""
    assert [guest_iface_name("vmware", i) for i in range(3)] == ["ens192", "ens224", "ens256"]


def test_a_qemu_ubuntu_guest_starts_at_ens3_not_eth0() -> None:
    assert guest_iface_name("ens", 0) == "ens3"
    assert guest_iface_name("ens", 1) == "ens4"


def test_srlinux_uses_its_own_convention() -> None:
    assert guest_iface_name("srl", 0) == "ethernet-1/1"


def test_an_unknown_scheme_falls_back_rather_than_raising() -> None:
    """A BYO image names a scheme we have never heard of; the node should
    still be creatable, with the safest guess."""
    assert guest_iface_name("no-such-scheme", 0) == "eth0"
    assert guest_iface_name(None, 1) == "eth1"


@pytest.mark.parametrize(
    ("runtime", "image", "expected"),
    [
        ("docker", "alpine:3.20", "eth"),
        ("docker", "ghcr.io/nokia/srlinux:latest", "srl"),
        ("docker", "ghcr.io/someone/unreviewed:latest", "eth"),
        ("qemu", "ubuntu-cloud-24.04", "ens"),
        ("qemu", "cirros", "eth"),
        ("qemu", "not-in-the-catalog", "eth"),
    ],
)
def test_the_scheme_is_resolved_from_the_image(runtime: str, image: str, expected: str) -> None:
    assert iface_scheme_for(runtime, image) == expected


def test_cirros_keeps_eth0_because_it_has_no_systemd() -> None:
    """Predictable names come from systemd/udev. CirrOS is busybox, so the
    QEMU default of ens3 would be wrong for it specifically."""
    assert iface_scheme_for("qemu", "cirros") == "eth"
    assert guest_name(0, None, iface_scheme_for("qemu", "cirros")) == "eth0"


def test_an_explicit_name_always_wins() -> None:
    """Importing a topology that already names its ports must not be
    second-guessed — the file is the authority there, not our table."""
    assert guest_name(0, "management0", "vmware") == "management0"


def test_next_free_slot_skips_off_scheme_names() -> None:
    """A prior interface at idx=0 named "eth1" — the shape the auto-link
    helper used to produce — would make the derived name for the next
    default idx collide at (node_id, name). Walk past it."""
    node = _StubNode(interfaces=[_StubIface(idx=0, name="eth1")])
    # next_iface_idx alone would return 1 → guest_name would derive "eth1"
    # → collision. next_free_iface_slot has to skip to 2 so the derived
    # name is "eth2".
    assert next_iface_idx(node) == 1
    idx = next_free_iface_slot(node, "eth")
    assert idx == 2
    assert guest_iface_name("eth", idx) == "eth2"


def test_next_free_slot_agrees_with_next_iface_idx_when_names_are_canonical() -> None:
    """A node with normally-named interfaces gets the plain next-idx answer;
    the extra check is invisible on the happy path."""
    node = _StubNode(interfaces=[_StubIface(idx=0, name="eth0"), _StubIface(idx=1, name="eth1")])
    assert next_free_iface_slot(node, "eth") == next_iface_idx(node) == 2


def test_next_free_slot_respects_scheme() -> None:
    """For a scheme where idx=0 means ens3, the derived name for idx=1 is
    ens4 — no collision with an existing ens3 named at idx=0, so the answer
    is still 1."""
    node = _StubNode(interfaces=[_StubIface(idx=0, name="ens3")])
    assert next_free_iface_slot(node, "ens") == 1


def test_every_scheme_is_self_describing() -> None:
    """The catalog ships these to the UI; a scheme with no label is a blank
    row in a dropdown."""
    for scheme in IFACE_SCHEMES.values():
        assert scheme.label
        assert scheme.name(0)
