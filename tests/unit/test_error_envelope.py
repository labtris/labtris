"""Every error response must be JSON, including the ones nobody planned for.

`unhandled_error_handler` existed but was never registered, so an unhandled
exception fell through to Starlette's default plain-text "Internal Server
Error". The UI parses every response as JSON, so what reached the user was
`Unexpected token 'I', "Internal S"... is not valid JSON` — the parser's
complaint standing in for a real failure it never got to see.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from labtris_api.errors import ApiError, api_error_handler, not_found, unhandled_error_handler


@pytest.fixture()
def app() -> FastAPI:
    application = FastAPI()
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, unhandled_error_handler)

    @application.get("/boom")
    async def boom() -> None:
        raise ConnectionRefusedError(111, "Connection refused")

    @application.get("/known")
    async def known() -> None:
        raise not_found("no such node")

    return application


async def _get(app: FastAPI, path: str):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_an_unhandled_exception_still_answers_in_json(app: FastAPI) -> None:
    r = await _get(app, "/boom")

    assert r.status_code == 500
    body = r.json()  # the assertion that matters: this must not raise
    assert body["error"]["code"] == "internal"


async def test_the_message_names_the_failure_rather_than_hiding_it(app: FastAPI) -> None:
    """A single-tenant tool you run yourself; "an error occurred" costs an
    afternoon of guessing."""
    r = await _get(app, "/boom")

    assert "ConnectionRefusedError" in r.json()["error"]["message"]


async def test_deliberate_errors_are_unchanged(app: FastAPI) -> None:
    r = await _get(app, "/known")

    assert r.status_code == 404
    assert r.json()["error"] == {"code": "not_found", "message": "no such node", "detail": {}}
