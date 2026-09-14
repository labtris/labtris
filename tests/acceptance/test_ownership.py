"""Who may destroy what.

Shared visibility was the decision: everyone sees and opens everyone's labs,
and two people working in one at the same time is a thing this supports. What
that must not mean is shared destruction — before this, any signed-in user
could delete anybody's lab, and ownership was decoration.

The split is deliberate. Building is collaborative; deleting is owned.
"""

from __future__ import annotations

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from labtris_api.auth import User, get_current_user
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.main import create_app

OWNER = User(id="01OWNERTEST0000000000001", name="Owner", username="owner", role="user")
STRANGER = User(id="01STRANGER000000000000001", name="Stranger", username="stranger", role="user")
BOSS = User(id="01ADMINTEST0000000000001", name="Boss", username="boss", role="admin")


@pytest.fixture()
async def clients():
    """A factory for "the same instance, seen as a different person".

    Follows the pattern the other acceptance suites use, including disposing
    the module-level engine: connections it opened belong to a loop that is
    gone by the time the next test runs, and asyncpg says so loudly.
    """
    from labtris_api.db import engine as db_engine
    from labtris_api.models import User as UserRow

    # Seed on an engine of its own and dispose it before the app gets one.
    # Sharing a pool between the seeding session and the request sessions left
    # a connection checked out against a loop the requests do not run in, and
    # asyncpg fails the first query with "attached to a different loop".
    seed_engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(seed_engine, expire_on_commit=False)() as session:
        for who in (OWNER, STRANGER, BOSS):
            if await session.get(UserRow, who.id) is None:
                session.add(
                    UserRow(id=who.id, username=who.username, display_name=who.name,
                            password_hash="scrypt$00$00", role=who.role)
                )
        await session.commit()
    await seed_engine.dispose()

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session

    def as_user(user: User) -> AsyncClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield as_user
    await engine.dispose()
    await db_engine.dispose()


async def _make_lab(as_user) -> dict:
    async with as_user(OWNER) as c:
        r = await c.post("/api/v1/labs", json={"name": f"own-{ulid.new().str[-8:].lower()}"})
        assert r.status_code == 201, r.text
        return r.json()


async def test_a_stranger_can_see_and_open_someone_elses_lab(clients) -> None:
    """The whole point of the shared model — an instructor opening a student's
    lab to look at it must not need permission."""
    lab = await _make_lab(clients)
    async with clients(STRANGER) as c:
        listing = await c.get("/api/v1/labs")
        detail = await c.get(f"/api/v1/labs/{lab['id']}")

    assert lab["name"] in [x["name"] for x in listing.json()]
    assert detail.status_code == 200

    async with clients(OWNER) as c:
        await c.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_stranger_can_build_in_it(clients) -> None:
    """Two people in one lab is a supported workflow, so adding a node cannot
    require owning the lab — otherwise co-working means watching."""
    lab = await _make_lab(clients)
    async with clients(STRANGER) as c:
        r = await c.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": "theirs", "runtime": "docker", "image": "alpine:3.20",
                  "interfaces": [{}]},
        )
    assert r.status_code == 201, r.text

    async with clients(OWNER) as c:
        await c.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_stranger_cannot_delete_it(clients) -> None:
    """This is the hole that existed until now."""
    lab = await _make_lab(clients)
    async with clients(STRANGER) as c:
        r = await c.delete(f"/api/v1/labs/{lab['id']}")

    assert r.status_code == 403
    assert "belongs to someone else" in r.text

    async with clients(STRANGER) as c:
        assert (await c.get(f"/api/v1/labs/{lab['id']}")).status_code == 200, "still there"

    async with clients(OWNER) as c:
        await c.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_stranger_cannot_rename_it(clients) -> None:
    lab = await _make_lab(clients)
    async with clients(STRANGER) as c:
        r = await c.patch(f"/api/v1/labs/{lab['id']}", json={"name": "hijacked"})

    assert r.status_code == 403

    async with clients(OWNER) as c:
        await c.delete(f"/api/v1/labs/{lab['id']}")


async def test_the_owner_can_delete_it(clients) -> None:
    lab = await _make_lab(clients)
    async with clients(OWNER) as c:
        assert (await c.delete(f"/api/v1/labs/{lab['id']}")).status_code == 204


async def test_an_admin_can_delete_anyone_s(clients) -> None:
    """Someone has to be able to clear up after a student who left, and an
    instructor is an admin."""
    lab = await _make_lab(clients)
    async with clients(BOSS) as c:
        assert (await c.delete(f"/api/v1/labs/{lab['id']}")).status_code == 204


async def test_an_unowned_lab_is_not_undeletable(clients) -> None:
    """Labs made before users existed have no owner. Refusing everyone would
    strand them forever."""
    from sqlalchemy import update

    from labtris_api.models import Lab

    lab = await _make_lab(clients)
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await s.execute(update(Lab).where(Lab.id == lab["id"]).values(owner_id=None))
        await s.commit()
    await engine.dispose()

    async with clients(STRANGER) as c:
        assert (await c.delete(f"/api/v1/labs/{lab['id']}")).status_code == 204
