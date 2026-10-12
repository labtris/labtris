"""sync_from_spec: what the row says reaches config.json before a start.

The options were the gap. create() wrote `opts` once and sync_from_spec
never touched them, so PATCH /nodes/{id}/qemu-options changed the row and
nothing else — a chipset or vIOMMU change on an existing node took effect
only if someone edited config.json by hand while the node was stopped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labtris_api.runtime import qemu as q
from labtris_api.runtime.base import NodeSpec, RuntimeHandle


@pytest.mark.asyncio
async def test_patched_options_reach_config_json(tmp_path: Path) -> None:
    vm_dir = tmp_path / "vm"
    vm_dir.mkdir()
    cfg_path = q._config_path(vm_dir)
    # As create() left it: the template's options at the time.
    cfg_path.write_text(json.dumps({
        "image": "custom:abc", "opts": q.resolved_options({"machine": "pc"}),
    }))
    spec = NodeSpec(
        node_id="n1", lab_id="l1", name="a", image="custom:abc",
        env={}, cmd=None, cpu_limit=None, ram_mb=None, nic_model=None,
        qemu_opts={"machine": "q35", "viommu": True, "cpu": "host"},
    )
    await q.QemuRuntime().sync_from_spec(RuntimeHandle(node_id="n1", ref=str(vm_dir)), spec)
    opts = json.loads(cfg_path.read_text())["opts"]
    assert opts["machine"] == "q35"
    assert opts["viommu"] is True
    assert opts["cpu"] == "host"
    # Resolved over the defaults, the same shape create() writes.
    assert opts["usb_tablet"] is True
    assert q.machine_args(opts)[1] == "q35,kernel-irqchip=split"
