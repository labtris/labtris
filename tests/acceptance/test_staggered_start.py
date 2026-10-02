"""Starting a big lab: in waves, most-connected first, and stopping before
the host dies rather than after.

start_all used to be a strictly sequential loop over whatever order the
database handed back, and one node that would not start aborted the rest. On
a 3267-node fabric that is hours of wall clock, 23 of 24 cores idle, hosts
coming up before the switch they peer with, and a single bad image taking the
whole run down.
"""

from __future__ import annotations

import asyncio

import ulid
from httpx import AsyncClient

from labtris_api.routers import tasks as tasks_mod
from labtris_api.schemas import TaskIn


class _FakeNode:
    """Enough of a Node for the wave logic: it only reads id and name."""

    def __init__(self, nid: str) -> None:
        self.id = nid
        self.name = nid


async def _identity(nodes):  # noqa: ANN001, ANN201
    return list(nodes)


async def _noop(*a, **k):  # noqa: ANN002, ANN003, ANN201
    return None


class _FakeSession:
    """Stands in for the session _run_in_waves opens per node.

    The wave re-fetches each node by id, because the list it was handed came
    from a session that is already closed. These tests are about pacing, not
    persistence, so the fetch hands back the fake it was asked for.
    """

    def __init__(self, by_id: dict[str, object]) -> None:
        self._by_id = by_id

    async def get(self, _model, nid):  # noqa: ANN001, ANN201
        return self._by_id.get(nid)

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN204
        return False


def _fake_sessions(nodes):  # noqa: ANN001, ANN201
    by_id = {n.id: n for n in nodes}
    return lambda: _FakeSession(by_id)


async def _lab_with(client: AsyncClient, shape: dict[str, int]) -> tuple[str, dict[str, str]]:
    """A lab whose nodes have the given interface counts."""
    lab = (await client.post("/api/v1/labs", json={"name": f"stag-{ulid.new().str[-8:].lower()}"})).json()
    ids: dict[str, str] = {}
    for name, ifaces in shape.items():
        n = (
            await client.post(
                f"/api/v1/labs/{lab['id']}/nodes",
                json={"name": name, "runtime": "docker", "image": "alpine:3.20"},
            )
        ).json()
        ids[name] = n["id"]
        for _ in range(ifaces):
            await client.post(f"/api/v1/nodes/{n['id']}/interfaces", json={})
    return lab["id"], ids


async def test_the_most_connected_nodes_start_first(client: AsyncClient) -> None:
    """A host whose edge switch is not up yet finds no peer and backs off, so
    the fabric converges on BGP's retry timer instead of on how fast nodes
    started. Interface count is the topology-agnostic way to get spines and
    cores up first — the task runner never has to know what a fat tree is.
    """
    lab_id, ids = await _lab_with(client, {"host-x": 1, "core-x": 6, "edge-x": 3})

    from sqlalchemy import select

    from labtris_api.db import SessionLocal
    from labtris_api.models import Node

    async with SessionLocal() as session:
        nodes = list((await session.execute(select(Node).where(Node.lab_id == lab_id))).scalars())
    ordered = await tasks_mod._ordered_for_start(nodes)  # noqa: SLF001

    assert [n.name for n in ordered] == ["core-x", "edge-x", "host-x"]
    await client.delete(f"/api/v1/labs/{lab_id}")


async def test_a_low_memory_host_stops_starting_instead_of_being_oom_killed(
    monkeypatch,
) -> None:
    """Without the floor, a fabric bigger than the host runs until the OOM
    killer picks a victim — which may be Postgres or the API rather than a
    node, leaving an instance that has to be rebooted to find out what
    happened.

    No database here on purpose: the guard is arithmetic on /proc/meminfo and
    a list, and reaching for the app's own session factory from a test only
    buys a different-event-loop failure in a test about memory.
    """
    said: list[str] = []
    started: list[str] = []

    async def fake_progress(task_id, progress, total, message):  # noqa: ANN001
        said.append(message)

    async def fake_start(session, node):  # noqa: ANN001
        started.append(node.id)

    monkeypatch.setattr(tasks_mod, "_set_progress", fake_progress)
    monkeypatch.setattr(tasks_mod, "start_node", fake_start)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 100)

    nodes = [_FakeNode(f"n{i}") for i in range(10)]
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    done, failed, reasons = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "start_all", nodes, len(nodes),
        TaskIn(kind="start_all", min_free_mb=2048, batch=2, stagger_ms=0),
    )

    assert (done, failed) == (0, 0)
    assert started == [], "nothing should have been attempted"
    assert any("under the 2048 MB floor" in m for m in said), said


async def test_a_node_that_will_not_start_does_not_abort_the_other_three_thousand(
    monkeypatch,
) -> None:
    """The old loop let one exception out of the whole task, so a single bad
    image took down a run of any size."""
    attempted: list[str] = []

    async def fake_start(session, node):  # noqa: ANN001
        attempted.append(node.id)
        if node.id == "n3":
            raise RuntimeError("no such image")

    async def fake_progress(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return None

    monkeypatch.setattr(tasks_mod, "_set_progress", fake_progress)
    monkeypatch.setattr(tasks_mod, "start_node", fake_start)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 999_999)

    nodes = [_FakeNode(f"n{i}") for i in range(6)]
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    done, failed, reasons = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "start_all", nodes, len(nodes),
        TaskIn(kind="start_all", min_free_mb=0, batch=2, stagger_ms=0),
    )

    assert len(attempted) == 6, "every node should still have been tried"
    assert (done, failed) == (5, 1)


async def test_nodes_go_up_in_waves_not_one_at_a_time(monkeypatch) -> None:
    """The point of the change: concurrency within a wave, a pause between."""
    concurrent = 0
    high_water = 0

    async def fake_start(session, node):  # noqa: ANN001
        nonlocal concurrent, high_water
        concurrent += 1
        high_water = max(high_water, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1

    async def fake_progress(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return None

    monkeypatch.setattr(tasks_mod, "_set_progress", fake_progress)
    monkeypatch.setattr(tasks_mod, "start_node", fake_start)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 999_999)

    nodes = [_FakeNode(f"n{i}") for i in range(16)]
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    done, _, _ = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "start_all", nodes, len(nodes),
        TaskIn(kind="start_all", min_free_mb=0, batch=8, stagger_ms=0),
    )

    assert done == 16
    assert high_water == 8, f"expected a wave of 8 in flight, saw {high_water}"


async def test_the_floor_can_be_switched_off(monkeypatch) -> None:
    """0 disables it. A host with cgroup limits, or any setup where
    MemAvailable reads oddly, should not be unable to start its own lab.
    """
    started: list[str] = []

    async def fake_start(session, node):  # noqa: ANN001
        started.append(node.id)

    async def fake_progress(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return None

    nodes = [_FakeNode(f"n{i}") for i in range(4)]
    monkeypatch.setattr(tasks_mod, "_set_progress", fake_progress)
    monkeypatch.setattr(tasks_mod, "start_node", fake_start)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    # Would stop everything if the floor were consulted at all.
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 1)

    done, failed, reasons = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "start_all", nodes, len(nodes),
        TaskIn(kind="start_all", min_free_mb=0, batch=2, stagger_ms=0),
    )

    assert (done, failed) == (4, 0)
    assert len(started) == 4


def test_mem_available_is_read_not_mem_free() -> None:
    """MemFree excludes reclaimable page cache and reads catastrophically low
    on a merely warm host, so gating on it would refuse labs that fit."""
    import inspect

    src = inspect.getsource(tasks_mod._mem_available_mb)  # noqa: SLF001
    assert "MemAvailable:" in src
    assert "MemFree" not in src.split('"""')[2]


def test_the_defaults_are_a_wave_and_a_pause() -> None:
    d = TaskIn(kind="start_all")
    assert d.batch > 1, "sequential was the bug"
    assert d.stagger_ms > 0, "flat out buries the container runtime"


async def test_a_huge_batch_cannot_drain_the_connection_pool(monkeypatch) -> None:
    """Each node in flight holds a session for as long as its start takes, so
    an unbounded wave starves the HTTP handlers. That is how this first
    showed up — not as a slow start, but as the interface getting

        TimeoutError: QueuePool limit of size 5 overflow 10 reached

    from /system/diagnostics, and nginx turning the backlog into 502s.
    """
    from labtris_api.config import settings

    concurrent = 0
    high_water = 0

    async def fake_start(session, node):  # noqa: ANN001
        nonlocal concurrent, high_water
        concurrent += 1
        high_water = max(high_water, concurrent)
        await asyncio.sleep(0.005)
        concurrent -= 1

    async def fake_progress(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return None

    nodes = [_FakeNode(f"n{i}") for i in range(256)]
    monkeypatch.setattr(tasks_mod, "_set_progress", fake_progress)
    monkeypatch.setattr(tasks_mod, "start_node", fake_start)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 999_999)

    done, _, _ = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "start_all", nodes, len(nodes),
        TaskIn(kind="start_all", min_free_mb=0, batch=256, stagger_ms=0),
    )

    budget = (settings.db_pool_size + settings.db_max_overflow) // 2
    assert done == 256, "every node still starts; only the width is capped"
    assert high_water <= budget, f"{high_water} in flight against a budget of {budget}"
    assert high_water < 256, "a batch of 256 must not put 256 sessions in flight"


async def test_a_failure_reports_why_not_a_pointer_to_an_empty_field(monkeypatch) -> None:
    """The first version counted failures and said "N failed (each node's
    last_error says why)". start_node records last_error for a failure during
    a start, but refuses outright for a node already in `starting` — before
    there is anything to record. So a real run reported 6 failures while no
    node carried an error, and the message sent you somewhere empty.
    """
    async def fake_start(session, node):  # noqa: ANN001
        if node.id in ("n1", "n2"):
            raise RuntimeError("node 'n1' is already starting")
        raise RuntimeError("no such image: frr:bogus")

    async def fake_progress(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return None

    nodes = [_FakeNode(f"n{i}") for i in range(5)]
    monkeypatch.setattr(tasks_mod, "_set_progress", fake_progress)
    monkeypatch.setattr(tasks_mod, "start_node", fake_start)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 999_999)

    done, failed, reasons = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "start_all", nodes, len(nodes),
        TaskIn(kind="start_all", min_free_mb=0, batch=2, stagger_ms=0),
    )

    assert (done, failed) == (0, 5)
    assert sum(reasons.values()) == 5
    assert any("no such image" in why for why in reasons), reasons
    # The node's own name is substituted out, so two nodes failing the same
    # way count as one reason rather than two near-identical strings.
    assert any("already starting" in why and "n1" not in why for why in reasons), reasons
    assert len(reasons) == 2, reasons


async def test_apply_configs_pushes_and_runs_the_startup_config(monkeypatch) -> None:
    """`generate --with-bgp-evpn` wrote each node a config and stopped there:
    applying it was push + exec, two manual calls per node. On a 3267-node fat
    tree that is 6534 operations, so in practice the fabric came up with
    bgpd=no everywhere and `show bgp summary` said "bgpd is not running".
    """
    pushed: list[tuple[str, str]] = []
    ran: list[str] = []

    class FakeRuntime:
        async def write_file(self, h, path, content):  # noqa: ANN001, ANN202
            pushed.append((h.node_id, path))

        async def exec_shell(self, h, command):  # noqa: ANN001, ANN202
            ran.append(command)
            return 0, ""

    class Node:
        def __init__(self, nid: str) -> None:
            self.id = nid
            self.name = nid
            self.startup_config = "#!/bin/sh\necho hi\n"
            self.state = "running"
            self.runtime_ref = f"ref-{nid}"
            self.runtime = "docker"

    nodes = [Node(f"n{i}") for i in range(4)]
    monkeypatch.setattr(tasks_mod, "get_runtime", lambda kind: FakeRuntime())
    monkeypatch.setattr(tasks_mod, "_set_progress", _noop)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 999_999)

    done, failed, _ = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "apply_configs", nodes, len(nodes),
        TaskIn(kind="apply_configs", min_free_mb=0, batch=2, stagger_ms=0),
    )

    assert (done, failed) == (4, 0)
    assert len(pushed) == 4
    assert all(p[1] == "/config/startup-config" for p in pushed)
    assert ran == ["sh /config/startup-config"] * 4, ran


async def test_a_config_that_fails_to_apply_is_reported_not_silently_skipped(
    monkeypatch,
) -> None:
    """A non-zero exit from the install script means the node is running FRR
    with whatever it had before — which looks identical to success unless the
    rc is checked."""
    class FakeRuntime:
        async def write_file(self, h, path, content):  # noqa: ANN001, ANN202
            return None

        async def exec_shell(self, h, command):  # noqa: ANN001, ANN202
            return 1, "vtysh: syntax error at line 4"

    class Node:
        def __init__(self, nid: str) -> None:
            self.id = nid
            self.name = nid
            self.startup_config = "x"
            self.state = "running"
            self.runtime_ref = f"ref-{nid}"
            self.runtime = "docker"

    nodes = [Node("n0"), Node("n1")]
    monkeypatch.setattr(tasks_mod, "get_runtime", lambda kind: FakeRuntime())
    monkeypatch.setattr(tasks_mod, "_set_progress", _noop)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 999_999)

    done, failed, reasons = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "apply_configs", nodes, len(nodes),
        TaskIn(kind="apply_configs", min_free_mb=0, batch=2, stagger_ms=0),
    )

    assert (done, failed) == (0, 2)
    assert any("syntax error" in why for why in reasons), reasons


async def test_a_node_with_no_config_is_not_a_failure(monkeypatch) -> None:
    """A plain alpine host in a generated fabric has no startup-config, and
    counting it as failed would make every mixed lab look broken."""
    class Node:
        def __init__(self, nid: str) -> None:
            self.id = nid
            self.name = nid
            self.startup_config = None
            self.state = "running"
            self.runtime_ref = "r"
            self.runtime = "docker"

    nodes = [Node("h0"), Node("h1")]
    monkeypatch.setattr(tasks_mod, "_set_progress", _noop)
    monkeypatch.setattr(tasks_mod, "_ordered_for_start", _identity)
    monkeypatch.setattr(tasks_mod, "SessionLocal", _fake_sessions(nodes))
    monkeypatch.setattr(tasks_mod, "_mem_available_mb", lambda: 999_999)

    done, failed, _ = await tasks_mod._run_in_waves(  # noqa: SLF001
        "t", "apply_configs", nodes, len(nodes),
        TaskIn(kind="apply_configs", min_free_mb=0, batch=2, stagger_ms=0),
    )

    assert (done, failed) == (2, 0)
