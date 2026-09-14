from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api import backup, gdrive
from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request, runtime_error
from labtris_api.models import Lab, Setting
from labtris_api.schemas import GDriveComplete, GDriveCredentials

router = APIRouter(tags=["backup"])


@router.get("/backup")
async def download_backup(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Response:
    """Every lab's topology plus the instance's settings, as one archive.

    Configuration only — see labtris_api.backup for why the images are not in
    here."""
    blob = await backup.build_archive(session)
    return Response(
        content=blob,
        media_type="application/gzip",
        headers={"Content-Disposition": 'attachment; filename="labtris-backup.tar.gz"'},
    )




async def _restore_payloads(
    session: AsyncSession, payloads: list[dict[str, Any]], mode: str
) -> tuple[list[str], list[str], list[str]]:
    """Put the labs from an archive back.

    Additive either way — a restore never deletes. The question is what to do
    about a lab that is already here, and the answer used to be "make another
    copy", which is right for moving a lab between instances and wrong for the
    ordinary case of restoring an instance's own backup onto itself: every run
    duplicated every lab, so a handful of restores turned one `demo` into
    `demo-1-1-2-1` and a hundred siblings. Identity comes from the lab id the
    export carries, so a lab that is already here is skipped by default and
    `clone` asks for the old behaviour explicitly."""
    from labtris_api.routers.labs import import_lab
    from labtris_api.schemas import LabImportIn

    restored: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for payload in payloads:
        src = payload.get("lab") or {}
        name = src.get("name", "?")
        if mode != "clone" and src.get("id"):
            already = await session.get(Lab, src["id"])
            if already is not None:
                skipped.append(already.name)
                continue
        try:
            created = await import_lab(LabImportIn(**payload), session, None)
            restored.append(created.name)
        except Exception as exc:  # noqa: BLE001 - one bad lab must not sink the rest
            # Without this the failed flush leaves the session unusable and
            # every remaining lab fails with the *first* lab's error.
            await session.rollback()
            failed.append(f"{name}: {str(exc).splitlines()[0]}")
    return restored, skipped, failed


@router.post("/backup/restore")
async def restore_backup(
    file: UploadFile,
    mode: Literal["skip", "clone"] = "skip",
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Import every lab in an archive.

    Additive: labs are created alongside what is already here rather than
    replacing an instance's contents. A restore that silently deletes is not a
    restore, it is a footgun. `mode=skip` (the default) leaves labs this
    instance already has alone; `mode=clone` copies them in under a suffixed
    name, which is what you want when merging another instance's archive."""
    blob = await file.read()
    try:
        manifest = backup.read_manifest(blob)
        payloads = backup.labs_in(blob)
    except Exception as exc:  # noqa: BLE001 - any malformed archive lands here
        raise bad_request(f"not a labtris backup archive: {exc}") from None
    if manifest.get("format") != backup.ARCHIVE_VERSION:
        raise bad_request(f"unknown archive format {manifest.get('format')!r}")

    restored, skipped, failed = await _restore_payloads(session, payloads, mode)
    return {
        "restored": restored,
        "skipped": skipped,
        "failed": failed,
        "created": manifest.get("created"),
    }


# --- Google Drive -----------------------------------------------------------
#
# Credentials live in the settings table rather than env, because an OAuth
# client is per-user rather than per-deployment. The refresh token is stored
# and never returned to the browser.

DRIVE_KEYS = ("gdrive_client_id", "gdrive_client_secret", "gdrive_refresh_token",
              "gdrive_folder_id")


async def _drive_config(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(Setting).where(Setting.key.in_(DRIVE_KEYS)))).scalars()
    return {r.key: str(r.value) for r in rows}


async def _store(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


@router.get("/backup/gdrive/status")
async def gdrive_status(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    cfg = await _drive_config(session)
    return {
        "configured": bool(cfg.get("gdrive_client_id") and cfg.get("gdrive_client_secret")),
        "linked": bool(cfg.get("gdrive_refresh_token")),
        "folder_id": cfg.get("gdrive_folder_id", ""),
    }


@router.post("/backup/gdrive/credentials")
async def gdrive_credentials(
    body: GDriveCredentials,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Your own OAuth client, from Google Cloud Console — a Desktop app client,
    which is the type the device flow accepts."""
    await _store(session, "gdrive_client_id", body.client_id)
    await _store(session, "gdrive_client_secret", body.client_secret)
    if body.folder_id is not None:
        await _store(session, "gdrive_folder_id", body.folder_id)
    return {"configured": True}


@router.post("/backup/gdrive/link")
async def gdrive_link(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Begin the device flow: returns a URL and a code to type into it."""
    cfg = await _drive_config(session)
    if not cfg.get("gdrive_client_id"):
        raise bad_request("set the Google OAuth client id and secret first")
    try:
        return dict(gdrive.begin_device_auth(cfg["gdrive_client_id"]))
    except gdrive.DriveError as exc:
        raise runtime_error(str(exc)) from None


@router.post("/backup/gdrive/link/complete")
async def gdrive_link_complete(
    body: GDriveComplete,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    cfg = await _drive_config(session)
    try:
        out = gdrive.poll_device_auth(
            cfg["gdrive_client_id"], cfg["gdrive_client_secret"], body.device_code
        )
    except gdrive.DriveError as exc:
        raise runtime_error(str(exc)) from None
    if out.get("pending"):
        return {"pending": True}
    if not out.get("refresh_token"):
        raise runtime_error("Google approved the code but returned no refresh token")
    await _store(session, "gdrive_refresh_token", out["refresh_token"])
    return {"linked": True}


async def _token(session: AsyncSession) -> tuple[str, str | None]:
    cfg = await _drive_config(session)
    if not cfg.get("gdrive_refresh_token"):
        raise bad_request("this instance is not linked to a Google account yet")
    try:
        token = gdrive.access_token(
            cfg["gdrive_client_id"], cfg["gdrive_client_secret"], cfg["gdrive_refresh_token"]
        )
    except gdrive.DriveError as exc:
        raise runtime_error(str(exc)) from None
    return token, cfg.get("gdrive_folder_id") or None


@router.post("/backup/gdrive/push")
async def gdrive_push(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Build the archive and put it in Drive."""
    token, folder = await _token(session)
    blob = await backup.build_archive(session)
    try:
        info = gdrive.upload(token, gdrive.default_name(), blob, folder)
    except gdrive.DriveError as exc:
        raise runtime_error(str(exc)) from None
    return {"uploaded": info.get("name"), "id": info.get("id"), "bytes": len(blob)}


@router.get("/backup/gdrive/list")
async def gdrive_list(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    token, folder = await _token(session)
    try:
        return {"files": gdrive.listing(token, folder)}
    except gdrive.DriveError as exc:
        raise runtime_error(str(exc)) from None


@router.post("/backup/gdrive/pull/{file_id}")
async def gdrive_pull(
    file_id: str,
    mode: Literal["skip", "clone"] = "skip",
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Fetch an archive from Drive and restore it, additively."""
    token, _ = await _token(session)
    try:
        blob = gdrive.download(token, file_id)
    except gdrive.DriveError as exc:
        raise runtime_error(str(exc)) from None
    try:
        backup.read_manifest(blob)
        payloads = backup.labs_in(blob)
    except Exception as exc:  # noqa: BLE001
        raise bad_request(f"not a labtris backup archive: {exc}") from None

    restored, skipped, failed = await _restore_payloads(session, payloads, mode)
    return {"restored": restored, "skipped": skipped, "failed": failed}
