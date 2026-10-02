"""The host limits a large lab needs, shipped rather than applied by hand.

Every value here was reached by hitting the stock one on a real fabric. The
defaults do not fail loudly: inotify exhaustion surfaces as "failed to create
shim task" and a full neighbour table surfaces as a routing bug, so a host
that has not been tuned looks broken in ways that point somewhere else.
"""

from __future__ import annotations

import re
from pathlib import Path

CONF = Path("packaging/sysctl/99-labtris-scale.conf")
INSTALLER = Path("packaging/install-labtris.sh")


def _values() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in CONF.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = int(v.strip())
    return out


def test_the_limits_that_actually_broke_are_raised() -> None:
    v = _values()

    # 128 is the stock value and the first hard wall at a few hundred nodes.
    assert v["fs.inotify.max_user_instances"] >= 8192
    # 1024 is stock; a few thousand links overflow it and resolution dies.
    assert v["net.ipv4.neigh.default.gc_thresh3"] >= 65536
    assert v["net.ipv6.neigh.default.gc_thresh3"] >= 65536


def test_the_neighbour_thresholds_are_ordered() -> None:
    """gc_thresh1 < gc_thresh2 < gc_thresh3, or the kernel garbage-collects
    against a threshold it has already passed."""
    v = _values()
    for fam in ("ipv4", "ipv6"):
        t1 = v[f"net.{fam}.neigh.default.gc_thresh1"]
        t2 = v[f"net.{fam}.neigh.default.gc_thresh2"]
        t3 = v[f"net.{fam}.neigh.default.gc_thresh3"]
        assert t1 < t2 < t3, f"{fam}: {t1}/{t2}/{t3}"


def test_rmem_max_is_deliberately_not_raised() -> None:
    """zebra gets its large netlink buffer through SO_RCVBUFFORCE, which
    bypasses this cap — measured at 8 MiB default against a 4 MiB rmem_max.
    Raising it would imply it mattered."""
    assert "net.core.rmem_max" not in _values()
    assert "SO_RCVBUFFORCE" in CONF.read_text()


def test_the_installer_ships_and_applies_it() -> None:
    src = INSTALLER.read_text()

    assert "packaging/sysctl/99-labtris-scale.conf" in src
    assert "/etc/sysctl.d/99-labtris-scale.conf" in src
    assert "sysctl -q --system" in src


def test_the_installer_makes_the_hostname_resolve() -> None:
    """Without it every sudo pays a DNS timeout, and the box feels broken
    under load for no visible reason."""
    src = INSTALLER.read_text()

    assert "getent hosts" in src
    assert "/etc/hosts" in src
    assert "127.0.1.1" in src


def test_every_setting_carries_its_reason() -> None:
    """A tuning file without the failure it prevents is a file nobody dares
    change later."""
    text = CONF.read_text()
    for term in ("shim task", "neighbor table overflow", "conntrack"):
        assert term in text, term
    assert re.search(r"^#", text, re.M)
