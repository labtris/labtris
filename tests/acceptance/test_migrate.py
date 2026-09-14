from __future__ import annotations

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings
from labtris_api.db import engine as db_engine
from labtris_api.db import get_session
from labtris_api.main import create_app
from labtris_api.migrate import detect, parse_clab, parse_unl

#: A topology in the shape real ones come in: defaults and kinds inheritance,
#: a vendor image we cannot run, a bridge, and an endpoint on the host.
CLAB = """
name: spine-leaf
topology:
  defaults:
    kind: linux
  kinds:
    linux:
      image: alpine:3.20
    nokia_srlinux:
      image: ghcr.io/nokia/srlinux:23.10
  nodes:
    spine1:
      kind: nokia_srlinux
    leaf1: {}
    leaf2:
      image: frrouting/frr:v8.4.0
    br0:
      kind: bridge
  links:
    - endpoints: ["spine1:e1-1", "leaf1:eth1"]
    - endpoints: ["spine1:e1-2", "leaf2:eth1"]
    - endpoints: ["leaf1:eth2", "br0:port1"]
    - endpoints: ["leaf2:eth2", "host:eth0"]
"""

UNL = """<?xml version="1.0" encoding="UTF-8"?>
<lab name="eve-lab">
  <topology>
    <nodes>
      <node id="1" name="ubu1" template="ubuntu" left="100" top="80">
        <interface id="0" name="eth0" network_id="1"/>
      </node>
      <node id="2" name="rtr1" template="csr1000v" left="300" top="80">
        <interface id="0" name="Gi0/0" network_id="1"/>
      </node>
      <node id="3" name="deb1" template="debian" left="500" top="80">
        <interface id="0" name="eth0"/>
      </node>
    </nodes>
  </topology>
</lab>
"""


async def _can_db() -> bool:
    try:
        engine = create_async_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture
async def client():
    if not await _can_db():
        pytest.skip("Postgres is not available (start with `make dev-db`)")
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def override_session():
        async with Session() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await engine.dispose()
    await db_engine.dispose()


def test_format_is_detected_from_content_not_filename() -> None:
    """A topology mailed around loses its extension long before its contents."""
    assert detect(CLAB, "whatever") == "clab"
    assert detect(UNL, "whatever") == "unl"
    assert detect('{"format":"labtris-lab-v1"}', "x") == "native"
    with pytest.raises(ValueError):
        detect("just some prose", "notes.txt")


def test_clab_inherits_kind_and_image_the_way_containerlab_does() -> None:
    plan = parse_clab(CLAB)
    by_name = {n.name: n for n in plan.nodes}
    # leaf1 declares nothing: kind from defaults, image from kinds.linux
    assert by_name["leaf1"].image == "alpine:3.20"
    # leaf2 overrides the image its kind would have given it
    assert by_name["leaf2"].image == "frrouting/frr:v8.4.0"


def test_clab_keeps_what_it_can_and_says_what_it_could_not() -> None:
    """A topology arriving with most of its nodes beats a rejection — but only
    if it is explicit about the difference."""
    plan = parse_clab(CLAB)
    by_name = {n.name: n for n in plan.nodes}

    assert by_name["spine1"].image == "alpine:3.20", "a gated vendor image becomes a placeholder"
    assert by_name["spine1"].env["LABTRIS_ORIGINAL_KIND"] == "nokia_srlinux"
    assert by_name["spine1"].env["LABTRIS_ORIGINAL_IMAGE"] == "ghcr.io/nokia/srlinux:23.10"
    assert any("registry-gated" in w for w in plan.warnings), plan.warnings

    assert "br0" not in by_name, "a clab bridge is a segment, not a node"
    assert "br0" in plan.networks

    # Only the two node-to-node links; the bridge and host endpoints are noted.
    assert len(plan.links) == 2
    assert any("br0" in w for w in plan.warnings)
    assert any("cloud network" in w for w in plan.warnings), plan.warnings

    # Interfaces named by the links exist on the nodes.
    assert "e1-1" in by_name["spine1"].ifaces and "e1-2" in by_name["spine1"].ifaces


def test_unl_now_maps_onto_the_qemu_catalog() -> None:
    """This is the regression that mattered: the importer predated the QEMU
    backend and turned every one of these into an Alpine placeholder."""
    plan = parse_unl(UNL)
    by_name = {n.name: n for n in plan.nodes}
    assert by_name["ubu1"].runtime == "qemu"
    assert by_name["ubu1"].image == "ubuntu-24.04"
    assert by_name["deb1"].runtime == "qemu"
    assert by_name["deb1"].image == "debian-12"
    # A licensed appliance still cannot run, but the warning says why.
    assert by_name["rtr1"].image == "alpine:3.20"
    assert any("vendor appliance" in w for w in plan.warnings), plan.warnings
    assert by_name["ubu1"].position == (100, 80), "canvas positions should survive"
    assert len(plan.links) == 1, "two interfaces sharing a network_id are one link"


async def test_importing_a_containerlab_file_builds_a_real_lab(client: AsyncClient) -> None:
    name = f"clab-{ulid.new().str[-8:].lower()}"
    r = await client.post(
        "/api/v1/labs/import/topology",
        json={"content": CLAB, "filename": "spine-leaf.clab.yml", "name": name},
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["source"] == "clab"
    assert out["imported"]["nodes"] == 3
    assert out["imported"]["links"] == 2
    assert out["warnings"], "it must say what it could not carry over"

    lab = out["lab"]
    try:
        assert {n["name"] for n in lab["nodes"]} == {"spine1", "leaf1", "leaf2"}
        assert len(lab["links"]) == 2
        assert "br0" in [n["name"] for n in lab["networks"]]
        geo = (await client.get(f"/api/v1/labs/{lab['id']}/geometry")).json()["data"]
        assert len(geo["nodes"]) == 3, "nodes should be placed, not stacked at the origin"
    finally:
        await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_importing_a_unl_builds_a_real_lab(client: AsyncClient) -> None:
    name = f"unl-{ulid.new().str[-8:].lower()}"
    r = await client.post(
        "/api/v1/labs/import/topology", json={"content": UNL, "filename": "x.unl", "name": name}
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["source"] == "unl"
    lab = out["lab"]
    try:
        runtimes = {n["name"]: n["runtime"] for n in lab["nodes"]}
        assert runtimes["ubu1"] == "qemu"
        assert len(lab["links"]) == 1
    finally:
        await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_file_we_cannot_parse_is_refused_clearly(client: AsyncClient) -> None:
    r = await client.post("/api/v1/labs/import/topology", json={"content": "hello there"})
    assert r.status_code == 400
    assert "cannot tell" in r.json()["error"]["message"]

    r = await client.post(
        "/api/v1/labs/import/topology",
        json={"content": "name: x\nsomething: else\n", "filename": "a.yml"},
    )
    assert r.status_code == 400
    assert "topology.nodes" in r.json()["error"]["message"]
