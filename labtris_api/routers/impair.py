from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.errors import not_found, runtime_error
from labtris_api.impair import PRESETS, apply_link_qos
from labtris_api.models import Interface, Link
from labtris_api.netd_client import NetdError
from labtris_api.schemas import LinkOut, LinkPatch

router = APIRouter(tags=["links"])


@router.get("/impair/presets")
async def presets(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    return {"presets": PRESETS}


@router.patch("/links/{link_id}", response_model=LinkOut)
async def patch_link(
    link_id: str,
    body: LinkPatch,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Link:
    link = await session.get(Link, link_id)
    if link is None:
        raise not_found(f"link {link_id} not found")
    data = body.model_dump(exclude_unset=True)
    if "preset" in data and data["preset"]:
        spec = PRESETS.get(data["preset"], {})
        link.impair_ab = spec
        link.impair_ba = spec
    if "impair_ab" in data and data["impair_ab"] is not None:
        link.impair_ab = data["impair_ab"]
    if "impair_ba" in data and data["impair_ba"] is not None:
        link.impair_ba = data["impair_ba"]
    if "admin_up" in data and data["admin_up"] is not None:
        link.admin_up = data["admin_up"]
    await session.commit()
    await session.refresh(link)
    a = await session.get(Interface, link.a_iface_id)
    b = await session.get(Interface, link.b_iface_id)
    try:
        await apply_link_qos(link, a, b, session=session)
    except NetdError as exc:
        raise runtime_error(exc.message) from exc
    return link
