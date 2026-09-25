"""RESTCONF router — mounts at `/restconf` in main.py.

URL scheme:

    /restconf/                                              -- root, per RFC 8040 §3.1
    /restconf/data/labtris:nodes                            -- list of nodes
    /restconf/data/labtris:nodes/node={name}                -- one node
    /restconf/data/labtris:nodes/node={name}/ietf-interfaces:interfaces
    /restconf/data/labtris:nodes/node={name}/ietf-interfaces:interfaces-state
    /restconf/data/labtris:nodes/node={name}/ietf-interfaces:interfaces/interface={ifname}
    /restconf/data/labtris:nodes/node={name}/ietf-interfaces:interfaces-state/interface={ifname}

PUT /interfaces/interface={ifname}/enabled  -- toggle admin state
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.models import Node
from labtris_api.restconf.serialize import YANG_DATA_JSON, envelope, errors
from labtris_api.restconf.yang import ietf_interfaces as ifmod

router = APIRouter(tags=["restconf"])


def _json(payload: dict[str, Any], status: int = 200) -> Response:
    """RFC 8040 responses use application/yang-data+json — a strict
    RESTCONF client checks the media type. FastAPI's default JSONResponse
    sends application/json, which some clients reject as non-conformant."""
    return Response(
        content=_dump(payload),
        status_code=status,
        media_type=YANG_DATA_JSON,
    )


def _dump(payload: Any) -> str:
    import json

    return json.dumps(payload, indent=2, default=str)


def _err(status: int, tag: str, message: str, *, path: str | None = None) -> Response:
    return _json(errors(tag, message, path=path), status=status)


@router.get("/restconf/")
async def root() -> Response:
    """RFC 8040 §3.1 API root — advertise the datastore subtree."""
    return _json(
        {
            "ietf-restconf:restconf": {
                "data": {},
                "operations": {},
                "yang-library-version": "2019-01-04",
            }
        }
    )


@router.get("/restconf/data/labtris:nodes")
async def list_nodes(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    rows = (
        await session.execute(select(Node.name, Node.runtime, Node.state).order_by(Node.name))
    ).all()
    return _json(
        envelope(
            "labtris",
            "nodes",
            {"node": [{"name": n, "runtime": r, "state": s} for n, r, s in rows]},
        )
    )


@router.get("/restconf/data/labtris:nodes/node={node_name}")
async def get_node(
    node_name: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    row = (
        await session.execute(
            select(Node.name, Node.runtime, Node.state).where(Node.name == node_name)
        )
    ).one_or_none()
    if row is None:
        return _err(
            404,
            "invalid-value",
            f"no such node {node_name!r}",
            path=f"/labtris:nodes/node={node_name}",
        )
    name, runtime, state = row
    return _json(envelope("labtris", "node", {"name": name, "runtime": runtime, "state": state}))


@router.get("/restconf/data/labtris:nodes/node={node_name}/ietf-interfaces:interfaces")
async def get_interfaces_config(
    node_name: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    result = await ifmod.get_interfaces_config(session, node_name)
    if result is None:
        return _err(404, "invalid-value", f"no such node {node_name!r}")
    return _json(envelope(ifmod.MODULE, "interfaces", result))


@router.get(
    "/restconf/data/labtris:nodes/node={node_name}"
    "/ietf-interfaces:interfaces/interface={iface_name}"
)
async def get_interface_config(
    node_name: str,
    iface_name: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    result = await ifmod.get_interface_config(session, node_name, iface_name)
    if result is None:
        return _err(
            404,
            "invalid-value",
            f"no such interface {iface_name!r} on {node_name!r}",
        )
    return _json(envelope(ifmod.MODULE, "interfaces", result))


@router.get("/restconf/data/labtris:nodes/node={node_name}/ietf-interfaces:interfaces-state")
async def get_interfaces_state(
    node_name: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    result = await ifmod.get_interfaces_state(session, node_name)
    if result is None:
        return _err(404, "invalid-value", f"no such node {node_name!r}")
    return _json(envelope(ifmod.MODULE, "interfaces-state", result))


@router.get(
    "/restconf/data/labtris:nodes/node={node_name}"
    "/ietf-interfaces:interfaces-state/interface={iface_name}"
)
async def get_interface_state(
    node_name: str,
    iface_name: str,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    result = await ifmod.get_interface_state(session, node_name, iface_name)
    if result is None:
        return _err(
            404,
            "invalid-value",
            f"no such interface {iface_name!r} on {node_name!r}",
        )
    return _json(envelope(ifmod.MODULE, "interfaces-state", result))


@router.put(
    "/restconf/data/labtris:nodes/node={node_name}"
    "/ietf-interfaces:interfaces/interface={iface_name}/enabled"
)
async def put_interface_enabled(
    node_name: str,
    iface_name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    """Toggle admin state. Body: {"ietf-interfaces:enabled": true|false}
    per RFC 8040 §4.5 (single-leaf PUT). We also accept a bare boolean
    for convenience — curl users won't remember the envelope."""
    try:
        body = await request.json()
    except Exception:
        return _err(400, "malformed-message", "body is not valid JSON")
    if isinstance(body, dict):
        val = body.get(f"{ifmod.MODULE}:enabled", body.get("enabled"))
    else:
        val = body
    if not isinstance(val, bool):
        return _err(
            400,
            "invalid-value",
            "expected {\"ietf-interfaces:enabled\": true|false}",
        )
    ok, msg = await ifmod.set_interface_enabled(session, node_name, iface_name, val)
    if not ok:
        return _err(400 if "no such" not in msg else 404, "operation-failed", msg)
    return Response(status_code=204)
