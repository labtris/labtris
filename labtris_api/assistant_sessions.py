"""Assistant conversations that outlive the browser.

A conversation is a row in `assistant_sessions` and an ordered list of
`assistant_events`. A turn runs as a task owned by `RunManager`, not by the
WebSocket that asked for it, so closing the tab or signing out leaves it
running, and an approval it asks for waits until the user is back, from
whatever device they come back on. Only an explicit cancel stops a turn.

What is stored is what a reader needs to rebuild the conversation: the user's
message, each finished model turn, approval requests and answers, and how the
turn ended. Streamed text deltas and "now calling X" steps are live-only; a
client that connects mid-turn gets the finished turns from the database and a
snapshot of the text streamed so far.

One API process owns all runs (uvicorn runs a single worker). A restart loses
the runs in flight; startup marks those sessions interrupted, and a pending
approval is lost with its run, which is the same as a denial.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from labtris_api.agent import Cancellation, LlmUnavailable, StreamCancelled

#: Kinds that are persisted and carry a seq. Everything else is live-only.
STORED_KINDS = frozenset(
    {"user", "turn", "confirm", "answer", "cancelled", "error", "interrupted", "end"}
)

#: How much earlier conversation is sent back to the model, in characters.
#: Roughly 6–8k tokens: enough for "do the same for leaf2" to mean something,
#: small enough that a long session does not grow its cost without bound.
HISTORY_BUDGET_CHARS = 24_000
#: Cap on the tool summary attached to one remembered turn.
_APPLIED_CAP_CHARS = 1_500
#: Cap on one stored text field. A model can write a lot; the database
#: should not have to keep a novel per turn.
_TEXT_CAP_CHARS = 100_000


class SessionGone(Exception):
    """The session was deleted while something was writing to it."""


class TurnBusy(Exception):
    """A turn is already running in this session."""


class Store(Protocol):
    async def create(
        self, user_id: str | None, lab_id: str, mode: str, title: str
    ) -> dict[str, Any]: ...
    async def get(self, session_id: str) -> dict[str, Any] | None: ...
    async def list_for(self, lab_id: str, user_id: str | None) -> list[dict[str, Any]]: ...
    async def append(self, session_id: str, kind: str, payload: dict[str, Any]) -> int: ...
    async def events(self, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]: ...
    async def set_status(self, session_id: str, status: str) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    async def unfinished(self) -> list[str]: ...


def _cap(text: str) -> str:
    return text if len(text) <= _TEXT_CAP_CHARS else text[:_TEXT_CAP_CHARS] + "\n[truncated]"


def model_history(
    events: list[dict[str, Any]], budget: int = HISTORY_BUDGET_CHARS
) -> list[dict[str, Any]]:
    """Earlier exchanges as chat messages, newest kept first, within `budget`.

    Each user message becomes one user message; everything the model said in
    reply becomes one assistant message, with the tool calls it made listed
    at the end rather than replayed. Replaying full tool traffic would resend
    screenshots and logs on every turn; the list says what was done, which is
    what a follow-up question needs.
    """
    pairs: list[tuple[str, list[str], list[str], str]] = []
    for ev in events:
        kind, p = ev["kind"], ev.get("payload") or {}
        if kind == "user":
            msg = str(p.get("message") or "")
            if not msg and p.get("attachments"):
                msg = "(sent attachments)"
            pairs.append((msg, [], [], ""))
        elif not pairs:
            continue
        elif kind == "turn":
            if p.get("text"):
                pairs[-1][1].append(str(p["text"]))
            pairs[-1][2].extend(str(a) for a in p.get("applied") or [])
        elif kind in ("cancelled", "interrupted", "error"):
            note = {
                "cancelled": "[the user stopped this turn]",
                "interrupted": "[this turn was cut off by a restart]",
                "error": f"[this turn failed: {p.get('message', '')}]",
            }[kind]
            pairs[-1] = (pairs[-1][0], pairs[-1][1], pairs[-1][2], note)

    rendered: list[tuple[str, str]] = []
    for user_msg, texts, applied, note in pairs:
        reply = "\n\n".join(texts)
        if applied:
            tools = "; ".join(applied)
            if len(tools) > _APPLIED_CAP_CHARS:
                tools = tools[:_APPLIED_CAP_CHARS] + " …"
            reply += f"\n\n[tools used: {tools}]"
        if note:
            reply += f"\n\n{note}"
        rendered.append((user_msg, reply.strip() or "(no reply)"))

    kept: list[tuple[str, str]] = []
    used = 0
    for user_msg, reply in reversed(rendered):
        size = len(user_msg) + len(reply)
        if kept and used + size > budget:
            break
        if not kept and size > budget:
            # One exchange bigger than the whole budget: keep its tail.
            reply = "…" + reply[-max(0, budget - len(user_msg)) :]
        kept.append((user_msg, reply))
        used += size
    kept.reverse()

    out: list[dict[str, Any]] = []
    for i, (user_msg, reply) in enumerate(kept):
        if i == 0 and len(kept) < len(rendered):
            user_msg = "(Earlier parts of this conversation were omitted.)\n\n" + user_msg
        out.append({"role": "user", "content": user_msg})
        out.append({"role": "assistant", "content": reply})
    return out


AgentFn = Callable[..., AsyncIterator[dict[str, Any]]]
PlanFallback = Callable[[str, str], Awaitable[list[str]]]


@dataclass
class _Run:
    session_id: str
    cancel: Cancellation = field(default_factory=Cancellation)
    task: asyncio.Task[None] | None = None
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    pending: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    live_text: str = ""
    step: str = ""


class RunManager:
    """Owns every running turn in this process."""

    def __init__(self, store: Store, agent_fn: AgentFn | None = None) -> None:
        self.store = store
        self._agent_fn = agent_fn
        self.plan_fallback: PlanFallback | None = None
        self._runs: dict[str, _Run] = {}

    @property
    def agent_fn(self) -> AgentFn:
        if self._agent_fn is None:
            from labtris_api.agent import stream_agent

            return stream_agent
        return self._agent_fn

    def is_running(self, session_id: str) -> bool:
        return session_id in self._runs

    # -- starting a turn -------------------------------------------------

    async def start_turn(
        self,
        session_id: str,
        lab_id: str,
        message: str,
        token: str | None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> int:
        """Record the user's message and start the turn. Returns its seq."""
        if session_id in self._runs:
            raise TurnBusy(session_id)
        run = _Run(session_id)
        # Claimed before the first await, so two sends racing each other
        # cannot both start a turn.
        self._runs[session_id] = run
        try:
            history = model_history(await self.store.events(session_id))
            # Images are sent to the model once and not kept: they are large,
            # and a conversation log is not where someone expects their
            # screenshots to live.
            meta = [
                {"filename": str(a.get("filename") or ""), "mime": str(a.get("mime") or "")}
                for a in attachments or []
            ]
            seq = await self.store.append(
                session_id, "user", {"message": _cap(message), "attachments": meta}
            )
            await self.store.set_status(session_id, "running")
        except BaseException:
            self._runs.pop(session_id, None)
            raise
        self._broadcast(run, {"kind": "user", "seq": seq, "message": message, "attachments": meta})
        run.task = asyncio.create_task(
            self._drive(run, lab_id, message, token, attachments, history),
            name=f"assistant-{session_id}",
        )
        return seq

    async def _record(self, run: _Run, kind: str, payload: dict[str, Any]) -> None:
        seq = await self.store.append(run.session_id, kind, payload)
        self._broadcast(run, {"kind": kind, "seq": seq, **payload})

    async def _drive(
        self,
        run: _Run,
        lab_id: str,
        message: str,
        token: str | None,
        attachments: list[dict[str, Any]] | None,
        history: list[dict[str, Any]],
    ) -> None:
        async def confirm(call_id: str, tool: str, args: dict[str, Any]) -> bool:
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            run.pending[call_id] = fut
            await self._record(run, "confirm", {"id": call_id, "tool": tool, "args": args})
            await self.store.set_status(run.session_id, "waiting")
            try:
                allow = await fut
            finally:
                run.pending.pop(call_id, None)
            await self._record(run, "answer", {"id": call_id, "allow": allow})
            await self.store.set_status(run.session_id, "running")
            return allow

        try:
            async for ev in self.agent_fn(
                lab_id,
                message,
                token=token,
                cancel=run.cancel,
                confirm=confirm,
                attachments=attachments,
                history=history,
            ):
                kind = ev.get("kind")
                if kind == "text":
                    run.live_text += ev.get("delta", "")
                    self._broadcast(run, ev)
                elif kind == "reset":
                    run.live_text = ""
                    self._broadcast(run, ev)
                elif kind == "step":
                    run.step = ev.get("tool", "")
                    self._broadcast(run, ev)
                elif kind == "turn":
                    run.live_text, run.step = "", ""
                    await self._record(
                        run,
                        "turn",
                        {
                            "text": _cap(str(ev.get("text") or "")),
                            "applied": list(ev.get("applied") or []),
                        },
                    )
                elif kind == "confirm":
                    # Recorded by the confirm callback, which runs right after.
                    continue
                else:
                    self._broadcast(run, ev)
        except StreamCancelled:
            await self._safe_record(run, "cancelled", {})
        except LlmUnavailable as exc:
            log: list[str] = []
            if self.plan_fallback is not None:
                with contextlib.suppress(Exception):
                    log = await self.plan_fallback(lab_id, message)
            reply = (
                "\n".join(log) if log else "No LLM configured and I could not match that request."
            )
            await self._safe_record(run, "turn", {"text": f"{reply}\n\n({exc})", "applied": log})
        except SessionGone:
            # The conversation was deleted mid-turn, most often with its lab
            # (a turn that ran delete_lab on its own lab). Nothing can be
            # stored any more, but whoever is watching still needs an ending.
            self._broadcast(run, {
                "kind": "error", "seq": None,
                "message": "this conversation was deleted while the turn ran",
            })
        except asyncio.CancelledError:
            # Shutdown. Startup marks the session interrupted.
            raise
        except Exception as exc:  # noqa: BLE001 - one broken turn must not leak
            await self._safe_record(run, "error", {"message": str(exc)[:500]})
        finally:
            for fut in run.pending.values():
                if not fut.done():
                    fut.set_result(False)
            self._runs.pop(run.session_id, None)
        await self._safe_record(run, "end", {})
        with contextlib.suppress(SessionGone, Exception):
            await self.store.set_status(run.session_id, "idle")

    async def _safe_record(self, run: _Run, kind: str, payload: dict[str, Any]) -> None:
        try:
            await self._record(run, kind, payload)
        except SessionGone:
            # Not stored, but still sent: a watcher waiting for "end" must
            # get it even when the session row is gone.
            self._broadcast(run, {"kind": kind, "seq": None, **payload})

    # -- watching a session ----------------------------------------------

    def _broadcast(self, run: _Run, event: dict[str, Any]) -> None:
        for q in list(run.subscribers):
            q.put_nowait(event)

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]] | None:
        """A queue of live events, or None when no turn is running."""
        run = self._runs.get(session_id)
        if run is None:
            return None
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        run.subscribers.add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        run = self._runs.get(session_id)
        if run is not None:
            run.subscribers.discard(q)

    def snapshot(self, session_id: str) -> list[dict[str, Any]]:
        """Live state a late subscriber missed: text streamed so far, the
        tool in flight. Pending approvals are stored events, so the replay
        already carries them."""
        run = self._runs.get(session_id)
        if run is None:
            return []
        out: list[dict[str, Any]] = []
        if run.live_text:
            out.append({"kind": "text", "delta": run.live_text})
        if run.step:
            out.append({"kind": "step", "tool": run.step})
        return out

    # -- steering a running turn -------------------------------------------

    def answer(self, session_id: str, call_id: str, allow: bool) -> bool:
        run = self._runs.get(session_id)
        fut = run.pending.get(call_id) if run else None
        if fut is None or fut.done():
            return False
        fut.set_result(bool(allow))
        return True

    def cancel(self, session_id: str) -> bool:
        run = self._runs.get(session_id)
        if run is None:
            return False
        run.cancel.cancel()
        for fut in run.pending.values():
            if not fut.done():
                fut.set_result(False)
        return True

    async def recover(self) -> int:
        """Mark sessions whose turn died with the previous process."""
        n = 0
        for sid in await self.store.unfinished():
            if sid in self._runs:
                continue
            with contextlib.suppress(SessionGone):
                await self.store.append(sid, "interrupted", {})
                await self.store.append(sid, "end", {})
                await self.store.set_status(sid, "interrupted")
                n += 1
        return n

    async def shutdown(self) -> None:
        tasks = [r.task for r in self._runs.values() if r.task is not None]
        for r in list(self._runs.values()):
            r.cancel.cancel()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class DbStore:
    """Store backed by the Labtris database. Each call opens its own short
    session: a turn can run for an hour, and holding a pooled connection for
    all of it is what exhausted the pool in 0.6.0."""

    def __init__(self, session_factory: Any = None) -> None:
        self._factory = session_factory

    @property
    def factory(self) -> Any:
        if self._factory is None:
            from labtris_api.db import SessionLocal

            return SessionLocal
        return self._factory

    @staticmethod
    def _row(s: Any) -> dict[str, Any]:
        return {
            # CHAR(26) pads anything shorter with spaces; ids compared in
            # Python must not carry them.
            "id": s.id.rstrip(),
            "user_id": s.user_id.rstrip() if s.user_id else None,
            "lab_id": s.lab_id.rstrip(),
            "mode": s.mode,
            "title": s.title,
            "status": s.status,
            "last_seq": s.last_seq,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }

    async def create(
        self, user_id: str | None, lab_id: str, mode: str, title: str
    ) -> dict[str, Any]:
        from labtris_api.lifecycle import new_id
        from labtris_api.models import AssistantSession

        async with self.factory() as db:
            row = AssistantSession(
                id=new_id(),
                user_id=user_id,
                lab_id=lab_id,
                mode=mode,
                title=title,
                status="idle",
                last_seq=0,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return self._row(row)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        from labtris_api.models import AssistantSession

        async with self.factory() as db:
            row = await db.get(AssistantSession, session_id)
            return self._row(row) if row else None

    async def list_for(self, lab_id: str, user_id: str | None) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from labtris_api.models import AssistantSession

        q = select(AssistantSession).where(AssistantSession.lab_id == lab_id)
        q = q.where(
            AssistantSession.user_id.is_(None)
            if user_id is None
            else AssistantSession.user_id == user_id
        )
        async with self.factory() as db:
            rows = (
                (await db.execute(q.order_by(AssistantSession.updated_at.desc()))).scalars().all()
            )
            return [self._row(r) for r in rows]

    async def append(self, session_id: str, kind: str, payload: dict[str, Any]) -> int:
        from sqlalchemy import func, update

        from labtris_api.models import AssistantEvent, AssistantSession

        async with self.factory() as db:
            seq = (
                await db.execute(
                    update(AssistantSession)
                    .where(AssistantSession.id == session_id)
                    .values(last_seq=AssistantSession.last_seq + 1, updated_at=func.now())
                    .returning(AssistantSession.last_seq)
                )
            ).scalar_one_or_none()
            if seq is None:
                raise SessionGone(session_id)
            db.add(AssistantEvent(session_id=session_id, seq=seq, kind=kind, payload=payload))
            await db.commit()
            return int(seq)

    async def events(self, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from labtris_api.models import AssistantEvent

        async with self.factory() as db:
            rows = (
                (
                    await db.execute(
                        select(AssistantEvent)
                        .where(
                            AssistantEvent.session_id == session_id, AssistantEvent.seq > after_seq
                        )
                        .order_by(AssistantEvent.seq)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "seq": r.seq,
                    "kind": r.kind,
                    "payload": r.payload,
                    "at": r.at.isoformat() if r.at else None,
                }
                for r in rows
            ]

    async def set_status(self, session_id: str, status: str) -> None:
        from sqlalchemy import update

        from labtris_api.models import AssistantSession

        async with self.factory() as db:
            await db.execute(
                update(AssistantSession)
                .where(AssistantSession.id == session_id)
                .values(status=status)
            )
            await db.commit()

    async def delete(self, session_id: str) -> None:
        from sqlalchemy import delete

        from labtris_api.models import AssistantSession

        async with self.factory() as db:
            await db.execute(delete(AssistantSession).where(AssistantSession.id == session_id))
            await db.commit()

    async def unfinished(self) -> list[str]:
        from sqlalchemy import select

        from labtris_api.models import AssistantSession

        async with self.factory() as db:
            rows = (
                (
                    await db.execute(
                        select(AssistantSession.id).where(
                            AssistantSession.status.in_(("running", "waiting"))
                        )
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)


def event_for_client(ev: dict[str, Any]) -> dict[str, Any]:
    """A stored event in the shape the live stream uses."""
    return {"kind": ev["kind"], "seq": ev["seq"], "at": ev.get("at"), **(ev.get("payload") or {})}


#: The process-wide manager.
manager = RunManager(DbStore())
