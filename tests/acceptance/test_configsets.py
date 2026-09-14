"""Named startup-config sets: the same topology at several teaching states.

One lab, four saved states — blank, addressed, solution, broken-ospf — and a
button to flip the whole lab between them. The parts worth guarding are the
honesty of "which set is applied" and the refusal to record an absent config
as an empty one, since applying that would blank a node rather than skip it.
"""

from __future__ import annotations

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from labtris_api.auth import User, get_current_user
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.main import create_app

TEACHER = User(id="01CFGTEACHER00000000001", name="Teacher", username="cfgteacher", role="user")


@pytest.fixture()
async def client():
    from labtris_api.db import engine as db_engine
    from labtris_api.models import User as UserRow

    seed_engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(seed_engine, expire_on_commit=False)() as session:
        if await session.get(UserRow, TEACHER.id) is None:
            session.add(
                UserRow(id=TEACHER.id, username=TEACHER.username, display_name=TEACHER.name,
                        password_hash="scrypt$00$00", role=TEACHER.role)
            )
        await session.commit()
    await seed_engine.dispose()

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: TEACHER
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await engine.dispose()
    await db_engine.dispose()


async def _lab_with_nodes(c, count: int = 2) -> tuple[dict, list[dict]]:
    lab = (await c.post("/api/v1/labs", json={"name": f"cfg-{ulid.new().str[-8:].lower()}"})).json()
    nodes = []
    for i in range(count):
        nodes.append(
            (
                await c.post(
                    f"/api/v1/labs/{lab['id']}/nodes",
                    json={"name": f"r{i}", "runtime": "docker", "image": "alpine:3.20",
                          "interfaces": [{}]},
                )
            ).json()
        )
    return lab, nodes


async def test_capture_saves_whatever_is_configured_now(client) -> None:
    """The button an instructor actually wants: finish configuring, name it."""
    lab, nodes = await _lab_with_nodes(client)
    for n in nodes:
        await client.put(
            f"/api/v1/nodes/{n['id']}/config", json={"content": f"hostname {n['name']}"}
        )

    r = await client.post(f"/api/v1/labs/{lab['id']}/configsets/solution/capture")

    assert r.status_code == 200, r.text
    assert r.json()["nodes"] == 2
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_node_with_no_config_is_skipped_not_stored_empty(client) -> None:
    """An empty entry would blank that node on apply, which is a destructive
    way to record "this one was never configured"."""
    lab, nodes = await _lab_with_nodes(client)
    await client.put(f"/api/v1/nodes/{nodes[0]['id']}/config", json={"content": "hostname r0"})

    captured = (await client.post(f"/api/v1/labs/{lab['id']}/configsets/partial/capture")).json()
    listing = (await client.get(f"/api/v1/labs/{lab['id']}/configsets")).json()

    assert captured["nodes"] == 1
    assert nodes[1]["id"] not in listing["configsets"]["partial"]
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_capturing_nothing_is_an_error_not_an_empty_set(client) -> None:
    lab, _ = await _lab_with_nodes(client)

    r = await client.post(f"/api/v1/labs/{lab['id']}/configsets/empty/capture")

    assert r.status_code == 400, r.text
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_applying_a_set_restores_every_nodes_config(client) -> None:
    """The whole point: one action puts the lab back to a known state."""
    lab, nodes = await _lab_with_nodes(client)
    for n in nodes:
        await client.put(f"/api/v1/nodes/{n['id']}/config", json={"content": f"good {n['name']}"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/solution/capture")
    for n in nodes:
        await client.put(f"/api/v1/nodes/{n['id']}/config", json={"content": "student broke it"})

    await client.post(f"/api/v1/labs/{lab['id']}/configsets/solution/apply")

    for n in nodes:
        got = (await client.get(f"/api/v1/nodes/{n['id']}/config")).json()
        assert got["content"] == f"good {n['name']}"
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_the_lab_reports_which_set_is_applied(client) -> None:
    lab, nodes = await _lab_with_nodes(client, 1)
    await client.put(f"/api/v1/nodes/{nodes[0]['id']}/config", json={"content": "x"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/blank/capture")

    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()

    assert detail["active_configset"] == "blank"
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_editing_one_node_drops_the_claim(client) -> None:
    """"Running `solution`" after someone edited a node is worse than saying
    nothing, so the claim is dropped rather than quietly kept."""
    lab, nodes = await _lab_with_nodes(client, 1)
    await client.put(f"/api/v1/nodes/{nodes[0]['id']}/config", json={"content": "x"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/solution/capture")

    await client.put(f"/api/v1/nodes/{nodes[0]['id']}/config", json={"content": "edited"})
    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()

    assert detail["active_configset"] is None
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_deleting_the_active_set_clears_the_claim(client) -> None:
    lab, nodes = await _lab_with_nodes(client, 1)
    await client.put(f"/api/v1/nodes/{nodes[0]['id']}/config", json={"content": "x"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/gone/capture")

    await client.delete(f"/api/v1/labs/{lab['id']}/configsets/gone")
    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()

    assert detail["active_configset"] is None
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_several_sets_coexist_and_are_summarised_by_node_name(client) -> None:
    """The raw map is keyed by node id; a picker showing ULIDs tells nobody
    anything, so the listing resolves names."""
    lab, nodes = await _lab_with_nodes(client, 2)
    for n in nodes:
        await client.put(f"/api/v1/nodes/{n['id']}/config", json={"content": "v1"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/blank/capture")
    for n in nodes:
        await client.put(f"/api/v1/nodes/{n['id']}/config", json={"content": "v2"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/solution/capture")

    listing = (await client.get(f"/api/v1/labs/{lab['id']}/configsets")).json()

    assert [s["name"] for s in listing["summary"]] == ["blank", "solution"]
    assert listing["summary"][0]["node_names"] == ["r0", "r1"]
    assert listing["active"] == "solution"
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_set_that_outlived_a_node_says_so(client) -> None:
    """A set that silently configures three of four nodes is how a lab comes
    up half-right and nobody knows why."""
    lab, nodes = await _lab_with_nodes(client, 2)
    for n in nodes:
        await client.put(f"/api/v1/nodes/{n['id']}/config", json={"content": "x"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/both/capture")
    await client.delete(f"/api/v1/nodes/{nodes[1]['id']}")

    listing = (await client.get(f"/api/v1/labs/{lab['id']}/configsets")).json()
    applied = (await client.post(f"/api/v1/labs/{lab['id']}/configsets/both/apply")).json()

    assert listing["summary"][0]["missing"] == 1
    assert len(applied["applied"]) == 1
    assert len(applied["missing"]) == 1
    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_clone_carries_the_sets_and_the_layout(client) -> None:
    """Cloning a teaching lab must bring its saved states with it — and its
    positions: geometry is nested under "nodes", and remapping the wrong level
    silently produced an empty layout."""
    lab, nodes = await _lab_with_nodes(client, 2)
    for n in nodes:
        await client.put(f"/api/v1/nodes/{n['id']}/config", json={"content": "x"})
    await client.post(f"/api/v1/labs/{lab['id']}/configsets/blank/capture")
    await client.put(
        f"/api/v1/labs/{lab['id']}/geometry",
        json={
            "data": {
                "nodes": {nodes[0]["id"]: {"x": 111, "y": 222}},
                "view": {"x": 5, "y": 6, "k": 2},
            }
        },
    )

    clone = (await client.post(f"/api/v1/labs/{lab['id']}/clone", json={})).json()
    sets = (await client.get(f"/api/v1/labs/{clone['id']}/configsets")).json()
    geo = (await client.get(f"/api/v1/labs/{clone['id']}/geometry")).json()

    assert "blank" in sets["configsets"]
    assert list(geo["data"]["nodes"].values()) == [{"x": 111, "y": 222}]
    assert geo["data"]["view"] == {"x": 5, "y": 6, "k": 2}
    await client.delete(f"/api/v1/labs/{lab['id']}")
    await client.delete(f"/api/v1/labs/{clone['id']}")
