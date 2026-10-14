"""POST /nodes/{id}/reboot is stop_node then start_node, with /stop's body."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from labtris_api.routers import nodes as r
from labtris_api.runtime.base import StopMode
from labtris_api.schemas import StopBody


@pytest.mark.asyncio
async def test_running_node_is_stopped_then_started(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    node = SimpleNamespace(id="n1", state="running")

    async def get_node(session, node_id):  # noqa: ANN001, ANN202
        return node

    async def stop_node(session, n, mode):  # noqa: ANN001, ANN202
        calls.append(("stop", mode))
        n.state = "stopped"
        return n

    async def start_node(session, n):  # noqa: ANN001, ANN202
        calls.append(("start", None))
        n.state = "running"
        return n

    monkeypatch.setattr(r, "get_node", get_node)
    monkeypatch.setattr(r, "stop_node", stop_node)
    monkeypatch.setattr(r, "start_node", start_node)
    out = await r.do_reboot("n1", StopBody(mode="force"), session=None, _user=None)
    assert calls == [("stop", StopMode.FORCE), ("start", None)]
    assert out.state == "running"


@pytest.mark.asyncio
async def test_stopped_node_just_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    node = SimpleNamespace(id="n1", state="stopped")

    async def get_node(session, node_id):  # noqa: ANN001, ANN202
        return node

    async def stop_node(session, n, mode):  # noqa: ANN001, ANN202
        calls.append("stop")
        return n

    async def start_node(session, n):  # noqa: ANN001, ANN202
        calls.append("start")
        return n

    monkeypatch.setattr(r, "get_node", get_node)
    monkeypatch.setattr(r, "stop_node", stop_node)
    monkeypatch.setattr(r, "start_node", start_node)
    await r.do_reboot("n1", None, session=None, _user=None)
    assert calls == ["start"]
