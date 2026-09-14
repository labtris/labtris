"""The two helpers that back both the upload endpoint and flatten_node_disk.

_install_custom is the "hash a qcow2, dedup, move into cache" step; the
whole "custom:<hash>" scheme rests on it doing exactly the same thing for
every caller so a re-save and a re-upload of the same bytes land on the
same file and the same DB reference.

_ensure_qcow2 is the "normalise to qcow2 (or don't, if it already is)"
step. Called via subprocess; here we mock the subprocess so a test box
does not need qemu-img."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from labtris_api.runtime import qemu as qemu_module
from labtris_api.runtime.qemu import _ensure_qcow2, _install_custom


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    """Redirect the module's cache dir at tmp_path for the test."""
    monkeypatch.setattr(qemu_module, "_cache_dir", lambda: tmp_path)
    return tmp_path


async def test_install_custom_stores_and_returns_content_hash(cache: Path) -> None:
    payload = b"pretend this is a small qcow2"
    tmp = cache / "upload-abc"
    tmp.write_bytes(payload)

    result = await _install_custom(tmp)

    digest = hashlib.sha256(payload).hexdigest()[:16]
    stored = cache / f"custom-{digest}.qcow2"
    assert stored.exists()
    assert result == {
        "image": f"custom:{digest}",
        "digest": digest,
        "bytes": len(payload),
        "path": str(stored),
    }
    assert not tmp.exists(), "temp file was moved, not left behind"


async def test_install_custom_dedupes_matching_bytes(cache: Path) -> None:
    payload = b"identical bytes get one file"
    first = cache / "upload-1"
    second = cache / "upload-2"
    first.write_bytes(payload)
    second.write_bytes(payload)

    r1 = await _install_custom(first)
    r2 = await _install_custom(second)

    assert r1["digest"] == r2["digest"]
    assert r1["path"] == r2["path"]
    # The second call should have removed its own copy — only the shared
    # file remains, no `upload-2` orphan.
    assert not (cache / "upload-2").exists()
    assert list(cache.iterdir()) == [Path(r1["path"])]


async def test_ensure_qcow2_moves_a_qcow2_in_place(cache: Path, monkeypatch) -> None:
    """When the source is already qcow2, _ensure_qcow2 is a rename — no
    subprocess involved. Faster and doesn't rewrite gigabytes."""
    src = cache / "in.qcow2"
    dst = cache / "out.qcow2"
    src.write_bytes(b"already qcow2 bytes")

    async def fake_run(*args):
        # `qemu-img info --output=json <src>` → looks like a qcow2
        if "info" in args:
            return 0, json.dumps({"format": "qcow2"})
        raise AssertionError(f"convert should not have run: {args}")

    monkeypatch.setattr(qemu_module, "_run", fake_run)

    result = await _ensure_qcow2(src, dst)
    assert result == dst
    assert dst.read_bytes() == b"already qcow2 bytes"
    assert not src.exists()


async def test_ensure_qcow2_converts_a_non_qcow2(cache: Path, monkeypatch) -> None:
    """A raw or vmdk source triggers `qemu-img convert -O qcow2`. Assert we
    called it and that the source file is cleaned up either way."""
    src = cache / "in.raw"
    dst = cache / "out.qcow2"
    src.write_bytes(b"raw bytes")
    calls: list[tuple] = []

    async def fake_run(*args):
        calls.append(args)
        if "info" in args:
            return 0, json.dumps({"format": "raw"})
        # Convert: create the destination file to simulate qemu-img's output
        dst.write_bytes(b"converted qcow2")
        return 0, ""

    monkeypatch.setattr(qemu_module, "_run", fake_run)

    result = await _ensure_qcow2(src, dst)
    assert result == dst
    assert dst.read_bytes() == b"converted qcow2"
    assert not src.exists()
    # convert was called, in addition to info
    assert any("convert" in call for call in calls)


async def test_ensure_qcow2_reports_a_failed_convert(cache: Path, monkeypatch) -> None:
    """A convert error must not leave a stray file at `dst` that another
    caller could mistake for a valid image."""
    from labtris_api.errors import ApiError

    src = cache / "in.vmdk"
    dst = cache / "out.qcow2"
    src.write_bytes(b"vmdk bytes")

    async def fake_run(*args):
        if "info" in args:
            return 0, json.dumps({"format": "vmdk"})
        return 1, "some qemu-img error"

    monkeypatch.setattr(qemu_module, "_run", fake_run)

    with pytest.raises(ApiError) as exc:
        await _ensure_qcow2(src, dst)
    assert "qemu-img convert" in str(exc.value)
    assert not dst.exists()
