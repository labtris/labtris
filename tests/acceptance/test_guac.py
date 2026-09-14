from __future__ import annotations

import asyncio
import shutil
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import ulid
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from labtris_api.config import settings
from labtris_api.db import engine as db_engine
from labtris_api.db import get_session
from labtris_api.guac import GUACD_HOST, GUACD_PORT, connect_guacd
from labtris_api.main import create_app
from labtris_api.netd_client import netd
from labtris_api.runtime.qemu import vnc_port


async def _can_db() -> bool:
    try:
        engine = create_async_engine(settings.database_url)
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        await engine.dispose()
        return True
    except Exception:
        return False


async def _guacd_reachable() -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(GUACD_HOST, GUACD_PORT), timeout=3
        )
        writer.close()
        return True
    except Exception:
        return False


async def _cirros_reachable() -> bool:
    from labtris_api.runtime.qemu import _resolve_base_image

    try:
        await asyncio.wait_for(_resolve_base_image("cirros"), timeout=60)
        return True
    except Exception:
        return False


@pytest.fixture
async def client():
    if shutil.which("qemu-system-x86_64") is None:
        pytest.skip("qemu-system-x86_64 is not installed")
    if not await _can_db():
        pytest.skip("Postgres is not available (start with `make dev-db`)")
    if not await netd.ping():
        pytest.skip("netd is not running (start with `make netd`)")
    if not await _guacd_reachable():
        pytest.skip("guacd is not running on 127.0.0.1:4822 (apt install guacd; guacd -f)")
    if not await _cirros_reachable():
        pytest.skip("cirros test image could not be fetched (no network?)")

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
    # The WebSocket routes open their own sessions from labtris_api.db's shared
    # engine rather than the overridden dependency. Each test gets a fresh
    # event loop, so a connection left in that pool would be handed to the
    # next test bound to a loop that no longer exists.
    await db_engine.dispose()


async def post(client: AsyncClient, path: str, body: dict | None = None) -> dict:
    r = await client.post(path, json=body or {})
    assert r.status_code in {200, 201}, r.text
    return r.json()


async def delete(client: AsyncClient, path: str) -> None:
    r = await client.delete(path)
    assert r.status_code in {200, 204}, r.text


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _parse(message: str) -> list[str]:
    """Minimal Guacamole instruction parser for assertions."""
    elements, rest = [], message
    while rest and rest != ";":
        length, _, rest = rest.partition(".")
        elements.append(rest[: int(length)])
        rest = rest[int(length) + 1 :]
    return elements


async def _read_until_sync(read: Callable[[], Awaitable[list[str]]], limit: int = 60) -> list[str]:
    """Collect opcodes until guacd finishes a frame. The first frame is a
    handful of instructions (`size`, `img`, `blob`, `end`, ...) before the
    `sync` that closes it, so a fixed small read count can miss it."""
    opcodes = []
    for _ in range(limit):
        opcodes.append((await asyncio.wait_for(read(), timeout=15))[0])
        if opcodes[-1] in {"sync", "error"}:
            break
    return opcodes


async def test_guacd_vnc_tunnel_gets_live_screen_updates_from_qemu(client: AsyncClient) -> None:
    """Proves the whole Guacamole stack end-to-end: our tunnel does the real
    select/args/connect/ready handshake with guacd, guacd connects out over
    real VNC to QEMU's `-vnc` display, and actual framebuffer instructions
    (not an error) start flowing back — a real remote desktop session, not a
    stub. VNC serves the VGA framebuffer from very early boot, so this
    doesn't need to wait for the OS to finish booting."""
    lab = await post(client, "/api/v1/labs", {"name": f"guac-{ulid.new().str[-8:].lower()}"})
    node = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "vm1", "runtime": "qemu", "image": "cirros", "interfaces": []},
    )
    started = await post(client, f"/api/v1/nodes/{node['id']}/start", {})
    assert started["state"] == "running"

    port = None
    for _ in range(30):
        port = await vnc_port(Path(started["runtime_ref"]))
        if port is not None:
            break
        await asyncio.sleep(0.2)
    assert port is not None, "qemu did not publish a vnc_display in its config"

    sock, connection_id = await connect_guacd(
        "vnc", {"hostname": "127.0.0.1", "port": str(port)}
    )
    assert connection_id
    try:
        opcodes = await _read_until_sync(sock.read)
    finally:
        sock.close()
    # `sync` ends a frame, and it is the instruction guacamole-common-js waits
    # for before it leaves the WAITING state. Anything short of it is a tunnel
    # that renders nothing.
    assert "error" not in opcodes, f"guacd reported an error instead of screen data: {opcodes}"
    assert "sync" in opcodes, f"guacd never completed a frame: {opcodes}"
    assert "img" in opcodes, f"guacd sent no framebuffer data: {opcodes}"

    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_vnc_tunnel_speaks_what_guacamole_common_js_expects(client: AsyncClient) -> None:
    """The browser half, over a real WebSocket rather than the guacd socket.

    guacamole-common-js opens the tunnel with the "guacamole" subprotocol and
    browsers fail the handshake if the server doesn't select it; the client
    then stays in WAITING until a `sync` arrives, and it cannot send anything
    at all — not a sync response, not a keystroke — until the first
    instruction has opened the tunnel. All three are asserted here, because
    each of them fails silently in a browser."""
    import uvicorn
    import websockets

    lab = await post(client, "/api/v1/labs", {"name": f"guacws-{ulid.new().str[-8:].lower()}"})
    node = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "vm1", "runtime": "qemu", "image": "cirros", "interfaces": []},
    )
    await post(client, f"/api/v1/nodes/{node['id']}/start", {})

    port = _free_port()
    config = uvicorn.Config(client._transport.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)
        assert server.started, "test uvicorn did not come up"

        url = f"ws://127.0.0.1:{port}/api/v1/nodes/{node['id']}/vnc/ws?width=800&height=600"
        async with websockets.connect(url, subprotocols=["guacamole"]) as ws:
            assert ws.subprotocol == "guacamole", (
                "the server did not select the subprotocol the browser asked for — "
                "a browser would have failed the handshake outright"
            )

            async def read_one() -> list[str]:
                return _parse(await ws.recv())

            first = await asyncio.wait_for(read_one(), timeout=10)
            assert first[0] == "ready", f"tunnel never opened: first instruction was {first}"

            opcodes = await _read_until_sync(read_one)
            assert "sync" in opcodes, f"browser would still be in WAITING: {opcodes}"

            # Tunnel-level keep-alive: answered here, never forwarded to guacd
            # (which has no handler for it). Without the echo the browser's
            # 15s receive timeout kills an idle desktop session.
            ping = "0.,4.ping,4.1234;"
            await ws.send(ping)
            for _ in range(50):
                echoed = await asyncio.wait_for(ws.recv(), timeout=10)
                if echoed == ping:
                    break
            else:
                raise AssertionError("keep-alive ping was never answered")
    finally:
        server.should_exit = True
        await serving

    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_rdp_tunnel_is_wired_and_reports_failures_instead_of_hanging(
    client: AsyncClient,
) -> None:
    """RDP goes through the same tunnel, to wherever the node's `console`
    config points. Aimed at a closed port it must come back as a Guacamole
    `error` instruction: guacd sends `ready` *before* it tries to connect, so
    a tunnel that swallows the subsequent failure looks exactly like a healthy
    one that has not drawn its first frame yet."""
    import uvicorn
    import websockets

    lab = await post(client, "/api/v1/labs", {"name": f"rdp-{ulid.new().str[-8:].lower()}"})
    node = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "win1", "runtime": "docker", "image": "alpine:3.20", "interfaces": []},
    )

    r = await client.patch(
        f"/api/v1/nodes/{node['id']}/console",
        json={"protocol": "rdp", "hostname": "127.0.0.1", "port": 1, "username": "osboxes"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["console"]["rdp"]["hostname"] == "127.0.0.1"

    port = _free_port()
    config = uvicorn.Config(client._transport.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)

        url = f"ws://127.0.0.1:{port}/api/v1/nodes/{node['id']}/rdp/ws"
        async with websockets.connect(url, subprotocols=["guacamole"]) as ws:
            assert ws.subprotocol == "guacamole"
            opcodes = []
            for _ in range(20):
                try:
                    opcodes.append(_parse(await asyncio.wait_for(ws.recv(), timeout=20))[0])
                except websockets.exceptions.ConnectionClosed:
                    break
                if opcodes[-1] == "error":
                    break
            assert opcodes[0] == "ready", f"tunnel never opened: {opcodes}"
            assert "error" in opcodes, f"a dead RDP target was never reported: {opcodes}"
    finally:
        server.should_exit = True
        await serving

    await delete(client, f"/api/v1/labs/{lab['id']}")


async def test_an_rdp_target_does_not_hijack_the_same_node_s_vnc_console(
    client: AsyncClient,
) -> None:
    """The two consoles share a node but not a destination. A QEMU guest's VNC
    display is on loopback at 5900+N while its RDP listener is somewhere on a
    lab network at 3389, so console settings are stored per protocol — with
    one flat dict, setting `port` for RDP silently pointed VNC at 3389 and the
    screen went away with nothing but "Aborted. See logs." to show for it."""
    lab = await post(client, "/api/v1/labs", {"name": f"both-{ulid.new().str[-8:].lower()}"})
    node = await post(
        client,
        f"/api/v1/labs/{lab['id']}/nodes",
        {"name": "vm1", "runtime": "qemu", "image": "cirros", "interfaces": []},
    )
    started = await post(client, f"/api/v1/nodes/{node['id']}/start", {})

    r = await client.patch(
        f"/api/v1/nodes/{node['id']}/console",
        json={"protocol": "rdp", "hostname": "10.0.0.9", "port": 3389},
    )
    assert r.status_code == 200, r.text
    console = r.json()["console"]
    assert console["rdp"]["port"] == 3389
    assert "vnc" not in console, "an RDP target must not become VNC's target"

    from labtris_api.routers.nodes import _console_settings
    from labtris_api.schemas import NodeOut

    node_row = await get_node_row(client, node["id"])
    vnc = await _console_settings(node_row, "vnc")
    assert vnc["port"] == str(await vnc_port(Path(started["runtime_ref"]))), (
        f"VNC must still dial the display QEMU published, got {vnc}"
    )
    assert vnc["hostname"] == "127.0.0.1"
    assert (await _console_settings(node_row, "rdp"))["hostname"] == "10.0.0.9"
    assert NodeOut.model_validate(node_row).console == console

    await delete(client, f"/api/v1/labs/{lab['id']}")


async def get_node_row(client: AsyncClient, node_id: str):
    """The ORM row, so the settings resolver can be called directly rather
    than inferred from a tunnel's behaviour."""
    from labtris_api.db import SessionLocal
    from labtris_api.lifecycle import get_node

    async with SessionLocal() as session:
        return await get_node(session, node_id)
