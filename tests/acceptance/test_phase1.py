from __future__ import annotations

import os
import re
from typing import Any

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.main import create_app
from labtris_api.runtime.docker import docker_runtime


async def _can_db() -> bool:
    try:
        engine = create_async_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        await engine.dispose()
        return True
    except Exception:
        return False


async def _can_docker() -> bool:
    return await docker_runtime.ping_engine()


async def _can_netd() -> bool:
    from labtris_api.netd_client import netd

    return await netd.ping()


@pytest.fixture
async def client():
    if not await _can_db():
        pytest.skip("Postgres is not available (start with `make dev-db`)")
    if not await _can_docker():
        pytest.skip("Docker is not available")
    if not await _can_netd():
        pytest.skip("netd is not running (start with `make netd`)")

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_session():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await engine.dispose()


async def post(client: AsyncClient, path: str, body: dict[str, Any] | None = None) -> Any:
    r = await client.post(path, json=body or {})
    assert r.status_code in {200, 201}, r.text
    return r.json()


async def delete(client: AsyncClient, path: str) -> None:
    r = await client.delete(path)
    assert r.status_code in {200, 204}, r.text


async def dexec(node: dict[str, Any], command: str) -> tuple[int, str]:
    from labtris_api.runtime.base import RuntimeHandle

    ref = node.get("runtime_ref")
    assert ref, node
    return await docker_runtime.exec_shell(RuntimeHandle(node_id=node["id"], ref=ref), command)


#: Both naming schemes, so this keeps seeing devices after the readable names
#: landed rather than quietly checking nothing.
LAB_DEVICE_RE = r"^(t|b|v|x)([0-9a-z]{8}|[0-9a-z]{1,9}-[0-9a-z]{4})$"


async def host_ifaces_matching(pat: str = LAB_DEVICE_RE) -> set[str]:
    rx = re.compile(pat)
    return {n for n in os.listdir("/sys/class/net") if rx.match(n)}


async def test_two_alpine_containers_ping_over_a_link(client: AsyncClient) -> None:
    # What this test wants to know is that *its* lab cleans up after itself.
    # It used to assert that no lab device existed anywhere on the host, and
    # made that true by deleting every one it found first — so running the
    # suite destroyed the dataplane of every other lab on the machine, live
    # ones included, and the damage was invisible because the lab rows stayed
    # behind claiming devices that were gone.
    before = await host_ifaces_matching()
    lab = await post(client, "/api/v1/labs", {"name": f"p1-{ulid.new().str[-8:].lower()}"})

    a = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {
            "name": "a",
            "runtime": "docker",
            "image": "alpine:3.20",
            "cmd": ["sleep", "3600"],
            "interfaces": [{"name": "eth1"}],
        },
    )
    b = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {
            "name": "b",
            "runtime": "docker",
            "image": "alpine:3.20",
            "cmd": ["sleep", "3600"],
            "interfaces": [{"name": "eth1"}],
        },
    )

    await post(
        client,
        f"/api/v1/labs/{lab['id']}/links",
        {"a_iface_id": a["interfaces"][0]["id"], "b_iface_id": b["interfaces"][0]["id"]},
    )

    a = await post(client, f"/api/v1/nodes/{a['id']}/start", {})
    b = await post(client, f"/api/v1/nodes/{b['id']}/start", {})

    rc, out = await dexec(a, "ip addr add 10.99.0.1/24 dev eth1 && ip link set eth1 up")
    assert rc == 0, out
    rc, out = await dexec(b, "ip addr add 10.99.0.2/24 dev eth1 && ip link set eth1 up")
    assert rc == 0, out
    rc, out = await dexec(a, "ping -c3 -W2 10.99.0.2")
    assert rc == 0, out

    ours = await host_ifaces_matching() - before
    assert ours, "the lab should have created host devices"

    await delete(client, f"/api/v1/labs/{lab['id']}")

    survived = await host_ifaces_matching() & ours
    assert not survived, f"deleting the lab left {sorted(survived)} behind"


async def test_link_uniqueness(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"uniq-link-{ulid.new().str[-8:].lower()}"})
    a = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "alpine:3.20", "interfaces": [{}]},
    )
    b = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "b", "runtime": "docker", "image": "alpine:3.20", "interfaces": [{}]},
    )
    await post(
        client,
        f"/api/v1/labs/{lab['id']}/links",
        {"a_iface_id": a["interfaces"][0]["id"], "b_iface_id": b["interfaces"][0]["id"]},
    )
    r = await client.post(
        f"/api/v1/labs/{lab['id']}/links",
        json={"a_iface_id": a["interfaces"][0]["id"], "b_iface_id": b["interfaces"][0]["id"]},
    )
    assert r.status_code == 409
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_start_is_idempotent(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"idem-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {
            "name": "solo",
            "runtime": "docker",
            "image": "alpine:3.20",
            "cmd": ["sleep", "3600"],
        },
    )
    first = await post(client, f"/api/v1/nodes/{n['id']}/start", {})
    second = await client.post(f"/api/v1/nodes/{n['id']}/start", json={})
    assert second.status_code == 200, second.text
    assert second.json()["state"] == first["state"]
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_start_after_stop_recreates_dataplane(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"restart-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {
            "name": "solo",
            "runtime": "docker",
            "image": "alpine:3.20",
            "cmd": ["sleep", "3600"],
        },
    )
    started = await post(client, f"/api/v1/nodes/{n['id']}/start", {})
    assert started["state"] == "running"
    stopped = await post(client, f"/api/v1/nodes/{n['id']}/stop", {"mode": "force"})
    assert stopped["state"] == "stopped"
    again = await post(client, f"/api/v1/nodes/{n['id']}/start", {})
    assert again["state"] == "running", again
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_capabilities_exposed(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"caps-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "c", "runtime": "docker", "image": "alpine:3.20"},
    )
    r = await client.get(f"/api/v1/nodes/{n['id']}")
    assert r.status_code == 200, r.text
    caps = r.json()["capabilities"]
    assert "exec" in caps
    assert "hotplug_nic" in caps
    assert "suspend" in caps
    assert "snapshot" not in caps
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_ai_creates_and_wires_two_alpine(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"ai-{ulid.new().str[-8:].lower()}"})
    r = await post(
        client, f"/api/v1/labs/{lab['id']}/ai", {"message": "create 2 alpine and wire them"}
    )
    assert any("created" in line for line in r["applied"])
    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()
    assert len(detail["nodes"]) == 2
    assert len(detail["links"]) == 1
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_impair_preset_and_capture_on_running_link(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"qos-{ulid.new().str[-8:].lower()}"})
    a = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {
            "name": "a",
            "runtime": "docker",
            "image": "alpine:3.20",
            "cmd": ["sleep", "3600"],
            "interfaces": [{"name": "eth1"}],
        },
    )
    b = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {
            "name": "b",
            "runtime": "docker",
            "image": "alpine:3.20",
            "cmd": ["sleep", "3600"],
            "interfaces": [{"name": "eth1"}],
        },
    )
    link = await post(
        client,
        f"/api/v1/labs/{lab['id']}/links",
        {"a_iface_id": a["interfaces"][0]["id"], "b_iface_id": b["interfaces"][0]["id"]},
    )
    a = await post(client, f"/api/v1/nodes/{a['id']}/start", {})
    b = await post(client, f"/api/v1/nodes/{b['id']}/start", {})
    patched = await client.patch(f"/api/v1/links/{link['id']}", json={"preset": "satellite"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["impair_ab"]["delay_ms"] == 550
    cap = await post(client, f"/api/v1/links/{link['id']}/capture/start", {"bpf": "icmp"})
    assert cap["status"] == "capturing"
    await dexec(a, "ip addr add 10.98.0.1/24 dev eth1 && ip link set eth1 up")
    await dexec(b, "ip addr add 10.98.0.2/24 dev eth1 && ip link set eth1 up")
    await dexec(a, "ping -c2 -W2 10.98.0.2")
    got = (await client.get(f"/api/v1/links/{link['id']}/capture")).json()
    assert "lines" in got
    await post(client, f"/api/v1/links/{link['id']}/capture/stop", {})
    presets = (await client.get("/api/v1/impair/presets")).json()
    assert "3g" in presets["presets"]
    tuning = (await client.get("/api/v1/system/tuning")).json()
    assert "wanted" in tuning
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_lab_lock_blocks_topology_edits(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"lock-{ulid.new().str[-8:].lower()}"})
    await post(client, f"/api/v1/labs/{lab['id']}/lock", {})
    r = await client.post(
        f"/api/v1/labs/{lab['id']}/nodes",
        json={"name": "a", "runtime": "docker", "image": "alpine:3.20"},
    )
    assert r.status_code == 409, r.text
    await post(client, f"/api/v1/labs/{lab['id']}/unlock", {})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "alpine:3.20"},
    )
    assert n["name"] == "a"
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_lab_rename(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"old-{ulid.new().str[-8:].lower()}"})
    new_name = f"new-{ulid.new().str[-8:].lower()}"
    r = await client.patch(f"/api/v1/labs/{lab['id']}", json={"name": new_name})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == new_name
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_node_style_patch(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"style-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "alpine:3.20"},
    )
    r = await client.patch(
        f"/api/v1/nodes/{n['id']}/style", json={"icon": "router", "color": "#f00"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["style"] == {"icon": "router", "color": "#f00"}
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_export_node_as_template_feeds_catalog(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"tmpl-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "nginx:alpine"},
    )
    tname = f"nginx-golden-{ulid.new().str[-6:].lower()}"
    tmpl = await post(client, f"/api/v1/nodes/{n['id']}/export", {"name": tname, "icon": "web"})
    assert tmpl["image"] == "nginx:alpine"
    cat = (await client.get("/api/v1/catalog")).json()
    assert any(t["label"] == tname for t in cat["templates"])
    await client.delete(f"/api/v1/templates/{tmpl['id']}")
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_config_save_read_and_push_when_running(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"cfg-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "alpine:3.20", "cmd": ["sleep", "3600"]},
    )
    saved = await client.put(f"/api/v1/nodes/{n['id']}/config", json={"content": "hostname demo\n"})
    assert saved.status_code == 200, saved.text
    got = (await client.get(f"/api/v1/nodes/{n['id']}/config")).json()
    assert got["content"] == "hostname demo\n"
    n = await post(client, f"/api/v1/nodes/{n['id']}/start", {})
    push = await client.post(f"/api/v1/nodes/{n['id']}/config/push")
    assert push.status_code == 200, push.text
    rc, out = await dexec(n, "cat /config/startup-config")
    assert rc == 0 and "hostname demo" in out, out
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_configset_apply_pushes_to_running_nodes(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"cfgset-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "alpine:3.20", "cmd": ["sleep", "3600"]},
    )
    n = await post(client, f"/api/v1/nodes/{n['id']}/start", {})
    r = await client.put(
        f"/api/v1/labs/{lab['id']}/configsets/golden",
        json={"configs": {n["id"]: "interface eth0\n no shutdown\n"}},
    )
    assert r.status_code == 200, r.text
    applied = await post(client, f"/api/v1/labs/{lab['id']}/configsets/golden/apply", {})
    assert n["id"] in applied["pushed"]
    rc, out = await dexec(n, "cat /config/startup-config")
    assert rc == 0 and "no shutdown" in out, out
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_suspend_resume_running_node(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"susp-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "alpine:3.20", "cmd": ["sleep", "3600"]},
    )
    n = await post(client, f"/api/v1/nodes/{n['id']}/start", {})
    suspended = await post(client, f"/api/v1/nodes/{n['id']}/suspend", {})
    assert suspended["paused"] is True
    resumed = await post(client, f"/api/v1/nodes/{n['id']}/resume", {})
    assert resumed["paused"] is False
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_log_tail_with_pattern(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"logs-{ulid.new().str[-8:].lower()}"})
    n = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {
            "name": "a",
            "runtime": "docker",
            "image": "alpine:3.20",
            "cmd": ["sh", "-c", "echo hello-pnl && sleep 3600"],
        },
    )
    n = await post(client, f"/api/v1/nodes/{n['id']}/start", {})
    r = await client.get(f"/api/v1/nodes/{n['id']}/logs", params={"lines": 50, "pattern": "hello"})
    assert r.status_code == 200, r.text
    assert any("hello-pnl" in line for line in r.json()["lines"])
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_task_queue_start_all_reports_progress(client: AsyncClient) -> None:
    import asyncio

    lab = await post(client, "/api/v1/labs", {"name": f"task-{ulid.new().str[-8:].lower()}"})
    for name in ("a", "b"):
        await post(
            client,
            f"/api/v1/labs/{lab['id']}/nodes",
            {"name": name, "runtime": "docker", "image": "alpine:3.20", "cmd": ["sleep", "3600"]},
        )
    task = await post(client, f"/api/v1/labs/{lab['id']}/tasks", {"kind": "start_all"})
    assert task["status"] == "pending"
    for _ in range(50):
        got = (await client.get(f"/api/v1/tasks/{task['id']}")).json()
        if got["status"] == "done":
            break
        await asyncio.sleep(0.2)
    assert got["status"] == "done", got
    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()
    assert all(node["state"] == "running" for node in detail["nodes"])
    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_export_then_import_json_round_trip(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"exp-{ulid.new().str[-8:].lower()}"})
    a = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "a", "runtime": "docker", "image": "alpine:3.20", "interfaces": [{}]},
    )
    b = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "b", "runtime": "docker", "image": "alpine:3.20", "interfaces": [{}]},
    )
    await post(
        client,
        f"/api/v1/labs/{lab['id']}/links",
        {"a_iface_id": a["interfaces"][0]["id"], "b_iface_id": b["interfaces"][0]["id"]},
    )
    await client.put(
        f"/api/v1/labs/{lab['id']}/geometry",
        json={"data": {"nodes": {a["id"]: {"x": 10, "y": 20}}, "view": {"x": 0, "y": 0, "k": 1}}},
    )
    exported = (await client.get(f"/api/v1/labs/{lab['id']}/export")).json()
    imported = await post(client, "/api/v1/labs/import", exported)
    assert len(imported["nodes"]) == 2
    assert len(imported["links"]) == 1
    geo = (await client.get(f"/api/v1/labs/{imported['id']}/geometry")).json()
    new_a = next(n for n in imported["nodes"] if n["name"] == "a")
    assert str(new_a["id"]) in geo["data"]["nodes"]
    await delete(client, f"/api/v1/labs/{lab['id']}")
    await delete(client, f"/api/v1/labs/{imported['id']}")


async def test_unl_import_maps_known_and_unknown_templates(client: AsyncClient) -> None:
    unl = """<lab name="demo"><topology><nodes>
      <node id="1" name="host1" template="linux" left="10" top="20">
        <interface id="0" name="eth0"/>
      </node>
      <node id="2" name="router1" template="csr1000v" left="200" top="20">
        <interface id="0" name="eth0"/>
      </node>
    </nodes></topology></lab>"""
    imported = await post(client, "/api/v1/labs/import/unl", {"xml": unl})
    assert len(imported["nodes"]) == 2
    host1 = next(n for n in imported["nodes"] if n["name"] == "host1")
    router1 = next(n for n in imported["nodes"] if n["name"] == "router1")

    # This assertion used to read alpine:3.20, because the importer predated
    # the QEMU backend and turned every Linux template into a placeholder.
    assert host1["runtime"] == "qemu"
    assert host1["image"] == "ubuntu-24.04"

    # A licensed appliance still cannot run, and still says why.
    assert router1["image"] == "alpine:3.20"
    assert router1["env"].get("LABTRIS_ORIGINAL_TEMPLATE") == "csr1000v"
    assert "csr1000v" in imported["description"]
    await delete(client, f"/api/v1/labs/{imported['id']}")


async def test_a_duplicate_node_name_is_a_conflict_not_a_crash(client: AsyncClient) -> None:
    """The (lab_id, name) unique violation fires at the flush that assigns the
    node an id for its interfaces, which sat outside the handler guarding the
    commit — so a repeated name came back as a 500 with a SQLAlchemy traceback
    in the log instead of a 409 saying what was wrong."""
    lab = (await client.post("/api/v1/labs", json={"name": f"dup-{ulid.new().str[-8:]}"})).json()
    body = {"name": "same", "runtime": "docker", "image": "alpine:3.20", "interfaces": [{}]}

    first = await client.post(f"/api/v1/labs/{lab['id']}/nodes", json=body)
    assert first.status_code == 201, first.text

    second = await client.post(f"/api/v1/labs/{lab['id']}/nodes", json=body)
    assert second.status_code == 409, f"expected a conflict, got {second.status_code}"
    assert "already exists" in second.text

    # and the session must still be usable afterwards
    third = await client.post(
        f"/api/v1/labs/{lab['id']}/nodes", json={**body, "name": "different"}
    )
    assert third.status_code == 201, third.text

    await client.delete(f"/api/v1/labs/{lab['id']}")


async def _mac_rows(owner_ids: list[str] | None = None):
    """Read the reservation table directly — it has no HTTP surface."""
    from sqlalchemy import select

    from labtris_api.models import MacRegistry

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            stmt = select(MacRegistry)
            if owner_ids is not None:
                stmt = stmt.where(MacRegistry.owner_id.in_(owner_ids))
            return [(str(r.mac), r.owner_id) for r in (await s.execute(stmt)).scalars()]
    finally:
        await engine.dispose()


async def test_a_mac_is_reserved_on_create_and_given_back_on_delete(
    client: AsyncClient,
) -> None:
    """A random 46-bit address collides rarely enough to feel safe and often
    enough to ruin a lab when it does — two NICs on a segment answering to the
    same address is a fault nobody thinks to look for. Reserving makes it
    impossible instead of unlikely, and gives deletion something to hand back.
    """
    async def reserved() -> int:
        return len(await _mac_rows())

    lab = (await client.post("/api/v1/labs", json={"name": f"mac-{ulid.new().str[-8:]}"})).json()
    before = await reserved()

    node = (
        await client.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": "n0", "runtime": "docker", "image": "alpine:3.20",
                  "interfaces": [{}, {}, {}]},
        )
    ).json()
    assert len(node["interfaces"]) == 3
    assert await reserved() == before + 3, "each interface must hold a reservation"

    macs = {i["mac"] for i in node["interfaces"]}
    assert len(macs) == 3, "three interfaces, three distinct addresses"

    rows = await _mac_rows([i["id"] for i in node["interfaces"]])
    assert {mac for mac, _ in rows} == macs, "the registry must name the same addresses"

    await client.delete(f"/api/v1/nodes/{node['id']}")
    assert await reserved() == before, "deleting a node returns its addresses"

    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_deleting_a_lab_returns_every_address_it_held(
    client: AsyncClient,
) -> None:
    async def reserved() -> int:
        return len(await _mac_rows())

    before = await reserved()
    lab = (await client.post("/api/v1/labs", json={"name": f"maclab-{ulid.new().str[-8:]}"})).json()
    for i in range(3):
        r = await client.post(
            f"/api/v1/labs/{lab['id']}/nodes",
            json={"name": f"n{i}", "runtime": "docker", "image": "alpine:3.20",
                  "interfaces": [{}, {}]},
        )
        assert r.status_code == 201, r.text
    assert await reserved() == before + 6

    await client.delete(f"/api/v1/labs/{lab['id']}")

    assert await reserved() == before, "a deleted lab must not leak addresses"
