"""SSH-proxy endpoint discovery.

Answers "for a given lab (or every lab I can see), what are the SSH
usernames and the one host:port I should point my tools at?" — so a
netmiko/scrapli inventory can be generated with one call, and the CLI
does not have to hard-code the host from the caller's session.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.models import Lab, Node
from labtris_api.ssh_proxy import _slug

router = APIRouter(tags=["ssh"])


def _resolve_host(request: Request) -> str:
    """Best guess at "the address a client outside the box would use to
    reach this proxy". Prefers the Host header the caller sent (usually
    a real hostname or the same LAN IP they hit this API on); falls
    back to the socket peer info; then to 127.0.0.1 as a final default.
    Client can always override via the ?host= query param."""
    h = request.headers.get("host") or ""
    if h:
        return h.split(":")[0]
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


@router.get("/ssh-endpoints")
async def list_ssh_endpoints(
    request: Request,
    lab_id: str | None = None,
    host: str | None = None,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One row per running node, with the exact ssh username to use.

    Rows also carry an OpenSSH-config fragment ready to paste into
    ~/.ssh/config — so an "ssh r1" muscle-memory shortcut works after
    one pipeline command:

        labtris --profile home ssh-endpoints | jq -r '.entries[].ssh_config'
            >> ~/.ssh/config
    """
    proxy_host = host or _resolve_host(request)
    port = settings.ssh_proxy_port

    stmt = (
        select(Node.id, Node.name, Node.runtime, Node.state, Lab.id, Lab.name)
        .join(Lab, Lab.id == Node.lab_id)
        .order_by(Lab.name, Node.name)
    )
    if lab_id:
        stmt = stmt.where(Node.lab_id == lab_id)
    rows = (await session.execute(stmt)).all()

    # Nodes that share a bare name across labs need the lab-slug prefix
    # to disambiguate in the SSH proxy. Compute the collision set once so
    # the same name in a single lab still uses the short form.
    name_counts: dict[str, int] = {}
    for _nid, name, _rt, _st, _lid, _ln in rows:
        name_counts[name] = name_counts.get(name, 0) + 1

    entries: list[dict[str, Any]] = []
    for node_id, name, runtime, state, lab_row_id, lab_name in rows:
        username = name if name_counts.get(name, 0) == 1 else f"{_slug(lab_name)}:{name}"
        supported = runtime in ("qemu", "docker")
        ssh_config = (
            f"Host {username}\n"
            f"    HostName {proxy_host}\n"
            f"    Port {port}\n"
            f"    User {username}\n"
            f"    StrictHostKeyChecking accept-new\n"
        )
        entries.append(
            {
                "node_id": node_id,
                "node_name": name,
                "lab_id": lab_row_id,
                "lab_name": lab_name,
                "runtime": runtime,
                "state": state,
                "username": username,
                "host": proxy_host,
                "port": port,
                "supported": supported,
                "ssh_config": ssh_config,
                # "ssh <username>@<host> -p <port>" is what clients need to
                # copy-paste when a browser can't launch a terminal.
                "command": f"ssh -p {port} {username}@{proxy_host}",
            }
        )
    return {
        "proxy_enabled": settings.ssh_proxy_enabled,
        "host": proxy_host,
        "port": port,
        "entries": entries,
    }
