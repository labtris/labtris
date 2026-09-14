from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from labtris_api.auth import User, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import bad_request, conflict, internal, not_found
from labtris_api.lifecycle import (
    allocate_mac,
    delete_lab,
    get_lab,
    get_unlocked_lab,
    guest_name,
    iface_scheme_for,
    new_id,
    require_lab_owner,
)
from labtris_api.migrate import Plan, detect, parse_clab, parse_unl
from labtris_api.models import Geometry, Interface, Lab, Link, Network, Node
from labtris_api.naming import normalise_folder
from labtris_api.schemas import (
    ConfigSetIn,
    FolderRename,
    GeometryBody,
    LabClone,
    LabCreate,
    LabDetail,
    LabImportIn,
    LabListItem,
    LabOut,
    LabPatch,
    LinkOut,
    NetworkOut,
    NodeOut,
    TopologyImportIn,
    UnlImportIn,
)

router = APIRouter(tags=["labs"])


def _lab_detail(lab: Lab) -> LabDetail:
    return LabDetail(
        id=lab.id,
        name=lab.name,
        description=lab.description,
        locked=lab.locked,
        folder=lab.folder or "",
        active_configset=lab.active_configset,
        nodes=[NodeOut.model_validate(n) for n in lab.nodes],
        networks=[NetworkOut.model_validate(n) for n in lab.networks],
        links=[LinkOut.model_validate(n) for n in lab.links],
    )


@router.get("/labs", response_model=list[LabListItem])
async def list_labs(
    q: str = "",
    mine: bool = False,
    folder: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Every lab on the instance, whoever made it.

    Shared visibility rather than per-user silos: an instructor opens a
    student's lab to help with it, and students learn from each other's. Who
    may *change* a lab is a separate question from who may see it.

    `q` matches the lab name or the owner's username, because "show me
    everything Priya is working on" is how people actually search.
    """
    from labtris_api.models import User as UserRow

    stmt = (
        select(Lab, UserRow)
        .join(UserRow, UserRow.id == Lab.owner_id, isouter=True)
        .order_by(Lab.folder, Lab.name)
    )
    if folder is not None:
        # Exact folder, not a prefix: opening "CCNA" should show what is in
        # CCNA, with "CCNA/Week 1" reached by opening that in turn. A prefix
        # match would flatten the tree the folders exist to create.
        stmt = stmt.where(Lab.folder == normalise_folder(folder))
    if mine:
        stmt = stmt.where(func.trim(Lab.owner_id) == user.id.strip())
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Lab.name).like(needle)
            | func.lower(func.coalesce(UserRow.username, "")).like(needle)
            | func.lower(func.coalesce(UserRow.display_name, "")).like(needle)
        )
    rows = (await session.execute(stmt)).all()
    # One grouped query for every lab's counts rather than a query per lab:
    # the switcher shows these for all labs at once, so N+1 here is N+1 on
    # every keystroke of the filter.
    counts = {
        lab_id: (int(total), int(running))
        for lab_id, total, running in (
            await session.execute(
                select(
                    Node.lab_id,
                    func.count(Node.id),
                    func.count(Node.id).filter(Node.state == "running"),
                ).group_by(Node.lab_id)
            )
        ).all()
    }
    return [
        {
            "id": lab.id,
            "name": lab.name,
            "description": lab.description,
            "locked": lab.locked,
            "owner_id": lab.owner_id,
            "owner": owner.username if owner else None,
            "owner_name": (owner.display_name or owner.username) if owner else None,
            # Stripped for the same CHAR(26) padding reason as require_lab_owner.
            "mine": bool(lab.owner_id and lab.owner_id.strip() == user.id.strip()),
            "folder": lab.folder or "",
            "nodes": counts.get(lab.id, (0, 0))[0],
            "running": counts.get(lab.id, (0, 0))[1],
        }
        for lab, owner in rows
    ]


@router.post("/labs", response_model=LabOut, status_code=201)
async def create_lab(
    body: LabCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Lab:
    lab = Lab(
        id=new_id(),
        name=body.name,
        description=body.description,
        folder=normalise_folder(body.folder),
        # Whoever made it. Not a permission — everyone can see and open it —
        # but "whose is this" is the first thing anyone asks of a shared list.
        owner_id=None if user.id in ("setup", "dev") else user.id,
    )
    session.add(lab)
    session.add(Geometry(lab_id=lab.id, data={}))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Only the name is a conflict the caller can do anything about.
        # Reporting every constraint violation as a duplicate name sent me
        # hunting for a phantom lab when the real failure was the new owner
        # foreign key.
        detail = str(getattr(exc, "orig", exc))
        if "labs_name" in detail or "name" in detail:
            raise conflict(f"lab name {body.name!r} already exists") from exc
        raise internal(
            f"could not create the lab: {detail.splitlines()[0][:200]}"
        ) from exc
    await session.refresh(lab)
    return lab


@router.get("/labs/{lab_id}", response_model=LabDetail)
async def get_lab_detail(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> LabDetail:
    result = await session.execute(
        select(Lab)
        .options(
            selectinload(Lab.nodes).selectinload(Node.interfaces),
            selectinload(Lab.networks),
            selectinload(Lab.links),
        )
        .where(Lab.id == lab_id)
    )
    lab = result.scalar_one_or_none()
    if lab is None:
        raise not_found(f"lab {lab_id} not found")
    return _lab_detail(lab)


@router.delete("/labs/{lab_id}", status_code=204)
async def destroy_lab(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    lab = await require_lab_owner(session, lab_id, user)
    await delete_lab(session, lab)
    return Response(status_code=204)


@router.post("/labs/{lab_id}/validate")
async def validate(
    lab_id: str,
    fmt: str = "json",
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Any:
    """Check that the running lab matches the lab that was asked for.

    Starting a topology and having a working topology are different claims.
    These compare the rows against the dataplane — taps present and up, on the
    bridge they belong to, carrying the impairment the link says they carry."""
    from fastapi.responses import PlainTextResponse

    from labtris_api.validate import render, validate_lab

    lab = await get_lab(session, lab_id)
    result = await validate_lab(session, lab)
    if fmt == "text":
        return PlainTextResponse(render(result))
    return result


@router.get("/labs/{lab_id}/geometry")
async def get_geometry(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    await get_lab(session, lab_id)
    geom = await session.get(Geometry, lab_id)
    return {"data": geom.data if geom else {}}


@router.put("/labs/{lab_id}/geometry")
async def put_geometry(
    lab_id: str,
    body: GeometryBody,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    await get_lab(session, lab_id)
    geom = await session.get(Geometry, lab_id)
    if geom is None:
        geom = Geometry(lab_id=lab_id, data=body.data)
        session.add(geom)
    else:
        geom.data = body.data
    await session.commit()
    return {"data": geom.data}


async def export_lab_payload(session: AsyncSession, lab_id: str) -> dict[str, Any]:
    """One lab as a portable document. Shared with the backup archive so the
    two cannot describe a lab differently."""
    detail = await get_lab_detail(lab_id, session, None)
    geom = await session.get(Geometry, lab_id)
    return {
        "format": "labtris-lab-v1",
        "lab": detail.model_dump(),
        "geometry": geom.data if geom else {},
    }


@router.get("/labs/{lab_id}/export")
async def export_lab(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    return await export_lab_payload(session, lab_id)


@router.post("/labs/{lab_id}/lock")
async def lock_lab(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    await require_lab_owner(session, lab_id, user)
    lab = await get_lab(session, lab_id)
    lab.locked = True
    await session.commit()
    return {"locked": True}


@router.post("/labs/{lab_id}/unlock")
async def unlock_lab(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    await require_lab_owner(session, lab_id, user)
    lab = await get_lab(session, lab_id)
    lab.locked = False
    await session.commit()
    return {"locked": False}


@router.patch("/labs/{lab_id}", response_model=LabOut)
async def rename_lab(
    lab_id: str,
    body: LabPatch,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Lab:
    """Rename / re-describe a lab — EVE-NG's `PUT api/labs{labpath}/move` minus folders.

    Owner-only: the name and the lock are the lab's identity rather than its
    contents, and renaming someone else's work out from under them is the kind
    of surprise a shared instance should not allow."""
    await require_lab_owner(session, lab_id, user)
    lab = await get_unlocked_lab(session, lab_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("folder") is not None:
        data["folder"] = normalise_folder(data["folder"])
    for key, value in data.items():
        if value is not None:
            setattr(lab, key, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"lab name {body.name!r} already exists") from exc
    await session.refresh(lab)
    return lab


@router.post("/labs/{lab_id}/clone", response_model=LabOut, status_code=201)
async def clone_lab(
    lab_id: str,
    body: LabClone,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Lab:
    """Copy a lab's topology into a new one, owned by whoever asked.

    Anyone may clone any lab they can see — that is the point of a shared
    instance, and it takes nothing away from the original. What is copied is
    the design: nodes, interfaces, networks, links, impairments, geometry and
    configs. What is deliberately not copied is anything live.

    Fresh MACs, no host ifnames, no runtime refs, every node back to `defined`.
    Two labs holding one MAC would collide the moment both were started, and a
    copied runtime ref would point the clone's stop button at the original's
    container.
    """
    src = await get_lab(session, lab_id)
    name = (body.name or "").strip() or f"{src.name} copy"
    folder = normalise_folder(body.folder if body.folder is not None else src.folder)

    dst = Lab(
        id=new_id(),
        name=name,
        description=src.description,
        folder=folder,
        # The clone belongs to whoever made it, not to the original's owner.
        owner_id=None if user.id in ("setup", "dev") else user.id,
        configsets=dict(src.configsets or {}),
    )
    session.add(dst)

    nets = (await session.execute(select(Network).where(Network.lab_id == lab_id))).scalars().all()
    net_map: dict[str, str] = {}
    for net in nets:
        clone_net = Network(
            id=new_id(),
            lab_id=dst.id,
            name=net.name,
            kind=net.kind,
            cloud_ref=net.cloud_ref,
            # host_ifname and vni are allocations against the live host, not
            # part of the design; the clone gets its own when it first starts.
        )
        net_map[net.id] = clone_net.id
        session.add(clone_net)

    node_rows = (
        (
            await session.execute(
                select(Node).where(Node.lab_id == lab_id).options(selectinload(Node.interfaces))
            )
        )
        .scalars()
        .all()
    )
    iface_map: dict[str, str] = {}
    node_map: dict[str, str] = {}
    for node in node_rows:
        clone_node = Node(
            id=new_id(),
            lab_id=dst.id,
            name=node.name,
            runtime=node.runtime,
            image=node.image,
            state="defined",
            cpu_limit=node.cpu_limit,
            ram_mb=node.ram_mb,
            env=dict(node.env or {}),
            cmd=list(node.cmd) if node.cmd else None,
            style=dict(node.style or {}),
            console=dict(node.console or {}),
            nic_model=node.nic_model,
            qemu_opts=dict(node.qemu_opts or {}),
            startup_config=node.startup_config,
        )
        node_map[node.id] = clone_node.id
        session.add(clone_node)
        for iface in node.interfaces:
            clone_iface = Interface(
                id=(iface_id := new_id()),
                node_id=clone_node.id,
                idx=iface.idx,
                name=iface.name,
                mac=await allocate_mac(session, iface_id),
                network_id=net_map.get(iface.network_id) if iface.network_id else None,
            )
            iface_map[iface.id] = clone_iface.id
            session.add(clone_iface)
    await session.flush()

    links = (await session.execute(select(Link).where(Link.lab_id == lab_id))).scalars().all()
    for link in links:
        a, b = iface_map.get(link.a_iface_id), iface_map.get(link.b_iface_id)
        link_net = net_map.get(link.network_id)
        # A link whose ends did not survive the copy is dropped rather than
        # half-created; a dangling end would fail at start, far from here.
        if not (a and b and link_net):
            continue
        session.add(
            Link(
                id=new_id(),
                lab_id=dst.id,
                a_iface_id=a,
                b_iface_id=b,
                network_id=link_net,
                impair_ab=dict(link.impair_ab) if link.impair_ab else None,
                impair_ba=dict(link.impair_ba) if link.impair_ba else None,
                admin_up=link.admin_up,
            )
        )

    geom = (
        await session.execute(select(Geometry).where(Geometry.lab_id == lab_id))
    ).scalar_one_or_none()
    # Positions are keyed by node id, so they have to be remapped or the clone
    # opens with every node stacked at the origin.
    # Geometry is {"nodes": {...}, "nets": {...}, "links": {}, "view": {...}},
    # not a flat map — and every id inside it changed. Remapping the wrong
    # level silently produces an empty layout, so the clone opens with
    # everything stacked at the origin.
    data = dict(geom.data or {}) if geom else {}
    session.add(
        Geometry(
            lab_id=dst.id,
            data={
                "nodes": {
                    node_map[k]: v for k, v in (data.get("nodes") or {}).items() if k in node_map
                },
                "nets": {
                    net_map[k]: v for k, v in (data.get("nets") or {}).items() if k in net_map
                },
                "links": {},
                "view": data.get("view") or {"x": 80, "y": 40, "k": 1},
            },
        )
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"lab name {name!r} already exists") from exc
    await session.refresh(dst)
    return dst


@router.get("/folders")
async def list_folders(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Every folder that has labs in it, with a count.

    Derived from the labs rather than stored, so there is no such thing as an
    empty folder left behind by a move — which is the failure mode a folders
    table would have brought with it."""
    rows = (
        await session.execute(
            select(Lab.folder, func.count(Lab.id)).group_by(Lab.folder).order_by(Lab.folder)
        )
    ).all()
    return {"folders": [{"path": path or "", "labs": count} for path, count in rows]}


@router.post("/folders/rename")
async def rename_folder(
    body: FolderRename,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Move a folder and everything beneath it.

    Only the caller's own labs move. Renaming a shared folder must not drag a
    colleague's work somewhere they did not ask for, so labs they own stay
    where they are and the count says how many moved."""
    frm, to = normalise_folder(body.frm), normalise_folder(body.to)
    if not frm:
        raise bad_request("the root folder cannot be renamed")
    if frm == to:
        return {"moved": 0, "folder": to}
    rows = (
        (
            await session.execute(
                select(Lab).where((Lab.folder == frm) | (Lab.folder.startswith(f"{frm}/")))
            )
        )
        .scalars()
        .all()
    )
    moved = 0
    for lab in rows:
        # Stripped for the CHAR(26) padding reason require_lab_owner documents.
        if not (lab.owner_id and lab.owner_id.strip() == user.id.strip()):
            continue
        suffix = lab.folder[len(frm) :]
        lab.folder = normalise_folder(f"{to}{suffix}" if to else suffix)
        moved += 1
    await session.commit()
    return {"moved": moved, "folder": to}


@router.get("/labs/{lab_id}/configsets")
async def list_configsets(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    lab = await get_lab(session, lab_id)
    sets = lab.configsets or {}
    node_names: dict[str, str] = {
        row.id: row.name
        for row in (
            await session.execute(select(Node.id, Node.name).where(Node.lab_id == lab_id))
        ).all()
    }
    return {
        "configsets": sets,
        # Which set was last applied, so the list can say so rather than
        # leaving the reader to guess from four equally plausible names.
        "active": lab.active_configset,
        # A summary per set, because the raw map is keyed by node id and a
        # picker showing ULIDs tells nobody anything.
        "summary": [
            {
                "name": name,
                "nodes": len(configs),
                # Names of nodes that still exist; a set outlives the node it
                # was captured from, and saying so beats a silent short count.
                "node_names": sorted(
                    node_names[nid].strip() for nid in configs if nid in node_names
                ),
                "missing": sum(1 for nid in configs if nid not in node_names),
            }
            for name, configs in sorted(sets.items())
        ],
    }


@router.post("/labs/{lab_id}/configsets/{name}/capture")
async def capture_configset(
    lab_id: str,
    name: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Save whatever the nodes are configured with now, under a name.

    The saving half of this feature was already possible, but only by
    assembling the whole {node_id: config} map yourself — which is not what an
    instructor who has just finished configuring a topology wants to do. This
    is the button that makes a set out of the current state.

    Nodes with no config are skipped rather than stored as empty strings: an
    empty entry would blank that node on apply, which is a destructive way to
    record "this one was never configured".
    """
    lab = await get_lab(session, lab_id)
    nodes = (await session.execute(select(Node).where(Node.lab_id == lab_id))).scalars().all()
    captured = {n.id: n.startup_config for n in nodes if n.startup_config}
    if not captured:
        raise bad_request("no node in this lab has a startup-config to capture")
    sets = dict(lab.configsets or {})
    sets[name] = captured
    lab.configsets = sets
    # Capturing means the lab now matches this set by construction.
    lab.active_configset = name
    await session.commit()
    return {"name": name, "nodes": len(captured), "active": name}


@router.put("/labs/{lab_id}/configsets/{name}")
async def save_configset(
    lab_id: str,
    name: str,
    body: ConfigSetIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Named, reusable bundle of per-node configs — EVE-NG's `configsets`."""
    lab = await get_lab(session, lab_id)
    sets = dict(lab.configsets or {})
    sets[name] = body.configs
    lab.configsets = sets
    await session.commit()
    return {"configsets": lab.configsets}


@router.delete("/labs/{lab_id}/configsets/{name}")
async def delete_configset(
    lab_id: str,
    name: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    lab = await get_lab(session, lab_id)
    sets = dict(lab.configsets or {})
    sets.pop(name, None)
    lab.configsets = sets
    if lab.active_configset == name:
        # The lab still has these configs on its nodes, but there is no longer
        # a set by that name to point at.
        lab.active_configset = None
    await session.commit()
    return {"configsets": lab.configsets}


@router.post("/labs/{lab_id}/configsets/{name}/apply")
async def apply_configset(
    lab_id: str,
    name: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Save each node's config from the set and push it live if the node is running."""
    from labtris_api.runtime.base import RuntimeHandle
    from labtris_api.runtime.registry import get_runtime

    lab = await get_lab(session, lab_id)
    configs = (lab.configsets or {}).get(name)
    if configs is None:
        raise not_found(f"configset {name!r} not found")
    applied, pushed, missing = [], [], []
    for node_id, content in configs.items():
        node = await session.get(Node, node_id)
        if node is None or node.lab_id != lab_id:
            # The set outlived the node. Worth reporting: a set that silently
            # configures three of four nodes is how a lab comes up half-right.
            missing.append(node_id)
            continue
        node.startup_config = content
        applied.append(node_id)
        if node.state == "running" and node.runtime_ref:
            runtime = get_runtime(node.runtime)
            handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
            await runtime.write_file(handle, "/config/startup-config", content)
            pushed.append(node_id)
    lab.active_configset = name
    await session.commit()
    return {"applied": applied, "pushed": pushed, "missing": missing, "active": name}


async def _clone_topology(
    session: AsyncSession,
    lab: Lab,
    src_nodes: list[dict[str, Any]],
    src_links: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Materialize nodes/interfaces/links from an export payload's shape under a
    fresh lab, returning (old_node_id -> new_node_id, old_iface_id -> new Interface)."""
    node_id_map: dict[str, str] = {}
    iface_id_map: dict[str, Interface] = {}
    for n in src_nodes:
        node = Node(
            id=new_id(),
            lab_id=lab.id,
            name=n["name"],
            runtime=n.get("runtime", "docker"),
            image=n["image"],
            env=n.get("env") or {},
            cmd=n.get("cmd"),
            cpu_limit=n.get("cpu_limit"),
            ram_mb=n.get("ram_mb"),
            style=n.get("style") or {},
            state="defined",
        )
        session.add(node)
        node_id_map[n["id"]] = node.id
        for i in n.get("interfaces") or []:
            iface = Interface(
                id=(iface_id := new_id()),
                node_id=node.id,
                idx=i["idx"],
                name=i["name"],
                mac=await allocate_mac(session, iface_id),
            )
            session.add(iface)
            iface_id_map[i["id"]] = iface
    await session.flush()
    for lk in src_links:
        a = iface_id_map.get(lk["a_iface_id"])
        b = iface_id_map.get(lk["b_iface_id"])
        if a is None or b is None:
            continue
        net = Network(
            id=new_id(), lab_id=lab.id, name=f"lnk-{new_id()[-8:].lower()}", kind="bridge"
        )
        session.add(net)
        await session.flush()
        a.network_id = net.id
        b.network_id = net.id
        session.add(
            Link(
                id=new_id(),
                lab_id=lab.id,
                a_iface_id=a.id,
                b_iface_id=b.id,
                network_id=net.id,
                impair_ab=lk.get("impair_ab"),
                impair_ba=lk.get("impair_ba"),
                admin_up=lk.get("admin_up", True),
            )
        )
    return node_id_map, {k: v.id for k, v in iface_id_map.items()}


def _remap_geometry(geometry: dict[str, Any], node_id_map: dict[str, str]) -> dict[str, Any]:
    nodes = {node_id_map.get(k, k): v for k, v in (geometry.get("nodes") or {}).items()}
    return {"nodes": nodes, "links": {}, "view": geometry.get("view") or {"x": 80, "y": 40, "k": 1}}


@router.post("/labs/import", response_model=LabDetail, status_code=201)
async def import_lab(
    body: LabImportIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> LabDetail:
    """Round-trips this app's own `labtris-lab-v1` export — EVE-NG's `POST /api/import`."""
    src = body.lab
    base_name = src.get("name") or "imported-lab"
    name = base_name
    for suffix in range(1, 50):
        exists = (await session.execute(select(Lab).where(Lab.name == name))).scalar_one_or_none()
        if exists is None:
            break
        name = f"{base_name}-{suffix}"
    lab = Lab(id=new_id(), name=name, description=src.get("description", ""))
    session.add(lab)
    await session.flush()
    node_id_map, _ = await _clone_topology(
        session, lab, src.get("nodes") or [], src.get("links") or []
    )
    session.add(Geometry(lab_id=lab.id, data=_remap_geometry(body.geometry, node_id_map)))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict("import failed — a referenced name already exists") from exc
    return await get_lab_detail(lab.id, session, _user)


def _unl_text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


async def realize_plan(session: AsyncSession, plan: Plan) -> tuple[Lab, list[str]]:
    """Turn a parsed topology into a lab.

    Shared by both importers so a containerlab file and a .unl file cannot
    produce subtly different labs from the same shape."""
    name = plan.name
    for suffix in range(1, 50):
        exists = (await session.execute(select(Lab).where(Lab.name == name))).scalar_one_or_none()
        if exists is None:
            break
        name = f"{plan.name}-{suffix}"

    lab = Lab(id=new_id(), name=name, description="")
    session.add(lab)
    await session.flush()

    geometry_nodes: dict[str, Any] = {}
    by_name: dict[str, Node] = {}
    iface_of: dict[tuple[str, str], Interface] = {}

    for index, planned in enumerate(plan.nodes):
        node = Node(
            id=new_id(),
            lab_id=lab.id,
            name=planned.name,
            runtime=planned.runtime,
            image=planned.image,
            cmd=planned.cmd,
            env=planned.env,
            state="defined",
        )
        session.add(node)
        await session.flush()
        by_name[planned.name] = node
        scheme = iface_scheme_for(node.runtime, node.image)
        for idx, iface_name in enumerate(planned.ifaces):
            iface = Interface(
                id=(iface_id := new_id()),
                node_id=node.id,
                idx=idx,
                name=guest_name(idx, iface_name, scheme),
                mac=await allocate_mac(session, iface_id),
            )
            session.add(iface)
            iface_of[(planned.name, iface_name)] = iface
        if planned.position:
            geometry_nodes[node.id] = {"x": planned.position[0], "y": planned.position[1]}
        else:
            # A node box is 176px wide; 220px columns left the wires 44px to
            # route through and the result read as a pile rather than a
            # topology. Four to a row, with room between them.
            geometry_nodes[node.id] = {
                "x": 140 + (index % 4) * 300,
                "y": 120 + (index // 4) * 230,
            }

    await session.flush()

    for planned_net in plan.networks:
        session.add(Network(id=new_id(), lab_id=lab.id, name=planned_net, kind="bridge"))

    for link in plan.links:
        a = iface_of.get((link.a_node, link.a_iface or ""))
        b = iface_of.get((link.b_node, link.b_iface or ""))
        if a is None or b is None:
            # An endpoint naming an interface the node never declared: create
            # it rather than dropping the link, which is what containerlab
            # itself does.
            for target, wanted in ((link.a_node, link.a_iface), (link.b_node, link.b_iface)):
                key = (target, wanted or "")
                if key in iface_of:
                    continue
                owner = by_name.get(target)
                if owner is None:
                    continue
                idx = len([k for k in iface_of if k[0] == target])
                extra = Interface(
                    id=(iface_id := new_id()),
                    node_id=owner.id,
                    idx=idx,
                    name=guest_name(idx, wanted, iface_scheme_for(owner.runtime, owner.image)),
                    mac=await allocate_mac(session, iface_id),
                )
                session.add(extra)
                iface_of[key] = extra
            await session.flush()
            a = iface_of.get((link.a_node, link.a_iface or ""))
            b = iface_of.get((link.b_node, link.b_iface or ""))
        if a is None or b is None:
            plan.warnings.append(f"could not wire {link.a_node} to {link.b_node}")
            continue
        net = Network(
            id=new_id(), lab_id=lab.id, name=f"lnk-{new_id()[-8:].lower()}", kind="bridge"
        )
        session.add(net)
        await session.flush()
        a.network_id = net.id
        b.network_id = net.id
        session.add(
            Link(
                id=new_id(),
                lab_id=lab.id,
                a_iface_id=a.id,
                b_iface_id=b.id,
                network_id=net.id,
            )
        )

    session.add(
        Geometry(lab_id=lab.id, data={"nodes": geometry_nodes, "nets": {}, "links": {},
                                      "view": {"x": 80, "y": 40, "k": 1}})
    )
    await session.commit()
    return lab, plan.warnings


@router.post("/labs/import/topology", status_code=201)
async def import_topology(
    body: TopologyImportIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    """Import a containerlab `.clab.yml` or an EVE-NG `.unl`, detected from
    the content.

    Anything that cannot be carried over becomes a labelled placeholder and a
    warning rather than a rejection: a topology that arrives with 14 of its 17
    nodes is worth more than an error message, and the warnings say exactly
    what was lost."""
    try:
        kind = body.format or detect(body.content, body.filename or "")
        if kind == "clab":
            plan = parse_clab(body.content)
        elif kind == "unl":
            plan = parse_unl(body.content, body.name)
        else:
            raise ValueError(f"unsupported topology format {kind!r}")
    except ValueError as exc:
        raise bad_request(str(exc)) from None

    if body.name:
        plan.name = body.name
    if not plan.nodes:
        raise bad_request("that topology has no nodes we could import")

    lab, warnings = await realize_plan(session, plan)
    detail = await get_lab_detail(lab.id, session, None)
    return {
        "lab": detail.model_dump(),
        "warnings": warnings,
        "source": kind,
        "imported": {"nodes": len(plan.nodes), "links": len(plan.links),
                     "networks": len(plan.networks)},
    }


@router.post("/labs/import/unl", response_model=LabDetail, status_code=201)
async def import_unl(
    body: UnlImportIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> LabDetail:
    """EVE-NG's `.unl` XML (schema in docs/01-findings.md §3, observed on a
    running system rather than copied).

    Kept for existing callers; it uses the same parser and template table as
    import_topology, so the two cannot disagree about what a template means."""
    try:
        plan = parse_unl(body.xml, body.name)
    except ValueError as exc:
        raise bad_request(str(exc)) from None
    if not plan.nodes:
        raise bad_request("that .unl has no nodes we could import")
    lab, warnings = await realize_plan(session, plan)
    if warnings:
        lab.description = "; ".join(warnings)[:2000]
        await session.commit()
    return await get_lab_detail(lab.id, session, None)

