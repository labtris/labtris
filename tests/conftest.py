from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from labtris_api.auth import User, get_current_user, require_admin
from labtris_api.main import create_app

#: Who the acceptance tests are. Authentication has one dedicated test file;
#: everywhere else it is a precondition, not the subject, and making every
#: test log in first would only prove the login endpoint works 50 more times.
TEST_USER = User(id="01TESTUSER00000000000001", name="Test", username="test", role="admin")


def _sign_in(application: Any) -> Any:
    application.dependency_overrides[get_current_user] = lambda: TEST_USER
    application.dependency_overrides[require_admin] = lambda: TEST_USER
    return application


@pytest.fixture(autouse=True)
async def _test_user_exists():
    """Labs reference their owner now, so the identity the tests run as has to
    be a real row — otherwise every lab creation trips the foreign key, and
    create_lab reports it as a duplicate name."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from labtris_api.config import settings
    from labtris_api.models import User as UserRow

    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            found = (
                await session.execute(select(UserRow).where(UserRow.id == TEST_USER.id))
            ).scalar_one_or_none()
            if found is None:
                session.add(
                    UserRow(
                        id=TEST_USER.id,
                        username=TEST_USER.username,
                        display_name=TEST_USER.name,
                        password_hash="scrypt$00$00",  # unusable: nobody signs in as this
                        role="admin",
                    )
                )
                await session.commit()
    except Exception:  # noqa: BLE001 - suites without a database skip themselves
        pass
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _signed_in(request, monkeypatch):
    """Every app a test builds comes pre-authenticated.

    Most suites call create_app() inside their own fixture so they can also
    override the database session, so overriding one shared instance reaches
    none of them — and they bind the name at import, so patching only
    labtris_api.main leaves their reference pointing at the original.
    Both are patched here, plus the module-level singleton the couple of
    suites that use it hold.
    """
    import sys

    import labtris_api.main as main

    # A test that is *about* being unauthenticated has to be able to opt out,
    # or the fixture meant to keep other suites working quietly defeats it.
    # Still a yield: a generator fixture that returns early is an error.
    if request.node.get_closest_marker("anonymous"):
        yield
        return

    original = main.create_app

    def signed_in_app(*args: Any, **kwargs: Any) -> Any:
        return _sign_in(original(*args, **kwargs))

    monkeypatch.setattr(main, "create_app", signed_in_app)
    # Match on identity, not module name: pytest imports these as bare
    # `test_networks` rather than `tests.acceptance.test_networks`, so
    # filtering by name silently patched nothing. Anything still holding the
    # original reference gets the wrapped one for the duration of the test.
    for module in list(sys.modules.values()):
        if getattr(module, "create_app", None) is original:
            monkeypatch.setattr(module, "create_app", signed_in_app, raising=False)

    _sign_in(main.app)
    yield
    for dep in (get_current_user, require_admin):
        main.app.dependency_overrides.pop(dep, None)


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def post(client: AsyncClient, path: str, body: dict[str, Any] | None = None) -> Any:
    r = await client.post(path, json=body or {})
    assert r.status_code in {200, 201}, r.text
    return r.json() if r.content else None


async def delete(client: AsyncClient, path: str) -> None:
    r = await client.delete(path)
    assert r.status_code in {200, 204}, r.text
