from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request, conflict, not_found
from labtris_api.lifecycle import (
    get_unlocked_lab,
    new_id,
    realize_link_if_running,
    require_lab_owner,
)
from labtris_api.models import Interface, Link, Network
from labtris_api.netd_client import NetdError, netd
from labtris_api.schemas import LinkCreate, LinkOut

router = APIRouter(tags=["links"])


@router.post("/labs/{lab_id}/links", response_model=LinkOut, status_code=201)
async def create_link(
    lab_id: str,
    body: LinkCreate,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Link:
    await get_unlocked_lab(session, lab_id)
    if body.a_iface_id == body.b_iface_id:
        raise bad_request("a link needs two distinct interfaces")
    a = await session.get(Interface, body.a_iface_id)
    b = await session.get(Interface, body.b_iface_id)
    if a is None or b is None:
        raise not_found("interface not found")
    existing = await session.execute(
        select(Link).where(
            or_(
                Link.a_iface_id.in_([a.id, b.id]),
                Link.b_iface_id.in_([a.id, b.id]),
            )
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise conflict("interface is already linked")
    net = Network(
        id=new_id(),
        lab_id=lab_id,
        # The *tail* of the ULID: its leading characters are the millisecond
        # timestamp, so two links made in the same instant collided on the
        # (lab_id, name) unique constraint.
        name=f"lnk-{new_id()[-8:].lower()}",
        kind="bridge",
    )
    session.add(net)
    await session.flush()
    a.network_id = net.id
    b.network_id = net.id
    link = Link(
        id=new_id(),
        lab_id=lab_id,
        a_iface_id=a.id,
        b_iface_id=b.id,
        network_id=net.id,
    )
    session.add(link)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict("interface is already linked") from exc
    await session.refresh(link)
    await realize_link_if_running(session, link)
    await session.refresh(link)
    return link


@router.delete("/links/{link_id}", status_code=204)
async def delete_link(
    link_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    link = await session.get(Link, link_id)
    if link is None:
        raise not_found(f"link {link_id} not found")
    await require_lab_owner(session, link.lab_id, user)
    await get_unlocked_lab(session, link.lab_id)
    net = await session.get(Network, link.network_id)
    a = await session.get(Interface, link.a_iface_id)
    b = await session.get(Interface, link.b_iface_id)
    if a is not None:
        a.network_id = None
    if b is not None:
        b.network_id = None
    if net is not None:
        if net.host_ifname:
            try:
                await netd.call("bridge.delete", {"name": net.host_ifname})
            except NetdError:
                pass
        await session.delete(net)
    await session.delete(link)
    await session.commit()
    return Response(status_code=204)
