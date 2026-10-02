"""Snapshot success and failure, told apart correctly.

The monitor used to be read over its raw socket, which echoes back what you
"type" one character at a time with cursor-movement escapes in between. The
reply therefore contained the command as well as its answer, and success was
decided with `"error" in reply.lower()` — so `savevm before-error-repro`
raised "savevm failed" on a snapshot that had in fact been written. These
tests pin both directions.
"""

from __future__ import annotations

import pytest

from labtris_api.errors import ApiError
from labtris_api.runtime.base import RuntimeHandle
from labtris_api.runtime.qemu import _hmp_error, qemu_runtime


def test_a_name_containing_error_is_not_a_failure() -> None:
    """The reported bug, at the level that got it wrong."""
    assert _hmp_error("") is None
    assert _hmp_error("savevm before-error-repro") is None
    assert _hmp_error("Error: Snapshot 'x' does not exist in one or more devices") == (
        "Error: Snapshot 'x' does not exist in one or more devices"
    )


def test_an_error_anywhere_in_the_reply_is_found() -> None:
    """QEMU prints the error on its own line, not necessarily the first."""
    assert _hmp_error("some preamble\nError: Device 'x' is writable but does not "
                      "support snapshots\n") is not None
    #: Matched at the start of a line, so prose mentioning the word is not one.
    assert _hmp_error("this mentions an error in passing") is None


async def test_save_snapshot_raises_on_a_real_error(monkeypatch) -> None:
    async def fake(vm_dir, command, timeout=10.0):
        return "Error: Device 'virtio0' does not support snapshots\n"

    monkeypatch.setattr("labtris_api.runtime.qemu._hmp_qmp", fake)
    with pytest.raises(ApiError) as exc:
        await qemu_runtime.save_snapshot(RuntimeHandle(node_id="n", ref="/tmp/x"), "s1")
    assert "does not support snapshots" in str(exc.value)
    #: and the message is the error itself, not a screenful of escape codes
    assert "\x1b" not in str(exc.value)


async def test_save_snapshot_accepts_a_name_with_error_in_it(monkeypatch) -> None:
    seen: dict[str, str] = {}

    async def fake(vm_dir, command, timeout=10.0):
        seen["command"] = command
        return ""           # savevm says nothing when it works

    monkeypatch.setattr("labtris_api.runtime.qemu._hmp_qmp", fake)
    await qemu_runtime.save_snapshot(
        RuntimeHandle(node_id="n", ref="/tmp/x"), "before-error-repro"
    )
    assert seen["command"] == "savevm before-error-repro"


async def test_snapshotting_waits_rather_than_sleeping(monkeypatch) -> None:
    """pods.py flattens the same qcow2 the moment this returns, so a save that
    reports success early hands the archiver a half-written disk."""
    from labtris_api.runtime import qemu

    captured: dict[str, float] = {}

    async def fake_qmp(vm_dir, command, arguments=None, timeout=3.0):
        captured["timeout"] = timeout
        captured["command_line"] = (arguments or {}).get("command-line", "")
        return ""

    monkeypatch.setattr(qemu, "_qmp", fake_qmp)
    await qemu_runtime.save_snapshot(RuntimeHandle(node_id="n", ref="/tmp/x"), "s1")

    #: It goes through human-monitor-command, which returns only once the
    #: command has finished — there is no interval to guess at.
    assert captured["command_line"] == "savevm s1"
    assert captured["timeout"] == qemu._SNAPSHOT_TIMEOUT
