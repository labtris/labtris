from __future__ import annotations

from typing import Any

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


@router.get("/links/{link_id}/stats")
async def link_stats(
    link_id: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Live counters + derived bps/pps for both endpoints of a link.

    One netd call fetches both interface counters; `link_stats.record`
    caches them, `link_stats.rate_for` computes deltas against the
    previous cached snapshot. First call after a restart returns
    counters with no rate; second call ~2s later gets rates."""
    from labtris_api import link_stats as ls
    from labtris_api.netd_client import NetdError, netd

    link = await session.get(Link, link_id)
    if link is None:
        raise not_found(f"link {link_id} not found")
    a = await session.get(Interface, link.a_iface_id)
    b = await session.get(Interface, link.b_iface_id)
    names = [i.host_ifname for i in (a, b) if i and i.host_ifname]
    if not names:
        return {"link_id": link_id, "endpoints": {}, "note": "no host interfaces"}
    try:
        got = await netd.call("iface.counters", {"names": names})
    except NetdError as exc:
        return {"link_id": link_id, "error": exc.message}
    counters = got.get("counters", {})
    ls.record(counters)
    return {
        "link_id": link_id,
        "endpoints": {
            "a": _endpoint_view(a, counters, ls),
            "b": _endpoint_view(b, counters, ls),
        },
    }


@router.get("/labs/{lab_id}/link-stats")
async def lab_link_stats(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One-shot: every link's counters + rates for a whole lab.

    The canvas overlay polls this every ~2 s. Batches the netd read
    across all endpoints of every link — a lab with 100 links + 200
    endpoints still costs one netd round-trip per poll, not 200."""
    from sqlalchemy import select as _select

    from labtris_api import link_stats as ls
    from labtris_api.netd_client import NetdError, netd

    links = (
        await session.execute(_select(Link).where(Link.lab_id == lab_id))
    ).scalars().all()
    # Collect every endpoint's host_ifname in one pass.
    ifaces_by_link: dict[str, tuple[Interface | None, Interface | None]] = {}
    all_names: list[str] = []
    for link in links:
        a = await session.get(Interface, link.a_iface_id)
        b = await session.get(Interface, link.b_iface_id)
        ifaces_by_link[link.id] = (a, b)
        for i in (a, b):
            if i and i.host_ifname:
                all_names.append(i.host_ifname)
    if not all_names:
        return {"lab_id": lab_id, "links": {}}
    try:
        got = await netd.call("iface.counters", {"names": all_names})
    except NetdError as exc:
        return {"lab_id": lab_id, "error": exc.message}
    counters = got.get("counters", {})
    ls.record(counters)
    return {
        "lab_id": lab_id,
        "links": {
            link_id: {
                "a": _endpoint_view(a, counters, ls),
                "b": _endpoint_view(b, counters, ls),
            }
            for link_id, (a, b) in ifaces_by_link.items()
        },
    }


def _endpoint_view(
    iface: Interface | None, counters: dict, ls_mod: Any
) -> dict[str, Any]:
    """Shape one endpoint's stats + derived rate for the response."""
    if iface is None or not iface.host_ifname:
        return {"host_ifname": None, "counters": None, "rate": None}
    name = iface.host_ifname
    return {
        "host_ifname": name,
        "iface_id": iface.id,
        "counters": counters.get(name),
        "rate": ls_mod.rate_for(name),
    }
