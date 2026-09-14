"""The agent loop now returns one entry per model turn — text plus the
tool calls that turn made — so the UI can render each step as its own
chat bubble rather than a summary and a flat list of function names.

`_post` is mocked with a scripted transcript so the loop runs
deterministically against a canned two-turn back-and-forth."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from labtris_api import agent as agent_module


class _FakePost:
    """Replays a list of canned OpenAI-shape choices, one per call.

    A `_post` monkeypatch returns the next reply on every invocation, so
    a test can script a full "assistant turn 1 → tool call → assistant
    turn 2" transcript in three entries."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
        self.calls.append(body)
        if not self.replies:
            # If the loop asks for more than we scripted, something is
            # wrong with the exit condition — surface it loudly.
            raise AssertionError("agent asked for more turns than scripted")
        return self.replies.pop(0)


def _reply_to_stream(reply: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a scripted non-streaming reply into the sequence of SSE-shaped
    delta chunks the streaming path would receive from a real provider.

    Emit one delta for the content, one delta per tool-call slot, and a
    final finish_reason delta. The important invariants for the tests
    are that the assembled message ends up shaped like the original
    `choices[0].message` and that at least one delta carries content
    text or a tool-call name."""
    msg = (reply.get("choices") or [{}])[0].get("message") or {}
    chunks: list[dict[str, Any]] = []
    if msg.get("content"):
        chunks.append({"choices": [{"delta": {"content": msg["content"]}}]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        chunks.append({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": i,
                        "id": tc.get("id", ""),
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": (tc.get("function") or {}).get("name", ""),
                            "arguments": (tc.get("function") or {}).get("arguments", ""),
                        },
                    }],
                }
            }]
        })
    if not chunks:
        # A totally empty reply still needs to yield something so the
        # aggregator sees an end-of-stream cleanly.
        chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return chunks


class _FakePostStream:
    """Streaming counterpart to _FakePost — pops one canned reply per
    invocation and yields the SSE-shaped chunks that reply would produce
    in a real streaming response. Registered as `_post_stream` on the
    agent module so the existing scripted-reply tests keep working
    against the new streaming code path."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: dict[str, Any], key: str, timeout: int) -> Any:
        self.calls.append(body)
        if not self.replies:
            raise AssertionError("agent asked for more turns than scripted")
        reply = self.replies.pop(0)
        return iter(_reply_to_stream(reply))


def _msg(content: str | None, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Shape one OpenAI `choices[0].message` for _FakePost."""
    m: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = calls
    return {"choices": [{"message": m}]}


def _tc(id_: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    import json

    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture()
def stub_tools(monkeypatch):
    """Replace the TOOLS table with a single canned tool so we don't
    depend on the real MCP surface (and don't accidentally hit HTTP)."""
    def _ok(_api: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "args": args}

    monkeypatch.setattr(
        agent_module,
        "TOOLS",
        {"pretend": ("pretend a tool", {}, _ok)},
    )
    # And a minimal OpenAI tool list so the request body is valid.
    monkeypatch.setattr(agent_module, "_openai_tools", lambda: [{"type": "function", "function": {"name": "pretend"}}])


@pytest.fixture()
def scripted(monkeypatch, stub_tools):
    """Return a factory that installs scripted _FakePost + _FakePostStream
    onto the module. The agent loop uses `_post_stream` for the LLM call
    now; the non-streaming `_post` is still exercised by the liveness
    probe elsewhere. Faking both keeps tests decoupled from which one the
    code happens to call."""
    def _install(replies: list[dict[str, Any]]) -> _FakePost:
        post = _FakePost(replies)
        stream = _FakePostStream(replies)
        monkeypatch.setattr(agent_module, "_post", post)
        monkeypatch.setattr(agent_module, "_post_stream", stream)
        # Provide a minimal settings object so the loop can start.
        monkeypatch.setattr(agent_module.settings, "llm_base_url", "http://x", raising=False)
        monkeypatch.setattr(agent_module.settings, "llm_api_key", "k", raising=False)
        monkeypatch.setattr(agent_module.settings, "llm_model", "m", raising=False)
        monkeypatch.setattr(agent_module.settings, "llm_max_steps", 5, raising=False)
        monkeypatch.setattr(agent_module.settings, "self_url", "http://x", raising=False)
        return post
    return _install


async def test_a_single_final_reply_still_returns_one_turn(scripted) -> None:
    """The simple case: the model answers immediately, no tools. The
    response should still carry the shape the UI expects — a `turns`
    array with one entry — so the browser has one code path."""
    scripted([_msg("hello there")])
    r = await agent_module.run_agent("lab1", "hi")
    assert len(r["turns"]) == 1
    assert r["turns"][0]["text"] == "hello there"
    assert r["turns"][0]["applied"] == []
    # Back-compat fields still present.
    assert r["reply"] == "hello there"
    assert r["applied"] == []


async def test_narration_between_tool_calls_becomes_two_turns(scripted) -> None:
    """The whole point of the change: the model's narration between tool
    calls is preserved as a separate turn, not concatenated onto the
    final summary or discarded."""
    scripted([
        # Turn 1: narrates + makes one tool call.
        _msg("Let me check the lab first.", [_tc("c1", "pretend", {"a": 1})]),
        # Turn 2: reports the result, no more tools.
        _msg("Done — everything looks right."),
    ])
    r = await agent_module.run_agent("lab1", "do the thing")
    assert len(r["turns"]) == 2
    assert r["turns"][0]["text"] == "Let me check the lab first."
    assert "pretend(a=1)" in r["turns"][0]["applied"]
    assert r["turns"][1]["text"] == "Done — everything looks right."
    assert r["turns"][1]["applied"] == []
    # The last turn's text is the compat `reply`, and applied is the
    # concatenation across all turns.
    assert r["reply"] == "Done — everything looks right."
    assert r["applied"] == ["pretend(a=1)"]


async def test_a_turn_with_only_tool_calls_and_no_text_still_lands(scripted) -> None:
    """Some models emit no narration text — just tool calls, then a final
    reply. The intermediate turn should still show up in `turns` (with
    empty text) so the applied list has a home; the UI is responsible
    for skipping empty bubbles."""
    scripted([
        _msg(None, [_tc("c1", "pretend", {})]),   # no text, one tool call
        _msg("all done"),
    ])
    r = await agent_module.run_agent("lab1", "go")
    assert len(r["turns"]) == 2
    assert r["turns"][0]["text"] == ""
    assert r["turns"][0]["applied"] == ["pretend()"]
    assert r["turns"][1]["text"] == "all done"


async def test_step_cap_stops_the_loop_and_records_it_as_the_last_turn(scripted, monkeypatch) -> None:
    """When the model keeps calling tools and never says stop, the loop
    hits the runaway backstop and reports so — as a real turn in the
    transcript, not a silent truncation. The synthetic turn also names
    the tools that actually landed so the user can see what got done
    before deciding whether to say "continue" or fix by hand.

    Uses the default max_steps from the scripted fixture (5); the script
    matches that so the cap trips deterministically."""
    replies = [
        _msg(f"t{i}", [_tc(f"c{i}", "pretend", {})]) for i in range(5)
    ]
    scripted(replies)
    r = await agent_module.run_agent("lab1", "loop")
    # Five real turns + one synthetic "runaway backstop" turn.
    assert len(r["turns"]) == 6
    tail = r["turns"][-1]["text"]
    assert "runaway backstop" in tail
    assert "5 tool calls" in tail
    # The five pretend() calls should be summarised (deduped to "pretend() ×5").
    assert "pretend()" in tail
    assert r["reply"] == r["turns"][-1]["text"]


# ---------------------------------------------------------------- streaming


async def test_stream_agent_yields_step_before_each_tool_call(scripted) -> None:
    """The websocket path relies on a `step` event landing before each
    tool call fires — that's what the working-strip in the UI uses to
    name what's currently in flight. If this test regresses, the strip
    would silently go blank whenever the model is between tools."""
    scripted([
        _msg("checking", [_tc("c1", "pretend", {"x": 1})]),
        _msg("done"),
    ])
    events = [ev async for ev in agent_module.stream_agent("lab1", "hi")]
    kinds = [e["kind"] for e in events]
    # Text lands first as the model narrates, then step (announcing the
    # tool call), then the turn terminator. The important invariant is
    # that a step always precedes its own turn's execution.
    assert "text" in kinds
    step_idx = kinds.index("step")
    first_turn_idx = kinds.index("turn")
    assert step_idx < first_turn_idx, f"step must precede its turn; got {kinds}"
    # The step names the right tool, and the first turn carries the
    # applied list.
    step = events[step_idx]
    assert step == {"kind": "step", "tool": "pretend"}
    first_turn = events[first_turn_idx]
    assert first_turn["applied"] == ["pretend(x=1)"]
    assert first_turn["text"] == "checking"
    # And the final turn is the "done" reply.
    assert events[-1] == {"kind": "turn", "text": "done", "applied": []}


async def test_stream_agent_multiple_tools_in_one_turn_get_a_step_each(scripted) -> None:
    """A model that batches two tool calls in one turn should emit two
    step events so the UI can name each one as it runs, not just the
    first — otherwise the strip freezes on the first tool for the whole
    duration of a multi-call turn."""
    scripted([
        _msg("doing both", [
            _tc("c1", "pretend", {"a": 1}),
            _tc("c2", "pretend", {"b": 2}),
        ]),
        _msg("done"),
    ])
    events = [ev async for ev in agent_module.stream_agent("lab1", "hi")]
    steps = [e for e in events if e["kind"] == "step"]
    assert steps == [{"kind": "step", "tool": "pretend"}, {"kind": "step", "tool": "pretend"}]


async def test_run_agent_still_batches_the_stream(scripted) -> None:
    """The compat wrapper must produce the same shape it always has, so
    MCP callers and older tests keep working. This is the invariant the
    external POST endpoint relies on."""
    scripted([
        _msg("t1", [_tc("c1", "pretend", {})]),
        _msg("t2"),
    ])
    r = await agent_module.run_agent("lab1", "hi")
    # Same shape run_agent has always returned: turns list + reply + applied.
    assert set(r.keys()) == {"turns", "reply", "applied", "llm"}
    assert len(r["turns"]) == 2
    assert r["reply"] == "t2"
    assert r["applied"] == ["pretend()"]


# ---------------------------------------------------------------- token stream


async def test_stream_agent_yields_text_deltas_as_they_arrive(scripted) -> None:
    """The whole point of token streaming: the UI sees text events land
    before the turn's terminator. A model producing "hello world" as one
    content string in the reply becomes one text delta on the wire (and
    the assembler still puts it in the terminating turn's text)."""
    scripted([_msg("hello world")])
    events = [ev async for ev in agent_module.stream_agent("lab1", "hi")]
    kinds = [e["kind"] for e in events]
    assert "text" in kinds, f"no text event; got {kinds}"
    # The text delta's content should be present.
    text_events = [e for e in events if e["kind"] == "text"]
    assert "".join(e["delta"] for e in text_events) == "hello world"
    # And the terminating turn still carries the assembled text.
    turn = [e for e in events if e["kind"] == "turn"][-1]
    assert turn["text"] == "hello world"


async def test_merge_delta_folds_tool_call_arguments() -> None:
    """Providers stream tool_call arguments as multiple partial deltas
    (`{"a`, `":1`, `}`). The merger has to reassemble them in order or
    the eventual JSON parse would silently fail."""
    assembled: dict[str, Any] = {}
    agent_module._merge_delta(assembled, {
        "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "pretend"}}]
    })
    agent_module._merge_delta(assembled, {
        "tool_calls": [{"index": 0, "function": {"arguments": '{"a'}}]
    })
    agent_module._merge_delta(assembled, {
        "tool_calls": [{"index": 0, "function": {"arguments": '":1}'}}]
    })
    assert assembled["tool_calls"][0]["function"]["name"] == "pretend"
    assert assembled["tool_calls"][0]["function"]["arguments"] == '{"a":1}'


async def test_merge_delta_recognises_reasoning_variants() -> None:
    """LiteLLM normalises `reasoning_content` / `reasoning` / `thinking`
    depending on the underlying provider. The UI should see reasoning
    from any of them, so the merger has to accept all three shapes."""
    for key in ("reasoning_content", "reasoning", "thinking"):
        assembled: dict[str, Any] = {}
        _text, reasoning = agent_module._merge_delta(assembled, {key: "step 1"})
        assert reasoning == "step 1", f"key {key!r} was not surfaced"
        assert assembled["reasoning_content"] == "step 1"


# ---------------------------------------------------------------- confirm gate


async def test_confirm_denial_reflects_as_a_403_to_the_model(scripted) -> None:
    """A user denial should look to the model like an API 403 refusal,
    same shape as any other tool-call error — so the model can decide to
    explain what it was going to do rather than trying again."""
    scripted([
        _msg(None, [_tc("c1", "delete_lab", {"lab_id": "L1"})]),
        _msg("ok, will not delete"),
    ])
    # Ensure delete_lab is in TOOLS for the loop (the stub_tools fixture
    # replaces TOOLS with just "pretend"; add delete_lab so the gate can
    # fire without hitting the "unknown tool" branch).
    import labtris_api.agent as ag
    ag.TOOLS["delete_lab"] = ("delete a lab", {}, lambda _api, _a: {"ok": True})
    try:
        async def deny(_id, _tool, _args):
            return False
        events = [
            ev async for ev in agent_module.stream_agent("lab1", "delete it", confirm=deny)
        ]
    finally:
        del ag.TOOLS["delete_lab"]

    # The confirm event must precede the tool being run — and since the
    # user denied, no `step` for delete_lab should appear at all.
    kinds = [(e.get("kind"), e.get("tool")) for e in events]
    assert ("confirm", "delete_lab") in kinds
    assert ("step", "delete_lab") not in kinds
    # The first turn's applied list records the refusal so the UI shows
    # what was attempted.
    turns = [e for e in events if e["kind"] == "turn"]
    assert any("refused" in a for t in turns for a in t.get("applied", []))


async def test_confirm_bypass_when_no_callback(scripted) -> None:
    """The batch path (POST /ai) does not pass a confirm callback; the
    loop should proceed without prompting so external MCP calls don't
    hang forever waiting for an answer that will never come."""
    scripted([
        _msg(None, [_tc("c1", "delete_lab", {"lab_id": "L1"})]),
        _msg("done"),
    ])
    import labtris_api.agent as ag
    ag.TOOLS["delete_lab"] = ("delete a lab", {}, lambda _api, _a: {"ok": True})
    try:
        events = [ev async for ev in agent_module.stream_agent("lab1", "delete it")]
    finally:
        del ag.TOOLS["delete_lab"]
    # No confirm event; the tool fires directly.
    kinds = [e.get("kind") for e in events]
    assert "confirm" not in kinds
    assert "step" in kinds


# ---------------------------------------------------------------- attachments


async def test_user_message_content_stays_string_without_attachments() -> None:
    """Providers that don't accept multi-part messages should not see
    them when nothing is attached — the empty-attachments case must
    render as a plain string, same as before this feature landed."""
    got = agent_module._user_message_content("L1", "hi there", None)
    assert isinstance(got, str)
    assert "hi there" in got


async def test_user_message_content_includes_image_parts() -> None:
    """One data-URL image → one image_url part alongside the text
    part. The order matters: text first, images after, so the model
    reads the prompt before the attachment."""
    got = agent_module._user_message_content("L1", "look", [{
        "filename": "x.png",
        "mime": "image/png",
        "data_url": "data:image/png;base64,QUJD",
    }])
    assert isinstance(got, list)
    assert got[0]["type"] == "text"
    assert got[1]["type"] == "image_url"
    assert got[1]["image_url"]["url"].startswith("data:image/png;base64,")
