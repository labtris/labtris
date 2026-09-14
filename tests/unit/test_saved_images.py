"""Saving a configured node as a reusable image.

EVE-NG does this with `qemu-img commit`, merging a node's overlay down into the
base image it sits on. That is the wrong move here, and these tests pin the two
reasons why:

  * one base file backs every node using that image, across every lab and every
    user, so committing rewrites disks underneath running guests; and
  * the cache is content-addressed, so a base whose bytes no longer match the
    digest in its own filename makes every later "is this cached?" wrong.

`qemu-img convert` has neither problem, so the first test is simply that the
command we run is convert.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from labtris_api.errors import ApiError
from labtris_api.runtime import qemu


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(qemu, "_cache_dir", lambda: d)
    return d


@pytest.fixture
def vm_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vms"
    monkeypatch.setattr(qemu, "_vm_dir", lambda node_id: root / node_id)
    return root


def _fake_run(recorder: list[list[str]], payload: bytes = b"flattened") -> Any:
    async def run(*args: str, timeout: float | None = None) -> tuple[int, str]:
        recorder.append(list(args))
        # convert's last argument is its destination; produce it so the hash
        # and rename that follow have something real to work on.
        Path(args[-1]).write_bytes(payload)
        return 0, ""

    return run


def test_it_converts_rather_than_commits(
    cache: Path, vm_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (vm_dir / "n1").mkdir(parents=True)
    (vm_dir / "n1" / "disk.qcow2").write_bytes(b"overlay")
    calls: list[list[str]] = []
    monkeypatch.setattr(qemu, "_run", _fake_run(calls))

    result = asyncio.run(qemu.flatten_node_disk("n1"))

    assert len(calls) == 1
    assert calls[0][:2] == ["qemu-img", "convert"]
    # The one command that must never appear: it would mutate the shared base.
    assert "commit" not in calls[0]
    # And the source is the node's own overlay, not the base it points at.
    assert calls[0][-2].endswith("n1/disk.qcow2")
    assert result["image"].startswith("custom:")


def test_the_name_is_the_content_hash(
    cache: Path, vm_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved images are addressed the same way downloaded ones are, so the
    filename keeps being a claim about the bytes that anyone can check."""
    (vm_dir / "n1").mkdir(parents=True)
    (vm_dir / "n1" / "disk.qcow2").write_bytes(b"overlay")
    monkeypatch.setattr(qemu, "_run", _fake_run([], payload=b"known-bytes"))

    result = asyncio.run(qemu.flatten_node_disk("n1"))

    expected = hashlib.sha256(b"known-bytes").hexdigest()[:16]
    assert result["digest"] == expected
    assert result["image"] == f"custom:{expected}"
    assert (cache / f"custom-{expected}.qcow2").read_bytes() == b"known-bytes"


def test_saving_identical_bytes_twice_stores_one_file(
    cache: Path, vm_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people saving the same untouched appliance should not cost two
    multi-GB files."""
    for node in ("n1", "n2"):
        (vm_dir / node).mkdir(parents=True)
        (vm_dir / node / "disk.qcow2").write_bytes(b"overlay")
    monkeypatch.setattr(qemu, "_run", _fake_run([], payload=b"same"))

    first = asyncio.run(qemu.flatten_node_disk("n1"))
    second = asyncio.run(qemu.flatten_node_disk("n2"))

    assert first["image"] == second["image"]
    assert sorted(p.name for p in cache.glob("*.qcow2")) == [
        f"custom-{first['digest']}.qcow2"
    ]
    # No half-written temporary left behind by the deduplicated second save.
    assert list(cache.glob("flatten-*")) == []


def test_a_node_with_no_disk_is_refused(vm_dir: Path, cache: Path) -> None:
    """Saving a node that has never booted would flatten nothing at all."""
    with pytest.raises(ApiError) as err:
        asyncio.run(qemu.flatten_node_disk("never-started"))
    assert "start it at least once" in err.value.message


def test_a_failed_convert_leaves_nothing_behind(
    cache: Path, vm_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial file left in the cache would be indistinguishable from a good
    one at the next start, which is how a corrupt template gets trusted."""
    (vm_dir / "n1").mkdir(parents=True)
    (vm_dir / "n1" / "disk.qcow2").write_bytes(b"overlay")

    async def failing(*args: str, timeout: float | None = None) -> tuple[int, str]:
        Path(args[-1]).write_bytes(b"half")
        return 1, "No space left on device"

    monkeypatch.setattr(qemu, "_run", failing)

    with pytest.raises(ApiError):
        asyncio.run(qemu.flatten_node_disk("n1"))
    assert list(cache.iterdir()) == []


def test_a_saved_image_resolves_to_the_cache(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = cache / "custom-abc123.qcow2"
    target.write_bytes(b"disk")
    assert asyncio.run(qemu._resolve_base_image("custom:abc123")) == target


def test_a_saved_image_that_vanished_says_so(cache: Path) -> None:
    """The cache can be cleared or moved. The message has to name that, because
    "not found" on an id the palette just offered reads as a bug."""
    with pytest.raises(ApiError) as err:
        asyncio.run(qemu._resolve_base_image("custom:missing"))
    detail = err.value.message
    assert "image cache" in detail
    assert "custom:missing" in detail
