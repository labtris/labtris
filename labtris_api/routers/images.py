"""Bring-your-own image uploads.

Users have qcow2 disks that Labtris's catalog doesn't know about — vendor
appliances, custom builds, images copied from another host. This endpoint
lets them upload one and land in the same shape as a saved-template flow:
`custom-<sha>.qcow2` in the image cache, a `Template` row in the DB, a
line in the "New node → image" picker.

Uploads can be gigabytes; streaming to disk in 4 MB chunks keeps the API's
memory footprint flat instead of buffering the whole thing (see the
`await file.read()` in routers/backup.py:90 — the shape we deliberately
avoid here).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, require_admin
from labtris_api.db import get_session
from labtris_api.errors import bad_request, conflict, runtime_error
from labtris_api.lifecycle import new_id
from labtris_api.models import Template
from labtris_api.runtime.qemu import (
    _bios_dir,
    _cache_dir,
    _cdrom_dir,
    _ensure_qcow2,
    _install_custom,
    _sha256,
)
from labtris_api.schemas import TemplateOut

router = APIRouter(tags=["images"])


_CHUNK = 4 * 1024 * 1024


@router.post("/images", response_model=TemplateOut, status_code=201)
async def upload_image(
    file: Annotated[UploadFile, File(description="qcow2 or other disk image")],
    name: Annotated[str, Form(description="display name for the new template")],
    runtime: Annotated[str, Form()] = "qemu",
    ram_mb: Annotated[int, Form()] = 256,
    cpus: Annotated[int, Form()] = 1,
    nic_model: Annotated[str, Form()] = "virtio-net-pci",
    disk_bus: Annotated[str, Form()] = "virtio",
    iface_scheme: Annotated[str, Form()] = "ens",
    graphical: Annotated[bool, Form()] = False,
    family: Annotated[str | None, Form()] = None,
    family_label: Annotated[str | None, Form()] = None,
    version: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    icon: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_admin),
) -> Template:
    """Take an uploaded disk image, normalise to qcow2, and register it as a
    Template. Admin only — a multi-GB write is not something the guest role
    should be able to trigger.

    ISO handling is deliberately not here yet: a bootable ISO needs `-cdrom`
    in the QEMU command and a scratch qcow2 for the install target, and
    that's a separate design. Refused with a pointer at the qcow2 path."""
    if runtime != "qemu":
        # Docker templates would need a registry reference, not a file upload.
        raise bad_request("only qemu images are supported by upload today")
    lower = (file.filename or "").lower()
    if lower.endswith(".iso"):
        raise bad_request(
            "iso uploads are not supported yet — install the ISO once locally, "
            "then upload the resulting qcow2"
        )

    # A Template row with this name already existing is the one preflight
    # check worth doing before writing 4 GB to disk. Everything else can wait
    # for the row insert to fail-and-clean-up.
    existing = await session.execute(select(Template).where(Template.name == name))
    if existing.scalar_one_or_none() is not None:
        raise conflict(f"template name {name!r} already exists")

    cache = _cache_dir()
    upload_tmp = cache / f"upload-{new_id()}"
    normalised_tmp = cache / f"upload-{new_id()}.qcow2"
    try:
        # Stream to disk rather than buffering — a 4 GB image would otherwise
        # sit in RAM twice (Starlette's UploadFile + the read()) and OOM the
        # API on a modest host.
        with upload_tmp.open("wb") as fh:
            while chunk := await file.read(_CHUNK):
                fh.write(chunk)
        await _ensure_qcow2(upload_tmp, normalised_tmp)
        stored = await _install_custom(normalised_tmp)
    except Exception:
        for stray in (upload_tmp, normalised_tmp):
            stray.unlink(missing_ok=True)
        raise

    spec: dict[str, Any] = {
        "origin": "uploaded",
        "bytes": stored["bytes"],
        "ram_mb": ram_mb,
        "cpus": cpus,
        "nic_model": nic_model,
        "disk_bus": disk_bus,
        "iface_scheme": iface_scheme,
        "graphical": graphical,
    }
    if family:
        spec["family"] = family
    if family_label:
        spec["family_label"] = family_label
    if version:
        spec["version"] = version
    if description:
        spec["description"] = description

    tmpl = Template(
        id=new_id(),
        name=name,
        runtime=runtime,
        image=stored["image"],
        cmd=None,
        env={},
        icon=icon,
        spec=spec,
        description=description,
    )
    session.add(tmpl)
    try:
        await session.commit()
    except IntegrityError as exc:
        # A concurrent upload could have raced and stolen the name after the
        # preflight check. The image bytes are already in the cache under
        # their content-addressed name — leave them; a future upload with the
        # same bytes will dedup onto them.
        await session.rollback()
        raise conflict(f"template name {name!r} already exists") from exc
    except Exception as exc:
        await session.rollback()
        raise runtime_error(f"registering uploaded image: {exc}") from exc
    await session.refresh(tmpl)
    return tmpl


@router.post("/images/companion", status_code=201)
async def upload_companion(
    file: Annotated[UploadFile, File(description="BIOS blob or CD-ROM ISO")],
    kind: Annotated[str, Form(description="'bios' or 'cdrom'")],
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Store a companion file for one or more templates. Content-addressed
    into ~/.cache/labtris/qemu-bios/ or qemu-cdrom/ so uploading the same
    bytes twice is a no-op (matches how the disk cache dedups). Admin-only
    for the same reason `/images` is: a multi-GB write should not be
    triggerable by anyone with a browser.

    Returns `{kind, path, sha256, size}`. The caller (usually the edit-
    template modal, or the labtris-image companion CLI) then PATCHes a
    template with `bios: <path>` or `cdrom: <path>`, and the runtime
    picks it up on next start via sync_from_spec.
    """
    kind = kind.strip().lower()
    if kind not in ("bios", "cdrom"):
        raise bad_request(f"kind must be 'bios' or 'cdrom', not {kind!r}")

    target_dir = _bios_dir() if kind == "bios" else _cdrom_dir()
    # Extension is the caller's original — we keep it so `ls` on the
    # cache dir tells you what things are. Content-hash is the identity.
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()[:8]

    tmp = target_dir / f"upload-{new_id()}{ext}"
    try:
        with tmp.open("wb") as fh:
            while chunk := await file.read(_CHUNK):
                fh.write(chunk)
        digest = await _sha256(tmp)
        final = target_dir / f"{digest}{ext}"
        if final.exists():
            # Same bytes already registered — drop the upload copy.
            tmp.unlink(missing_ok=True)
        else:
            tmp.rename(final)
        size = final.stat().st_size
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return {
        "kind": kind,
        "path": str(final),
        "sha256": digest,
        "size": size,
    }
