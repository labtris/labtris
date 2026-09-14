"""Signing in, and who is signed in."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import (
    SESSION_COOKIE,
    TOKEN_TTL,
    User,
    create_user,
    forbidden,
    get_current_user,
    issue_token,
    require_admin,
    unauthorized,
    users_exist,
    verify_password,
)
from labtris_api.db import get_session
from labtris_api.errors import conflict, not_found
from labtris_api.models import User as UserRow

router = APIRouter(tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class NewUser(Credentials):
    display_name: str = ""
    role: str = "user"


def _public(row: UserRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "username": row.username,
        "display_name": row.display_name or row.username,
        "role": row.role,
        "disabled": row.disabled,
    }


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(TOKEN_TTL.total_seconds()),
        httponly=True,  # unreadable from JS, so an XSS cannot lift the session
        samesite="lax",
        # Not secure=True: this is routinely run on a LAN over plain http, and
        # a cookie the browser refuses to send is an instance nobody can log
        # into. Put it behind TLS in production and set this.
        secure=False,
        path="/",
    )


@router.get("/auth/state")
async def auth_state(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """What the login screen needs before anyone has signed in.

    Unauthenticated on purpose — it says whether setup is still required, not
    anything about who exists."""
    return {"setup_required": not await users_exist(session)}


@router.post("/auth/setup")
async def setup(
    body: NewUser, response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Create the first administrator.

    Open until it succeeds and closed forever after — an instance with no
    account cannot be administered, and one that ships with a default password
    is worse than one that asks."""
    if await users_exist(session):
        raise conflict("this instance already has an administrator")
    row = await create_user(
        session, body.username, body.password, role="admin", display_name=body.display_name
    )
    await session.commit()
    _set_cookie(response, issue_token(row))
    return {"user": _public(row)}


@router.post("/auth/login")
async def login(
    body: Credentials, response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(UserRow).where(UserRow.username == body.username.strip().lower())
        )
    ).scalar_one_or_none()
    # One message for both cases: saying "no such user" tells an attacker
    # which names are worth guessing passwords for.
    if row is None or row.disabled or not verify_password(body.password, row.password_hash):
        raise unauthorized("that username and password do not match")
    row.last_login_at = datetime.now(UTC)
    await session.commit()
    _set_cookie(response, issue_token(row))
    return {"user": _public(row)}


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed out"}


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.name,
        "role": user.role,
        "is_admin": user.is_admin,
    }


@router.get("/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Visible to everyone: you cannot search labs by owner without knowing
    who the owners are, and this instance is a shared workshop."""
    rows = (await session.execute(select(UserRow).order_by(UserRow.username))).scalars()
    return {"users": [_public(r) for r in rows]}


@router.post("/users", status_code=201)
async def add_user(
    body: NewUser,
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    existing = (
        await session.execute(
            select(UserRow).where(UserRow.username == body.username.strip().lower())
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise conflict(f"user {body.username!r} already exists")
    row = await create_user(
        session, body.username, body.password,
        role=body.role if body.role in ("admin", "user") else "user",
        display_name=body.display_name,
    )
    await session.commit()
    return {"user": _public(row)}


@router.delete("/users/{user_id}", status_code=204)
async def remove_user(
    user_id: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> Response:
    if user_id == admin.id:
        raise forbidden("you cannot delete the account you are signed in with")
    row = await session.get(UserRow, user_id)
    if row is None:
        raise not_found(f"user {user_id} not found")
    # Their labs survive with owner_id set null by the FK: deleting a person
    # should not delete the work, and somebody else may still be teaching it.
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)
