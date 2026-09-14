"""The node console has to show the boot, and hold nothing on disk.

Two constraints that pull against each other. QEMU's serial socket discards
everything written while nobody is connected, so a console attached after the
guest starts sees a blank screen — the kernel messages someone actually wanted
are gone. The obvious fix is a log file, which was rejected: consoles should
live in the socket, not on the disk of a host running a hundred of them.

So the guest is held at the starting line until the session attaches. Nothing
is lost and nothing is stored.
"""

from __future__ import annotations

import re
from pathlib import Path

QEMU = (Path(__file__).resolve().parents[2] / "labtris_api" / "runtime" / "qemu.py").read_text()


def test_qemu_waits_for_the_console_before_booting_the_guest() -> None:
    """wait=off boots immediately and throws away everything printed before a
    client connects, which is the whole kernel boot. Measured: 51 KB of output
    from `[0.000000]` onwards, all of it lost."""
    serial = re.search(r'f"unix:\{serial_sock\},server=on,wait=(\w+)"', QEMU)

    assert serial, "the serial socket argument moved"
    assert serial.group(1) == "on", "wait=off drops boot output before anyone attaches"


def test_the_console_attaches_before_the_monitor_handshake() -> None:
    """Ordering is the fix, not an optimisation. The monitor handshake takes
    seconds; under the old order those were seconds of discarded boot output.
    Attaching first is also what releases QEMU to start the guest at all."""
    attach = QEMU.index("session = SerialSession(serial_sock)")
    monitor = QEMU.index("if not await _await_monitor(mon_sock, proc):")

    assert attach < monitor, "the console attaches too late and the boot is lost"


def test_the_console_attaches_exactly_once() -> None:
    """It used to be created after the monitor. Moving it left a second
    creation behind, which would have replaced a live session with a fresh
    empty one and lost the history it had just captured."""
    assert QEMU.count("session = SerialSession(serial_sock)") == 1


def test_nothing_about_the_console_is_written_to_disk() -> None:
    """Explicitly rejected: a host running a hundred nodes should not be
    writing a hundred console logs. The buffer lives in the session."""
    assert "serial.log" not in QEMU
    assert "logfile=" not in QEMU


def test_the_buffer_is_bounded_by_bytes_not_by_chunk_count() -> None:
    """It was deque(maxlen=4000) of chunks up to 4 KB — 16 MB per node, 1.6 GB
    across a hundred. That was survivable when a file also existed; as the only
    store it is not."""
    assert "_history_max" in QEMU
    assert "deque(maxlen=4000)" not in QEMU

    cap = re.search(r"_history_max = (\d+) \* 1024", QEMU)
    assert cap and int(cap.group(1)) <= 1024, "per-node console history is unbounded in practice"


def test_the_oldest_output_is_dropped_not_the_newest() -> None:
    """A console that keeps the first 256 KB and discards everything since is
    worse than useless — it shows a boot that finished an hour ago."""
    assert "self.history.popleft()" in QEMU


def test_graphical_guests_are_left_alone() -> None:
    """Their boot never reaches ttyS0 — it is pixels on a framebuffer — so
    none of this applies to them, and the catalog already records which is
    which."""
    catalog_flag = "graphical: bool"

    assert catalog_flag in QEMU, "the graphical distinction has gone from the catalog"
