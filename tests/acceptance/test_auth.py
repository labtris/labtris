"""Signing in, and what shared visibility means.

The model here is a shared workshop, not per-tenant silos: everyone sees
everyone's labs, because an instructor opening a student's lab to help is the
normal case. What separates roles is who may do destructive and instance-wide
things, not who may look.
"""

from __future__ import annotations

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
