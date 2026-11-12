"""Conversations kept on the server, against a real database.

A scripted OpenAI-compatible endpoint stands in for the model. Nothing here
calls a tool that needs the API running behind it: the turns either answer in
text or ask for an approval that is denied, which is enough to show what is
stored, who can read it, and that the next turn is given what came before.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_agent import _call, _can_db

from labtris_api.assistant_sessions import DbStore, RunManager
from labtris_api.auth import User, get_current_user
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.main import create_app


class ScriptedLLM:
    """An OpenAI-compatible endpoint that streams replies from a script.

    The assistant reads the model's reply as a server-sent event stream, so
    the stand-in has to speak it: one delta for the text, one per tool call,
    then [DONE]."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.seen: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append(body)
                msg = outer.script[min(len(outer.seen) - 1, len(outer.script) - 1)]
                deltas: list[dict[str, Any]] = []
                if msg.get("content"):
                    deltas.append({"content": msg["content"]})
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    deltas.append({"tool_calls": [{
                        "index": i, "id": tc["id"], "type": "function",
                        "function": tc["function"],
                    }]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for d in deltas:
                    chunk = {"choices": [{"delta": d}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
async def app_client():
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
        yield app, ac
    await engine.dispose()


async def _lab(ac: AsyncClient) -> dict[str, Any]:
    r = await ac.post("/api/v1/labs", json={"name": f"conv-{ulid.new().str[-8:].lower()}"})
    assert r.status_code == 201, r.text
    return r.json()


async def _finish(mgr: RunManager, sid: str) -> None:
    for _ in range(500):
        if not mgr.is_running(sid):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("turn never finished")


@pytest.fixture
def scripted_llm(monkeypatch):
    llms: list[ScriptedLLM] = []

    def make(script: list[dict[str, Any]]) -> ScriptedLLM:
        llm = ScriptedLLM(script)
        llms.append(llm)
        monkeypatch.setattr(settings, "llm_base_url", llm.url)
        monkeypatch.setattr(settings, "llm_api_key", "")
        return llm

    yield make
    for llm in llms:
        llm.stop()


async def test_a_conversation_is_stored_and_the_next_turn_remembers_it(
    app_client, scripted_llm
) -> None:
    app, ac = app_client
    lab = await _lab(ac)
    store = DbStore()
    mgr = RunManager(store)
    llm = scripted_llm([{"role": "assistant", "content": "Noted: three leaves."}])
    try:
        sess = await store.create("01TESTUSER00000000000001", lab["id"], "assistant", "plan")
        await mgr.start_turn(sess["id"], lab["id"], "we want three leaves", token=None)
        await _finish(mgr, sess["id"])
        await mgr.start_turn(sess["id"], lab["id"], "how many leaves?", token=None)
        await _finish(mgr, sess["id"])

        events = await store.events(sess["id"])
        assert [e["kind"] for e in events] == ["user", "turn", "end", "user", "turn", "end"]
        assert [e["seq"] for e in events] == [1, 2, 3, 4, 5, 6]
        assert (await store.get(sess["id"]))["status"] == "idle"

        # The second request carried the first exchange.
        second = llm.seen[-1]["messages"]
        assert second[1] == {"role": "user", "content": "we want three leaves"}
        assert second[2] == {"role": "assistant", "content": "Noted: three leaves."}
        assert "how many leaves?" in second[3]["content"]

        # Readable over the API, after the fact, by its owner.
        r = await ac.get(f"/api/v1/ai/sessions/{sess['id']}/events?after_seq=3")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [e["kind"] for e in body["events"]] == ["user", "turn", "end"]
        assert body["events"][0]["message"] == "how many leaves?"
        r = await ac.get(f"/api/v1/labs/{lab['id']}/ai/sessions")
        assert [s["id"] for s in r.json()["sessions"]] == [sess["id"]]
    finally:
        await ac.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_denied_approval_is_recorded_and_nothing_runs(app_client, scripted_llm) -> None:
    app, ac = app_client
    lab = await _lab(ac)
    store = DbStore()
    mgr = RunManager(store)
    scripted_llm([
        _call("delete_lab", {"lab_id": lab["id"]}),
        {"role": "assistant", "content": "Left it alone."},
    ])
    try:
        sess = await store.create("01TESTUSER00000000000001", lab["id"], "assistant", "t")
        await mgr.start_turn(sess["id"], lab["id"], "delete this lab", token=None)
        for _ in range(200):
            if (await store.get(sess["id"]))["status"] == "waiting":
                break
            await asyncio.sleep(0.02)
        assert (await store.get(sess["id"]))["status"] == "waiting"
        pending = [e for e in await store.events(sess["id"]) if e["kind"] == "confirm"]
        assert mgr.answer(sess["id"], pending[0]["payload"]["id"], False)
        await _finish(mgr, sess["id"])
        kinds = [e["kind"] for e in await store.events(sess["id"])]
        assert kinds == ["user", "confirm", "answer", "turn", "turn", "end"]
        assert (await ac.get(f"/api/v1/labs/{lab['id']}")).status_code == 200
    finally:
        await ac.delete(f"/api/v1/labs/{lab['id']}")


async def test_someone_elses_conversation_is_not_found(app_client) -> None:
    app, ac = app_client
    lab = await _lab(ac)
    store = DbStore()
    try:
        sess = await store.create("01TESTUSER00000000000001", lab["id"], "assistant", "mine")
        other = User(id="01OTHERUSER0000000000001", name="O", username="o", role="admin")
        app.dependency_overrides[get_current_user] = lambda: other
        for method, path in (
            ("GET", f"/api/v1/ai/sessions/{sess['id']}/events"),
            ("POST", f"/api/v1/ai/sessions/{sess['id']}/cancel"),
            ("DELETE", f"/api/v1/ai/sessions/{sess['id']}"),
        ):
            r = await ac.request(method, path)
            assert r.status_code == 404, (method, path, r.status_code)
        r = await ac.get(f"/api/v1/labs/{lab['id']}/ai/sessions")
        assert r.json()["sessions"] == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        await ac.delete(f"/api/v1/labs/{lab['id']}")


async def test_deleting_the_lab_deletes_its_conversations(app_client) -> None:
    app, ac = app_client
    lab = await _lab(ac)
    store = DbStore()
    sess = await store.create("01TESTUSER00000000000001", lab["id"], "assistant", "t")
    await store.append(sess["id"], "user", {"message": "hi"})
    assert (await ac.delete(f"/api/v1/labs/{lab['id']}")).status_code in (200, 204)
    assert await store.get(sess["id"]) is None
    assert await store.events(sess["id"]) == []


async def test_new_and_delete_over_the_api(app_client) -> None:
    app, ac = app_client
    lab = await _lab(ac)
    try:
        r = await ac.post(f"/api/v1/labs/{lab['id']}/ai/sessions", json={"title": "fresh"})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        assert r.json()["status"] == "idle"
        assert (await ac.delete(f"/api/v1/ai/sessions/{sid}")).status_code == 204
        assert (await ac.get(f"/api/v1/ai/sessions/{sid}/events")).status_code == 404
    finally:
        await ac.delete(f"/api/v1/labs/{lab['id']}")


async def test_startup_marks_a_turn_that_died_with_the_process(app_client) -> None:
    app, ac = app_client
    lab = await _lab(ac)
    store = DbStore()
    try:
        sess = await store.create("01TESTUSER00000000000001", lab["id"], "assistant", "t")
        await store.append(sess["id"], "user", {"message": "x"})
        await store.set_status(sess["id"], "running")
        assert await RunManager(store).recover() >= 1
        assert (await store.get(sess["id"]))["status"] == "interrupted"
        kinds = [e["kind"] for e in await store.events(sess["id"])]
        assert kinds == ["user", "interrupted", "end"]
    finally:
        await ac.delete(f"/api/v1/labs/{lab['id']}")
