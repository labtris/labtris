from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.db import get_session
from labtris_api.netd_client import netd
from labtris_api.runtime.docker import docker_runtime
from labtris_api.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> HealthOut:
    db_ok = False
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    netd_ok = await netd.ping()
    docker_ok = await docker_runtime.ping_engine()
    status = "ok" if db_ok and netd_ok and docker_ok else "degraded"
    return HealthOut(status=status, netd=netd_ok, db=db_ok, docker=docker_ok)
