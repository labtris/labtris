from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.errors import conflict, not_found, runtime_error
from labtris_api.lifecycle import new_id
from labtris_api.models import Host
from labtris_api.netd_client import NetdError, client_for_endpoint, client_for_host, netd
from labtris_api.schemas import HostCreate, HostOut, HostPatch

#: Long enough for a busy satellite on a LAN, short enough that a dead
#: one does not hold up a page load.
PROBE_TIMEOUT = 3.0

router = APIRouter(tags=["hosts"])


@router.get("/hosts", response_model=list[HostOut])
async def list_hosts(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> list[Host]:
    result = await session.execute(select(Host).order_by(Host.name))
    hosts = list(result.scalars())
    if not hosts:
        local = Host(
            id=new_id(),
            name="local",
            endpoint=netd.endpoint,
            is_local=True,
            reachable=await netd.ping(),
        )
        session.add(local)
        await session.commit()
        hosts = [local]
    # An unreachable satellite used to hang this endpoint on a TCP connect
    # with no deadline — 133 seconds for one dead host here, during which the
    # whole UI was stuck behind it. A host that cannot answer in three seconds
    # is unreachable for the purpose of showing a green dot.
    async def probe(host: Host) -> None:
        try:
            host.reachable = await asyncio.wait_for(
                client_for_host(host).ping(), timeout=PROBE_TIMEOUT
            )
        except (TimeoutError, Exception):  # noqa: BLE001 - any failure means "down"
            host.reachable = False

    # Concurrently: ten dead hosts should cost three seconds, not thirty.
    await asyncio.gather(*(probe(h) for h in hosts))
    await session.commit()
    return hosts


@router.post("/hosts", response_model=HostOut, status_code=201)
async def register_host(
    body: HostCreate,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Host:
    """Register a *second* host's netd — the multi-host control plane. Give
    it `endpoint=tcp://ip:port` and the `--token` that host's netd was
    started with (see `make netd-remote` / `labtris-netd --tcp-listen --token`)."""
    host = Host(
        id=new_id(),
        name=body.name,
        endpoint=body.endpoint,
        token=body.token,
        underlay_ip=body.underlay_ip,
        is_local=False,
    )
    client = client_for_endpoint(host.endpoint, host.token)
    host.reachable = await client.ping()
    session.add(host)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"host name {body.name!r} already exists") from exc
    await session.refresh(host)
    return host


@router.get("/hosts/{host_id}/capabilities")
async def host_capabilities(
    host_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """What that host's kernel can actually do — probed by netd, not assumed.
    `links.vxlan.supported` is the one that decides whether a multi-host
    stretched network can exist there at all."""
    host = await session.get(Host, host_id)
    if host is None:
        raise not_found(f"host {host_id} not found")
    client = client_for_host(host)
    try:
        return await client.call("host.capabilities")
    except NetdError as exc:
        raise runtime_error(f"host {host.name!r}: {exc.message}") from exc


@router.patch("/hosts/{host_id}", response_model=HostOut)
async def patch_host(
    host_id: str,
    body: HostPatch,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Host:
    host = await session.get(Host, host_id)
    if host is None:
        raise not_found(f"host {host_id} not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        if value is not None:
            setattr(host, key, value)
    await session.commit()
    await session.refresh(host)
    return host


@router.delete("/hosts/{host_id}", status_code=204)
async def delete_host(
    host_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Response:
    host = await session.get(Host, host_id)
    if host is None:
        raise not_found(f"host {host_id} not found")
    if host.is_local:
        raise conflict("cannot delete the local host")
    await session.delete(host)
    await session.commit()
    return Response(status_code=204)
