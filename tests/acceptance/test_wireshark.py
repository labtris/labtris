from __future__ import annotations

import shutil

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api import wireshark
from labtris_api.config import settings
from labtris_api.db import engine as db_engine
from labtris_api.db import get_session
from labtris_api.main import create_app
from labtris_api.netd_client import netd


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
    for tool in ("Xvfb", "wireshark", "x11vnc"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed (see docs/reference/install-from-source.mdx)")
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await wireshark.stop_all()
    await engine.dispose()
    await db_engine.dispose()


async def test_wireshark_runs_against_a_segment_and_is_reachable_over_vnc(
    client: AsyncClient,
) -> None:
    """The real binary on a headless X server, exported over VNC on loopback.

    Asserting the processes exist is not enough — an Xvfb with a crashed
    Wireshark on it looks identical from the outside — so this checks the VNC
    port actually accepts a connection, which is what the browser tunnel
    needs."""
    import socket

    r = await client.post("/api/v1/labs", json={"name": f"ws-{ulid.new().str[-8:].lower()}"})
    lab = r.json()
    net = (
        await client.post(
            f"/api/v1/labs/{lab['id']}/networks", json={"name": "seg", "kind": "bridge"}
        )
    ).json()
    try:
        r = await client.post("/api/v1/wireshark/start", json={"network_id": net["id"]})
        assert r.status_code == 200, r.text
        session = r.json()
        assert session["ifname"] == net["host_ifname"], "must capture the segment's own bridge"

        live = wireshark.get(session["id"])
        assert live is not None
        with socket.socket() as probe:
            probe.settimeout(5)
            probe.connect(("127.0.0.1", live.vnc_port))

        listed = (await client.get("/api/v1/wireshark")).json()["sessions"]
        assert session["id"] in [s["id"] for s in listed]

        # Asking twice gives the same session rather than a second X server.
        again = (
            await client.post("/api/v1/wireshark/start", json={"network_id": net["id"]})
        ).json()
        assert again["display"] == session["display"]

        assert (await client.post(f"/api/v1/wireshark/{session['id']}/stop")).json()["stopped"]
        assert wireshark.get(session["id"]) is None
    finally:
        await client.delete(f"/api/v1/labs/{lab['id']}")


async def test_wireshark_needs_a_target_that_actually_exists(client: AsyncClient) -> None:
    r = await client.post("/api/v1/wireshark/start", json={})
    assert r.status_code == 422
    assert "interface_id" in r.json()["error"]["message"]

    r = await client.post("/api/v1/wireshark/start", json={"network_id": "nope"})
    assert r.status_code == 404
