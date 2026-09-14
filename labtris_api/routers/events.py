from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["events"])


class Hub:
    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    async def publish(self, lab_id: str, msg: dict[str, Any]) -> None:
        for q in list(self._subs.get(lab_id, [])):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def subscribe(self, lab_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subs.setdefault(lab_id, []).append(q)
        return q

    def unsubscribe(self, lab_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subs.get(lab_id) or []
        if q in subs:
            subs.remove(q)


hub = Hub()


async def announce_topology(lab_id: str) -> None:
    """Nudge every browser watching this lab that the shape of it changed —
    a node added, an interface joined to a network, a link drawn, a template
    swapped in. State changes have their own channel (announce() in
    lifecycle.py); this one covers everything else that the canvas needs to
    redraw for.

    Broadcast rather than incremental. The client already refetches the
    whole lab on any WS frame, so we don't send a diff; the coalescing
    setTimeout on the browser side turns a burst (add three interfaces in
    a loop) into one refresh."""
    try:
        await hub.publish(lab_id, {"type": "topology"})
    except Exception:  # noqa: BLE001 - the mutation is the news, not this
        pass


@router.websocket("/labs/{lab_id}/ws")
async def lab_ws(websocket: WebSocket, lab_id: str) -> None:
    from labtris_api.auth import resolve_ws_user
    from labtris_api.db import SessionLocal

    await websocket.accept()
    async with SessionLocal() as session:
        user = await resolve_ws_user(websocket, session)
    if user is None:
        # A silent close reads to the client like "the server dropped
        # you" and triggers reconnect loops. Send a hello-shaped error
        # first so the browser can show it plainly.
        await websocket.send_json({"type": "error", "error": "sign in to watch this lab"})
        await websocket.close(code=4401, reason="unauthorized")
        return
    q = hub.subscribe(lab_id)
    try:
        await websocket.send_json({"type": "hello", "lab_id": lab_id})
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    finally:
        hub.unsubscribe(lab_id, q)
