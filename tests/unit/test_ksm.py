"""Kernel Samepage Merging — the capacity lever, and it defaults to off.

docs/04-scaling.md measured 8.5:1 deduplication across fifteen VMs on a
comparable host: ~62 GiB of guest memory backed by ~7.3 GiB of real pages.
That is the difference between roughly twenty labs on a machine and roughly a
hundred and fifty, and stock Ubuntu ships it disabled.

The doc's own conclusion — "it must be explicit configuration, not an accident
of the base image" — is what these tests enforce.
"""

from __future__ import annotations

import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KSM_SH = ROOT / "packaging" / "labtris-ksm.sh"
KSM_UNIT = ROOT / "packaging" / "systemd" / "labtris-ksm.service"
INSTALL_SH = ROOT / "packaging" / "install-labtris.sh"


def test_ksm_is_turned_on_at_boot() -> None:
    """Not left to the base image, which switches it off."""
    assert KSM_SH.exists() and KSM_SH.stat().st_mode & stat.S_IXUSR
    assert KSM_UNIT.exists()
    assert "labtris-ksm" in INSTALL_SH.read_text()


def test_the_measured_values_are_used_not_the_defaults() -> None:
    """100 pages every 20ms is far too gentle: dedup then trails minutes
    behind a lab being started rather than keeping up with it. 1250 every 10ms
    is what the measured host ran."""
    text = KSM_SH.read_text()

    assert "1250" in text, "pages_to_scan left at something other than the measured value"
    assert "LABTRIS_KSM_SLEEP_MS:-10" in text


def test_every_knob_is_optional() -> None:
    """They come and go between kernel versions. A missing smart_scan must not
    fail the unit and leave KSM off altogether — which would trade a small
    optimisation for the entire feature."""
    text = KSM_SH.read_text()

    assert "not available on this kernel" in text
    assert 'ConditionPathIsDirectory=/sys/kernel/mm/ksm' in KSM_UNIT.read_text()


def test_run_is_set_last() -> None:
    """So a failure while tuning leaves KSM off rather than running with
    defaults that scan too gently to be worth the CPU."""
    text = KSM_SH.read_text()

    assert text.index("pages_to_scan") < text.index("set_knob run")


def test_it_is_tunable_from_the_env_file() -> None:
    """docs/04-scaling.md: "make it explicit + tunable"."""
    assert "/etc/labtris/labtris.env" in KSM_SH.read_text()
    assert "LABTRIS_KSM_PAGES_TO_SCAN" in INSTALL_SH.read_text()


def test_ksmtuned_is_installed() -> None:
    """It backs the scan rate off when memory is plentiful and pushes it when
    it is not, on top of the static floor."""
    packages = (ROOT / "packaging" / "packages.txt").read_text()

    assert "ksmtuned" in packages


def test_the_doctor_reports_the_dedup_ratio() -> None:
    """The one number that answers "can this box take another class?" — free
    memory does not, because most of what identical guests occupy is the same
    pages counted many times."""
    diagnose = (ROOT / "labtris_api" / "diagnose.py").read_text()

    assert "pages_sharing" in diagnose
    assert "ratio" in diagnose
    assert "KSM is OFF" in diagnose, "a disabled lever must read as a warning, not a fact"
