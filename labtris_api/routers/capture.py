from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.errors import not_found
from labtris_api.models import Interface, Link
from labtris_api.netd_client import netd
from labtris_api.schemas import CaptureIn

router = APIRouter(tags=["capture"])


@router.post("/interfaces/{iface_id}/capture/start")
async def start_iface_capture(
    iface_id: str,
    body: CaptureIn | None = None,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, str]:
    iface = await session.get(Interface, iface_id)
    if iface is None or not iface.host_ifname:
        raise not_found("interface is not realized on the host yet — start the node")
    bpf = body.bpf if body else ""
    await netd.call("capture.start", {"name": iface.host_ifname, "bpf": bpf})
    return {"status": "capturing", "target": iface.host_ifname}


@router.post("/interfaces/{iface_id}/capture/stop")
async def stop_iface_capture(
    iface_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, str]:
    iface = await session.get(Interface, iface_id)
    if iface is None or not iface.host_ifname:
        raise not_found("interface not found")
    await netd.call("capture.stop", {"name": iface.host_ifname})
    return {"status": "stopped"}


@router.get("/interfaces/{iface_id}/capture")
async def read_iface_capture(
    iface_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, list[str]]:
    iface = await session.get(Interface, iface_id)
    if iface is None or not iface.host_ifname:
        raise not_found("interface not found")
    return await netd.call("capture.read", {"name": iface.host_ifname})


@router.post("/links/{link_id}/capture/start")
async def start_link_capture(
    link_id: str,
    body: CaptureIn | None = None,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, str]:
    link = await session.get(Link, link_id)
    if link is None:
        raise not_found("link not found")
    a = await session.get(Interface, link.a_iface_id)
    b = await session.get(Interface, link.b_iface_id)
    bpf = body.bpf if body else ""
    started = 0
    for iface in (a, b):
        if iface and iface.host_ifname:
            await netd.call("capture.start", {"name": iface.host_ifname, "bpf": bpf})
            started += 1
    if not started:
        raise not_found("link is not realized — start both nodes")
    return {"status": "capturing"}


@router.post("/links/{link_id}/capture/stop")
async def stop_link_capture(
    link_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, str]:
    link = await session.get(Link, link_id)
    if link is None:
        raise not_found("link not found")
    for iid in (link.a_iface_id, link.b_iface_id):
        iface = await session.get(Interface, iid)
        if iface and iface.host_ifname:
            await netd.call("capture.stop", {"name": iface.host_ifname})
    return {"status": "stopped"}


@router.get("/links/{link_id}/capture")
async def read_link_capture(
    link_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, list[str]]:
    link = await session.get(Link, link_id)
    if link is None:
        raise not_found("link not found")
    lines: list[str] = []
    for iid in (link.a_iface_id, link.b_iface_id):
        iface = await session.get(Interface, iid)
        if iface and iface.host_ifname:
            got = await netd.call("capture.read", {"name": iface.host_ifname})
            tag = "A" if iid == link.a_iface_id else "B"
            lines.extend(f"{tag} {ln}" for ln in got.get("lines") or [])
    return {"lines": lines}
