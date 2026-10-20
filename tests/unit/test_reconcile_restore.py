"""Startup: wait for a runtime that is still coming up, then start again what was running.

Seen on the Hetzner compose install on 2026-10-20: an upgrade recreated dind
along with the api, reconcile asked dind about 13 container nodes once,
got errors because dind was not answering yet, skipped them all, and they
stayed "running" with nothing behind them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from labtris_api import reconcile as r
from labtris_api.runtime.base import RuntimeState


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _Rows(self.rows)


def _node(name, runtime="docker"):
    return SimpleNamespace(id=f"id-{name}", name=name, runtime=runtime,
                           runtime_ref=f"ref-{name}", state="running", interfaces=[])


@pytest.mark.asyncio
async def test_a_runtime_that_answers_late_is_waited_for(monkeypatch) -> None:
    calls = {"n": 0}

    class LateDocker:
        async def observe(self, h):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("dind not up yet")
            return RuntimeState(exists=True, running=False, pid=None, exit_code=0)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr("labtris_api.runtime.registry.get_runtime", lambda _n: LateDocker())
    monkeypatch.setattr(r.asyncio, "sleep", no_sleep)
    stale = await r._stale_running_nodes(_Session([_node("trex0")]), wait_s=60)
    assert [n.name for n in stale] == ["trex0"], "dead once dind answers, so it is stale"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_without_a_wait_an_unreachable_runtime_is_still_not_proof(monkeypatch) -> None:
    class Down:
        async def observe(self, h):
            raise RuntimeError("no dind")

    monkeypatch.setattr("labtris_api.runtime.registry.get_runtime", lambda _n: Down())
    stale = await r._stale_running_nodes(_Session([_node("trex0")]), wait_s=0)
    assert stale == [], "never answered: left alone, not marked stopped on a guess"


@pytest.mark.asyncio
async def test_restore_starts_each_node_and_reports_failures(monkeypatch) -> None:
    nodes = {"a": _node("a"), "b": _node("b")}
    for n in nodes.values():
        n.state = "stopped"

    async def get_node(_s, node_id):
        return nodes[node_id.removeprefix("id-")]

    async def start_node(_s, node):
        if node.name == "b":
            raise RuntimeError("disk locked")
        node.state = "running"
        return node

    monkeypatch.setattr("labtris_api.lifecycle.get_node", get_node)
    monkeypatch.setattr("labtris_api.lifecycle.start_node", start_node)

    @asynccontextmanager
    async def factory():
        yield None

    out = await r.restore_nodes(factory, ["id-a", "id-b"])
    assert out["a"] == "running"
    assert out["id-b"].startswith("failed: disk locked")
