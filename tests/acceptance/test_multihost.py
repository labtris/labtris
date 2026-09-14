from __future__ import annotations

import subprocess

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.main import create_app
from labtris_api.netd_client import NetdError, client_for_endpoint, netd

HOSTB_ENDPOINT = "tcp://10.201.0.2:9601"
HOSTB_TOKEN = "labtris-demo-token-b"  # noqa: S105 - fixed demo token for the local test netns


async def _can_db() -> bool:
    try:
        engine = create_async_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        await engine.dispose()
        return True
    except Exception:
        return False


async def _hostb_reachable() -> bool:
    try:
        client = client_for_endpoint(HOSTB_ENDPOINT, HOSTB_TOKEN)
        return await client.ping()
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


async def post(client: AsyncClient, path: str, body: dict | None = None) -> dict:
    r = await client.post(path, json=body or {})
    assert r.status_code in {200, 201}, r.text
    return r.json()


async def test_hosts_list_bootstraps_local_host(client: AsyncClient) -> None:
    r = await client.get("/api/v1/hosts")
    assert r.status_code == 200, r.text
    hosts = r.json()
    assert any(h["is_local"] and h["reachable"] for h in hosts)


async def test_host_capabilities_reports_what_the_kernel_can_actually_build() -> None:
    """netd probes rather than assumes: it creates and deletes a device of
    each type. This is what lets the API refuse a stretched network up front
    on a kernel built without CONFIG_VXLAN, instead of failing halfway through
    the mesh with an ENOTSUP that reads like a bug in the overlay."""
    caps = await netd.call("host.capabilities")
    links = caps["links"]
    assert links["bridge"]["supported"], "a kernel with no bridge device type cannot run this"
    assert "supported" in links["vxlan"]
    assert caps["tools"]["ip"] is True
    assert await netd.ping(), "probing must not disturb netd"


async def test_vxlan_verb_builds_a_real_tunnel_device_or_says_why_not() -> None:
    """The overlay primitive against the real kernel. Where the vxlan device
    type exists, the verb must produce a device carrying the VNI, remote and
    UDP port it was asked for — checked with `ip -d link`, not by trusting the
    verb's own return value. Where it doesn't (minimal or sandbox kernels
    build in only bridge/veth/tuntap), it must fail cleanly with ENOTSUP and
    leave netd alive; netd used to die on an unrelated encoding error here."""
    caps = await netd.call("host.capabilities")
    supported = caps["links"]["vxlan"]["supported"]
    params = {
        "name": "xacctest1",
        "vni": 4242,
        "remote": "10.201.0.2",
        "local": "10.201.0.1",
        "dstport": 4789,
    }

    if not supported:
        with pytest.raises(NetdError) as exc_info:
            await netd.call("vxlan.create", params)
        assert exc_info.value.code == "ENOTSUP"
        assert await netd.ping(), "netd must still be alive after a failed vxlan.create"
        return

    # A device left behind by an interrupted run would otherwise turn every
    # later run into an EEXIST failure.
    for name in ("xacctest1", "bacctest1"):
        try:
            await netd.call("iface.delete", {"name": name})
        except NetdError:
            pass

    try:
        result = await netd.call("vxlan.create", params)
        assert result["index"] > 0
        shown = subprocess.run(
            ["ip", "-d", "link", "show", "xacctest1"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "vxlan id 4242" in shown, shown
        assert "remote 10.201.0.2" in shown, shown
        assert "dstport 4789" in shown, shown
        assert "state UP" in shown or "UP," in shown, shown

        await netd.call("bridge.create", {"name": "bacctest1"})
        await netd.call("iface.attach", {"name": "xacctest1", "bridge": "bacctest1"})
        assert "master bacctest1" in subprocess.run(
            ["ip", "-d", "link", "show", "xacctest1"], capture_output=True, text=True, check=True
        ).stdout
    finally:
        for name in ("xacctest1", "bacctest1"):
            try:
                await netd.call("iface.delete", {"name": name})
            except NetdError:
                pass
    assert "xacctest1" not in subprocess.run(
        ["ip", "link"], capture_output=True, text=True, check=True
    ).stdout


async def test_register_and_control_a_second_host_over_tcp(client: AsyncClient) -> None:
    if not await _hostb_reachable():
        pytest.skip(
            "simulated second host is not up — run `make netd-hostb` "
            "(see docs/04-scaling.md for the multi-host demo setup)"
        )
    name = f"hostb-{ulid.new().str[-8:].lower()}"
    host = await post(
        client,
        "/api/v1/hosts",
        {
            "name": name,
            "endpoint": HOSTB_ENDPOINT,
            "token": HOSTB_TOKEN,
            "underlay_ip": "10.201.0.2",
        },
    )
    assert host["reachable"] is True
    assert host["is_local"] is False

    listed = (await client.get("/api/v1/hosts")).json()
    assert any(h["id"] == host["id"] for h in listed)

    # Prove the control plane genuinely reaches an isolated remote host: issue
    # a real bridge.create over the authenticated TCP connection and confirm
    # the interface exists in labtris-hostb's namespace but not in ours.
    remote = client_for_endpoint(HOSTB_ENDPOINT, HOSTB_TOKEN)
    try:
        await remote.call("bridge.create", {"name": "btest1234"})
        in_hostb = subprocess.run(
            ["sudo", "ip", "netns", "exec", "labtris-hostb", "ip", "link", "show", "btest1234"],
            capture_output=True,
        )
        in_root = subprocess.run(["ip", "link", "show", "btest1234"], capture_output=True)
        assert in_hostb.returncode == 0, in_hostb.stderr
        assert in_root.returncode != 0, "the bridge must not leak into the root netns"
    finally:
        await remote.call("bridge.delete", {"name": "btest1234"})

    await client.delete(f"/api/v1/hosts/{host['id']}")


def _ip(*args: str, netns: str | None = None) -> str:
    cmd = ["sudo", "ip", "netns", "exec", netns] if netns else []
    return subprocess.run([*cmd, "ip", *args], capture_output=True, text=True).stdout


async def test_vxlan_network_carries_traffic_between_two_hosts(client: AsyncClient) -> None:
    """The overlay's whole point, end to end: one API call builds a bridge and
    a vxlan endpoint on *both* hosts, and a frame put on one bridge comes out
    the other — encapsulated in UDP 4789 over the underlay in between.

    On a kernel without the vxlan device type the same call must be refused up
    front, naming the host, rather than half-building the mesh."""
    if not await _hostb_reachable():
        pytest.skip("simulated second host is not up — run `make netd-hostb`")

    hosts = (await client.get("/api/v1/hosts")).json()
    local = next(h for h in hosts if h["is_local"])
    await client.patch(f"/api/v1/hosts/{local['id']}", json={"underlay_ip": "10.201.0.1"})
    hostb = await post(
        client,
        "/api/v1/hosts",
        {
            "name": f"hostb2-{ulid.new().str[-8:].lower()}",
            "endpoint": HOSTB_ENDPOINT,
            "token": HOSTB_TOKEN,
            "underlay_ip": "10.201.0.2",
        },
    )
    supported = (await client.get(f"/api/v1/hosts/{hostb['id']}/capabilities")).json()["links"][
        "vxlan"
    ]["supported"]

    lab = await post(client, "/api/v1/labs", {"name": f"vxlan-{ulid.new().str[-8:].lower()}"})
    try:
        r = await client.post(
            f"/api/v1/labs/{lab['id']}/networks",
            json={"name": "spanning", "kind": "vxlan", "host_ids": [local["id"], hostb["id"]]},
        )

        if not supported:
            assert r.status_code == 422, r.text
            assert "vxlan" in r.json()["error"]["message"].lower()
            return

        assert r.status_code == 201, r.text
        net = r.json()
        bridge, vni = net["host_ifname"], net["vni"]
        assert bridge and vni

        endpoints = (await client.get(f"/api/v1/networks/{net['id']}/hosts")).json()
        assert {e["host_name"] for e in endpoints} == {local["name"], hostb["name"]}
        for endpoint, netns in zip(
            sorted(endpoints, key=lambda e: e["underlay_ip"]),
            (None, "labtris-hostb"),
            strict=True,
        ):
            shown = _ip("-d", "link", "show", endpoint["vxlan_ifname"], netns=netns)
            where = netns or "the local host"
            assert f"vxlan id {vni}" in shown, f"no vxlan endpoint on {where}: {shown}"
            assert "dstport 4789" in shown, f"wrong encapsulation on {where}: {shown}"
            assert f"master {bridge}" in shown, f"endpoint not on the bridge on {where}: {shown}"

        # Address the two bridges into one subnet and make them talk. Nothing
        # routes between them except the tunnel: they are in different network
        # namespaces, joined only by the vxlan endpoints just built.
        subprocess.run(["sudo", "ip", "addr", "add", "192.0.2.1/24", "dev", bridge], check=False)
        subprocess.run(
            ["sudo", "ip", "netns", "exec", "labtris-hostb", "ip", "addr", "add",
             "192.0.2.2/24", "dev", bridge],
            check=False,
        )
        ping = subprocess.run(
            ["ping", "-c", "3", "-W", "3", "-I", bridge, "192.0.2.2"],
            capture_output=True,
            text=True,
        )
        assert ping.returncode == 0, f"nothing crossed the overlay:\n{ping.stdout}{ping.stderr}"

        remote_ifname = next(
            e["vxlan_ifname"] for e in endpoints if e["host_name"] != local["name"]
        )
        r = await client.delete(f"/api/v1/networks/{net['id']}")
        assert r.status_code in {200, 204}, r.text
        assert not _ip("link", "show", remote_ifname, netns="labtris-hostb"), (
            "tearing the network down must remove the far-side vxlan device too"
        )
    finally:
        await client.delete(f"/api/v1/labs/{lab['id']}")
        await client.delete(f"/api/v1/hosts/{hostb['id']}")


async def test_deleting_a_lab_tears_the_mesh_off_every_host(client: AsyncClient) -> None:
    """Deleting the lab, not the network, is the path that used to leak: it
    dropped the local bridge and left every remote host's bridge and vxlan
    endpoint behind forever, along with their interface-name reservations.
    Nothing ever revisits them, so they accumulate on the far host until
    someone notices by hand."""
    if not await _hostb_reachable():
        pytest.skip("simulated second host is not up — run `make netd-hostb`")

    hosts = (await client.get("/api/v1/hosts")).json()
    local = next(h for h in hosts if h["is_local"])
    await client.patch(f"/api/v1/hosts/{local['id']}", json={"underlay_ip": "10.201.0.1"})
    hostb = await post(
        client,
        "/api/v1/hosts",
        {
            "name": f"hostb3-{ulid.new().str[-8:].lower()}",
            "endpoint": HOSTB_ENDPOINT,
            "token": HOSTB_TOKEN,
            "underlay_ip": "10.201.0.2",
        },
    )
    caps = (await client.get(f"/api/v1/hosts/{hostb['id']}/capabilities")).json()
    if not caps["links"]["vxlan"]["supported"]:
        pytest.skip("this kernel has no vxlan device type")

    lab = await post(client, "/api/v1/labs", {"name": f"vxdel-{ulid.new().str[-8:].lower()}"})
    try:
        net = await post(
            client,
            f"/api/v1/labs/{lab['id']}/networks",
            {"name": "spanning", "kind": "vxlan", "host_ids": [local["id"], hostb["id"]]},
        )
        endpoints = (await client.get(f"/api/v1/networks/{net['id']}/hosts")).json()
        remote = next(e for e in endpoints if e["host_name"] == hostb["name"])
        assert _ip("link", "show", remote["vxlan_ifname"], netns="labtris-hostb")

        r = await client.delete(f"/api/v1/labs/{lab['id']}")
        assert r.status_code in {200, 204}, r.text

        assert not _ip("link", "show", remote["vxlan_ifname"], netns="labtris-hostb"), (
            "the far host kept its vxlan endpoint after the lab was deleted"
        )
        assert not _ip("link", "show", net["host_ifname"], netns="labtris-hostb"), (
            "the far host kept its bridge after the lab was deleted"
        )
    finally:
        await client.delete(f"/api/v1/labs/{lab['id']}")
        await client.delete(f"/api/v1/hosts/{hostb['id']}")
