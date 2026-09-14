from __future__ import annotations

import asyncio
import subprocess

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings
from labtris_api.db import engine as db_engine
from labtris_api.db import get_session
from labtris_api.main import create_app
from labtris_api.netd_client import netd

PROBE_NIC = "pnltestnic0"
PROBE_NIC2 = "pnltestnic1"


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
    if not await netd.ping():
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
    await db_engine.dispose()


@pytest.fixture
def probe_nic():
    """A dummy NIC to enslave. Emphatically not a real one: binding a cloud to
    the interface carrying the default route takes the host off the network,
    which in CI means taking the test runner off the network."""
    for nic in (PROBE_NIC, PROBE_NIC2):
        subprocess.run(["sudo", "ip", "link", "add", nic, "type", "dummy"], check=False)
        subprocess.run(["sudo", "ip", "link", "set", nic, "up"], check=False)
    yield PROBE_NIC
    for nic in (PROBE_NIC, PROBE_NIC2):
        subprocess.run(["sudo", "ip", "link", "del", nic], check=False)


async def post(client: AsyncClient, path: str, body: dict | None = None) -> dict:
    r = await client.post(path, json=body or {})
    assert r.status_code in {200, 201}, r.text
    return r.json()


def _master_of(name: str) -> str | None:
    out = subprocess.run(
        ["ip", "-d", "link", "show", name], capture_output=True, text=True
    ).stdout
    parts = out.split()
    return parts[parts.index("master") + 1] if "master" in parts else None


async def test_a_bridge_network_joins_interfaces_to_one_segment(client: AsyncClient) -> None:
    """An internal bridge is the thing a point-to-point link cannot be: more
    than two nodes on one L2 segment. It also has to be joinable after the
    fact — interfaces could previously only pick a network at creation."""
    lab = await post(client, "/api/v1/labs", {"name": f"br-{ulid.new().str[-8:].lower()}"})
    net = await post(
        client, f"/api/v1/labs/{lab['id']}/networks", {"name": "seg", "kind": "bridge"}
    )
    assert net["host_ifname"], "a bridge network should exist on the host immediately"
    assert _master_of(net["host_ifname"]) is None
    assert (
        subprocess.run(["ip", "link", "show", net["host_ifname"]], capture_output=True).returncode
        == 0
    ), "the bridge named in the API response must actually exist"

    joined = []
    for i in range(3):
        node = await post(
            client,
            f"/api/v1/labs/{lab['id']}/nodes",
            {"name": f"n{i}", "runtime": "docker", "image": "alpine:3.20"},
        )
        iface = node["interfaces"][0]
        r = await client.patch(f"/api/v1/interfaces/{iface['id']}", json={"network_id": net["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["network_id"] == net["id"]
        joined.append(iface["id"])

    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()
    on_segment = [
        i for n in detail["nodes"] for i in n["interfaces"] if i["network_id"] == net["id"]
    ]
    assert len(on_segment) == 3, "all three nodes should share the one segment"

    r = await client.delete(f"/api/v1/labs/{lab['id']}")
    assert r.status_code in {200, 204}


async def test_a_cloud_binds_a_host_nic_and_gives_it_back(
    client: AsyncClient, probe_nic: str
) -> None:
    """A cloud is a lab bridge with one of the host's own NICs enslaved to it,
    which is how a lab reaches anything outside itself. Deleting it has to
    return the interface, or the host quietly loses a NIC per lab."""
    lab = await post(client, "/api/v1/labs", {"name": f"cl-{ulid.new().str[-8:].lower()}"})
    net = await post(
        client,
        f"/api/v1/labs/{lab['id']}/networks",
        {"name": "uplink", "kind": "cloud", "cloud_ref": probe_nic},
    )
    assert net["kind"] == "cloud"
    assert _master_of(probe_nic) == net["host_ifname"], "the host NIC should be on the lab bridge"

    r = await client.delete(f"/api/v1/networks/{net['id']}")
    assert r.status_code in {200, 204}, r.text
    assert _master_of(probe_nic) is None, "deleting the cloud must release the host's NIC"

    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_a_cloud_refuses_the_hosts_default_route_nic(client: AsyncClient) -> None:
    """The one that takes the machine off the network. Enslaving a NIC moves
    its traffic to the bridge and the host's address does not follow, so this
    is refused unless the caller says explicitly that it meant it."""
    caps = await netd.call("host.interfaces")
    default = next((i for i in caps["interfaces"] if i["default_route"]), None)
    if default is None:
        pytest.skip("this host has no default route to protect")

    lab = await post(client, "/api/v1/labs", {"name": f"dflt-{ulid.new().str[-8:].lower()}"})
    r = await client.post(
        f"/api/v1/labs/{lab['id']}/networks",
        json={"name": "danger", "kind": "cloud", "cloud_ref": default["name"]},
    )
    assert r.status_code == 422, r.text
    assert "default route" in r.json()["error"]["message"]
    assert _master_of(default["name"]) is None, "the refusal must not have bound it anyway"

    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()
    assert detail["networks"] == [], "a refused network must not be left behind"

    await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_several_clouds_coexist_but_one_nic_backs_only_one(
    client: AsyncClient, probe_nic: str
) -> None:
    """Several uplinks are fine — one cloud per NIC, each its own bridge. What
    is not fine is two clouds naming the same NIC: an interface can only be
    enslaved to one bridge, so the second bind *moves* it, and the first cloud
    is left with a bridge that still looks connected and carries nothing."""
    lab = await post(client, "/api/v1/labs", {"name": f"multi-{ulid.new().str[-8:].lower()}"})
    first = await post(
        client,
        f"/api/v1/labs/{lab['id']}/networks",
        {"name": "up1", "kind": "cloud", "cloud_ref": PROBE_NIC},
    )
    second = await post(
        client,
        f"/api/v1/labs/{lab['id']}/networks",
        {"name": "up2", "kind": "cloud", "cloud_ref": PROBE_NIC2},
    )
    assert first["host_ifname"] != second["host_ifname"], "each cloud gets its own bridge"
    assert _master_of(PROBE_NIC) == first["host_ifname"]
    assert _master_of(PROBE_NIC2) == second["host_ifname"]

    r = await client.post(
        f"/api/v1/labs/{lab['id']}/networks",
        json={"name": "steal", "kind": "cloud", "cloud_ref": PROBE_NIC},
    )
    assert r.status_code == 409, r.text
    assert "up1" in r.json()["error"]["message"]
    assert _master_of(PROBE_NIC) == first["host_ifname"], "the refused cloud must not have moved it"

    detail = (await client.get(f"/api/v1/labs/{lab['id']}")).json()
    clouds = [n["name"] for n in detail["networks"] if n["kind"] == "cloud"]
    assert sorted(clouds) == ["up1", "up2"], f"a refused cloud must leave nothing behind: {clouds}"

    await client.delete(f"/api/v1/labs/{lab['id']}")
    assert _master_of(PROBE_NIC) is None
    assert _master_of(PROBE_NIC2) is None


async def test_a_bridge_can_be_captured_like_a_tap(client: AsyncClient) -> None:
    """A segment is capturable in its own right, not only through one node's
    port. A bridge sees every frame its ports flood, which is the closest thing
    to a mirror port — and it works on an empty bridge, which is how you prove
    that nothing is arriving at all."""
    lab = await post(client, "/api/v1/labs", {"name": f"cap-{ulid.new().str[-8:].lower()}"})
    net = await post(
        client, f"/api/v1/labs/{lab['id']}/networks", {"name": "seg", "kind": "bridge"}
    )
    bridge = net["host_ifname"]
    try:
        r = await client.post(f"/api/v1/networks/{net['id']}/capture/start", json={"bpf": ""})
        assert r.status_code == 200, r.text
        assert r.json()["started"] == bridge, "should attach to the segment's own bridge"

        # A port on the segment, so there is something to flood between.
        subprocess.run(["sudo", "ip", "netns", "add", "labcapns"], check=False)
        subprocess.run(["sudo", "ip", "link", "add", "labcapA", "type", "veth",
                        "peer", "name", "labcapB"], check=False)
        subprocess.run(["sudo", "ip", "link", "set", "labcapA", "master", bridge], check=False)
        subprocess.run(["sudo", "ip", "link", "set", "labcapA", "up"], check=False)
        subprocess.run(["sudo", "ip", "link", "set", "labcapB", "netns", "labcapns"], check=False)
        subprocess.run(["sudo", "ip", "netns", "exec", "labcapns", "ip", "addr", "add",
                        "198.51.100.2/24", "dev", "labcapB"], check=False)
        subprocess.run(["sudo", "ip", "netns", "exec", "labcapns", "ip", "link", "set",
                        "labcapB", "up"], check=False)
        subprocess.run(["sudo", "ip", "addr", "add", "198.51.100.1/24", "dev", bridge],
                       check=False)
        subprocess.run(["sudo", "ip", "link", "set", bridge, "up"], check=False)
        await asyncio.sleep(1)
        subprocess.run(["sudo", "ip", "netns", "exec", "labcapns", "ping", "-c", "3",
                        "-W", "1", "198.51.100.1"], capture_output=True, check=False)
        await asyncio.sleep(2)

        lines = (await client.get(f"/api/v1/networks/{net['id']}/capture")).json()["lines"]
        packets = [ln for ln in lines if "tcpdump" not in ln and "listening on" not in ln]
        assert packets, f"nothing captured crossing the segment: {lines}"
        assert any("ICMP echo request" in p or "ARP" in p for p in packets), packets
    finally:
        await client.post(f"/api/v1/networks/{net['id']}/capture/stop")
        subprocess.run(["sudo", "ip", "netns", "del", "labcapns"], check=False)
        subprocess.run(["sudo", "ip", "link", "del", "labcapA"], check=False)
        await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_capturing_a_network_with_no_bridge_says_why(client: AsyncClient) -> None:
    lab = await post(client, "/api/v1/labs", {"name": f"nobr-{ulid.new().str[-8:].lower()}"})
    r = await client.post("/api/v1/networks/nonexistent/capture/start", json={"bpf": ""})
    assert r.status_code == 404
    await client.delete(f"/api/v1/labs/{lab['id']}")
