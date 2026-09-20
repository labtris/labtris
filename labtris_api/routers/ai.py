from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from labtris_api.agent import Cancellation, LlmUnavailable, StreamCancelled, llm_reachable, run_agent, stream_agent
from labtris_api.auth import User, get_current_user
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.errors import ApiError, bad_request, not_found
from labtris_api.impair import PRESETS
from labtris_api.lifecycle import (
    allocate_mac,
    get_lab,
    guest_name,
    iface_scheme_for,
    new_id,
    next_iface_idx,
    start_node,
    stop_node,
)
from labtris_api.models import Interface, Lab, Link, Network, Node
from labtris_api.runtime.base import StopMode
from labtris_api.schemas import AiChatIn
from labtris_mcp.tools import TOOLS

router = APIRouter(tags=["ai"])


async def _lab_context(session: AsyncSession, lab_id: str) -> dict[str, Any]:
    result = await session.execute(
        select(Lab)
        .options(
            selectinload(Lab.nodes).selectinload(Node.interfaces),
            selectinload(Lab.links),
        )
        .where(Lab.id == lab_id)
    )
    lab = result.scalar_one_or_none()
    if lab is None:
        raise not_found("lab not found")
    return {
        "id": lab.id,
        "name": lab.name,
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "image": n.image,
                "state": n.state,
                "ifaces": [i.name for i in n.interfaces],
            }
            for n in lab.nodes
        ],
        "links": [{"id": ln.id, "a": ln.a_iface_id, "b": ln.b_iface_id} for ln in lab.links],
    }


async def _add_node(session: AsyncSession, lab_id: str, name: str, image: str) -> Node:
    node = Node(
        id=new_id(),
        lab_id=lab_id,
        name=name,
        runtime="docker",
        image=image,
        cmd=["sleep", "3600"]
        if image.startswith("alpine") or "ubuntu" in image or "busybox" in image
        else None,
        env={},
        state="defined",
    )
    session.add(node)
    await session.flush()
    session.add(
        Interface(
            id=(iface_id := new_id()),
            node_id=node.id,
            idx=0,
            name="eth1",
            mac=await allocate_mac(session, iface_id),
        )
    )
    await session.commit()
    return node


async def _wire(session: AsyncSession, lab_id: str, a_name: str, b_name: str) -> Link:
    from labtris_api.lifecycle import realize_link_if_running

    nodes = (
        await session.execute(
            select(Node).options(selectinload(Node.interfaces)).where(Node.lab_id == lab_id)
        )
    ).scalars()
    by_name = {n.name: n for n in nodes}
    a, b = by_name[a_name], by_name[b_name]
    # Both interfaces get the on-scheme name for idx 0 (containers: eth0,
    # QEMU virtio: ens3, etc). Naming the first interface "eth1" by hand — as
    # this used to — put the name and the idx out of sync, and every later
    # manual "add interface" collided at the (node_id, name) unique index.
    if not a.interfaces:
        a_idx = next_iface_idx(a)
        a.interfaces.append(
            Interface(
                id=(iface_id := new_id()),
                node_id=a.id,
                idx=a_idx,
                name=guest_name(a_idx, None, iface_scheme_for(a.runtime, a.image)),
                mac=await allocate_mac(session, iface_id),
            )
        )
    if not b.interfaces:
        b_idx = next_iface_idx(b)
        b.interfaces.append(
            Interface(
                id=(iface_id := new_id()),
                node_id=b.id,
                idx=b_idx,
                name=guest_name(b_idx, None, iface_scheme_for(b.runtime, b.image)),
                mac=await allocate_mac(session, iface_id),
            )
        )
    net = Network(id=new_id(), lab_id=lab_id, name=f"lnk-{new_id()[-8:].lower()}", kind="bridge")
    session.add(net)
    await session.flush()
    a.interfaces[0].network_id = net.id
    b.interfaces[0].network_id = net.id
    link = Link(
        id=new_id(),
        lab_id=lab_id,
        a_iface_id=a.interfaces[0].id,
        b_iface_id=b.interfaces[0].id,
        network_id=net.id,
    )
    session.add(link)
    await session.commit()
    await realize_link_if_running(session, link)
    return link


async def local_plan(session: AsyncSession, lab_id: str, text: str) -> list[str]:
    log: list[str] = []
    t = text.lower()
    await get_lab(session, lab_id)
    n = 2
    m = re.search(r"(\d+)\s*(alpine|node|host|leaf|spine)", t)
    if m:
        n = min(int(m.group(1)), 12)
    image = "alpine:3.20"
    if "nginx" in t:
        image = "nginx:alpine"
    if "frr" in t:
        image = "frrouting/frr:v8.4.0"
    if "clos" in t or "spine" in t:
        spines, leaves = 2, 4
        sm = re.search(r"(\d+)\s*spine", t)
        lm = re.search(r"(\d+)\s*lea", t)
        if sm:
            spines = min(int(sm.group(1)), 8)
        if lm:
            leaves = min(int(lm.group(1)), 16)
        s_nodes = []
        l_nodes = []
        for i in range(spines):
            node = await _add_node(session, lab_id, f"spine{i+1}", image)
            s_nodes.append(node.name)
            log.append(f"created {node.name}")
        for i in range(leaves):
            node = await _add_node(session, lab_id, f"leaf{i+1}", image)
            l_nodes.append(node.name)
            log.append(f"created {node.name}")
        for s in s_nodes:
            for leaf in l_nodes:
                await _wire(session, lab_id, s, leaf)
                log.append(f"wired {s} ↔ {leaf}")
        return log
    if any(w in t for w in ("create", "add", "build", "two", "2 node", "alpine", "lab")):
        names = []
        for i in range(n):
            node = await _add_node(session, lab_id, f"n{i+1}", image)
            names.append(node.name)
            log.append(f"created {node.name} ({image})")
        for a, b in zip(names, names[1:], strict=False):
            await _wire(session, lab_id, a, b)
            log.append(f"wired {a} ↔ {b}")
    if "start" in t:
        nodes = (await session.execute(select(Node).where(Node.lab_id == lab_id))).scalars()
        for node in nodes:
            node = await start_node(session, node)
            log.append(f"started {node.name} → {node.state}")
    if "stop" in t:
        nodes = (await session.execute(select(Node).where(Node.lab_id == lab_id))).scalars()
        for node in nodes:
            await stop_node(session, node, StopMode.FORCE)
            log.append(f"stopped {node.name}")
    for preset, spec in PRESETS.items():
        if preset.replace("-", " ") in t or preset in t:
            links = (await session.execute(select(Link).where(Link.lab_id == lab_id))).scalars()
            from labtris_api.impair import apply_link_qos

            for link in links:
                link.impair_ab = spec
                link.impair_ba = spec
                await session.commit()
                iface_a = await session.get(Interface, link.a_iface_id)
                iface_b = await session.get(Interface, link.b_iface_id)
                await apply_link_qos(link, iface_a, iface_b, session=session)
                log.append(f"applied {preset} on link {link.id[:8]}")
    if "delay" in t:
        dm = re.search(r"delay\s+(\d+)", t)
        delay = int(dm.group(1)) if dm else 50
        spec = {"delay_ms": delay, "jitter_ms": 5, "loss_pct": 0}
        links = (await session.execute(select(Link).where(Link.lab_id == lab_id))).scalars()
        from labtris_api.impair import apply_link_qos

        for link in links:
            link.impair_ab = spec
            link.impair_ba = spec
            await session.commit()
            iface_a = await session.get(Interface, link.a_iface_id)
            iface_b = await session.get(Interface, link.b_iface_id)
            await apply_link_qos(link, iface_a, iface_b, session=session)
            log.append(f"set delay {delay}ms on {link.id[:8]}")
    if not log:
        log.append(
            "I can create nodes, wire them, start/stop, and apply impairment. "
            "Try: “create 2 alpine and wire them”, “start all”, “satellite delay”, "
            "or “2 spines 4 leaves clos”."
        )
    return log


def _llm(prompt: str, context: dict[str, Any]) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    body = json.dumps(
        {
            "model": os.environ.get("LABTRIS_AI_MODEL", "claude-3-5-haiku-latest"),
            "max_tokens": 400,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "You are the Labtris assistant. Topology JSON:\n"
                        + json.dumps(context)
                        + "\nUser: "
                        + prompt
                        + "\nReply with a short plan of API actions."
                    ),
                }
            ],
        }
    ).encode()
    url = "https://api.anthropic.com/v1/messages"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        return "".join(
            p.get("text", "") for p in data.get("content", []) if p.get("type") == "text"
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


@router.post("/labs/{lab_id}/ai")
async def ai_chat(
    lab_id: str,
    body: AiChatIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Ask the assistant to do something to this lab.

    The model decides and the MCP tools act — the same tool table an external
    agent gets, so there is one implementation of "create a node" rather than
    a second one here quietly drifting from it. Falls back to the built-in
    pattern planner when no LLM is configured, which handles the handful of
    shapes it knows and says so."""
    await _lab_context(session, lab_id)
    try:
        # Mint a session for the caller so the agent's own HTTP calls carry
        # their identity. Without it every tool call is anonymous, and since
        # authentication landed that means every tool call is a 401 — which
        # the agent reported as work done.
        result = await run_agent(lab_id, body.message, token=_agent_token(user))
    except LlmUnavailable as exc:
        log = await local_plan(session, lab_id, body.message)
        reply = (
            "\n".join(log) if log else "No LLM configured and I could not match that request."
        ) + f"\n\n({exc})"
        return {
            # Same shape as the LLM path so the UI's one code path handles both.
            "turns": [{"text": reply, "applied": log}],
            "reply": reply,
            "applied": log,
            "lab": await _lab_context(session, lab_id),
            "llm": False,
        }
    return {**result, "lab": await _lab_context(session, lab_id)}


def _agent_token(user: User) -> str | None:
    """A session for the agent to act with, in the caller's name.

    Short-lived by virtue of being minted per request and never stored. The
    setup pseudo-user has no database row to sign for, so it gets nothing —
    which is correct: nobody should be driving an agent before the instance
    has an account."""
    from labtris_api.auth import issue_token
    from labtris_api.models import User as UserRow

    if user.id in ("setup", "dev"):
        return None
    return issue_token(
        UserRow(id=user.id, username=user.username, role=user.role, display_name=user.name)
    )


@router.websocket("/labs/{lab_id}/ai/ws")
async def ai_chat_ws(websocket: WebSocket, lab_id: str) -> None:
    """Streaming counterpart to POST /labs/{lab_id}/ai.

    Sends one JSON message per assistant event so the browser can render
    each bubble as the model produces it, plus a "step" event before each
    tool call so the working-strip in the UI can name what's currently in
    flight. Ends with `{kind: "done"}` and closes.

    Auth: session cookie on the upgrade request, same as the other WS
    routes. resolve_ws_user reads it and refuses if there is no valid
    session — otherwise an unauthenticated client could burn a real
    LLM turn.
    """
    from labtris_api.auth import resolve_ws_user
    from labtris_api.db import SessionLocal

    await websocket.accept()
    async with SessionLocal() as _s:
        user = await resolve_ws_user(websocket, _s)
    if user is None:
        # Same close-with-a-visible-error shape as the other WS routes.
        await websocket.send_json({"kind": "error", "message": "sign in to talk to the assistant"})
        await websocket.close(code=4401, reason="unauthorized")
        return
    cancel = Cancellation()
    #: Per-tool-call futures the confirm callback awaits. Keyed by the
    #: tool_call_id the model gave, so out-of-order answers still land
    #: on the right pending confirm.
    pending_confirms: dict[str, asyncio.Future[bool]] = {}

    async def _watch_client() -> None:
        """Block on the socket for follow-up messages from the client
        during a turn: `{kind: "cancel"}` sets the Cancellation flag
        (which the agent loop and streaming pump both check), and
        `{kind: "answer", id, allow}` resolves the pending confirm
        with matching id. Treats a plain close as an implicit cancel."""
        try:
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                k = msg.get("kind")
                if k == "cancel":
                    cancel.cancel()
                    return
                if k == "answer":
                    fut = pending_confirms.pop(msg.get("id", ""), None)
                    if fut is not None and not fut.done():
                        fut.set_result(bool(msg.get("allow")))
        except WebSocketDisconnect:
            cancel.cancel()
            for fut in list(pending_confirms.values()):
                if not fut.done():
                    fut.set_result(False)
        except Exception:  # noqa: BLE001 - anything unexpected treats as cancel
            cancel.cancel()

    async def _ask_confirm(call_id: str, tool: str, args: dict) -> bool:
        """Wait for the client to answer a `confirm` event. Returns True
        for allow, False for deny. A disconnect while awaiting resolves
        as False so the loop terminates cleanly."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        pending_confirms[call_id] = fut
        try:
            return await fut
        except asyncio.CancelledError:
            return False

    try:
        req = await websocket.receive_json()
        message = str(req.get("message") or "").strip()
        attachments = req.get("attachments") or []
        if not message and not attachments:
            await websocket.send_json({"kind": "error", "message": "empty message"})
            return
        # Concurrent listener for cancel and confirm-answer messages;
        # runs alongside the generator drain below.
        watcher = asyncio.create_task(_watch_client())
        # DB session used to sit open for the whole turn — many minutes
        # if the model ran a lot of tool calls. Every yield point held
        # a pooled connection, and N concurrent chats or hung
        # disconnects progressively exhausted the pool (see the QueuePool
        # timeout error users hit in 0.6.0). Now every DB touch opens
        # its own short-lived session, so the connection lives only as
        # long as the query it wraps.
        try:
            async with SessionLocal() as _ctx_session:
                await _lab_context(_ctx_session, lab_id)
        except ApiError as exc:
            await websocket.send_json({"kind": "error", "message": exc.message})
            return
        # Mint a short-lived bearer token for the resolved WS user so
        # the agent loop's callbacks into our own API (via
        # settings.self_url) carry an Authorization header.
        try:
            async for ev in stream_agent(
                lab_id, message, token=_agent_token(user),
                cancel=cancel, confirm=_ask_confirm,
                attachments=attachments,
            ):
                await websocket.send_json(ev)
        except StreamCancelled:
            # Cancel is a first-class outcome, not an error.
            await websocket.send_json({"kind": "cancelled"})
        except LlmUnavailable as exc:
            async with SessionLocal() as _fb_session:
                log = await local_plan(_fb_session, lab_id, message)
            reply = (
                "\n".join(log) if log else "No LLM configured and I could not match that request."
            ) + f"\n\n({exc})"
            await websocket.send_json({"kind": "turn", "text": reply, "applied": log})
        await websocket.send_json({"kind": "done"})
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
    except WebSocketDisconnect:
        cancel.cancel()
        return
    except Exception as exc:  # noqa: BLE001 - never let a bug drop the socket silently
        try:
            await websocket.send_json({"kind": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001 — socket may already be gone
            pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/ai/status")
async def ai_status(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    """Whether the assistant has a model to talk to, and which tools it has."""
    return {
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "has_key": bool(settings.llm_api_key),
        "tools": sorted(TOOLS),
        "reachable": await llm_reachable(),
    }


@router.get("/agent/tools")
async def agent_tools(_user: User = Depends(get_current_user)) -> dict[str, Any]:
    """The tool schemas, in the shape an OpenAI-compatible API wants.

    Handing these to the browser is what lets the key stay there: the client
    runs the conversation with its own provider, and comes back here only to
    execute what the model decided to call. Labtris never sees the key, which
    means an operator cannot leak one — the strongest version of that promise
    is not having it.
    """
    from labtris_api.agent import SYSTEM, _openai_tools

    return {"tools": _openai_tools(), "system": SYSTEM}


class ToolCall(BaseModel):
    """One tool invocation the model asked for."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    lab_id: str | None = None


@router.post("/agent/tool")
async def agent_tool(
    body: ToolCall,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Run one tool, as the person who asked.

    Deliberately one call per request rather than a batch: the browser drives
    the loop, so it decides what to do with a refusal, and a partial batch
    would leave it guessing which half ran.

    The caller's own credentials are used, so nothing the assistant can do
    exceeds what that person could do by hand. An agent that could is a
    privilege-escalation feature with a chat box on it.
    """
    from labtris_mcp.client import Api
    from labtris_mcp.tools import TOOLS

    entry = TOOLS.get(body.name)
    if entry is None:
        raise bad_request(f"unknown tool {body.name!r}")

    api = Api(base=settings.self_url, token=_agent_token(user))
    args = dict(body.arguments)
    if body.lab_id and "lab_id" in entry[1].get("properties", {}):
        args.setdefault("lab_id", body.lab_id)

    try:
        result = await asyncio.to_thread(entry[2], api, args)
    except ApiError as exc:
        # Returned, not raised: a refusal is information the model should act
        # on, and a 4xx here would look to the browser like its own bug.
        return {"ok": False, "error": f"{exc.status}: {exc.message}", "tool": body.name}
    except Exception as exc:  # noqa: BLE001 - one bad call must not end the turn
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tool": body.name}
    return {"ok": True, "result": result, "tool": body.name}
