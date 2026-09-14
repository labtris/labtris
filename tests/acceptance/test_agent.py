from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api import agent
from labtris_api.config import settings
from labtris_api.db import engine as db_engine
from labtris_api.db import get_session
from labtris_api.main import create_app


async def _can_db() -> bool:
    try:
        engine = create_async_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        await engine.dispose()
        return True
    except Exception:
        return False


class ScriptedLLM:
    """An OpenAI-compatible endpoint that replies from a fixed script.

    The point is to exercise our half of the loop — schema conversion, tool
    dispatch, feeding results back, the step cap — without a key, a network
    call, or a model's judgement in the way. A real model's *choices* are not
    ours to test; the plumbing carrying them is."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.seen: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:  # /models liveness probe
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append(body)
                reply = outer.script[min(len(outer.seen) - 1, len(outer.script) - 1)]
                payload = json.dumps({"choices": [{"message": reply}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()


def _call(name: str, args: dict[str, Any], cid: str = "c1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


@pytest.fixture
async def client():
    if not await _can_db():
        pytest.skip("Postgres is not available (start with `make dev-db`)")
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_session():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await engine.dispose()
    await db_engine.dispose()


def test_the_model_is_offered_every_mcp_tool() -> None:
    """One tool table, two front ends. A tool added for an external agent is
    available to the built-in assistant without a second definition."""
    from labtris_mcp.tools import TOOLS

    tools = agent._openai_tools()
    assert {t["function"]["name"] for t in tools} == set(TOOLS)
    for t in tools:
        assert t["type"] == "function"
        assert t["function"]["description"]
        assert t["function"]["parameters"]["type"] == "object"


async def test_the_assistant_acts_through_the_tools(client: AsyncClient, monkeypatch) -> None:
    """End to end: the model asks for a node, we execute it against the real
    API, feed the result back, and it answers. The node has to actually exist
    afterwards — the assistant reporting success is not evidence."""
    r = await client.post("/api/v1/labs", json={"name": f"ag-{ulid.new().str[-8:].lower()}"})
    lab = r.json()
    name = f"agent-made-{ulid.new().str[-4:].lower()}"

    llm = ScriptedLLM(
        [
            _call("add_node", {"lab_id": lab["id"], "name": name,
                               "runtime": "docker", "image": "alpine:3.20"}),
            {"role": "assistant", "content": f"Added {name}."},
        ]
    )
    monkeypatch.setattr(settings, "llm_base_url", llm.url)
    monkeypatch.setattr(settings, "llm_api_key", "")
    try:
        r = await client.post(f"/api/v1/labs/{lab['id']}/ai", json={"message": "add a node"})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["llm"] is True
        assert name in out["reply"]
        assert any("add_node" in a for a in out["applied"]), out["applied"]

        detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()
        assert name in [n["name"] for n in detail["nodes"]], "the tool call must have run for real"

        # The tool results have to reach the model, or it is guessing.
        last = llm.seen[-1]["messages"]
        assert any(m.get("role") == "tool" for m in last), "tool output was never fed back"
    finally:
        llm.stop()
        await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_refusal_is_handed_back_to_the_model_not_raised(
    client: AsyncClient, monkeypatch
) -> None:
    """A rejected call is information, not a crash: the model should see why
    and pick something else rather than the turn dying."""
    r = await client.post("/api/v1/labs", json={"name": f"ag2-{ulid.new().str[-8:].lower()}"})
    lab = r.json()
    llm = ScriptedLLM(
        [
            _call("create_network", {"lab_id": lab["id"], "name": "x", "kind": "cloud",
                                     "cloud_ref": "definitely-not-a-nic"}),
            {"role": "assistant", "content": "That interface does not exist."},
        ]
    )
    monkeypatch.setattr(settings, "llm_base_url", llm.url)
    try:
        r = await client.post(f"/api/v1/labs/{lab['id']}/ai", json={"message": "make a cloud"})
        assert r.status_code == 200, r.text
        assert r.json()["llm"] is True
        tool_msgs = [m for m in llm.seen[-1]["messages"] if m.get("role") == "tool"]
        assert tool_msgs and "error" in tool_msgs[0]["content"], tool_msgs
    finally:
        llm.stop()
        await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_no_llm_configured_says_so_rather_than_pretending(
    client: AsyncClient, monkeypatch
) -> None:
    r = await client.post("/api/v1/labs", json={"name": f"ag3-{ulid.new().str[-8:].lower()}"})
    lab = r.json()
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1")
    r = await client.post(f"/api/v1/labs/{lab['id']}/ai", json={"message": "hello"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["llm"] is False
    assert "cannot reach the LLM" in out["reply"]
    await client.delete(f"/api/v1/labs/{lab['id']}")
