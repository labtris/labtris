"""Pod HTTP surface: snapshot a lab, list local pods, download, restore, delete.

Kept minimal — the tar writer/reader lives in `labtris_api/pods.py`. This
module is just the FastAPI skin. Uploads are handled as either a local path
(when the caller has filesystem access to the pod already, which the CLI
does when the user names a file on the same host) or a multipart file
(for a remote CLI/UI upload). Both drop into the same `pods.load` call."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api import pods
from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request, not_found

router = APIRouter(tags=["pods"])


class SnapshotIn(BaseModel):
    mode: Literal["cold", "hot"] = "cold"
    # cold: lab must be stopped; small archive; nodes boot fresh on load
    # hot:  running nodes accepted; QEMU savevm + docker commit/save
    #       into the archive; loadvm/docker-load on first start


class SnapshotOut(BaseModel):
    pod_id: str
    path: str
    bytes: int
    node_count: int
    mode: str
    created_at: str


class PodSummary(BaseModel):
    pod_id: str
    lab_name: str
    mode: str
    created_at: str
    node_count: int
    bytes: int
    #: Absolute path on the SERVER host. Included so `labtris lab load`
    #: can round-trip a `pod list` entry without a second API call.
    path: str


class LoadIn(BaseModel):
    path: str
    name: str | None = None


class LoadOut(BaseModel):
    lab_id: str
    name: str
    node_count: int
    mode: str
    pod_id: str | None


@router.post("/labs/{lab_id}/snapshot", response_model=SnapshotOut)
async def snapshot_lab(
    lab_id: str,
    body: SnapshotIn,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> SnapshotOut:
    """Write a pod archive to `settings.pod_dir` and return its metadata.

    Cold mode only in Phase C — the lab must be stopped. Hot mode arrives
    in Phase D with QMP snapshot-save + docker commit + save."""
    info = await pods.save(session, lab_id, mode=body.mode)
    return SnapshotOut(**info)


@router.get("/pods", response_model=list[PodSummary])
async def list_pods(_user: User = Depends(get_current_user)) -> list[PodSummary]:
    return [PodSummary(**p) for p in pods.list_pods()]


@router.get("/pods/{pod_id}/download")
async def download_pod(
    pod_id: str, _user: User = Depends(get_current_user)
) -> FileResponse:
    p = pods.pod_path(pod_id)
    if not p.exists():
        raise not_found(f"pod {pod_id}")
    return FileResponse(
        path=p,
        media_type="application/gzip",
        filename=f"{pod_id}.tar.gz",
    )


@router.delete("/pods/{pod_id}", status_code=204)
async def rm_pod(pod_id: str, _user: User = Depends(get_current_user)) -> Response:
    pods.delete_pod(pod_id)
    return Response(status_code=204)


@router.post("/pods/load", response_model=LoadOut)
async def load_pod(
    body: LoadIn,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> LoadOut:
    """Restore a pod archive from a local path as a new lab.

    The `path` must be under `settings.pod_dir` OR an absolute path this
    process can read — no traversal outside those, but a pod that landed
    in `/tmp` via scp is fine to load in place. Use `POST /pods/upload`
    when the archive is coming in over HTTP."""
    p = Path(body.path).expanduser()
    if not p.is_absolute():
        p = pods.pods_dir() / p
    info = await pods.load(session, p, name_override=body.name)
    return LoadOut(**info)


@router.post("/pods/upload", response_model=LoadOut)
async def upload_pod(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> LoadOut:
    """Streamed multipart upload of a pod archive. Streams into pod_dir
    so a 20 GB pod never fits in RAM, then loads it. The uploaded file
    stays in pod_dir as a normal pod entry — this is 'upload + load' by
    design; if you want upload-and-forget, DELETE it after."""
    if not file.filename or not file.filename.endswith((".tar.gz", ".tgz")):
        raise bad_request("upload must be a .tar.gz file")
    dst = pods.pods_dir() / f"upload-{file.filename}"
    with dst.open("wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    info = await pods.load(session, dst, name_override=name)
    return LoadOut(**info)
