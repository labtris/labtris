"""A VM that dies at launch used to be indistinguishable from one that came up.

QEMU creates its chardev sockets before it finishes validating its arguments,
so a VM that exited on a missing disk or a taken VNC port still left mon.sock
behind. start() saw the file, called it success, and the first monitor command
failed several layers later with a bare ConnectionRefusedError — which escaped
as a plain-text 500 that the browser then reported as a JSON parse error. The
reason was in qemu.log the whole time and nothing read it.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from pathlib import Path

import pytest

from labtris_api.errors import ApiError
from labtris_api.runtime.qemu import (
    _await_monitor,
    _hmp,
    _last_qemu_error,
    _rebase_if_orphaned,
)


class _DeadProc:
    returncode = 1


class _LiveProc:
    returncode = None


async def test_monitor_wait_gives_up_when_qemu_exits(tmp_path: Path) -> None:
    mon = tmp_path / "mon.sock"
    mon.touch()  # the file QEMU left behind on its way out

    assert await _await_monitor(mon, _DeadProc()) is False


async def test_monitor_wait_succeeds_once_something_listens(tmp_path: Path) -> None:
    mon = tmp_path / "mon.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(mon))
    server.listen(1)
    try:
        assert await _await_monitor(mon, _LiveProc()) is True
    finally:
        server.close()


def test_the_reported_reason_is_qemus_own_words(tmp_path: Path) -> None:
    (tmp_path / "qemu.log").write_text(
        "[W] pw.conf | can't load config client.conf: No such file or directory\n"
        "qemu-system-x86_64: -vnc 127.0.0.1:4: Failed to find an available port: "
        "Address already in use\n"
    )

    assert "Failed to find an available port" in _last_qemu_error(tmp_path)


def test_a_stale_monitor_socket_is_an_api_error_not_a_traceback(tmp_path: Path) -> None:
    """The socket file outlives the process, so its presence proves nothing."""
    mon = tmp_path / "mon.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(mon))
    dead.close()  # file stays, nobody listens — connect() gets ECONNREFUSED
    (tmp_path / "qemu.log").write_text("qemu-system-x86_64: could not open disk\n")

    with pytest.raises(ApiError) as exc:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _hmp(tmp_path, "info status")
        )

    assert exc.value.status == 502
    assert "stale" in exc.value.message
    assert "could not open disk" in exc.value.message


def _qcow2(path: Path, backing: Path | None = None) -> None:
    cmd = ["qemu-img", "create", "-f", "qcow2"]
    if backing is not None:
        cmd += ["-b", str(backing), "-F", "qcow2"]
    cmd += [str(path)]
    if backing is None:
        cmd += ["1M"]
    subprocess.run(cmd, capture_output=True, check=True)


def _backing_of(disk: Path) -> str | None:
    out = subprocess.run(
        ["qemu-img", "info", "--output=json", str(disk)], capture_output=True, check=True
    )
    return json.loads(out.stdout).get("backing-filename")


@pytest.mark.skipif(
    subprocess.run(["which", "qemu-img"], capture_output=True).returncode != 0,
    reason="qemu-img not installed",
)
def test_a_moved_base_image_is_repointed_not_left_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming the project moved the image cache from ~/.cache/pnl to
    ~/.cache/labtris, and every overlay created before the move still named the
    old path. The base is content-addressed, so the same filename in the new
    cache is the same bytes."""
    old_cache = tmp_path / "old-cache"
    new_cache = tmp_path / "new-cache"
    old_cache.mkdir()
    new_cache.mkdir()
    base_name = "deadbeef12345678.qcow2"
    _qcow2(old_cache / base_name)

    vm_dir = tmp_path / "vm"
    vm_dir.mkdir()
    _qcow2(vm_dir / "disk.qcow2", backing=old_cache / base_name)

    # the cache moves, exactly as the rename did
    (old_cache / base_name).rename(new_cache / base_name)
    assert not Path(_backing_of(vm_dir / "disk.qcow2") or "").exists()

    monkeypatch.setattr("labtris_api.runtime.qemu._cache_dir", lambda: new_cache)
    _rebase_if_orphaned(vm_dir)

    assert _backing_of(vm_dir / "disk.qcow2") == str(new_cache / base_name)
    subprocess.run(["qemu-img", "check", str(vm_dir / "disk.qcow2")], check=True,
                   capture_output=True)


def test_a_disk_whose_base_is_simply_gone_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a *moved* base is safe to repoint. If nothing in the cache matches,
    silently rebasing onto something else would corrupt the guest."""
    vm_dir = tmp_path / "vm"
    vm_dir.mkdir()
    missing = tmp_path / "nowhere" / "cafebabe.qcow2"
    _qcow2(tmp_path / "base.qcow2")
    _qcow2(vm_dir / "disk.qcow2", backing=tmp_path / "base.qcow2")
    subprocess.run(
        ["qemu-img", "rebase", "-u", "-F", "qcow2", "-b", str(missing),
         str(vm_dir / "disk.qcow2")],
        capture_output=True, check=True,
    )
    empty = tmp_path / "empty-cache"
    empty.mkdir()
    monkeypatch.setattr("labtris_api.runtime.qemu._cache_dir", lambda: empty)

    _rebase_if_orphaned(vm_dir)

    assert _backing_of(vm_dir / "disk.qcow2") == str(missing)
