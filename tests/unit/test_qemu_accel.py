"""Which accelerator a VM starts with, and whether that is recorded."""

from __future__ import annotations

import os

from labtris_api.runtime import qemu


def test_explicit_values_pass_through(monkeypatch) -> None:
    assert qemu.resolve_accel("kvm") == "kvm"
    assert qemu.resolve_accel("tcg") == "tcg"


def test_auto_is_kvm_only_when_dev_kvm_is_usable(monkeypatch, tmp_path) -> None:
    """The default used to be a hard "tcg". On a host with hardware
    virtualisation that meant every VM booted ten to fifty times slower than
    it needed to, unless the operator knew the setting existed."""
    fake = tmp_path / "kvm"
    monkeypatch.setattr(qemu, "Path", lambda p: fake if str(p) == "/dev/kvm" else qemu.Path(p))

    monkeypatch.setattr(os, "access", lambda p, m: False)
    assert qemu.resolve_accel("auto") == "tcg", "no device → tcg"

    fake.write_text("")
    monkeypatch.setattr(os, "access", lambda p, m: True)
    assert qemu.resolve_accel("auto") == "kvm", "device present and writable → kvm"

    monkeypatch.setattr(os, "access", lambda p, m: False)
    assert qemu.resolve_accel("auto") == "tcg", "present but not writable → tcg, not a crash later"


def test_the_setting_is_the_default_argument(monkeypatch) -> None:
    monkeypatch.setattr(qemu.settings, "qemu_accel", "tcg", raising=False)
    assert qemu.resolve_accel() == "tcg"
