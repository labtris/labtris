"""Recording what happened, and who or what did it.

Reads are not recorded. Listing labs is not interesting, there are orders of
magnitude more of them, and a log nobody can scan is a log nobody reads. Every
mutation is, with the actor taken from the signed token rather than a header —
an audit log that can be told what to say is not an audit log.

Retention is finite by design. This table grows without bound otherwise, and
on a host running thousands of nodes it grows fast: the 931-node start was 931
mutations on its own. Rows older than LABTRIS_AUDIT_RETENTION_DAYS are pruned,
default 7.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.config import settings
from labtris_api.models import AuditEntry

#: Methods that change something. GET and HEAD are not recorded.
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

#: Paths never recorded, however they are called.
#:
#: Auth endpoints carry passwords in the body, and a log that stores them is
#: worse than no log. The audit reader itself is excluded because reading the
#: log is not an event in the log — otherwise an operator scrolling through
#: last week generates the only entries they can see.
_SKIP = re.compile(
    r"^/api/v1/(auth/(login|setup)|users/[^/]+/password|audit)\b"
)

#: Body fields never stored, matched case-insensitively anywhere in the key.
#: An audit entry exists to say what happened, not to become the next place a
#: credential leaks from.
_REDACT = re.compile(
    r"pass|secret|token|key|credential|authorization", re.IGNORECASE
)

#: Bodies are summarised, not stored whole. A topology import or a startup
#: config is tens of kilobytes, and a hundred of those is a table nobody can
#: query.
_MAX_VALUE = 200
_MAX_FIELDS = 12


def redact(body: Any) -> dict[str, Any]:
    """Keep the shape of a request body without keeping its secrets or bulk."""
    if not isinstance(body, dict):
        return {}
    out: dict[str, Any] = {}
    for i, (k, v) in enumerate(body.items()):
        if i >= _MAX_FIELDS:
            out["…"] = f"{len(body) - _MAX_FIELDS} more fields"
            break
        if _REDACT.search(k):
            out[k] = "«redacted»"
        elif isinstance(v, str):
            out[k] = v if len(v) <= _MAX_VALUE else f"{v[:_MAX_VALUE]}… ({len(v)} chars)"
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = f"[{len(v)} items]"
        else:
            out[k] = "{…}"
    return out


def summarise(method: str, route: str, status: int) -> str:
    """One scannable line. The route template rather than the path, so a
    thousand node starts read as one kind of thing."""
    verb = {
        "POST": "created",
        "PUT": "replaced",
        "PATCH": "changed",
        "DELETE": "deleted",
    }.get(method, method.lower())
    tail = route.removeprefix("/api/v1/") or route
    if status >= 400:
        return f"{verb} {tail} — refused ({status})"
    return f"{verb} {tail}"


async def record(session: AsyncSession, **fields: Any) -> None:
    """Write one entry.

    Never raises into the request. An audit write that fails must not turn a
    successful operation into an error the user sees — the operation already
    happened, and failing afterwards would misreport it.
    """
    try:
        session.add(AuditEntry(**fields))
        await session.commit()
    except Exception:  # noqa: BLE001 - see docstring
        await session.rollback()


async def prune(session: AsyncSession, days: int | None = None) -> int:
    """Drop entries older than the retention window. Returns how many went."""
    keep = days if days is not None else settings.audit_retention_days
    if keep <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep)
    result = await session.execute(delete(AuditEntry).where(AuditEntry.at < cutoff))
    await session.commit()
    return int(result.rowcount or 0)


async def oldest(session: AsyncSession) -> datetime | None:
    """How far back the log actually reaches.

    Worth surfacing: "we keep 7 days" and "we have 7 days" are different
    claims, and on a fresh install or after a prune the second is the true
    one.
    """
    return (await session.execute(select(func.min(AuditEntry.at)))).scalar()
