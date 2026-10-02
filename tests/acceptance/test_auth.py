"""Signing in, and what shared visibility means.

The model here is a shared workshop, not per-tenant silos: everyone sees
everyone's labs, because an instructor opening a student's lab to help is the
normal case. What separates roles is who may do destructive and instance-wide
things, not who may look.
"""

from __future__ import annotations

from typing import Any

import pytest
import ulid
from httpx import ASGITransport, AsyncClient

from labtris_api.auth import hash_password, verify_password


def test_a_password_is_never_stored_in_the_clear() -> None:
    stored = hash_password("correct horse battery staple")

    assert "correct horse" not in stored
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct horse battery staple", stored)
    assert not verify_password("", stored)


def test_the_same_password_hashes_differently_every_time() -> None:
    """Per-password salt: identical passwords must not be identifiable as
    identical from the database alone."""
    a = hash_password("same")
    b = hash_password("same")

    assert a != b
    assert verify_password("same", a) and verify_password("same", b)


def test_a_corrupt_hash_fails_closed() -> None:
    for junk in ("", "nonsense", "scrypt$notahex$notahex", "md5$aa$bb"):
        assert not verify_password("anything", junk)


@pytest.fixture()
async def client():
    """Same shape as the other acceptance tests: an engine created inside the
    running loop, with get_session overridden to use it. Reaching for the
    app's own engine instead binds futures to a loop that is already gone."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from labtris_api.config import settings
    from labtris_api.db import get_session
    from labtris_api.main import app

    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_the_bootstrap_door_closes_after_the_first_account(client) -> None:
    """An instance with no account cannot be administered, so setup is open
    until it succeeds — and never again, or anyone could mint an admin."""
    r = await client.get("/api/v1/auth/state")

    assert r.status_code == 200
    if r.json()["setup_required"]:
        pytest.skip("this instance has no users; run against one that does")

    r = await client.post(
        "/api/v1/auth/setup",
        json={"username": f"sneak-{ulid.new().str[-6:]}", "password": "x" * 12},
    )
    assert r.status_code == 409


@pytest.mark.anonymous
async def test_an_anonymous_caller_cannot_list_labs(client) -> None:
    r = await client.get("/api/v1/labs")

    assert r.status_code == 401


async def test_a_wrong_password_and_a_missing_user_look_identical(client) -> None:
    """Different messages would tell an attacker which usernames exist and are
    therefore worth spending guesses on."""
    a = await client.post(
        "/api/v1/auth/login", json={"username": "definitely-not-here", "password": "x"}
    )
    b = await client.post("/api/v1/auth/login", json={"username": "rajesh", "password": "x"})

    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["message"] == b.json()["error"]["message"]


async def _throwaway(client) -> tuple[dict[str, Any], str]:
    """A real account to change the password of.

    Reusing a standing user would mean a failed assertion leaves somebody
    unable to sign in, which is a nasty way for a test suite to fail.

    Deliberately a helper and not a fixture. As an async-generator fixture
    this created its account in a different event loop from the test body,
    and the connection the pool handed back was then bound to a loop that no
    longer existed — "attached to a different loop", on a test whose subject
    had nothing to do with either.
    """
    name = f"pwtest-{ulid.new().str[-6:]}"
    r = await client.post(
        "/api/v1/users", json={"username": name, "password": "first-password", "role": "user"}
    )
    assert r.status_code == 201, r.text
    return r.json()["user"], name


async def test_an_admin_resets_a_password_without_knowing_the_old_one(client) -> None:
    """The point of a reset. An admin who had to supply the current password
    could only reset accounts whose password they already knew, which is not
    the situation anybody needs a reset for."""
    created, name = await _throwaway(client)
    try:
        r = await client.post(
            f"/api/v1/users/{created['id']}/password", json={"new_password": "second-password"}
        )
        assert r.status_code == 204, r.text

        good = await client.post(
            "/api/v1/auth/login", json={"username": name, "password": "second-password"}
        )
        stale = await client.post(
            "/api/v1/auth/login", json={"username": name, "password": "first-password"}
        )
        assert good.status_code == 200, good.text
        assert stale.status_code == 401
    finally:
        await client.delete(f"/api/v1/users/{created['id']}")


async def test_changing_your_own_password_requires_the_current_one(client) -> None:
    """A session left open on an unlocked laptop should not be enough to lock
    the owner out of their own account."""
    from tests.conftest import TEST_USER

    missing = await client.post(
        f"/api/v1/users/{TEST_USER.id}/password", json={"new_password": "x" * 12}
    )
    wrong = await client.post(
        f"/api/v1/users/{TEST_USER.id}/password",
        json={"current_password": "not it", "new_password": "x" * 12},
    )

    assert missing.status_code == 403, missing.text
    assert wrong.status_code == 403, wrong.text


async def test_a_regular_user_cannot_change_somebody_elses_password(client) -> None:
    """Otherwise any account on a shared instance is one request away from
    taking over the admin's."""
    from labtris_api.auth import User, get_current_user
    from labtris_api.main import app
    from tests.conftest import TEST_USER

    created, _ = await _throwaway(client)
    plain = User(id=created["id"], name="Pw Test", username=created["username"], role="user")
    was = app.dependency_overrides[get_current_user]
    app.dependency_overrides[get_current_user] = lambda: plain
    try:
        r = await client.post(
            f"/api/v1/users/{TEST_USER.id}/password", json={"new_password": "x" * 12}
        )
    finally:
        # Restored rather than popped: the autouse sign-in fixture installed
        # this, and the cleanup below still has to be an admin.
        app.dependency_overrides[get_current_user] = was
        await client.delete(f"/api/v1/users/{created['id']}")
    assert r.status_code == 403, r.text


async def test_a_password_change_is_refused_below_eight_characters(client) -> None:
    created, _ = await _throwaway(client)
    try:
        r = await client.post(
            f"/api/v1/users/{created['id']}/password", json={"new_password": "short"}
        )
        assert r.status_code == 422, r.text
    finally:
        await client.delete(f"/api/v1/users/{created['id']}")


async def test_every_user_shape_says_whether_they_are_an_admin(client) -> None:
    """The sign-in response, the user list and /auth/me must agree.

    They did not: /auth/me carried is_admin but the login response and the
    user list did not, because both are built by one _public() helper that
    sent `role` and nothing else. The UI gates the Audit log tab on is_admin,
    so an admin who had just signed in did not get the tab — and did get it
    after a reload, when /auth/me answered instead. A bug that fixes itself
    on refresh is one nobody can report.
    """
    name = f"adm-{ulid.new().str[-6:]}"
    created = (
        await client.post(
            "/api/v1/users",
            json={"username": name, "password": "a-password", "role": "admin"},
        )
    ).json()["user"]
    assert created["is_admin"] is True, "the create response hid it"

    signed_in = await client.post(
        "/api/v1/auth/login", json={"username": name, "password": "a-password"}
    )
    assert signed_in.json()["user"]["is_admin"] is True, "the login response hid it"

    listed = (await client.get("/api/v1/users")).json()["users"]
    mine = next(u for u in listed if u["id"] == created["id"])
    assert mine["is_admin"] is True, "the user list hid it"

    #: And a plain user is not quietly promoted by the same field.
    other = f"usr-{ulid.new().str[-6:]}"
    plain = (
        await client.post(
            "/api/v1/users",
            json={"username": other, "password": "a-password", "role": "user"},
        )
    ).json()["user"]
    assert plain["is_admin"] is False
