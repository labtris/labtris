"""Bind a Labtris lab to containers containerlab already deployed.

containerlab does its wiring over netlink, not through the Docker API, so
there is no way to sit in front of it and intercept. What does work is
running the real binary inside Labtris's own namespace — proven in
docs/design/containerlab-in-namespace.mdx — after which the nodes exist as
containers on the same dockerd Labtris uses and the veths exist where netd
can see them. Everything is real; Labtris simply has no rows for any of it.

This is the missing half: take the topology file containerlab was given,
build the lab from it the ordinary way, then adopt the running containers
instead of starting new ones.

What is deliberately NOT done here is touching the dataplane. clab already
built it, and its device names (vethc9f7a85, br-3f03464b7aff) do not match
IFNAME_RE — the gate that stops netd deleting docker0 or a host NIC. So
adopted interfaces keep host_ifname=None, which means netd is never asked
about a device it must not manage. The links still draw on the canvas,
because the canvas reads the Link rows, not the host devices.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.models import Node

log = structlog.get_logger(__name__)

#: containerlab names every container it starts this way.
def container_name(clab_lab: str, node: str) -> str:
    return f"clab-{clab_lab}-{node}"


#: Set on an adopted node so the rest of the system can tell that its
#: lifecycle belongs to containerlab. Without it a stop/start would try to
#: create a container that already exists, and a delete would orphan one.
ADOPTED_ENV = "LABTRIS_CLAB_ADOPTED"


async def _running_containers() -> dict[str, str]:
    """name -> id, for everything on the daemon Labtris drives."""
    import aiodocker

    from labtris_api.config import settings

    docker = aiodocker.Docker(settings.docker_host)
    try:
        found: dict[str, str] = {}
        for c in await docker.containers.list():
            # show() rather than the cached summary: the list payload's
            # shape differs between aiodocker versions, and a wrong guess
            # here fails silently as "nothing was deployed".
            info = await c.show()
            name = (info.get("Name") or "").lstrip("/")
            if name:
                found[name] = info.get("Id") or c.id
        return found
    finally:
        await docker.close()


async def adopt(session: AsyncSession, lab_id: str, clab_lab: str) -> dict[str, Any]:
    """Point each node in `lab_id` at the container clab already started.

    Nodes with no matching container are left alone in their planned state
    rather than marked failed — a topology file can legitimately describe
    more than was deployed, and a half-adopted lab is still useful.
    """
    running = await _running_containers()
    rows = (await session.execute(select(Node).where(Node.lab_id == lab_id))).scalars().all()

    adopted, missing = [], []
    for node in rows:
        cname = container_name(clab_lab, node.name)
        ref = running.get(cname)
        if ref is None:
            missing.append(node.name)
            continue
        node.runtime_ref = ref
        node.state = "running"
        node.env = {**(node.env or {}), ADOPTED_ENV: clab_lab}
        adopted.append(node.name)

    await session.commit()
    log.info("clab.adopted", lab=lab_id, clab=clab_lab,
             adopted=len(adopted), missing=len(missing))
    return {"adopted": adopted, "missing": missing, "clab_lab": clab_lab}
