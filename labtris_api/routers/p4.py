"""P4 program surface for bmv2 nodes.

A bmv2 node's per-instance bind mount at `/p4` carries `prog.p4` —
either one of the curated built-ins in `packaging/p4-programs/*.p4` or
a file the user uploaded via POST. This module is only the HTTP skin;
the bytes live on the host filesystem under
`~/.local/share/labtris/node-mounts/<node_id>/p4/prog.p4`.

Every endpoint refuses if the target node is not a bmv2 node — running
these against a plain container would silently store a file the
runtime never reads.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request, not_found
from labtris_api.lifecycle import get_node
from labtris_api.runtime.containers import profile_for
from labtris_api.runtime.docker import per_node_dir

router = APIRouter(tags=["p4"])


_P4_BUILTINS_DIR = Path(__file__).resolve().parents[2] / "packaging" / "p4-programs"
_P4_MOUNT_NAME = "p4"
_P4_FILE = "prog.p4"


class P4State(BaseModel):
    node_id: str
    source: str  # "builtin" | "uploaded" | "none"
    program: str | None
    path: str
    exists: bool
    contents: str | None = None


class BuiltinIn(BaseModel):
    builtin: str


class BuiltinOut(BaseModel):
    name: str
    description: str
    bytes: int


def _builtins_available() -> list[BuiltinOut]:
    out: list[BuiltinOut] = []
    if not _P4_BUILTINS_DIR.is_dir():
        return out
    # One-line description = the first `/* ... */` block's first line
    # after the filename, taken from each .p4. Cheap; keeps the list
    # accurate without a separate manifest that could drift.
    for p in sorted(_P4_BUILTINS_DIR.glob("*.p4")):
        name = p.stem
        first_line = ""
        try:
            with p.open() as fh:
                for line in fh:
                    stripped = line.strip().lstrip("*").strip()
                    if stripped and not stripped.startswith("/*") and " " in stripped:
                        first_line = stripped
                        break
        except OSError:
            pass
        out.append(BuiltinOut(name=name, description=first_line, bytes=p.stat().st_size))
    return out


def _require_bmv2(node: Any) -> None:
    if node.runtime != "docker":
        raise bad_request("this endpoint requires a docker node")
    profile = profile_for(node.image)
    if profile is None or profile.id != "bmv2":
        raise bad_request(
            f"node {node.name!r} is not a bmv2 P4 switch (image={node.image})"
        )


def _prog_path(node_id: str) -> Path:
    return Path(per_node_dir(node_id, _P4_MOUNT_NAME)) / _P4_FILE


@router.get("/p4/builtins", response_model=list[BuiltinOut])
async def list_builtins(_user: User = Depends(get_current_user)) -> list[BuiltinOut]:
    return _builtins_available()


@router.get("/nodes/{node_id}/p4", response_model=P4State)
async def get_p4(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> P4State:
    node = await get_node(session, node_id)
    _require_bmv2(node)
    path = _prog_path(node_id)
    exists = path.exists()
    opts = dict(node.opts or {})
    source = opts.get("p4_source", "none" if not exists else "unknown")
    program = opts.get("p4_program")
    contents = path.read_text() if exists and path.stat().st_size < 200_000 else None
    return P4State(
        node_id=node_id,
        source=source,
        program=program,
        path=str(path),
        exists=exists,
        contents=contents,
    )


@router.put("/nodes/{node_id}/p4", response_model=P4State)
async def set_builtin(
    node_id: str,
    body: BuiltinIn,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> P4State:
    """Point the node's /p4/prog.p4 at one of the curated built-ins.

    Copies the built-in file into the node's mount dir so a subsequent
    edit to the shipped file (say a version bump) does not silently
    change an already-running lab. The node keeps its snapshot of the
    program at spawn time."""
    node = await get_node(session, node_id)
    _require_bmv2(node)
    src = _P4_BUILTINS_DIR / f"{body.builtin}.p4"
    if not src.exists():
        raise not_found(
            f"unknown P4 built-in {body.builtin!r} — try "
            + ", ".join(b.name for b in _builtins_available())
        )
    dst = _prog_path(node_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    opts = dict(node.opts or {})
    opts["p4_source"] = "builtin"
    opts["p4_program"] = body.builtin
    node.opts = opts
    await session.commit()
    return await get_p4(node_id, session)  # type: ignore[return-value]


@router.post("/nodes/{node_id}/p4", response_model=P4State)
async def upload_custom(
    node_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> P4State:
    """Stream a custom `.p4` from a client upload into the node's mount dir.

    File size is not enforced here — bmv2's own compile step will refuse
    anything malformed on next start, and the payload has to fit in
    memory only if the caller chose not to stream, which we do. Cleared
    p4_program in opts because a custom upload no longer maps to a
    named built-in."""
    node = await get_node(session, node_id)
    _require_bmv2(node)
    if not (file.filename or "").endswith(".p4"):
        raise bad_request("upload must be a .p4 file")
    dst = _prog_path(node_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as fh:
        while True:
            chunk = await file.read(1024 * 128)
            if not chunk:
                break
            fh.write(chunk)
    opts = dict(node.opts or {})
    opts["p4_source"] = "uploaded"
    opts["p4_program"] = None
    node.opts = opts
    await session.commit()
    return await get_p4(node_id, session)  # type: ignore[return-value]


@router.delete("/nodes/{node_id}/p4", status_code=204)
async def clear_p4(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    """Drop the per-node program. On next start bmv2 will fall back to
    the built-in `basic_switch` set by set_builtin at spawn time — or,
    if the node was never given one, fail to compile. Clearing is safe
    (nothing is unlinked outside the node's own mount dir)."""
    node = await get_node(session, node_id)
    _require_bmv2(node)
    _prog_path(node_id).unlink(missing_ok=True)
    opts = dict(node.opts or {})
    opts.pop("p4_source", None)
    opts.pop("p4_program", None)
    node.opts = opts if opts else None
    await session.commit()
    return Response(status_code=204)
