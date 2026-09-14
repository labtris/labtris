from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.errors import not_found
from labtris_api.lifecycle import new_id
from labtris_api.models import Feedback
from labtris_api.schemas import FeedbackIn, FeedbackOut, FeedbackPatch

router = APIRouter(tags=["feedback"])


@router.post("/feedback", response_model=FeedbackOut, status_code=201)
async def create_feedback(
    body: FeedbackIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Feedback:
    """Record something a user says is broken, with the context to reproduce it."""
    row = Feedback(id=new_id(), note=body.note, context=body.context, status="open")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@router.get("/feedback", response_model=list[FeedbackOut])
async def list_feedback(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> list[Feedback]:
    query = select(Feedback).order_by(Feedback.created_at.desc())
    if status:
        query = query.where(Feedback.status == status)
    return list((await session.execute(query)).scalars())


@router.patch("/feedback/{feedback_id}", response_model=FeedbackOut)
async def update_feedback(
    feedback_id: str,
    body: FeedbackPatch,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Feedback:
    row = await session.get(Feedback, feedback_id)
    if row is None:
        raise not_found(f"feedback {feedback_id} not found")
    row.status = body.status
    await session.commit()
    await session.refresh(row)
    return row


@router.delete("/feedback/{feedback_id}", status_code=204)
async def delete_feedback(
    feedback_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Response:
    row = await session.get(Feedback, feedback_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return Response(status_code=204)


@router.get("/feedback/report", response_class=Response)
async def feedback_report(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Response:
    """Every open report as plain text.

    Exists so whoever is fixing these can read them with curl from a terminal
    instead of clicking through a UI that may be the thing that is broken."""
    rows = list(
        (
            await session.execute(
                select(Feedback).where(Feedback.status == "open").order_by(Feedback.created_at)
            )
        ).scalars()
    )
    if not rows:
        return Response("no open reports\n", media_type="text/plain")

    out: list[str] = []
    for row in rows:
        ctx: dict[str, Any] = row.context or {}
        out.append(f"--- {row.id}  {row.created_at:%Y-%m-%d %H:%M}")
        out.append(f"    {row.note}")
        if ctx.get("element"):
            out.append(f"    element : {ctx['element']}")
        if ctx.get("label"):
            out.append(f"    text    : {ctx['label']!r}")
        where = [
            f"{k}={ctx[k]}"
            for k in ("theme", "viewport", "lab", "selected", "dock")
            if ctx.get(k)
        ]
        if where:
            out.append(f"    where   : {', '.join(where)}")
        for err in (ctx.get("errors") or [])[:5]:
            out.append(f"    error   : {err}")
        for call in (ctx.get("failedCalls") or [])[:5]:
            out.append(f"    api     : {call}")
        out.append("")
    return Response("\n".join(out), media_type="text/plain")
