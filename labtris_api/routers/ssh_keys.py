"""SSH pubkey registry — HTTP surface for `labtris ssh-keys {add,list,rm}`.

Users register their OpenSSH-format pubkey (the contents of
`~/.ssh/id_ed25519.pub`), and the SSH proxy on port 2222 will let them
sign in with the matching private key without a JWT password.

Two things kept intentionally simple:
* One row = one pubkey. No key groups / labels / scoped-to-nodes yet.
  Labtris auth is user-global; SSH access is user-global.
* Fingerprint is derived server-side from the submitted key body, not
  taken from the client. That way a client cannot register a body whose
  fingerprint mismatches what the SSH proxy will compute at auth time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import ulid
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request, conflict, not_found
from labtris_api.models import SshKey

router = APIRouter(tags=["auth"])


class SshKeyIn(BaseModel):
    """Payload for adding a pubkey.

    `body` is the full OpenSSH single-line format: `<algo> <base64> [comment]`.
    Both `ssh-ed25519` and `ssh-rsa` are common; `ecdsa-*` and `sk-*` are
    fine too. Reject on parse failure (asyncssh's import raises).
    """

    name: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=32, max_length=8192)


def _parse_and_fingerprint(body: str) -> tuple[str, str, str]:
    """Return (algorithm, canonical_body, sha256_fingerprint).

    Uses asyncssh's OpenSSH parser so what we accept here matches exactly
    what the SSH proxy will read at auth time. Any parse error surfaces
    as a 400 with the underlying message.
    """
    try:
        import asyncssh
    except ImportError as exc:
        raise bad_request("asyncssh is not installed on this server") from exc
    try:
        key = asyncssh.import_public_key(body.strip())
    except (asyncssh.KeyImportError, ValueError) as exc:
        raise bad_request(f"could not parse the pubkey: {exc}") from exc
    algo = key.get_algorithm()
    fp = key.get_fingerprint()  # "SHA256:...."
    # Serialize the canonical form. Keeps whatever comment the caller put
    # on the end (useful in `labtris ssh-keys list`).
    canonical = key.export_public_key("openssh").decode().strip()
    return algo, canonical, fp


def _public(row: SshKey) -> dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "algorithm": row.algorithm,
        "fingerprint": row.fingerprint,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


@router.post("/auth/ssh-keys", status_code=201)
async def add_ssh_key(
    body: SshKeyIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    algo, canonical, fp = _parse_and_fingerprint(body.body)
    # Fingerprint uniqueness is DB-enforced; do a friendlier preflight to
    # give a clear 409 instead of a raw integrity error.
    existing = (
        await session.execute(select(SshKey).where(SshKey.fingerprint == fp))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.user_id == user.id:
            raise conflict(f"you already have this key registered as {existing.name!r}")
        raise conflict("this key is already registered under a different user")

    row = SshKey(
        id=ulid.new().str,
        user_id=user.id,
        name=body.name.strip(),
        algorithm=algo,
        key_body=canonical,
        fingerprint=fp,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _public(row)


@router.get("/auth/ssh-keys")
async def list_ssh_keys(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return only the calling user's keys. Even admins don't get the
    superset here — the pubkey table isn't a secret but there's no reason
    to make it a global registry either. If admin key management ever
    becomes a real ask, add a second endpoint scoped to admin."""
    rows = (
        await session.execute(
            select(SshKey)
            .where(SshKey.user_id == user.id)
            .order_by(SshKey.created_at)
        )
    ).scalars()
    return {"keys": [_public(r) for r in rows]}


@router.delete("/auth/ssh-keys/{key_id}", status_code=204)
async def remove_ssh_key(
    key_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    row = await session.get(SshKey, key_id)
    if row is None or row.user_id != user.id:
        # Same message so a probe can't distinguish "not yours" from "no
        # such key" — same principle as the login endpoint.
        raise not_found("no such key")
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


async def touch_last_used(fingerprint: str) -> None:
    """Called by the SSH proxy after a successful pubkey auth so the UI's
    `last_used_at` reflects real activity. Best-effort — a failure here
    must not fail the auth."""
    from datetime import UTC

    try:
        from labtris_api.db import SessionLocal

        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(SshKey).where(SshKey.fingerprint == fingerprint)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.last_used_at = datetime.now(UTC)
                await session.commit()
    except Exception:  # noqa: BLE001
        pass
