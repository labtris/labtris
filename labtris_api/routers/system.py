from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, get_current_user, require_admin
from labtris_api.config import settings
from labtris_api.db import get_session
from labtris_api.errors import bad_request, unsupported
from labtris_api.models import Template
from labtris_api.naming import IFACE_SCHEMES
from labtris_api.netd_client import netd
from labtris_api.runtime.containers import CONTAINER_CATALOG
from labtris_api.runtime.qemu import (
    NIC_MODELS,
    QEMU_CATALOG,
    available_nic_models,
    image_status,
)

router = APIRouter(tags=["system"])

KEYS = [
    "net.core.somaxconn",
    "net.ipv4.neigh.default.gc_thresh1",
    "net.ipv4.neigh.default.gc_thresh2",
    "net.ipv4.neigh.default.gc_thresh3",
    "vm.swappiness",
    "fs.file-max",
]

WANTED = {
    "net.core.somaxconn": "4096",
    "net.ipv4.neigh.default.gc_thresh1": "4096",
    "net.ipv4.neigh.default.gc_thresh2": "8192",
    "net.ipv4.neigh.default.gc_thresh3": "16384",
    "vm.swappiness": "1",
    "fs.file-max": "1048576",
}


@router.get("/system/tuning")
async def read_tuning(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    values = await netd.call("host.tuning_read", {"keys": KEYS})
    return {"wanted": WANTED, **values}


@router.post("/system/tuning")
async def apply_tuning(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    applied = await netd.call("host.tune", {"sysctls": WANTED})
    limits = Path("/etc/security/limits.d/99-labtris.conf")
    try:
        limits.write_text("* soft nofile 1048576\n* hard nofile 1048576\n")
    except OSError:
        pass
    return applied


@router.get("/system/status")
async def system_status(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    """Lightweight facts operators check first when something feels off.

    Currently: the SSH proxy — most-frequently-toggled optional listener
    on the box, and the one people ask "is it running?" about the moment
    they enable it. More fields go here as they become check-first
    troubleshooting signals (never full diagnostics — /system/diagnostics
    is the dump)."""
    from labtris_api import ssh_proxy

    return {"ssh_proxy": ssh_proxy.status()}


@router.get("/system/diagnostics")
async def diagnostics(
    fmt: str = "json", _user: object = Depends(get_current_user)
) -> Any:
    """One document describing this install. `?fmt=text` is what to paste into
    an issue."""
    from fastapi.responses import PlainTextResponse

    from labtris_api.diagnose import collect, render

    facts = await collect()
    if fmt == "text":
        return PlainTextResponse(render(facts))
    return facts


@router.get("/catalog")
async def catalog(
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, Any]:
    templates = (await session.execute(select(Template).order_by(Template.name))).scalars()
    # Probe every catalog docker image in parallel for cache status.
    # aiodocker inspect is one HTTP round-trip against the local docker
    # socket, so ~10 concurrent probes finish in tens of ms and let the
    # palette render Pull-now buttons on uncached images without a
    # second endpoint. Silently defaults to None on any error so a
    # missing docker daemon does not fail the catalog fetch entirely.
    import asyncio as _asyncio

    from labtris_api.runtime.docker import is_image_cached as _cached

    async def _safe_cached(img: str) -> bool | None:
        try:
            return await _cached(img)
        except Exception:  # noqa: BLE001
            return None

    _images = list(CONTAINER_CATALOG.values())
    _cached_results = await _asyncio.gather(
        *[_safe_cached(i.image) for i in _images], return_exceptions=False
    )
    _cached_map = {i.image: c for i, c in zip(_images, _cached_results)}
    return {
        "templates": [
            {
                "id": t.id,
                "label": t.name,
                "image": t.image,
                "cmd": t.cmd,
                "icon": t.icon,
                # Which runtime a template drops as. It used to be assumed
                # Docker, which was true while templates were only ever a
                # reference — a saved QEMU appliance needs the palette to know
                # it is placing a VM, not a container.
                "runtime": t.runtime,
                "description": t.description,
                # Sizing for a saved image, which has no catalog entry behind
                # it. The server applies these anyway; sending them lets the
                # palette show what it is about to place — and the edit modal
                # pre-fill its form.
                "ram_mb": (t.spec or {}).get("ram_mb"),
                "cpus": (t.spec or {}).get("cpus"),
                "graphical": (t.spec or {}).get("graphical", False),
                "bytes": (t.spec or {}).get("bytes"),
                "from_image": (t.spec or {}).get("from_image"),
                "nic_model": (t.spec or {}).get("nic_model"),
                "disk_bus": (t.spec or {}).get("disk_bus"),
                "iface_scheme": (t.spec or {}).get("iface_scheme"),
                # Companion files + template extras. The edit modal
                # reads these to pre-fill; a template with none set
                # renders those fields as "no BIOS" / "no CD-ROM" /
                # empty extras, matching how it looks today.
                "bios": (t.spec or {}).get("bios"),
                "cdrom": (t.spec or {}).get("cdrom"),
                "qemu_extra_args": (t.spec or {}).get("qemu_extra_args") or [],
            }
            for t in templates
        ],
        "images": [
            {
                "id": img.id,
                "label": img.label,
                "image": img.image,
                "cmd": img.cmd,
                "source": img.source,
                "credentials": img.credentials,
                # The palette hides these from non-admins, who would only be
                # refused at create time; `notes` says why for those who see it.
                "privileged": img.privileged,
                "notes": img.notes,
                "boot_seconds": img.boot_seconds,
                # Cache status so the palette can show a Pull-now button
                # against uncached docker images and avoid a silent
                # multi-minute wait on first spawn. Probed in parallel
                # above; None on docker-daemon errors so the palette
                # falls back to "unknown" (no button, no ✓).
                "cached": _cached_map.get(img.image),
            }
            for img in CONTAINER_CATALOG.values()
        ],
        "qemu_images": [
            {
                "id": img.id,
                "label": img.label,
                "image": img.id,
                "runtime": "qemu",
                "ram_mb": img.ram_mb,
                "cpus": img.cpus,
                # Graphical guests have no serial console worth opening — the
                # UI should offer VNC for these, not a terminal.
                "graphical": img.graphical,
                "credentials": img.credentials,
                "source": img.source,
                # Siblings of the same OS at different releases. The palette
                # groups on these so a version is a choice inside one entry
                # rather than several entries competing for the same row.
                "family": img.family or img.id,
                "family_label": img.family_label or img.label,
                "version": img.version,
                "default_version": img.default_version,
                "cloud_init": img.cloud_init,
                **image_status(img.id),
            }
            for img in QEMU_CATALOG.values()
        ],
        # What a guest calls its own ports, per convention. The UI shows the
        # first few names so "ens192, ens224" reads as a known VMware quirk
        # rather than something Labtris invented.
        "iface_schemes": [
            {
                "id": s.id,
                "label": s.label,
                "example": ", ".join(s.name(i) for i in range(3)),
            }
            for s in IFACE_SCHEMES.values()
        ],
        # Only what this qemu build implements — offering a device it lacks
        # fails at hotplug, far from where the choice was made.
        "nic_models": [
            {"id": m, "label": NIC_MODELS[m]}
            for m in NIC_MODELS
            if m in await available_nic_models()
        ],
    }


class RegistryImport(BaseModel):
    """Where to read GNS3 appliance definitions from."""

    #: A directory of .gns3a files the operator already has. Deliberately a
    #: local path rather than a URL we fetch: the registry is GPL-3.0, and
    #: choosing to bring it is the user's call, not something this project does
    #: on their behalf.
    path: str
    #: Preview by default. Importing 228 entries into a palette is the kind of
    #: thing you want to see the shape of first.
    apply: bool = False


@router.post("/system/registry/import")
async def import_registry(
    body: RegistryImport,
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Read a GNS3 registry directory, and report what it would add.

    Admin-only: it changes what every user of this instance sees in the palette.

    What comes back is the honest count rather than the impressive one. "228
    appliances" is the number on their website; "how many can I boot today,
    without a vendor account" is the number that decides whether this was worth
    doing.
    """
    from labtris_api.gns3_registry import load_dir, summarise

    root = Path(body.path).expanduser()
    if not root.is_dir():
        raise bad_request(f"{body.path!r} is not a directory")

    # `apply` is in the request model and does nothing yet: there is no table
    # to persist an appliance into, so importing for real is still #82's other
    # half. Refusing is the honest answer — returning "applied": false to a
    # caller that asked for true reads as a silent failure, and the caller
    # goes looking for appliances that were never going to appear.
    if body.apply:
        raise bad_request(
            "apply is not implemented yet — this endpoint previews what a registry "
            "contains. Use the reported ram_mb, nic_model and disk_bus to create the "
            "node, and point it at the image you obtained yourself."
        )

    items, failed = load_dir(root)
    if not items:
        raise bad_request(
            f"no .gns3a files under {body.path!r}. Clone github.com/GNS3/gns3-registry "
            "and point this at its appliances/ directory."
        )

    summary = summarise(items)
    return {
        "summary": summary,
        "skipped": failed,
        "applied": False,
        "appliances": [
            {
                "name": a.name,
                "vendor": a.vendor,
                "category": a.category,
                "runtime": a.runtime,
                "ram_mb": a.ram_mb,
                "nic_model": a.nic_model,
                "disk_bus": a.disk_bus,
                "obtainable": a.obtainable,
                "download_url": a.download_url,
                "docker_image": a.docker_image,
                "credentials": a.credentials,
                "supported": a.supported,
            }
            for a in sorted(items, key=lambda x: (not x.supported, x.category, x.name))
        ],
        "note": (
            "Definitions only. No image is downloaded, and none is redistributed — "
            f"{summary['by_obtainable'].get('account', 0)} of these send you to a vendor login."
        ),
    }


# ---------------------------------------------------------- management network


@router.get("/system/management-network")
async def read_management_network(
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Current management NIC config + candidate NICs, so the UI can render
    the Management network pane with values that match what's actually on the
    box rather than an empty form."""
    from labtris_api.netd_client import NetdError

    try:
        return await netd.call("host.management_probe")
    except NetdError as exc:
        raise bad_request(f"probing management network: {exc.message}") from exc


@router.post("/system/management-network")
async def apply_management_network(
    body: "ManagementNetworkIn",
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Write a netplan drop-in for the management NIC and apply it.

    Runs synchronously; the response is what netd reported. If the address
    changes, the browser session that issued the request will not receive
    the response body — nginx and the API layer both live at the old
    address for a fraction of a second and then move. Retry the browser at
    the new URL from the confirmation modal."""
    if not settings.host_network_features:
        raise unsupported(
            "this deployment cannot reconfigure the host's network: it runs in "
            "a private namespace, so writing /etc/netplan would change nothing. "
            "Configure the host's own networking outside Labtris."
        )
    from labtris_api.netd_client import NetdError

    payload: dict[str, Any] = {
        "mode": body.mode,
        "interface": body.interface,
        "kind": body.kind,
    }
    if body.mode == "manual":
        payload["address"] = body.address
        payload["gateway"] = body.gateway
        payload["dns"] = body.dns or []
    else:
        payload["dns"] = []
    try:
        return await netd.call("host.configure_management", payload)
    except NetdError as exc:
        raise bad_request(f"applying management network: {exc.message}") from exc


# ------------------------------------------------------------ uplink bridges


@router.get("/system/uplink-bridges")
async def read_uplink_bridges(
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """What uplink bridges exist today + which NICs a new one could be
    built from. Admin-only for consistency with the write path."""
    from labtris_api.netd_client import NetdError

    try:
        return await netd.call("host.uplink_probe")
    except NetdError as exc:
        raise bad_request(f"probing uplink bridges: {exc.message}") from exc


@router.post("/system/uplink-bridges")
async def apply_uplink_bridges(
    body: "UplinkBridgesIn",
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Replace the full set of labtris-managed uplink bridges.

    A shrink (removing a bridge that's still referenced by a Cloud
    network) is refused up front — the netd write is atomic and cannot
    unwind a partial state, so the check happens here rather than
    ping-ponging errors between the two layers."""
    if not settings.host_network_features:
        raise unsupported(
            "this deployment cannot reconfigure the host's network: it runs in "
            "a private namespace, so writing /etc/netplan would change nothing. "
            "Configure the host's own networking outside Labtris."
        )
    from labtris_api.models import Network
    from labtris_api.netd_client import NetdError

    # What we're currently exposing, so we can compute the shrink and
    # refuse if any of it is still in use.
    try:
        probe = await netd.call("host.uplink_probe")
    except NetdError as exc:
        raise bad_request(f"pre-apply probe: {exc.message}") from exc
    existing_names = {b["name"] for b in probe.get("existing_bridges", [])}

    # After apply, bridges get renamed by position (br1, br2 …). Rather
    # than trying to model per-slot rename semantics, refuse if any
    # currently-existing uplink bridge is referenced by a Cloud and would
    # not survive verbatim under the new list. The simplest safe rule:
    # if the set of NICs matches exactly (regardless of order), we let it
    # through; otherwise every referenced bridge blocks the change.
    new_nics = [entry.nic for entry in body.pairs]
    # A conservative check that catches the risky cases without preventing
    # legitimate reorders: block if any bridge that would disappear (nic
    # no longer in the list) is referenced.
    old_bridges_by_nic = {
        b.get("nic"): b["name"]
        for b in probe.get("existing_bridges", [])
        if b.get("nic")
    }
    disappearing = [
        (nic, br) for nic, br in old_bridges_by_nic.items() if nic not in new_nics
    ]
    if disappearing:
        # Any Cloud pointing at one of these bridges blocks the apply.
        gone_names = [br for _nic, br in disappearing]
        used = (
            await session.execute(
                select(Network).where(
                    Network.kind == "cloud",
                    Network.cloud_ref.in_(gone_names),
                )
            )
        ).scalars().all()
        if used:
            names = ", ".join(sorted({n.name for n in used})[:5])
            raise bad_request(
                f"cannot remove uplink bridge(s) {gone_names}: still in use by "
                f"cloud network(s) {names}. Delete those networks first."
            )

    payload = {"pairs": [entry.model_dump() for entry in body.pairs]}
    try:
        return await netd.call("host.configure_uplinks", payload)
    except NetdError as exc:
        raise bad_request(f"applying uplink bridges: {exc.message}") from exc


# Import at module bottom to keep the file's earlier structure unchanged and
# to avoid pulling schemas.py at import time for callers that don't need it.
from labtris_api.schemas import ManagementNetworkIn, UplinkBridgesIn  # noqa: E402

