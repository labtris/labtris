from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any

from labtris_api.config import settings
from labtris_mcp.client import Api, ApiError
from labtris_mcp.tools import TOOLS

SYSTEM = """You operate a Labtris network emulation lab through the tools provided.

Rules that matter here:
- Act, do not describe. If asked to build something, build it with the tools.
- Work in the lab whose id you are given. Do not create another one unless asked.
- Check what exists before adding to it; names must be unique within a lab.
- A qemu image that is not already cached costs a multi-GB download on its
  first start and can take many minutes. Say so before starting one, and never
  retry a start that is already in progress.
- A refusal explains itself. Read it and adapt rather than repeating the call.
- When you are done, say briefly what you built. No preamble."""


def _openai_tools() -> list[dict[str, Any]]:
    """The MCP tool table as OpenAI-style function definitions.

    Same source of truth as the MCP server: a tool added there is available
    here without a second definition to keep in step."""
    return [
        {
            "type": "function",
            "function": {"name": name, "description": desc, "parameters": schema},
        }
        for name, (desc, schema, _) in sorted(TOOLS.items())
    ]


def _post(url: str, body: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())  # type: ignore[no-any-return]


class _SseStream:
    """Iterator over SSE chunks from an LLM response, with a
    `close_hook` that closes the underlying urllib response so the
    blocking readline returns EOF promptly. That hook is what makes a
    Stop button feel instant instead of "when the next chunk arrives"."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.close_hook = resp.close

    def __iter__(self) -> Iterator[dict[str, Any]]:
        try:
            while True:
                raw = self._resp.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if not line or line.startswith(":"):
                    # SSE: blank line separates events; ":" prefix is a
                    # comment some proxies send as keepalive. Skip both.
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    # A malformed chunk is a proxy bug, not fatal — skip
                    # and keep reading. The user still sees text appear
                    # from the chunks that did parse.
                    continue
        finally:
            try:
                self._resp.close()
            except BaseException:  # noqa: BLE001
                pass


def _post_stream(
    url: str, body: dict[str, Any], key: str, timeout: int
) -> _SseStream:
    """Streaming counterpart to _post. Returns an iterator over SSE
    chunks the LLM produces, exposing `close_hook` for the caller to
    interrupt a blocking readline.

    Blocks in urllib.request.readline() while waiting for the next chunk;
    the caller is expected to run this inside `asyncio.to_thread` — the
    same pattern the non-streaming _post already uses. Errors surface via
    HTTPError/URLError so the caller's LlmUnavailable mapping still works.
    """
    streaming_body = {**body, "stream": True}
    req = urllib.request.Request(
        url, data=json.dumps(streaming_body).encode(), method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    resp = urllib.request.urlopen(req, timeout=timeout)
    return _SseStream(resp)


class LlmUnavailable(RuntimeError):
    pass


async def llm_reachable() -> bool:
    """A cheap liveness probe so the UI can say "no model configured" up front
    rather than after the user has typed a request."""

    def _probe() -> bool:
        req = urllib.request.Request(settings.llm_base_url.rstrip("/") + "/models")
        if settings.llm_api_key:
            req.add_header("Authorization", f"Bearer {settings.llm_api_key}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return bool(200 <= resp.status < 500)
        except urllib.error.HTTPError:
            return True  # answered, just not to an unauthenticated probe
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    return await asyncio.to_thread(_probe)


def _merge_delta(assembled: dict[str, Any], delta: dict[str, Any]) -> tuple[str, str]:
    """Fold one streaming delta into the running assembled message and
    report what new text / reasoning surfaced from it.

    Delta shapes vary across providers; the OpenAI-compatible one that
    LiteLLM emits is `{content?, tool_calls?[{index, id?, function:{name?,
    arguments?}}], reasoning_content?}`. We accumulate both content and
    per-index tool-call fields into `assembled`.
    """
    text_delta = ""
    reasoning_delta = ""

    if isinstance(delta.get("role"), str):
        assembled["role"] = delta["role"]
    if isinstance(delta.get("content"), str) and delta["content"]:
        text_delta = delta["content"]
        assembled["content"] = (assembled.get("content") or "") + text_delta
    # Reasoning is exposed under different keys depending on the provider;
    # collect any of them so a Claude/thinking or o1/reasoning_content
    # model surfaces without a special case.
    for reasoning_key in ("reasoning_content", "reasoning", "thinking"):
        val = delta.get(reasoning_key)
        if isinstance(val, str) and val:
            reasoning_delta += val
    if reasoning_delta:
        assembled["reasoning_content"] = (assembled.get("reasoning_content") or "") + reasoning_delta

    for tc_delta in delta.get("tool_calls") or []:
        if not isinstance(tc_delta, dict):
            continue
        idx = tc_delta.get("index", 0)
        calls = assembled.setdefault("tool_calls", [])
        while len(calls) <= idx:
            calls.append({"type": "function", "function": {"name": "", "arguments": ""}})
        slot = calls[idx]
        if tc_delta.get("id"):
            slot["id"] = tc_delta["id"]
        if tc_delta.get("type"):
            slot["type"] = tc_delta["type"]
        fn_delta = tc_delta.get("function") or {}
        if fn_delta.get("name"):
            slot["function"]["name"] = (slot["function"].get("name") or "") + fn_delta["name"]
        if fn_delta.get("arguments"):
            slot["function"]["arguments"] = (slot["function"].get("arguments") or "") + fn_delta["arguments"]

    return text_delta, reasoning_delta


class StreamCancelled(Exception):
    """Raised inside stream_agent when the caller signals cancel via the
    Cancellation object. The websocket handler catches this and treats
    the turn as ended (no error surfaced to the user)."""


class Cancellation:
    """A cancel flag the caller (websocket handler) can raise from any
    coroutine; the agent loop checks it between LLM chunks and at every
    tool-call boundary. Cheap: a plain bool + optional callback so the
    pump thread can close its urllib response on cancel and return
    quickly rather than waiting for the next SSE frame."""

    def __init__(self) -> None:
        self._cancelled = False
        self._close_hook: Any = None

    def cancel(self) -> None:
        self._cancelled = True
        # Best-effort: close the current urllib response so the blocking
        # readline returns EOF immediately instead of waiting for the
        # next chunk from the provider.
        if self._close_hook is not None:
            try:
                self._close_hook()
            except BaseException:  # noqa: BLE001
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_close_hook(self, hook: Any) -> None:
        self._close_hook = hook


async def _async_stream_completion(
    url: str,
    body: dict[str, Any],
    key: str,
    timeout: int,
    assembled: dict[str, Any],
    cancel: Cancellation | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run _post_stream in a worker thread, yield text/reasoning events
    to the async caller, and fill `assembled` with the final aggregated
    message (`{content, tool_calls, ...}`) so the caller can decide
    whether to make tool calls without waiting for a second request.

    Bridged via an asyncio.Queue because urllib is blocking. Cancellation:
    when `cancel.cancel()` fires, we close the urllib response so the
    thread's readline returns EOF, then raise StreamCancelled from the
    async side after draining any already-queued chunks.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    exc_holder: list[BaseException] = []

    def _pump() -> None:
        try:
            # _post_stream is the single point of truth for how the LLM
            # request is opened; we consume its iterator here so tests
            # can monkeypatch _post_stream and still influence what the
            # pump yields. If the iterator returned by _post_stream
            # carries a `.set_close_hook` (real implementation does when
            # cancellation is wired in), use it for prompt cancel.
            it = _post_stream(url, body, key, timeout)
            close_hook = getattr(it, "close_hook", None)
            if close_hook and cancel is not None:
                cancel.set_close_hook(close_hook)
            for chunk in it:
                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
        except BaseException as exc:  # noqa: BLE001 — surface across the thread boundary
            # A cancel close() shows up here as URLError/ValueError; hide
            # it since the caller will detect cancellation itself.
            if cancel is not None and cancel.cancelled:
                return
            exc_holder.append(exc)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    task = asyncio.create_task(asyncio.to_thread(_pump))
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                if cancel is not None and cancel.cancelled:
                    raise StreamCancelled()
                if exc_holder:
                    raise exc_holder[0]
                return
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                text, reasoning = _merge_delta(assembled, delta)
                if text:
                    yield {"kind": "text", "delta": text}
                if reasoning:
                    yield {"kind": "reasoning", "delta": reasoning}
    finally:
        # Wait for the pump to finish so we do not leak a thread on
        # generator close.
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass


#: Tools that mutate state in ways a person would want to see before
#: they happen. Not a security boundary — anyone driving the agent can
#: refuse or approve — just a fingernail on the trigger for the tools
#: that are hardest to reverse.
CONFIRM_TOOLS = frozenset({
    "delete_lab",
    "delete_node",
    "stop_node",
    "create_network",
    "join_network",
})


def _user_message_content(
    lab_id: str, message: str, attachments: list[dict[str, Any]] | None
) -> Any:
    """Build the user-message content in the multi-part shape a
    vision-capable LLM accepts. Text-only messages stay as a plain
    string for maximum provider compatibility.

    Attachments come from the browser as `{filename, mime, data_url}`
    where data_url is a base64 data URI. PDFs are pre-rendered to PNG
    per page here so the LLM only ever sees images — vision models
    with native PDF support are rare enough that always converting is
    the simpler shape."""
    if not attachments:
        return f"Lab id: {lab_id}\n\n{message}"
    import base64
    import io
    import subprocess

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": f"Lab id: {lab_id}\n\n{message or '(see attachments)'}"}
    ]
    for a in attachments:
        mime = str(a.get("mime") or "").lower()
        data_url = str(a.get("data_url") or "")
        if not data_url.startswith("data:"):
            continue
        if mime == "application/pdf":
            # PDFs → PNGs via pdftoppm. Cap at first 5 pages so an
            # accidentally-uploaded book does not blow the context.
            try:
                header, b64 = data_url.split(",", 1)
                raw = base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                continue
            with io.BytesIO(raw) as buf:
                proc = subprocess.run(
                    ["pdftoppm", "-png", "-r", "120", "-f", "1", "-l", "5", "-", "/tmp/pdfimg"],
                    input=buf.read(),
                    capture_output=True,
                    timeout=20,
                )
            if proc.returncode != 0:
                continue
            # pdftoppm writes /tmp/pdfimg-1.png, /tmp/pdfimg-2.png, …
            import glob
            import os
            for path in sorted(glob.glob("/tmp/pdfimg-*.png")):
                try:
                    with open(path, "rb") as fh:
                        page_b64 = base64.b64encode(fh.read()).decode()
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{page_b64}"},
                    })
                finally:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        elif mime.startswith("image/"):
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
    return parts


async def stream_agent(
    lab_id: str,
    message: str,
    token: str | None = None,
    cancel: Cancellation | None = None,
    retry_on_interruption: bool = True,
    confirm: Any = None,
    attachments: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """The tool-calling loop as an async iterator of events.

    Yields three kinds of events, in order:

    - `{"kind": "step", "tool": <name>}` — right before a tool call fires,
      so the UI can name what's running rather than staring at nothing
      while the model waits on it.
    - `{"kind": "turn", "text": str, "applied": [str]}` — one per model
      turn, as soon as the turn finishes (i.e. all its tool calls have
      returned). Includes the narration text the model produced and the
      list of tool calls that turn made.
    - Terminates by returning; the caller signals "done" itself so the
      transport (websocket, batching wrapper) can attach its own trailer.

    Same loop as before, same step cap, same error mapping. Extracted so
    the websocket path can push turns as they land instead of batching."""
    # The agent acts as whoever asked, not as some service identity: labs it
    # creates then belong to that person, and nothing it can do exceeds what
    # they could do themselves.
    api = Api(base=settings.self_url, token=token)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": _user_message_content(lab_id, message, attachments)},
    ]
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    emitted_any_turn = False
    # Every tool call that actually landed (excluding refused ones), so if
    # the loop caps we can tell the user what got done instead of a bare
    # "stopped after N steps".
    all_applied: list[str] = []

    for _ in range(max(1, settings.llm_max_steps)):
        if cancel is not None and cancel.cancelled:
            raise StreamCancelled()
        body = {
            "model": settings.llm_model,
            "messages": messages,
            "tools": _openai_tools(),
            "tool_choice": "auto",
        }
        # Stream the LLM response over a queue so text tokens can be
        # yielded to the caller (websocket) as they arrive, while the
        # loop still collects them into a full `choice` at the end to
        # decide whether tool calls follow.
        #
        # Retry-once-on-interruption: providers occasionally close the
        # SSE mid-turn (a proxy hiccup, a rate-limit blink). We re-issue
        # the same request one time and start the turn over. The user
        # sees the partial output discarded and a fresh growing bubble
        # begin — the alternative (surface an error, ask the user to
        # click retry) turns every provider blip into a friction event.
        attempts = 2 if retry_on_interruption else 1
        choice: dict[str, Any] = {}
        interrupted_partials_yielded = False
        for attempt in range(attempts):
            choice = {}
            try:
                async for ev in _async_stream_completion(
                    url, body, settings.llm_api_key, 120, choice, cancel=cancel
                ):
                    if ev.get("kind") in ("text", "reasoning"):
                        interrupted_partials_yielded = True
                    yield ev
                break  # normal end-of-stream
            except StreamCancelled:
                raise
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                raise LlmUnavailable(f"{settings.llm_base_url} returned {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt + 1 < attempts:
                    # Only retry a mid-stream drop — a fresh URLError on
                    # the initial connect surfaces via LlmUnavailable
                    # below on the retry attempt too, but the user gets
                    # a clearer message.
                    if interrupted_partials_yielded:
                        # Tell the UI to discard its growing bubble
                        # before we start emitting the retry's tokens.
                        yield {"kind": "reset"}
                        interrupted_partials_yielded = False
                    continue
                raise LlmUnavailable(
                    f"cannot reach the LLM at {settings.llm_base_url}: {exc}. Set "
                    "LABTRIS_LLM_BASE_URL / LABTRIS_LLM_API_KEY / LABTRIS_LLM_MODEL."
                ) from None

        calls = choice.get("tool_calls") or []
        messages.append(choice)
        turn_text = choice.get("content") or ""
        turn_applied: list[str] = []

        if not calls:
            # Terminal turn — emit it and stop. Skip empty tail turns
            # so a model that ends with tool calls only doesn't append a
            # blank bubble to the UI.
            if turn_text or not emitted_any_turn:
                yield {"kind": "turn", "text": turn_text, "applied": turn_applied}
            return

        for call in calls:
            if cancel is not None and cancel.cancelled:
                raise StreamCancelled()
            fn = (call.get("function") or {}).get("name") or ""
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # Consent gate: destructive tools stop the loop and wait for
            # the user's answer via the confirm callback. A denial gets
            # reflected back to the model as an ApiError-shaped tool
            # response so the model can adapt (say "OK, I won't"), same
            # as the natural refusal path already handles below.
            if fn in CONFIRM_TOOLS and confirm is not None:
                yield {"kind": "confirm", "tool": fn, "args": args, "id": call.get("id", "")}
                allow = await confirm(call.get("id", ""), fn, args)
                if not allow:
                    result: Any = "error 403: user denied this tool call"
                    turn_applied.append(f"{fn} refused: user denied")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result)[:8000],
                    })
                    continue
            # Announce the tool call before it fires — this is what the
            # "assistant is working" strip in the UI reads to name what
            # is currently in flight.
            yield {"kind": "step", "tool": fn}
            entry = TOOLS.get(fn)
            if entry is None:
                result = f"unknown tool {fn!r}"
            else:
                try:
                    result = await asyncio.to_thread(entry[2], api, args)
                    call_str = f"{fn}({', '.join(f'{k}={v}' for k, v in args.items())})"
                    turn_applied.append(call_str)
                except ApiError as exc:
                    # Handed back as content, not raised: a refusal is
                    # information the model should act on.
                    result = f"error {exc.status}: {exc.message}"
                    refused = f"{fn} refused: {exc.message}"
                    turn_applied.append(refused)
                except Exception as exc:  # noqa: BLE001 - one bad call must not end the turn
                    result = f"{type(exc).__name__}: {exc}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, default=str)[:8000],
                }
            )
        yield {"kind": "turn", "text": turn_text, "applied": turn_applied}
        emitted_any_turn = True
        all_applied.extend(turn_applied)

    # Reached the step cap without the model saying it was done. Give the
    # user a real accounting — what the model did do, and what the two
    # ways forward are — instead of a bare "stopped after N".
    if all_applied:
        # Deduplicate consecutive identical calls so a long "start each
        # node in turn" loop reads as one line ("start_node ×12"), not
        # twelve. Keeps the message short on the common bulk-edit shape.
        summary_lines: list[str] = []
        prev, count = None, 0
        for call in all_applied:
            if call == prev:
                count += 1
            else:
                if prev is not None:
                    summary_lines.append(f"  • {prev}" + (f" ×{count}" if count > 1 else ""))
                prev, count = call, 1
        if prev is not None:
            summary_lines.append(f"  • {prev}" + (f" ×{count}" if count > 1 else ""))
        summary = "\n".join(summary_lines)
        stopped = (
            f"Hit the runaway backstop at {settings.llm_max_steps} tool "
            f"calls in one turn. This ceiling exists to catch models "
            f"stuck in a loop, and it usually means something is wrong — "
            f"a real debug session rarely goes past 100 calls. Here's "
            f"what did land ({len(all_applied)} call"
            f"{'s' if len(all_applied) != 1 else ''}):\n{summary}\n\n"
            f"To keep going: say \"continue\" (I'll pick up where I "
            f"stopped). If this keeps happening on legitimate work, "
            f"raise Settings → Assistant → Max tool steps per turn."
        )
    else:
        stopped = (
            f"Hit the runaway backstop at {settings.llm_max_steps} steps "
            f"without landing a tool call at all. The model is stuck in "
            f"a reasoning loop. Try again with a more specific ask."
        )
    yield {"kind": "turn", "text": stopped, "applied": []}


async def run_agent(lab_id: str, message: str, token: str | None = None) -> dict[str, Any]:
    """Batch adapter over stream_agent — collects every event and returns
    the same `{turns, reply, applied, llm}` shape POST /ai has always
    returned. External callers (MCP surface, tests) keep working with no
    change; the streaming path (websocket) uses stream_agent directly."""
    turns: list[dict[str, Any]] = []
    applied: list[str] = []
    async for ev in stream_agent(lab_id, message, token):
        if ev["kind"] == "turn":
            turns.append({"text": ev["text"], "applied": ev["applied"]})
            applied.extend(ev["applied"])
    return {
        "turns": turns,
        "reply": turns[-1]["text"] if turns else "",
        "applied": applied,
        "llm": True,
    }
