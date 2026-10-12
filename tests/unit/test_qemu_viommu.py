"""viommu: an emulated VT-d IOMMU with every virtio NIC behind it.

Every string here was accepted by QEMU 10.0 under KVM on 2026-10-12; the
two refusals quoted in the builders' docstrings are what the wrong forms
produce. The point of the tests is that the option changes the machine,
adds the device, and marks the NICs, together — a NIC left transitional
or without iommu_platform is one the guest's vfio will bind and DPDK will
DMA through untranslated.
"""

from __future__ import annotations

import pytest

from labtris_api.errors import ApiError
from labtris_api.runtime import qemu as q


def test_off_is_the_old_machine_line() -> None:
    assert q.machine_args({"machine": "pc"}) == ["-M", "pc"]
    assert q.machine_args({"machine": "q35", "viommu": False}) == ["-M", "q35"]
    assert q.device_args("virtio-net-pci", "net0", "52:54:00:00:00:01", "nic0", 1) == (
        "virtio-net-pci,netdev=net0,mac=52:54:00:00:00:01,id=nic0"
    )


def test_on_splits_the_irqchip_and_adds_the_device_first() -> None:
    assert q.machine_args({"machine": "q35", "viommu": True}) == [
        "-M", "q35,kernel-irqchip=split",
        "-device", "intel-iommu,intremap=on,caching-mode=on,device-iotlb=on",
    ]


def test_on_needs_q35() -> None:
    with pytest.raises(ApiError) as e:
        q.machine_args({"machine": "pc", "viommu": True})
    assert "q35" in e.value.message


def test_nics_go_behind_the_iommu_as_modern_only_devices() -> None:
    assert q.device_args(
        "virtio-net-pci", "net0", "52:54:00:00:00:01", "nic0", 4, viommu=True
    ) == (
        "virtio-net-pci,netdev=net0,mac=52:54:00:00:00:01,id=nic0,mq=on,vectors=10"
        ",iommu_platform=on,ats=on,disable-legacy=on"
    )


def test_non_virtio_nics_are_left_alone() -> None:
    # e1000 has no iommu_platform property; passing it would be refused.
    assert q.device_args("e1000", "net0", "52:54:00:00:00:01", "nic0", 1, viommu=True) == (
        "e1000,netdev=net0,mac=52:54:00:00:00:01,id=nic0"
    )


def test_the_option_is_declared_and_defaults_off() -> None:
    assert q.QEMU_OPTIONS["viommu"]["type"] == "bool"
    assert q.option_defaults()["viommu"] is False
    assert q.resolved_options({"viommu": True})["viommu"] is True


async def test_a_template_option_added_later_reaches_existing_nodes() -> None:
    """create() copies the template's options into the node row. A key the
    template gains afterwards is missing from that copy, and until node_spec
    merged the two it never reached the node — adding viommu to a template
    that already had nodes did nothing. The node's own keys still win."""
    from types import SimpleNamespace

    from labtris_api import lifecycle
    from labtris_api.runtime.qemu import CUSTOM_PREFIX

    template = SimpleNamespace(spec={
        "cpus": 4, "qemu_opts": {"machine": "q35", "viommu": True, "cpu": "host"},
    })

    class _Result:
        def scalars(self):  # noqa: ANN202
            return self
        def first(self):  # noqa: ANN202
            return template

    class _Session:
        async def execute(self, stmt):  # noqa: ANN001, ANN202
            return _Result()

    node = SimpleNamespace(
        id="n1", lab_id="l1", name="r1", image=f"{CUSTOM_PREFIX}appliance",
        env={}, cmd=None, cpu_limit=None, nic_model=None, nic_queues=None, ram_mb=None,
        qemu_opts={"machine": "q35", "cpu": "max"},  # the copy create() made, before viommu
        interfaces=[], runtime="qemu", startup_config=None, opts=None,
        console={}, iface_scheme=None,
    )
    spec = await lifecycle.node_spec(_Session(), node)
    assert spec.qemu_opts["viommu"] is True, "gained from the template"
    assert spec.qemu_opts["cpu"] == "max", "the node's own choice is kept"
