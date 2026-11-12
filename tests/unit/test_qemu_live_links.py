"""Links on a running QEMU VM: re-pointed live, hot-plugged honestly.

Re-pointing a link on a running VM used to delete and recreate the tap and
ask QEMU to swap the NIC. On q35 the swap never happened (a boot-time NIC on
pcie.0 cannot be hot-unplugged) and the API still said 201, leaving the port
dead (perf-fw, 2026-11-05). These tests fake netd and the QEMU monitor and
check the three paths: a tap that is already the NIC's backend is only
moved; a NIC that has to be plugged goes into a spare slot on q35 and is
checked afterwards; anything that cannot happen live says needs_restart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from labtris_api.errors import ApiError
from labtris_api.runtime import qemu as q
from labtris_api.runtime.base import IfaceSpec, RuntimeHandle


class FakeVM:
    """Taps on the host and NICs in one VM, with the monitor commands that
    change them. `pcie0` lists NICs that sit on a bus that cannot unplug."""

    def __init__(self, machine: str = "q35") -> None:
        self.machine = machine
        self.taps: dict[str, dict[str, Any]] = {}
        self.nics: dict[str, dict[str, Any]] = {}
        self.netdevs: dict[str, str] = {}
        self.ports = {f"lt-hp{n}" for n in range(4)} if machine == "q35" else set()
        self.pcie0: set[str] = set()
        self.calls: list[tuple[str, Any]] = []

    # netd
    async def netd(self, verb: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((verb, args))
        name = args.get("name")
        if verb == "iface.inspect":
            return {"interfaces": {n: ({"exists": True, **self.taps[n]} if n in self.taps
                                       else {"exists": False}) for n in args["names"]}}
        if verb == "tap.create":
            self.taps[name] = {"master": None, "carrier": False, "up": False}
        elif verb == "iface.delete":
            self.taps.pop(name, None)
            # A NIC whose tap is deleted under it keeps the dead fd.
            for nic in self.nics.values():
                if nic["ifname"] == name:
                    nic["dead"] = True
        elif verb == "iface.attach":
            self.taps[name]["master"] = args["bridge"]
        elif verb == "iface.detach":
            self.taps[name]["master"] = None
        elif verb == "iface.set_state":
            if name in self.taps:
                self.taps[name]["up"] = args["up"]
        return {}

    # monitor
    async def hmp(self, vm_dir: Path, cmd: str, **_: Any) -> str:
        self.calls.append(("hmp", cmd))
        if cmd == "info network":
            lines = []
            for nic_id, nic in self.nics.items():
                lines.append(
                    f"{nic_id}: index=0,type=nic,model=virtio-net-pci,macaddr={nic['mac']}"
                )
                if not nic.get("dead"):
                    lines.append(f" \\ {nic['netdev']}: index=0,type=tap,ifname={nic['ifname']},"
                                 "script=no,downscript=no")
            return "\r\n".join(lines)
        if cmd.startswith("device_del "):
            nic_id = cmd.split()[1]
            if nic_id in self.pcie0:
                return ""  # accepted, and nothing ever happens
            nic = self.nics.pop(nic_id, None)
            return "" if nic else f"Error: Device '{nic_id}' not found"
        if cmd.startswith("netdev_del "):
            self.netdevs.pop(cmd.split()[1], None)
            return ""
        if cmd.startswith("netdev_add "):
            kv = dict(p.split("=", 1) for p in cmd.split(" ", 1)[1].split(",")[1:])
            self.netdevs[kv["id"]] = kv["ifname"]
            if kv["ifname"] in self.taps:
                self.taps[kv["ifname"]]["carrier"] = True
            return ""
        if cmd.startswith("device_add "):
            kv = dict(p.split("=", 1) for p in cmd.split(" ", 1)[1].split(",")[1:])
            bus = kv.get("bus")
            if self.machine == "q35" and (bus is None or bus not in self.ports):
                return f"Error: Bus '{bus or 'pcie.0'}' does not support hotplugging"
            self.nics[kv["id"]] = {"netdev": kv["netdev"], "ifname": self.netdevs[kv["netdev"]],
                                   "mac": kv["mac"]}
            return ""
        if cmd.startswith("set_link"):
            return ""
        raise AssertionError(f"unexpected monitor command {cmd!r}")

    def boot_nic(self, idx: int, tap: str, bridge: str | None) -> None:
        """A NIC cold-plugged at boot, on pcie.0 for q35."""
        self.taps[tap] = {"master": bridge, "carrier": True, "up": True}
        self.netdevs[f"net{idx}"] = tap
        self.nics[f"nic{idx}"] = {"netdev": f"net{idx}", "ifname": tap,
                                  "mac": f"52:54:00:00:00:{idx:02x}"}
        if self.machine == "q35":
            self.pcie0.add(f"nic{idx}")

    def monitor_cmds(self) -> list[str]:
        return [c for v, c in self.calls if v == "hmp" and c != "info network"]


@pytest.fixture
def vm(tmp_path, monkeypatch):
    def make(machine: str = "q35", opts: dict[str, Any] | None = None) -> tuple[FakeVM, Path]:
        fake = FakeVM(machine)
        (tmp_path / "config.json").write_text(json.dumps({
            "nic_model": "virtio-net-pci", "opts": {"machine": machine, **(opts or {})},
        }))
        monkeypatch.setattr(q.netd, "call", fake.netd)
        monkeypatch.setattr(q, "_hmp_qmp", fake.hmp)
        monkeypatch.setattr(q, "_hmp", fake.hmp)
        monkeypatch.setattr(q, "_read_pid", lambda _d: 4242)
        monkeypatch.setattr(q, "UNPLUG_WAIT_S", 0.0)
        return fake, tmp_path
    return make


def spec(idx: int, tap: str) -> IfaceSpec:
    return IfaceSpec(iface_id=f"i{idx}", idx=idx, guest_name=f"eth{idx}", host_ifname=tap,
                     mac=f"52:54:00:00:00:{idx:02x}")


async def test_repointing_a_link_moves_the_tap_and_touches_no_device(vm) -> None:
    """perf-fw's case: eth8 moves from one peer's bridge to another's."""
    fake, d = vm("q35")
    fake.boot_nic(8, "vperffeth8", bridge="br-old")
    await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(8, "vperffeth8"), "br-new")
    assert fake.taps["vperffeth8"]["master"] == "br-new"
    assert fake.nics["nic8"]["ifname"] == "vperffeth8"
    assert not fake.nics["nic8"].get("dead")
    verbs = [v for v, _ in fake.calls]
    assert "tap.create" not in verbs and "iface.delete" not in verbs
    assert not [c for c in fake.monitor_cmds() if c.startswith(("device_", "netdev_"))]


async def test_rejoining_an_unwired_port_works_the_same(vm) -> None:
    fake, d = vm("pc")
    fake.boot_nic(3, "tap3", bridge=None)
    await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(3, "tap3"), "br-x")
    assert fake.taps["tap3"]["master"] == "br-x"
    assert not [c for c in fake.monitor_cmds() if c.startswith(("device_", "netdev_"))]


async def test_a_new_nic_on_q35_goes_into_a_spare_slot_and_is_checked(vm) -> None:
    fake, d = vm("q35")
    await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(9, "tap9"), "br-9")
    adds = [c for c in fake.monitor_cmds() if c.startswith("device_add")]
    assert adds and "bus=lt-hp0" in adds[0]
    assert fake.nics["nic9"]["ifname"] == "tap9"
    assert fake.taps["tap9"]["master"] == "br-9"
    assert json.loads((d / "hotplug.json").read_text()) == {"nic9": "lt-hp0"}
    # The next one takes the next slot.
    await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(10, "tap10"), "br-10")
    assert json.loads((d / "hotplug.json").read_text()) == {"nic9": "lt-hp0", "nic10": "lt-hp1"}


async def test_a_new_nic_on_pc_needs_no_slot(vm) -> None:
    fake, d = vm("pc")
    await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(4, "tap4"), "br-4")
    adds = [c for c in fake.monitor_cmds() if c.startswith("device_add")]
    assert adds and "bus=" not in adds[0]
    assert "nic4" in fake.nics


async def test_q35_without_spare_slots_says_restart_not_201(vm) -> None:
    """A VM started before it had slots: device_add fails, the backend is
    taken back out, and the caller hears needs_restart."""
    fake, d = vm("q35")
    fake.ports = set()
    with pytest.raises(ApiError) as exc:
        await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(9, "tap9"), "br-9")
    assert exc.value.code == "needs_restart"
    assert "net9" not in fake.netdevs
    # The tap is still wired, so the next start's cold-plug finds it ready.
    assert fake.taps["tap9"]["master"] == "br-9"


async def test_all_slots_taken_says_restart(vm) -> None:
    fake, d = vm("q35", {"hotplug_slots": 1})
    await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(9, "tap9"), "br-9")
    with pytest.raises(ApiError) as exc:
        await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(10, "tap10"), "br-10")
    assert exc.value.code == "needs_restart"


async def test_a_nic_left_on_a_dead_tap_on_pcie0_says_restart(vm) -> None:
    """The state perf-fw was left in: the guest's NIC holds a tap that was
    deleted. On pcie.0 it cannot be swapped; say so."""
    fake, d = vm("q35")
    fake.boot_nic(6, "tap6", bridge="br-a")
    await fake.netd("iface.delete", {"name": "tap6"})
    await fake.netd("tap.create", {"name": "tap6"})
    with pytest.raises(ApiError) as exc:
        await q.QemuRuntime().attach_iface(RuntimeHandle("n", str(d)), spec(6, "tap6"), "br-b")
    assert exc.value.code == "needs_restart"
    assert fake.taps["tap6"]["master"] == "br-b"


async def test_detach_unplugs_a_hotplugged_nic(vm) -> None:
    fake, d = vm("q35")
    rt = q.QemuRuntime()
    await rt.attach_iface(RuntimeHandle("n", str(d)), spec(9, "tap9"), "br-9")
    await rt.detach_iface(RuntimeHandle("n", str(d)), spec(9, "tap9"))
    assert "nic9" not in fake.nics and "tap9" not in fake.taps
    assert json.loads((d / "hotplug.json").read_text()) == {}


async def test_detach_of_a_boot_nic_on_q35_pulls_the_cable_and_says_restart(vm) -> None:
    fake, d = vm("q35")
    fake.boot_nic(2, "tap2", bridge="br-2")
    with pytest.raises(ApiError) as exc:
        await q.QemuRuntime().detach_iface(RuntimeHandle("n", str(d)), spec(2, "tap2"))
    assert exc.value.code == "needs_restart"
    assert "nic2" in fake.nics
    assert fake.taps["tap2"]["master"] is None
    assert "set_link net2 off" in fake.monitor_cmds()


def test_info_network_is_parsed_including_a_dead_backend() -> None:
    text = (
        "nic0: index=0,type=nic,model=virtio-net-pci,macaddr=52:54:00:00:00:00\r\n"
        " \\ net0: index=0,type=tap,ifname=vperffeth0-abcd,script=no,downscript=no\r\n"
        "nic6: index=0,type=nic,model=virtio-net-pci,macaddr=52:54:00:00:00:06\r\n"
        "hub 0\r\n"
    )
    assert q.parse_info_network(text) == {
        "nic0": {"netdev": "net0", "ifname": "vperffeth0-abcd"},
        "nic6": {"netdev": None, "ifname": None},
    }


def test_spare_slots_only_on_q35_and_last_on_the_command_line() -> None:
    assert q.hotplug_port_args({"machine": "pc"}) == []
    assert q.hotplug_port_args({"machine": "q35", "hotplug_slots": 0}) == []
    args = q.hotplug_port_args({"machine": "q35"})
    ids = [re.search(r"id=([^,]+)", a).group(1) for a in args if a.startswith("pcie-root-port")]
    assert ids == ["lt-hp0", "lt-hp1", "lt-hp2", "lt-hp3"]
    chassis = [re.search(r"chassis=(\d+)", a).group(1) for a in args if "chassis" in a]
    assert len(set(chassis)) == 4
    assert q.QEMU_OPTIONS["hotplug_slots"]["default"] == 4


def test_two_vms_started_back_to_back_get_different_vnc_displays(monkeypatch) -> None:
    # QEMU binds its VNC port only after the serial socket is connected, so a
    # bind test alone handed the same display to two quick starts and the
    # second VM died (labtris-012, 2026-11-12).
    from labtris_api.runtime import qemu

    monkeypatch.setattr(qemu, "_vnc_claims", {})
    a = qemu._pick_free_vnc_display(owner="/vms/a")
    b = qemu._pick_free_vnc_display(owner="/vms/b")
    assert a != b
    # A VM restarting keeps its own claim.
    assert not qemu._claimed_by_other(a, "/vms/a")
    assert qemu._claimed_by_other(a, "/vms/b")
