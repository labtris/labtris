"""nic_queues: multi-queue virtio-net and vhost, opt-in per node.

Unset keeps today's command line byte for byte. Set, it changes the tap, the
netdev and the device together — the three places that have to agree, or
the guest sees one queue (tap made single-queue), fails to boot (queues=N on
a single-queue tap: EINVAL), or gets an error from QEMU (mq=on on e1000).
"""

from __future__ import annotations

from labtris_api.runtime import qemu as q


def test_unset_is_the_old_command_line() -> None:
    cfg = {"nic_model": "virtio-net-pci"}
    assert q.nic_queue_count(cfg) == 1
    assert q.netdev_args("net0", "t0123abcd", 1, False) == (
        "tap,id=net0,ifname=t0123abcd,script=no,downscript=no"
    )
    assert q.device_args("virtio-net-pci", "net0", "52:54:00:00:00:01", "nic0", 1) == (
        "virtio-net-pci,netdev=net0,mac=52:54:00:00:00:01,id=nic0"
    )


def test_four_queues_with_vhost() -> None:
    cfg = {"nic_model": "virtio-net-pci", "nic_queues": 4}
    assert q.nic_queue_count(cfg) == 4
    assert q.netdev_args("net2", "t0123abcd", 4, True) == (
        "tap,id=net2,ifname=t0123abcd,script=no,downscript=no,queues=4,vhost=on"
    )
    # 2N+2 MSI-X vectors: RX and TX per queue, plus config and control.
    assert q.device_args("virtio-net-pci", "net2", "52:54:00:00:00:02", "nic2", 4) == (
        "virtio-net-pci,netdev=net2,mac=52:54:00:00:00:02,id=nic2,mq=on,vectors=10"
    )


def test_four_queues_without_vhost_still_multi_queue() -> None:
    """A host that lacks /dev/vhost-net gets the queues and not the vhost;
    the warning is logged, the node still boots."""
    assert q.netdev_args("net0", "t0123abcd", 4, False) == (
        "tap,id=net0,ifname=t0123abcd,script=no,downscript=no,queues=4"
    )


def test_non_virtio_models_ignore_the_setting() -> None:
    """QEMU rejects mq= and vectors= on e1000 rather than ignoring them, so
    the count collapses to 1 before any string is built."""
    assert q.nic_queue_count({"nic_model": "e1000", "nic_queues": 4}) == 1
    assert q.device_args("e1000", "net0", "52:54:00:00:00:01", "nic0", 1) == (
        "e1000,netdev=net0,mac=52:54:00:00:00:01,id=nic0"
    )


def test_count_is_bounded_and_tolerant() -> None:
    assert q.nic_queue_count({"nic_queues": 99}) == 8
    assert q.nic_queue_count({"nic_queues": 0}) == 1
    assert q.nic_queue_count({"nic_queues": "four"}) == 1
    assert q.nic_queue_count({}) == 1


def test_router_clamps_queues_to_vcpus() -> None:
    from labtris_api.routers.nodes import _clamp_queues

    assert _clamp_queues(None, 4) is None
    assert _clamp_queues(4, 2) == 2
    assert _clamp_queues(4, None) == 4
    assert _clamp_queues(4, 0.5) == 1
    assert _clamp_queues(16, 32) == 8
