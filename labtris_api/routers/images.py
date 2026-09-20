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
    QEMU_CATALOG,
    _bios_dir,
    _cache_dir,
    _cdrom_dir,
    _ensure_qcow2,
    _install_custom,
    _resolve_base_image,
    _sha256,
    image_status,
)
from labtris_api.schemas import ImagePullIn, ImageStatusOut, TemplateOut

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


# ------------------------------------------------------------------ pull
#
# Same download machinery `_resolve_base_image` uses when a node starts,
# exposed as an explicit "prefetch this image" call so operators can seed
# the cache before a class or a scripted lab run instead of watching the
# first node's start hang for eight minutes on the download.
#
# One in-flight pull per image is enough — `_resolve_base_image` locks on
# the URL's digest internally, so two concurrent pulls of the same image
# collapse to one. Callers poll GET /images/status?image=... for
# progress.


import asyncio

# One background task per image, kept until it finishes. Nothing here
# needs to survive a restart — a re-request after restart will happily
# start a fresh pull, and _resolve_base_image will find the partial
# `.part` file and resume from where curl left off.
_pulls: dict[str, asyncio.Task[Any]] = {}


def _looks_like_docker_ref(image: str) -> bool:
    """True for anything Docker's registry client would understand.

    QEMU refs go through the catalog / https / custom-<sha> paths;
    Docker refs are `repo`, `repo:tag`, `ns/repo:tag`, `registry/ns/repo:tag`.
    The distinction that matters here: anything with a slash, or the plain
    catalog names that our own docker catalog uses. QEMU catalog ids never
    contain a slash and are checked first, so this is a safe fallback."""
    from labtris_api.runtime.containers import CONTAINER_CATALOG

    if image in QEMU_CATALOG:
        return False
    if image.startswith(("http://", "https://", "custom-")):
        return False
    if "/" in image:
        return True
    # A bare catalog docker image ref (`alpine:3.20`) has no slash but
    # is still docker-shaped. Match it via our container catalog.
    return any(entry.image == image for entry in CONTAINER_CATALOG.values())


async def _do_pull(image: str) -> None:
    try:
        if _looks_like_docker_ref(image):
            from labtris_api.runtime.docker import pull_image_now

            await pull_image_now(image)
        else:
            await _resolve_base_image(image)
    finally:
        _pulls.pop(image, None)


async def _current_status(image: str) -> dict[str, Any]:
    """Runtime-agnostic status snapshot. Docker refs get a live inspect
    probe; QEMU refs stay on the on-disk cache lookup."""
    if _looks_like_docker_ref(image):
        from labtris_api.runtime.docker import is_image_cached

        cached = await is_image_cached(image)
        pulling = image in _pulls
        return {
            "cached": cached,
            "phase": "cached" if cached else ("pulling" if pulling else "not-downloaded"),
            "bytes": 0,
        }
    return image_status(image)


@router.post("/images/pull", response_model=ImageStatusOut, status_code=202)
async def pull_image(
    body: ImagePullIn,
    _user: User = Depends(require_admin),
) -> Any:
    """Fetch a QEMU or Docker image now, without waiting for a node start.

    Accepts a QEMU catalog id (`ubuntu-24.04`), an https URL, a
    `custom-…` reference, OR a Docker image ref (`alpine:3.20`,
    `p4lang/behavioral-model:latest`). Returns immediately with a 202
    and the current status — poll `GET /images/status?image=<same>`
    to watch the pull.

    The pull runs as a background asyncio task so the response is not
    tied to the transfer. Multiple concurrent pulls of the same image
    collapse to one; a fresh pull of an image that finished sees
    `cached: true` and returns without work."""
    image = body.image.strip()
    if not image:
        raise bad_request("image is required")
    is_docker = _looks_like_docker_ref(image)
    # Best-effort validation. Docker refs are anything with a slash or a
    # known container-catalog image; QEMU refs must be a catalog id, an
    # https URL, or a custom-<sha>.
    if not is_docker and (
        image not in QEMU_CATALOG
        and not image.startswith(("http://", "https://", "custom-"))
    ):
        raise bad_request(
            f"unknown image {image!r}: expected a catalog id, an https URL, "
            "a custom-<sha> reference, a local path, or a docker image "
            "ref (e.g. alpine:3.20 or p4lang/behavioral-model:latest)"
        )
    status = await _current_status(image)
    if status.get("cached"):
        return {"image": image, **status}
    if image not in _pulls:
        _pulls[image] = asyncio.create_task(_do_pull(image))
    return {"image": image, **await _current_status(image)}


@router.get("/images/status", response_model=ImageStatusOut)
async def image_status_get(
    image: str,
) -> Any:
    """Snapshot: whether this image is on disk, being fetched, or
    somewhere in between. Cheap — for docker refs it is one
    `docker inspect`; for qemu refs an in-memory + stat() lookup.
    Poll every 1–5 seconds during a pull."""
    if not image:
        raise bad_request("image is required (query param)")
    return {"image": image, **await _current_status(image)}
