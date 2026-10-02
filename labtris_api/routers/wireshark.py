from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, WebSocket
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api import wireshark
from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.errors import not_found, unprocessable
from labtris_api.models import Interface, Link, Network
from labtris_api.schemas import WiresharkIn

router = APIRouter(tags=["wireshark"])


async def _resolve(session: AsyncSession, body: WiresharkIn) -> tuple[str, str]:
    """Which host interface a request means, and a stable session key for it.

    A link has two ends and only one can be watched at a time; the a-side is
    the convention, matching which direction `impair_ab` describes."""
    if body.network_id:
        net = await session.get(Network, body.network_id)
        if net is None:
            raise not_found(f"network {body.network_id} not found")
        if not net.host_ifname:
            raise unprocessable(f"network {net.name!r} has no bridge on this host yet")
        return f"net-{net.id}", net.host_ifname
    if body.link_id:
        link = await session.get(Link, body.link_id)
        if link is None:
            raise not_found(f"link {body.link_id} not found")
        iface = await session.get(Interface, link.a_iface_id)
        if iface is None or not iface.host_ifname:
            raise unprocessable("that link has no host interface yet — start both nodes first")
        return f"link-{link.id}", iface.host_ifname
    if body.interface_id:
        iface = await session.get(Interface, body.interface_id)
        if iface is None:
            raise not_found(f"interface {body.interface_id} not found")
        if not iface.host_ifname:
            raise unprocessable("that interface has no host device yet — start the node first")
        return f"iface-{iface.id}", iface.host_ifname
    raise unprocessable("give one of interface_id, link_id or network_id")


@router.post("/wireshark/start")
async def start_wireshark(
    body: WiresharkIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Launch the real Wireshark against a lab interface and hand back a
    session id to open a screen on."""
    key, ifname = await _resolve(session, body)
    sess = await wireshark.start(key, ifname)
    return {"id": sess.id, "ifname": sess.ifname, "display": sess.display}


@router.post("/wireshark/{session_id}/stop")
async def stop_wireshark(
    session_id: str, _user: object = Depends(get_current_user)
) -> dict[str, Any]:
    return {"stopped": await wireshark.stop(session_id)}


@router.get("/wireshark")
async def list_wireshark(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    return {"sessions": wireshark.listing()}


@router.get("/wireshark/{session_id}/files")
async def list_saved_captures(
    session_id: str, _user: object = Depends(get_current_user)
) -> dict[str, Any]:
    """Captures saved inside this session that are ready to download.

    Wireshark runs on the server, so File → Save As writes to the server.
    The browser polls this while the window is open and fetches anything it
    has not seen, which is what makes a save arrive in the user's Downloads
    without them doing anything about it.

    Files still being written are withheld — a pcap handed over mid-save
    downloads truncated and reads as a corrupt capture.
    """
    if wireshark.get(session_id) is None:
        raise not_found(f"no Wireshark session {session_id!r}")
    return {"files": wireshark.saved_files(session_id)}


@router.get("/wireshark/{session_id}/files/{name}")
async def download_saved_capture(
    session_id: str, name: str, _user: object = Depends(get_current_user)
) -> FileResponse:
    if wireshark.get(session_id) is None:
        raise not_found(f"no Wireshark session {session_id!r}")
    path = wireshark.saved_file(session_id, name)
    return FileResponse(
        path,
        media_type="application/vnd.tcpdump.pcap",
        # Content-Disposition is what turns this into a download rather than
        # something the browser tries to render.
        filename=path.name,
    )


@router.websocket("/wireshark/{session_id}/ws")
async def wireshark_ws(websocket: WebSocket, session_id: str) -> None:
    """The Wireshark window itself, over the same Guacamole path the QEMU
    consoles use — one tunnel implementation, not two."""
    from labtris_api.auth import resolve_ws_user
    from labtris_api.db import SessionLocal
    from labtris_api.routers.nodes import guac_tunnel_to

    async with SessionLocal() as _s:
        user = await resolve_ws_user(websocket, _s)
    if user is None:
        await guac_tunnel_to(websocket, None, "sign in to open Wireshark")
        return
    sess = wireshark.get(session_id)
    if sess is None:
        await guac_tunnel_to(websocket, None, f"no Wireshark session {session_id!r}")
        return
    await guac_tunnel_to(
        websocket,
        {"hostname": "127.0.0.1", "port": str(sess.vnc_port), "color-depth": "24"},
        None,
    )
