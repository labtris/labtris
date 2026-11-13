"""A companion BIOS or CD-ROM that is set but missing is an error, not a shrug.

fluxos, 2026-11-11: deleting a template removed its uploaded OVMF, a PATCH to
another template with the same path was accepted, and at start the missing
file was dropped from the command line, so a UEFI image booted SeaBIOS and
nothing said why.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from labtris_api.config import settings
from labtris_api.errors import ApiError
from labtris_api.routers.templates import patch_template
from labtris_api.runtime.base import RuntimeHandle
from labtris_api.runtime.qemu import QemuRuntime
from labtris_api.schemas import TemplatePatch


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    images = tmp_path / "qemu-images"
    images.mkdir()
    monkeypatch.setattr(settings, "qemu_image_cache_dir", str(images))
    return tmp_path


class _Session:
    def __init__(self, tmpl: object) -> None:
        self.tmpl = tmpl
        self.committed = False

    async def get(self, _model: object, _id: str) -> object:
        return self.tmpl

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _obj: object) -> None:
        pass


async def test_patch_refuses_a_bios_path_with_no_file(cache: Path) -> None:
    tmpl = SimpleNamespace(spec={}, name="t", icon=None, description=None)
    session = _Session(tmpl)
    gone = str(cache / "qemu-bios" / "7104ea75.fd")

    with pytest.raises(ApiError) as exc:
        await patch_template("t1", TemplatePatch(bios=gone), session, None)

    assert exc.value.status == 422
    assert "POST /images/companion" in exc.value.message
    assert tmpl.spec == {} and not session.committed


async def test_patch_accepts_an_uploaded_bios_and_clearing_it(cache: Path) -> None:
    bios = cache / "qemu-bios"
    bios.mkdir()
    (bios / "ok.fd").write_bytes(b"\0")
    tmpl = SimpleNamespace(spec={}, name="t", icon=None, description=None)
    session = _Session(tmpl)

    await patch_template("t1", TemplatePatch(bios=str(bios / "ok.fd")), session, None)
    assert tmpl.spec["bios"] == str(bios / "ok.fd")

    await patch_template("t1", TemplatePatch(bios=None), session, None)
    assert tmpl.spec["bios"] is None


async def test_start_refuses_a_vm_whose_bios_is_gone(cache: Path) -> None:
    vm = cache / "vm"
    vm.mkdir()
    (vm / "config.json").write_text(json.dumps({"bios": str(cache / "qemu-bios" / "x.fd")}))

    with pytest.raises(ApiError) as exc:
        await QemuRuntime().start(RuntimeHandle(node_id="n", ref=str(vm)))

    assert "bios file" in exc.value.message and "missing" in exc.value.message
    assert not (vm / "qemu.pid").exists()
