"""A NIC model per port.

One node, two kinds of card: an e1000 management port for a guest whose
installer only knows e1000, virtio for the data plane. The card, its queue
count and its tap's queue flag are decided per port, because they have to
agree: a multi-queue tap behind an e1000 fails at boot with EINVAL.
"""

from __future__ import annotations

from labtris_api.runtime import qemu as q
from labtris_api.runtime.base import IfaceSpec


def test_a_port_without_a_model_follows_the_node() -> None:
    cfg = {"nic_model": "virtio-net-pci", "nic_queues": 4}
    assert q.port_nic(cfg, None) == ("virtio-net-pci", 4)
    assert q.port_nic({}, None) == ("virtio-net-pci", 1)


def test_an_e1000_port_on_a_multi_queue_node_gets_one_queue() -> None:
    cfg = {"nic_model": "virtio-net-pci", "nic_queues": 4}
    assert q.port_nic(cfg, "e1000") == ("e1000", 1)


def test_a_virtio_port_on_an_e1000_node_gets_the_node_queues() -> None:
    cfg = {"nic_model": "e1000", "nic_queues": 4}
    assert q.port_nic(cfg, "virtio-net-pci") == ("virtio-net-pci", 4)


def test_the_port_model_reaches_config_json() -> None:
    ifaces = [
        IfaceSpec(iface_id="a", idx=0, guest_name="eth0", host_ifname="t0", mac="52:54:00:00:00:01",
                  nic_model="e1000"),
        IfaceSpec(iface_id="b", idx=1, guest_name="eth1", host_ifname="t1", mac="52:54:00:00:00:02"),
    ]
    assert q._serialize_ifaces(ifaces) == [
        {"idx": 0, "host_ifname": "t0", "mac": "52:54:00:00:00:01", "nic_model": "e1000"},
        # Unset stays out, so config.json of an untouched node is unchanged.
        {"idx": 1, "host_ifname": "t1", "mac": "52:54:00:00:00:02"},
    ]


def test_start_builds_each_card_from_its_own_port() -> None:
    """Source check: the cold-plug loop must ask port_nic per interface, not
    take one model for the whole node before the loop."""
    src = (q.__file__ and open(q.__file__).read()) or ""
    loop = src[src.index("for iface in cfg.get(\"interfaces\") or []:"):]
    loop = loop[: loop.index("template_extras")]
    assert "port_nic(cfg, iface.get(\"nic_model\"))" in loop
