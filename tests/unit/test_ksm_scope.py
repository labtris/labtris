"""KSM reports whether container nodes are actually included.

"enabled: true, scan 1250/10ms" was true on a host merging exactly zero
pages, because KSM only merges memory a process marked MADV_MERGEABLE. QEMU
marks guest RAM; nothing marks a container's. Every knob looked right, which
is precisely why it went unnoticed on a 1500-container fabric.
"""

from __future__ import annotations

from pathlib import Path

from labtris_api import diagnose


def _ksm(monkeypatch, *, run=1, sharing=0, shared=0, optedin=False):  # noqa: ANN001, ANN202
    values = {
        "run": run, "pages_sharing": sharing, "pages_shared": shared,
        "pages_to_scan": 1250, "sleep_millisecs": 10, "smart_scan": 1,
    }

    class FakePath:
        def __init__(self, p: str) -> None:
            self._p = str(p)

        def is_dir(self) -> bool:
            return self._p == "/sys/kernel/mm/ksm"

        def is_file(self) -> bool:
            return optedin and "labtris-ksm.conf" in self._p

        def __truediv__(self, other: str) -> FakePath:
            return FakePath(f"{self._p}/{other}")

        def read_text(self) -> str:
            return str(values[self._p.rsplit("/", 1)[-1]])

    monkeypatch.setattr(diagnose, "Path", FakePath)
    return diagnose._ksm()  # noqa: SLF001


def test_on_but_qemu_only_says_so(monkeypatch) -> None:
    f = _ksm(monkeypatch, run=1, sharing=0, shared=0, optedin=False)

    assert f["enabled"] is True
    assert f["containers_included"] is False
    assert "container memory is never offered" in f["note"]


def test_opted_in_and_merging_reports_no_warning(monkeypatch) -> None:
    f = _ksm(monkeypatch, run=1, sharing=440_000, shared=16_000, optedin=True)

    assert f["containers_included"] is True
    assert f["ratio"] == 27.5  # 440_000 / 16_000
    assert "note" not in f, f.get("note")


def test_opted_in_but_nothing_merged_yet_is_distinguished(monkeypatch) -> None:
    """Different from "qemu only": the scanner just has not got there."""
    f = _ksm(monkeypatch, run=1, sharing=0, shared=0, optedin=True)

    assert f["containers_included"] is True
    assert "nothing merged yet" in f["note"]


def test_off_still_reports_off_first(monkeypatch) -> None:
    f = _ksm(monkeypatch, run=0, optedin=True)

    assert f["enabled"] is False
    assert "KSM is OFF" in f["note"]


def test_the_drop_in_path_is_the_one_the_installer_writes() -> None:
    """If these drift, diagnostics reports "qemu only" on a host that is in
    fact deduplicating containers — a false warning is as bad as none."""
    installer = Path("packaging/install-labtris.sh").read_text()
    assert "/etc/systemd/system/$_unit.service.d/labtris-ksm.conf" in installer
    src = Path("labtris_api/diagnose.py").read_text()
    assert "/etc/systemd/system/{unit}.service.d/labtris-ksm.conf" in src
    for unit in ("containerd", "docker"):
        assert f"_ksm_drop_in {unit}" in installer
