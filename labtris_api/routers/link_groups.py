"""LinkGroup HTTP surface — create/list/delete parallel Link bundles.

A LinkGroup is metadata over N normal Links that share a node pair.
Real DC fabrics dual-home hosts, spine-multi-home leaves, and give
leaves 4×100G aggregate uplinks to a single spine — none of which
the old two-endpoint-with-UNIQUE Link schema allowed. Now they do.

Runtime today: nothing special. Each member Link is still a normal
p2p bridge; the guest's control plane (usually FRR + `bgp bestpath
as-path multipath-relax`) does the actual ECMP across the parallel
uplinks. Future work: install a Linux bond on the endpoint netns for
kernel-side ECMP without a routing daemon.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request, not_found
from labtris_api.lifecycle import (
    allocate_mac,
    get_lab,
    get_unlocked_lab,
    iface_scheme_for,
    new_id,
    require_lab_owner,
)
from labtris_api.models import Interface, Link, LinkGroup, Network, Node
from labtris_api.naming import guest_iface_name

router = APIRouter(tags=["link-groups"])


class LinkGroupIn(BaseModel):
    name: str = ""
    a_node_id: str
    b_node_id: str
    count: int = Field(2, ge=2, le=32)
    hash_policy: str = "layer3+4"


class LinkGroupOut(BaseModel):
    id: str
    lab_id: str
    name: str
    hash_policy: str
    a_node_id: str
    b_node_id: str
    link_ids: list[str]


@router.post("/labs/{lab_id}/link-groups", response_model=LinkGroupOut, status_code=201)
async def create_group(
    lab_id: str,
    body: LinkGroupIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> LinkGroupOut:
    """Create a group of N parallel Links between two nodes.

    Materialises `count` fresh interface pairs (one per side) + N
    `lnk-*` bridge networks + N Link rows all pointing at the new
    group_id. Same shape as N calls to POST /links against a lab
    where the schema still forbade it."""
    lab = await get_lab(session, lab_id)
    await require_lab_owner(session, lab_id, user)
    await get_unlocked_lab(session, lab_id)

    a_node = await session.get(Node, body.a_node_id)
    b_node = await session.get(Node, body.b_node_id)
    if a_node is None or b_node is None:
        raise not_found("one of the endpoint nodes")
    if a_node.lab_id != lab.id or b_node.lab_id != lab.id:
        raise bad_request("both endpoint nodes must be in this lab")
    if a_node.id == b_node.id:
        raise bad_request("group endpoints must differ")

    group = LinkGroup(
        id=new_id(), lab_id=lab.id, name=body.name, hash_policy=body.hash_policy,
    )
    session.add(group)
    await session.flush()

    link_ids: list[str] = []
    for _ in range(body.count):
        a_iface = await _new_iface(session, a_node)
        b_iface = await _new_iface(session, b_node)
        net = Network(
            id=new_id(),
            lab_id=lab.id,
            name=f"lnk-{new_id()[-8:].lower()}",
            kind="bridge",
        )
        session.add(net)
        await session.flush()
        a_iface.network_id = net.id
        b_iface.network_id = net.id
        link = Link(
            id=new_id(),
            lab_id=lab.id,
            a_iface_id=a_iface.id,
            b_iface_id=b_iface.id,
            network_id=net.id,
            group_id=group.id,
            admin_up=True,
        )
        session.add(link)
        link_ids.append(link.id)

    await session.commit()
    return LinkGroupOut(
        id=group.id,
        lab_id=lab.id,
        name=group.name,
        hash_policy=group.hash_policy,
        a_node_id=body.a_node_id,
        b_node_id=body.b_node_id,
        link_ids=link_ids,
    )


@router.get("/labs/{lab_id}/link-groups", response_model=list[LinkGroupOut])
async def list_groups(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> list[LinkGroupOut]:
    lab = await get_lab(session, lab_id)
    groups = (
        await session.execute(select(LinkGroup).where(LinkGroup.lab_id == lab.id))
    ).scalars().all()
    out: list[LinkGroupOut] = []
    for g in groups:
        member_links = (
            await session.execute(select(Link).where(Link.group_id == g.id))
        ).scalars().all()
        if not member_links:
            continue
        a_node_id, b_node_id = await _endpoint_nodes(session, member_links[0])
        out.append(LinkGroupOut(
            id=g.id, lab_id=g.lab_id, name=g.name, hash_policy=g.hash_policy,
            a_node_id=a_node_id, b_node_id=b_node_id,
            link_ids=[l.id for l in member_links],
        ))
    return out


@router.delete("/link-groups/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    group = await session.get(LinkGroup, group_id)
    if group is None:
        raise not_found(f"link group {group_id}")
    await require_lab_owner(session, group.lab_id, user)
    await get_unlocked_lab(session, group.lab_id)
    # Group delete cascades to member Links (ondelete=CASCADE on the
    # FK); each Link's ondelete=CASCADE on the interface FKs cleans
    # up the interface rows too.
    await session.delete(group)
    await session.commit()
    return Response(status_code=204)


async def _new_iface(session: AsyncSession, node: Node) -> Interface:
    """Allocate the next interface on `node` — same shape as the
    topology_gen helper but standalone."""
    from sqlalchemy import func as _func

    idx = (
        await session.execute(
            select(_func.count(Interface.id)).where(Interface.node_id == node.id)
        )
    ).scalar_one()
    scheme = iface_scheme_for(node.runtime, node.image)
    iface_id = new_id()
    iface = Interface(
        id=iface_id,
        node_id=node.id,
        idx=int(idx),
        name=guest_iface_name(scheme, int(idx)),
        mac=await allocate_mac(session, iface_id),
    )
    session.add(iface)
    await session.flush()
    return iface


async def _endpoint_nodes(session: AsyncSession, link: Link) -> tuple[str, str]:
    a = await session.get(Interface, link.a_iface_id)
    b = await session.get(Interface, link.b_iface_id)
    return (a.node_id if a else "", b.node_id if b else "")
