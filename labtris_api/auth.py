"""Who is asking.

Every handler already depended on get_current_user(), which returned a fixed
"dev" user — the note in the original said real auth would be a one-file
change, and this is that file. The 114 call sites are untouched.

Two roles, admin and user, because anything finer is a guess about how a team
works, and a lab tool that makes you model permissions before you can draw a
topology has already lost. What separates them is destructive and
instance-wide actions, not visibility: everyone can see everyone's labs, which
is the point of a shared workshop.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import structlog
import ulid
from fastapi import Cookie, Depends, Header
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.errors import ApiError
from labtris_api.models import User as UserRow

logger = structlog.get_logger(__name__)

SESSION_COOKIE = "labtris_session"
TOKEN_TTL = timedelta(days=7)
_ALGORITHM = "HS256"

#: scrypt parameters. n=2**15 costs tens of milliseconds per verification,
#: slow enough to make a stolen database expensive to crack and fast enough
#: that a classroom signing in at once does not queue.
#:
#: maxmem is not optional: scrypt needs 128*n*r bytes — 32 MiB exactly at
#: these settings — and OpenSSL's default ceiling is 32 MiB, so the call fails
#: with "memory limit exceeded" unless the ceiling is raised past what the
#: work factor actually requires.
_SCRYPT = {"n": 2**15, "r": 8, "p": 1, "dklen": 32, "maxmem": 96 * 1024 * 1024}


@dataclass(frozen=True)
class User:
    id: str
    name: str
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """False for anything that is not a valid hash of this password.

    A damaged or truncated row must fail closed, not raise: fromhex() throws
    on the first non-hex character, which would turn a corrupt password column
    into a 500 on the login page instead of a refusal."""
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    expected = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    # Constant time: a timing difference here leaks how much of a guess was right.
    return hmac.compare_digest(expected, digest)


def _secret() -> str:
    """The key sessions are signed with.

    Generated and persisted on first use rather than shipped, so two installs
    never share one, and so a default secret cannot be left in place by
    somebody who did not know to change it. Rotating the file logs everyone
    out, which is the correct behaviour for a leaked key."""
    configured = getattr(settings, "session_secret", "") or os.environ.get(
        "LABTRIS_SESSION_SECRET", ""
    )
    if configured:
        return configured
    path = settings.session_secret_path
    from pathlib import Path

    p = Path(path).expanduser()
    if p.exists():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    p.write_text(value)
    p.chmod(0o600)
    logger.info("auth.secret.generated", path=str(p))
    return value


def issue_token(user: UserRow) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user.id,
            "username": user.username,
            "role": user.role,
            "iat": int(now.timestamp()),
            "exp": int((now + TOKEN_TTL).timestamp()),
        },
        _secret(),
        algorithm=_ALGORITHM,
    )


def read_token(token: str) -> dict[str, Any] | None:
    try:
        # Bound to a typed name rather than returned straight out: jwt.decode
        # is annotated as returning Any, which only became visible once PyJWT
        # was a declared dependency — before that mypy had no stubs to read
        # and silently let it through.
        claims: dict[str, Any] = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
        return claims
    except jwt.PyJWTError:
        return None


def unauthorized(message: str = "sign in to continue") -> ApiError:
    return ApiError("unauthorized", message, 401)


def forbidden(message: str) -> ApiError:
    return ApiError("forbidden", message, 403)


async def users_exist(session: AsyncSession) -> bool:
    return bool(
        (await session.execute(select(func.count()).select_from(UserRow))).scalar_one()
    )


async def create_user(
    session: AsyncSession,
    username: str,
    password: str,
    *,
    role: str = "user",
    display_name: str = "",
) -> UserRow:
    row = UserRow(
        id=ulid.new().str,
        username=username.strip().lower(),
        display_name=display_name or username,
        password_hash=hash_password(password),
        role=role,
    )
    session.add(row)
    await session.flush()
    return row


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    labtris_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    authorization: str | None = Header(default=None),
) -> User:
    """The signed-in user, from the session cookie or a bearer token.

    The cookie is what the browser uses — it rides along on the WebSocket
    handshake too, which a token in localStorage would not. The bearer header
    is for the MCP server, the CLI and anything scripted.

    Requires a real account. The two endpoints that need to work before the
    first account exists — /auth/setup (creates it) and /auth/state (tells
    the browser it needs to be created) — do not depend on this at all: the
    former takes no user, the latter is fully unauth. Every OTHER endpoint
    refuses when there is no valid session.

    An earlier version handed out a synthetic "setup" admin from here when
    the DB was empty, on the theory that "an instance with no way in is not
    more secure, it is bricked". True for the setup endpoints themselves,
    but every OTHER endpoint also got that admin, so a fresh install was
    wide open: the UI showed a functional canvas to any browser reaching
    the port, and every mutation ran under the synthetic admin. The
    two setup endpoints are the narrower place to solve that.
    """
    token = labtris_session
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise unauthorized()

    claims = read_token(token)
    if not claims:
        raise unauthorized("your session has expired")

    row = await session.get(UserRow, claims.get("sub"))
    if row is None or row.disabled:
        raise unauthorized("this account is no longer active")
    return User(id=row.id, name=row.display_name or row.username,
                username=row.username, role=row.role)


async def resolve_ws_user(websocket, session: AsyncSession) -> User | None:
    """Same auth as get_current_user but for a WebSocket, which cannot
    raise HTTP errors — starlette's WebSocket has no Depends chain for
    the exception handler to catch. Reads the session cookie from the
    upgrade request's headers (present because the browser attaches it
    to the handshake), or a bearer token from an Authorization header,
    then does the same DB lookup. Returns None on any failure so the
    caller can close the socket cleanly with a protocol-specific error
    message (Guacamole clients hide the WebSocket close code and only
    show what came over the wire, so "close it silently" is invisible).
    """
    cookies = websocket.cookies or {}
    token = cookies.get(SESSION_COOKIE)
    if not token:
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    claims = read_token(token)
    if not claims:
        return None
    row = await session.get(UserRow, claims.get("sub"))
    if row is None or row.disabled:
        return None
    return User(id=row.id, name=row.display_name or row.username,
                username=row.username, role=row.role)


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise forbidden("this needs an administrator")
    return user


CurrentUser = Depends(get_current_user)
AdminUser = Depends(require_admin)
