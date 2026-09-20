"""Ready-hook HTTP surface — GET/PUT/POST/DELETE /labs/{id}/hooks.

The runtime lives in `labtris_api/runtime/hooks.py`; this module is only
the API skin plus the auto-fire watcher lifecycle (spawned on PUT, cancelled
on DELETE, joined at shutdown from `main.py`).

Kept in its own router file rather than folded into `routers/labs.py`
because that file is already the biggest in the tree; separating a
self-contained subsystem here avoids one more scroll."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request
from labtris_api.lifecycle import get_lab
from labtris_api.runtime import hooks as hk

router = APIRouter(tags=["labs"])


class HooksIn(BaseModel):
    source: str


class HooksOut(BaseModel):
    source: str | None
    parsed: dict[str, Any] | None
    state: dict[str, Any] | None
    watcher_running: bool


@router.get("/labs/{lab_id}/hooks", response_model=HooksOut)
async def get_hooks(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> HooksOut:
    lab = await get_lab(session, lab_id)
    return HooksOut(
        source=lab.hooks_source,
        parsed=lab.hooks,
        state=lab.hooks_state,
        watcher_running=hk.is_running(lab_id),
    )


@router.put("/labs/{lab_id}/hooks", response_model=HooksOut)
async def put_hooks(
    lab_id: str,
    body: HooksIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> HooksOut:
    """Replace the lab's hooks block. Parses YAML, validates, persists.

    On success, cancels any in-flight auto-fire watcher for this lab and
    spawns a fresh one. The watcher fires the hooks once the ready
    condition holds and then exits — subsequent runs are on-demand via
    POST /hooks/run."""
    lab = await get_lab(session, lab_id)
    try:
        parsed = hk.parse_source(body.source)
    except ValueError as exc:
        raise bad_request(str(exc)) from None
    lab.hooks_source = body.source
    lab.hooks = parsed
    lab.hooks_state = None  # Clear any prior run history.
    await session.commit()
    # Restart the auto-fire watcher against the new spec.
    hk.cancel(lab_id)
    runner = hk.HookRunner(lab_id, parsed)
    task = asyncio.create_task(runner.watch(), name=f"hooks-watch-{lab_id}")
    hk.register(lab_id, task)
    return HooksOut(
        source=lab.hooks_source,
        parsed=lab.hooks,
        state=lab.hooks_state,
        watcher_running=True,
    )


@router.post("/labs/{lab_id}/hooks/run", response_model=HooksOut)
async def run_hooks(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> HooksOut:
    """Force a hooks run, ignoring the ready_when trigger.

    Returns after every hook has run (or the first failure). Progress is
    also published on the lab websocket as `{"type":"hook", ...}` frames
    so a UI can render it live."""
    lab = await get_lab(session, lab_id)
    if not lab.hooks:
        raise bad_request("this lab has no hooks defined")
    runner = hk.HookRunner(lab_id, lab.hooks)
    await runner.run_all()
    # Re-read to see the state the runner just wrote.
    await session.refresh(lab)
    return HooksOut(
        source=lab.hooks_source,
        parsed=lab.hooks,
        state=lab.hooks_state,
        watcher_running=hk.is_running(lab_id),
    )


@router.delete("/labs/{lab_id}/hooks", status_code=204)
async def clear_hooks(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    lab = await get_lab(session, lab_id)
    lab.hooks_source = None
    lab.hooks = None
    lab.hooks_state = None
    await session.commit()
    hk.cancel(lab_id)
    return Response(status_code=204)


async def resume_watchers() -> None:
    """Called from `main.py` lifespan on startup — spawn a watcher for
    every lab that already has hooks defined.

    Without this, a restart would leave hooks defined but never firing
    until the user next PUTs the same spec back. Idempotent — cancels
    any existing watcher first."""
    from sqlalchemy import select

    from labtris_api.db import SessionLocal
    from labtris_api.models import Lab

    async with SessionLocal() as db:
        labs = (
            await db.execute(select(Lab).where(Lab.hooks.isnot(None)))
        ).scalars().all()
        for lab in labs:
            spec = lab.hooks or {}
            if not spec.get("hooks"):
                continue
            hk.cancel(lab.id)
            runner = hk.HookRunner(lab.id, spec)
            task = asyncio.create_task(runner.watch(), name=f"hooks-watch-{lab.id}")
            hk.register(lab.id, task)
