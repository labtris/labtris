"""UEFI with a variable store per node, and option values checked when set.

A template's BIOS companion is passed as a read-only -bios, so a combined
OVMF.fd keeps its variables in a file nobody may write: every boot entry a
guest creates (an installer, efibootmgr) is gone at the next start, and only
images with the fallback \\EFI\\BOOT\\BOOTX64.EFI boot (fluxos, 2026-11-11).
firmware=uefi boots split OVMF code with a writable qcow2 store per node.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from labtris_api.errors import ApiError
from labtris_api.runtime import qemu as q


def _fake_ovmf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    files = {}
    for kind in ("uefi", "uefi-secure"):
        code = tmp_path / f"{kind}-CODE.fd"
        tmpl = tmp_path / f"{kind}-VARS.fd"
        code.write_bytes(b"c")
        tmpl.write_bytes(kind.encode())
        files[kind] = tmpl
        monkeypatch.setitem(q._OVMF, kind, [(str(code), str(tmpl))])

    async def fake_run(*args: str, timeout: float | None = None) -> tuple[int, str]:
        assert args[:6] == ("qemu-img", "convert", "-f", "raw", "-O", "qcow2"), args
        Path(args[7]).write_bytes(b"qcow2 of " + Path(args[6]).read_bytes())
        return 0, ""

    monkeypatch.setattr(q, "_run", fake_run)
    return files


def test_blank_firmware_is_the_old_command_line(tmp_path: Path) -> None:
    opts = q.resolved_options({})
    assert opts["firmware"] == ""
    assert asyncio.run(q.firmware_args(opts, tmp_path, None)) == []
    bios = tmp_path / "OVMF.fd"
    assert asyncio.run(q.firmware_args(opts, tmp_path, bios)) == ["-bios", str(bios)]
    assert not (tmp_path / q.EFIVARS).exists()


def test_uefi_gets_writable_qcow2_vars_next_to_read_only_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_ovmf(tmp_path, monkeypatch)
    vm = tmp_path / "vm"
    vm.mkdir()
    args = asyncio.run(q.firmware_args({"firmware": "uefi"}, vm, None))
    store = vm / q.EFIVARS
    assert args == [
        "-drive", f"if=pflash,unit=0,format=raw,readonly=on,file={tmp_path / 'uefi-CODE.fd'}",
        "-drive", f"if=pflash,unit=1,format=qcow2,file={store}",
    ]
    assert store.read_bytes() == b"qcow2 of uefi"


def test_the_store_is_kept_across_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of it: what the guest wrote is still there next boot."""
    _fake_ovmf(tmp_path, monkeypatch)
    vm = tmp_path / "vm"
    vm.mkdir()
    asyncio.run(q.firmware_args({"firmware": "uefi"}, vm, None))
    (vm / q.EFIVARS).write_bytes(b"guest wrote a boot entry")
    asyncio.run(q.firmware_args({"firmware": "uefi"}, vm, None))
    assert (vm / q.EFIVARS).read_bytes() == b"guest wrote a boot entry"


def test_switching_to_secure_boot_starts_a_fresh_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_ovmf(tmp_path, monkeypatch)
    vm = tmp_path / "vm"
    vm.mkdir()
    asyncio.run(q.firmware_args({"firmware": "uefi"}, vm, None))
    (vm / q.EFIVARS).write_bytes(b"entries for the non-secure keys")
    args = asyncio.run(q.firmware_args({"firmware": "uefi-secure", "machine": "q35"}, vm, None))
    assert args[:2] == ["-global", "driver=cfi.pflash01,property=secure,value=on"]
    assert (vm / q.EFIVARS).read_bytes() == b"qcow2 of uefi-secure"


def test_uefi_overrides_a_companion_bios(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ovmf(tmp_path, monkeypatch)
    vm = tmp_path / "vm"
    vm.mkdir()
    args = asyncio.run(q.firmware_args({"firmware": "uefi"}, vm, tmp_path / "OVMF.fd"))
    assert "-bios" not in args


def test_a_host_without_ovmf_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(q._OVMF, "uefi", [("/nonexistent/CODE.fd", "/nonexistent/VARS.fd")])
    with pytest.raises(ApiError, match="install the ovmf"):
        q.ovmf_files("uefi")


def test_secure_boot_needs_q35_and_smm() -> None:
    assert q.machine_args({"machine": "q35", "firmware": "uefi-secure"}) == ["-M", "q35,smm=on"]
    assert q.machine_args({"machine": "q35", "firmware": "uefi-secure", "viommu": True})[:2] == [
        "-M", "q35,smm=on,kernel-irqchip=split"]
    assert q.machine_args({"machine": "pc", "firmware": "uefi"}) == ["-M", "pc"]
    with pytest.raises(ApiError, match="q35"):
        q.machine_args({"machine": "pc", "firmware": "uefi-secure"})


@pytest.mark.parametrize("opts,msg", [
    ({"firmware": "uefii"}, "firmware='uefii' is not one of"),
    ({"machine": "q36"}, "machine='q36'"),
    ({"viommu": "yes"}, "viommu must be true or false"),
    ({"hotplug_slots": 99}, "between 0 and 32"),
    ({"hotplug_slots": "4"}, "whole number"),
    ({"firmware": "uefi-secure"}, "needs machine=q35"),
])
def test_bad_values_are_refused_when_set(opts: dict, msg: str) -> None:
    with pytest.raises(ApiError, match=msg):
        q.check_option_values(opts)


def test_good_values_pass() -> None:
    q.check_option_values({"firmware": "uefi-secure", "machine": "q35", "hotplug_slots": 0,
                           "viommu": False, "cpu": "host", "extra_args": []})
    # Keys it does not know are someone else's business (load_snapshot_on_boot).
    q.check_option_values({"load_snapshot_on_boot": "pod-1"})
