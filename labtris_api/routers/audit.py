"""Reading the audit log.

Admin-only. The log names who did what, across everyone's labs, and that is
not a thing a shared workshop should hand to every signed-in user — the labs
themselves are deliberately visible to all, but "what did Alice do on Tuesday"
is a different question.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api import audit as audit_mod
from labtris_api.auth import User, require_admin
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.models import AuditEntry

router = APIRouter(tags=["audit"])


def _row(e: AuditEntry) -> dict[str, Any]:
    return {
        "id": e.id,
        "at": e.at,
        "actor": e.actor_name or None,
        "actor_id": e.actor_id,
        "via": e.via,
        "method": e.method,
        "path": e.path,
        "route": e.route,
        "status": e.status,
        "lab_id": e.lab_id,
        "target_kind": e.target_kind,
        "target_id": e.target_id,
        "summary": e.summary,
        "detail": e.detail,
        "duration_ms": e.duration_ms,
    }


@router.get("/audit")
async def list_audit(
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
    lab_id: str | None = None,
    via: str | None = Query(default=None, description='"human" or "assistant"'),
    actor: str | None = None,
    since_hours: int | None = Query(default=None, ge=1, le=24 * 90),
    failures_only: bool = False,
    limit: int = Query(default=200, ge=1, le=2000),
    before: str | None = Query(
        default=None,
        description="id of the oldest row you already have, for the next page",
    ),
) -> dict[str, Any]:
    """Newest first.

    Paged by id rather than offset: entries arrive while you are reading, and
    an offset page would skip or repeat rows as the table grows underneath it.
    """
    q = select(AuditEntry).order_by(AuditEntry.at.desc(), AuditEntry.id.desc())
    if lab_id:
        q = q.where(AuditEntry.lab_id == lab_id)
    if via:
        q = q.where(AuditEntry.via == via)
    if actor:
        q = q.where(AuditEntry.actor_name == actor)
    if since_hours:
        q = q.where(AuditEntry.at >= datetime.now(UTC) - timedelta(hours=since_hours))
    if failures_only:
        q = q.where(AuditEntry.status >= 400)
    if before:
        anchor = await session.get(AuditEntry, before)
        if anchor is not None:
            q = q.where(AuditEntry.at < anchor.at)
    rows = list((await session.execute(q.limit(limit))).scalars())

    return {
        "entries": [_row(e) for e in rows],
        # What the log can actually answer, which is not the same as the
        # retention setting: on a fresh install or just after a prune, "we
        # keep 7 days" and "we have 7 days" are different claims.
        "retention_days": settings.audit_retention_days,
        "oldest": await audit_mod.oldest(session),
        "next_before": rows[-1].id if len(rows) == limit else None,
    }


@router.get("/audit/summary")
async def audit_summary(
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
    since_hours: int = Query(default=24, ge=1, le=24 * 90),
) -> dict[str, Any]:
    """Counts worth seeing before reading any rows: how much happened, how
    much of it was the assistant, and how much of it failed."""
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    base = select(func.count()).select_from(AuditEntry).where(AuditEntry.at >= since)

    total = (await session.execute(base)).scalar() or 0
    by_assistant = (
        await session.execute(base.where(AuditEntry.via == "assistant"))
    ).scalar() or 0
    failed = (await session.execute(base.where(AuditEntry.status >= 400))).scalar() or 0

    top = (
        await session.execute(
            select(AuditEntry.route, func.count().label("n"))
            .where(AuditEntry.at >= since)
            .group_by(AuditEntry.route)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()

    return {
        "since_hours": since_hours,
        "total": total,
        "by_assistant": by_assistant,
        "by_human": total - by_assistant,
        "failed": failed,
        "busiest": [{"route": r, "count": n} for r, n in top],
        "retention_days": settings.audit_retention_days,
        "oldest": await audit_mod.oldest(session),
    }


@router.post("/audit/prune", status_code=200)
async def prune_now(
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
    days: int | None = Query(default=None, ge=0, le=3650),
) -> dict[str, Any]:
    """Prune on demand. Pruning also runs on a timer; this is for anyone who
    wants the space back now, or who has just lowered the retention."""
    removed = await audit_mod.prune(session, days)
    return {"removed": removed, "retention_days": days if days is not None
            else settings.audit_retention_days}
