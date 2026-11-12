"""Conversations that outlive the browser: the run manager and history.

The manager is exercised against an in-memory store and a scripted agent, so
these tests say what a turn does without a database or a model: it keeps
running with nobody watching, a late watcher catches up, an approval waits
for an answer, cancel stops it, and the next turn sees what was said before.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from labtris_api.agent import LlmUnavailable, StreamCancelled
from labtris_api.assistant_sessions import (
    RunManager,
    SessionGone,
    TurnBusy,
    model_history,
)


class MemStore:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.ev: dict[str, list[dict[str, Any]]] = {}
        self._n = 0

    async def create(self, user_id, lab_id, mode, title):
        self._n += 1
        sid = f"S{self._n}"
        self.sessions[sid] = {"id": sid, "user_id": user_id, "lab_id": lab_id, "mode": mode,
                              "title": title, "status": "idle", "last_seq": 0}
        self.ev[sid] = []
        return dict(self.sessions[sid])

    async def get(self, sid):
        s = self.sessions.get(sid)
        return dict(s) if s else None

    async def list_for(self, lab_id, user_id):
        return [dict(s) for s in self.sessions.values()
                if s["lab_id"] == lab_id and s["user_id"] == user_id]

    async def append(self, sid, kind, payload):
        if sid not in self.sessions:
            raise SessionGone(sid)
        self.sessions[sid]["last_seq"] += 1
        seq = self.sessions[sid]["last_seq"]
        self.ev[sid].append({"seq": seq, "kind": kind, "payload": payload, "at": None})
        return seq

    async def events(self, sid, after_seq=0):
        return [e for e in self.ev.get(sid, []) if e["seq"] > after_seq]

    async def set_status(self, sid, status):
        if sid in self.sessions:
            self.sessions[sid]["status"] = status

    async def delete(self, sid):
        self.sessions.pop(sid, None)
        self.ev.pop(sid, None)

    async def unfinished(self):
        return [s for s, v in self.sessions.items() if v["status"] in ("running", "waiting")]

    def kinds(self, sid):
        return [e["kind"] for e in self.ev[sid]]


def scripted(*steps, seen: list | None = None):
    """An agent_fn that yields the given events. A step that is a callable
    is awaited with the confirm callback, for approvals; a step that is an
    asyncio.Event is waited on, for "the turn is still going"."""

    async def agent(lab_id, message, token=None, cancel=None, confirm=None,
                    attachments=None, history=None):
        if seen is not None:
            seen.append({"message": message, "history": history, "token": token})
        for step in steps:
            if cancel is not None and cancel.cancelled:
                raise StreamCancelled()
            if isinstance(step, asyncio.Event):
                while not step.is_set():
                    if cancel is not None and cancel.cancelled:
                        raise StreamCancelled()
                    await asyncio.sleep(0.01)
            elif isinstance(step, Exception):
                raise step
            elif callable(step):
                async for ev in step(confirm):
                    yield ev
            else:
                yield step

    return agent


async def finish(mgr: RunManager, sid: str) -> None:
    for _ in range(500):
        if not mgr.is_running(sid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn never finished")


async def test_a_turn_is_stored_and_runs_with_nobody_watching() -> None:
    store = MemStore()
    mgr = RunManager(store, scripted(
        {"kind": "text", "delta": "Build"},
        {"kind": "step", "tool": "add_node"},
        {"kind": "turn", "text": "Building", "applied": ["add_node(name=r1)"]},
        {"kind": "turn", "text": "Done.", "applied": []},
    ))
    s = await store.create("U1", "L1", "assistant", "t")
    seq = await mgr.start_turn(s["id"], "L1", "add r1", token="tok")
    assert seq == 1
    await finish(mgr, s["id"])
    # Text deltas and steps are live-only; what was said is the turns.
    assert store.kinds(s["id"]) == ["user", "turn", "turn", "end"]
    assert store.sessions[s["id"]]["status"] == "idle"


async def test_a_second_send_while_running_is_refused() -> None:
    store = MemStore()
    gate = asyncio.Event()
    mgr = RunManager(store, scripted(gate, {"kind": "turn", "text": "ok", "applied": []}))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "one", token=None)
    with pytest.raises(TurnBusy):
        await mgr.start_turn(s["id"], "L1", "two", token=None)
    gate.set()
    await finish(mgr, s["id"])
    # Free again once it ended.
    await mgr.start_turn(s["id"], "L1", "three", token=None)
    await finish(mgr, s["id"])


async def test_a_late_watcher_gets_the_live_text_and_the_rest() -> None:
    store = MemStore()
    gate = asyncio.Event()
    mgr = RunManager(store, scripted(
        {"kind": "text", "delta": "Half a "},
        gate,
        {"kind": "text", "delta": "sentence"},
        {"kind": "turn", "text": "Half a sentence", "applied": []},
    ))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "hi", token=None)
    await asyncio.sleep(0.05)
    q = mgr.subscribe(s["id"])
    assert q is not None
    assert mgr.snapshot(s["id"]) == [{"kind": "text", "delta": "Half a "}]
    gate.set()
    got = []
    while True:
        ev = await asyncio.wait_for(q.get(), 2)
        got.append(ev["kind"])
        if ev["kind"] == "end":
            break
    assert got == ["text", "turn", "end"]


async def test_an_approval_waits_for_the_answer_and_is_recorded() -> None:
    store = MemStore()

    async def ask(confirm):
        allow = await confirm("c1", "delete_lab", {"lab_id": "L1"})
        yield {"kind": "turn", "text": "deleted" if allow else "kept", "applied": []}

    mgr = RunManager(store, scripted(ask))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "delete it", token=None)
    await asyncio.sleep(0.05)
    # Nobody is connected; it waits rather than deciding.
    assert store.sessions[s["id"]]["status"] == "waiting"
    assert mgr.is_running(s["id"])
    assert not mgr.answer(s["id"], "wrong-id", True)
    assert mgr.answer(s["id"], "c1", True)
    await finish(mgr, s["id"])
    kinds = store.kinds(s["id"])
    assert kinds == ["user", "confirm", "answer", "turn", "end"]
    assert store.ev[s["id"]][2]["payload"] == {"id": "c1", "allow": True}
    assert store.ev[s["id"]][3]["payload"]["text"] == "deleted"


async def test_cancel_stops_the_turn_and_denies_a_pending_approval() -> None:
    store = MemStore()

    async def ask(confirm):
        allow = await confirm("c1", "delete_node", {})
        yield {"kind": "turn", "text": f"allow={allow}", "applied": []}

    gate = asyncio.Event()
    mgr = RunManager(store, scripted(ask, gate))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "x", token=None)
    await asyncio.sleep(0.05)
    assert mgr.cancel(s["id"])
    await finish(mgr, s["id"])
    kinds = store.kinds(s["id"])
    assert kinds[:3] == ["user", "confirm", "answer"]
    assert store.ev[s["id"]][2]["payload"]["allow"] is False
    assert kinds[-2:] == ["cancelled", "end"]


async def test_the_next_turn_sees_the_conversation_so_far() -> None:
    store = MemStore()
    seen: list = []
    mgr = RunManager(store, scripted(
        {"kind": "turn", "text": "Added r1.", "applied": ["add_node(name=r1)"]}, seen=seen,
    ))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "add r1", token=None)
    await finish(mgr, s["id"])
    await mgr.start_turn(s["id"], "L1", "now r2", token=None)
    await finish(mgr, s["id"])
    assert seen[0]["history"] == []
    assert seen[1]["history"] == [
        {"role": "user", "content": "add r1"},
        {"role": "assistant", "content": "Added r1.\n\n[tools used: add_node(name=r1)]"},
    ]


async def test_no_llm_falls_back_to_the_planner() -> None:
    store = MemStore()
    mgr = RunManager(store, scripted(LlmUnavailable("no llm")))

    async def plan(lab_id, message):
        return ["added node r1"]

    mgr.plan_fallback = plan
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "add r1", token=None)
    await finish(mgr, s["id"])
    turn = store.ev[s["id"]][1]
    assert turn["kind"] == "turn"
    assert turn["payload"]["applied"] == ["added node r1"]


async def test_an_error_is_recorded_and_the_session_is_free_again() -> None:
    store = MemStore()
    mgr = RunManager(store, scripted(RuntimeError("boom")))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "x", token=None)
    await finish(mgr, s["id"])
    assert store.kinds(s["id"]) == ["user", "error", "end"]
    assert store.sessions[s["id"]]["status"] == "idle"


async def test_deleting_the_session_mid_turn_ends_it_quietly() -> None:
    store = MemStore()
    gate = asyncio.Event()
    mgr = RunManager(store, scripted(gate, {"kind": "turn", "text": "late", "applied": []}))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "x", token=None)
    await store.delete(s["id"])
    gate.set()
    await finish(mgr, s["id"])
    assert s["id"] not in store.sessions


async def test_images_are_not_kept_only_their_names() -> None:
    store = MemStore()
    mgr = RunManager(store, scripted({"kind": "turn", "text": "ok", "applied": []}))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "look", token=None, attachments=[
        {"filename": "topo.png", "mime": "image/png", "data_url": "data:image/png;base64,AAAA"},
    ])
    await finish(mgr, s["id"])
    assert store.ev[s["id"]][0]["payload"]["attachments"] == [
        {"filename": "topo.png", "mime": "image/png"}
    ]


async def test_recover_marks_turns_that_died_with_the_process() -> None:
    store = MemStore()
    s = await store.create("U1", "L1", "assistant", "t")
    await store.append(s["id"], "user", {"message": "x"})
    await store.set_status(s["id"], "waiting")
    mgr = RunManager(store, scripted())
    assert await mgr.recover() == 1
    assert store.kinds(s["id"]) == ["user", "interrupted", "end"]
    assert store.sessions[s["id"]]["status"] == "interrupted"
    # And the conversation takes a new message.
    await mgr.start_turn(s["id"], "L1", "again", token=None)
    await finish(mgr, s["id"])


def _ev(kind, seq, **payload):
    return {"kind": kind, "seq": seq, "payload": payload}


def test_history_folds_a_turn_into_one_reply_with_its_tools() -> None:
    h = model_history([
        _ev("user", 1, message="build a triangle"),
        _ev("turn", 2, text="Adding nodes.", applied=["add_node(r1)", "add_node(r2)"]),
        _ev("confirm", 3, id="c", tool="stop_node", args={}),
        _ev("answer", 4, id="c", allow=False),
        _ev("turn", 5, text="Done.", applied=["stop_node refused: user denied"]),
        _ev("end", 6),
    ])
    assert h == [
        {"role": "user", "content": "build a triangle"},
        {"role": "assistant", "content": (
            "Adding nodes.\n\nDone.\n\n[tools used: add_node(r1); add_node(r2); "
            "stop_node refused: user denied]"
        )},
    ]


def test_history_notes_a_stopped_or_broken_turn() -> None:
    h = model_history([
        _ev("user", 1, message="a"), _ev("cancelled", 2), _ev("end", 3),
        _ev("user", 4, message="b"), _ev("interrupted", 5), _ev("end", 6),
    ])
    assert h[1]["content"] == "[the user stopped this turn]"
    assert "cut off by a restart" in h[3]["content"]


def test_history_keeps_the_newest_exchanges_within_budget() -> None:
    events = []
    for i in range(50):
        events.append(_ev("user", 2 * i + 1, message=f"q{i} " + "x" * 200))
        events.append(_ev("turn", 2 * i + 2, text=f"a{i} " + "y" * 200, applied=[]))
    h = model_history(events, budget=2_000)
    assert h[-2]["content"].startswith("q49")
    assert h[0]["content"].startswith("(Earlier parts of this conversation were omitted.)")
    assert sum(len(m["content"]) for m in h) < 2_000 + 200
    assert [m["role"] for m in h[:2]] == ["user", "assistant"]


def test_history_of_a_new_conversation_is_empty() -> None:
    assert model_history([]) == []


async def test_a_watcher_still_gets_an_end_when_the_session_is_deleted_mid_turn() -> None:
    # Found on labtris-012: an approved delete_lab on the conversation's own
    # lab deletes the session with it, and the watching tab waited forever.
    store = MemStore()
    gate = asyncio.Event()
    mgr = RunManager(store, scripted(gate, {"kind": "turn", "text": "deleted", "applied": []}))
    s = await store.create("U1", "L1", "assistant", "t")
    await mgr.start_turn(s["id"], "L1", "delete this lab", token=None)
    q = mgr.subscribe(s["id"])
    assert q is not None
    await store.delete(s["id"])
    gate.set()
    await finish(mgr, s["id"])
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    assert [e["kind"] for e in got] == ["error", "end"]
    assert all(e["seq"] is None for e in got)
