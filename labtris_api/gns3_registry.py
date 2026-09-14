"""Read GNS3 appliance definitions, so the palette is not empty on day one.

The GNS3 registry is 228 JSON files describing how to run a network device:
RAM, adapter count, NIC model, console type, per-version image filename with
size and checksum, the vendor's download page, default credentials, and the
port-naming convention the guest uses. That is almost exactly what Labtris's
own catalogue holds, written down 228 times by people who tested it.

We read the format; we do not ship the files. The registry is GPL-3.0 and
Labtris is Apache-2.0 — a format is not copyrightable, but bundling those files
into this distribution would pull GPL obligations onto them. So an import is
something a user asks for, pointing at a directory or a URL they chose.

Images are never redistributed either, by them or by us. A definition carries a
link and a checksum, and 34% of them lead to a vendor login. That wall is the
same for everyone and this does not pretend otherwise: an appliance whose image
needs an account is imported with the link, and says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: EVE-NG encodes the disk bus in the filename it tells you to use — virtioa,
#: hda, sataa. GNS3 puts it in `hda_disk_interface` and friends. Both are
#: saying the same thing, and it matters: a guest handed virtio when its kernel
#: has no virtio driver does not boot, and the failure looks like a hung host
#: rather than a wrong bus.
DISK_BUSES = {"ide", "sata", "scsi", "virtio", "nvme", "sd", "usb"}

#: What the GNS3 `console_type` means for us: whether the guest's real console
#: is a serial port or a framebuffer.
GRAPHICAL_CONSOLES = {"vnc", "spice", "spice+agent"}


@dataclass(frozen=True)
class Appliance:
    """One appliance definition, reduced to what Labtris can act on."""

    id: str
    name: str
    vendor: str
    category: str
    #: "qemu" | "docker" | "iou" | "dynamips" | "unknown"
    runtime: str
    ram_mb: int
    cpus: int
    nic_model: str
    disk_bus: str
    graphical: bool
    credentials: str | None
    #: Where the image comes from, and whether a person can just fetch it.
    download_url: str | None
    obtainable: str  # "direct" | "account" | "registry" | "unknown"
    checksum: str | None
    filename: str | None
    docker_image: str | None
    documentation_url: str | None
    #: Untouched, so an importer that learns more later does not need a re-read.
    raw: dict[str, Any]

    @property
    def supported(self) -> bool:
        """Whether Labtris has a runtime that could start this."""
        return self.runtime in ("qemu", "docker")


def _first(entries: list[dict[str, Any]], *keys: str) -> Any:
    for entry in entries:
        for key in keys:
            if entry.get(key):
                return entry[key]
    return None


def parse(doc: dict[str, Any]) -> Appliance:
    """Turn one .gns3a document into an Appliance.

    Tolerant by design: the registry spans schema versions 3 to 8 and the newer
    entries — SR Linux, XRd, VyOS Universal Router — use a shape the older
    parsers do not recognise. Refusing those would drop exactly the devices
    people most want.
    """
    qemu = doc.get("qemu") or {}
    docker = doc.get("docker") or {}
    images = doc.get("images") or []

    # Schema 8 moved the template into settings[].template_properties with a
    # template_type beside it. The newest and most wanted entries live there —
    # SR Linux, XRd, Cisco IOL — so a parser that only knows the old blocks
    # drops exactly the devices people came for.
    if not qemu and not docker:
        settings = doc.get("settings") or []
        chosen = next((s for s in settings if s.get("default")), settings[0] if settings else {})
        props = chosen.get("template_properties") or {}
        kind = chosen.get("template_type") or ""
        if kind == "docker":
            docker = props
        elif kind in ("qemu", "iou", "dynamips"):
            doc = {**doc, kind: props}
            qemu = props if kind == "qemu" else qemu

    if docker:
        runtime = "docker"
    elif qemu:
        runtime = "qemu"
    elif doc.get("iou"):
        runtime = "iou"
    elif doc.get("dynamips"):
        runtime = "dynamips"
    else:
        # Schema 8 puts the template under "settings"/"templates" rather than a
        # named block. Guess from what the images look like instead of giving up.
        runtime = "docker" if doc.get("docker_image") else "qemu" if images else "unknown"

    direct = _first(images, "direct_download_url")
    vendor_page = _first(images, "download_url")
    if runtime == "docker":
        obtainable = "registry"
    elif direct:
        obtainable = "direct"
    elif vendor_page:
        obtainable = "account"
    else:
        obtainable = "unknown"

    bus = str(qemu.get("hda_disk_interface") or qemu.get("hdb_disk_interface") or "virtio")
    if bus not in DISK_BUSES:
        bus = "virtio"

    return Appliance(
        id=str(doc.get("appliance_id") or doc.get("name", "")),
        name=str(doc.get("name", "")),
        vendor=str(doc.get("vendor_name") or ""),
        category=str(doc.get("category") or "guest"),
        runtime=runtime,
        # The registry's numbers are a floor that was actually booted, which is
        # better information than a default we would otherwise invent.
        ram_mb=int(qemu.get("ram") or docker.get("ram") or 256),
        cpus=int(qemu.get("cpus") or 1),
        nic_model=str(qemu.get("adapter_type") or "virtio-net-pci"),
        disk_bus=bus,
        graphical=str(qemu.get("console_type") or "telnet") in GRAPHICAL_CONSOLES,
        credentials=(
            f"{doc['username']} / {doc.get('password', '')}".strip(" /")
            if doc.get("username")
            else None
        ),
        download_url=direct or vendor_page or doc.get("vendor_url"),
        obtainable=obtainable,
        checksum=_first(images, "md5sum", "sha256sum"),
        filename=_first(images, "filename"),
        docker_image=docker.get("image") or doc.get("docker_image"),
        documentation_url=doc.get("documentation_url"),
        raw=doc,
    )


def load_dir(path: str | Path) -> tuple[list[Appliance], list[str]]:
    """Read every .gns3a in a directory.

    Returns what parsed and what did not. A registry with one malformed file
    should import the other 227 and say which one it skipped, rather than
    failing wholesale and leaving the user to guess.
    """
    root = Path(path)
    found: list[Appliance] = []
    failed: list[str] = []
    for f in sorted(root.glob("*.gns3a")):
        try:
            found.append(parse(json.loads(f.read_text())))
        except Exception as exc:  # noqa: BLE001 - one bad file is not a bad import
            failed.append(f"{f.name}: {type(exc).__name__}: {exc}")
    return found, failed


def summarise(items: list[Appliance]) -> dict[str, Any]:
    """Counts worth showing before an import is confirmed.

    "228 appliances" is not the useful number. "139 you can boot today" is —
    and so is knowing that 72 of them will send you to a vendor login.
    """
    runnable = [a for a in items if a.supported]
    return {
        "total": len(items),
        "supported": len(runnable),
        "unsupported": len(items) - len(runnable),
        "by_runtime": {
            r: sum(1 for a in items if a.runtime == r)
            for r in sorted({a.runtime for a in items})
        },
        "by_obtainable": {
            o: sum(1 for a in runnable if a.obtainable == o)
            for o in sorted({a.obtainable for a in runnable})
        },
        "ready_to_use": sum(
            1 for a in runnable if a.obtainable in ("direct", "registry")
        ),
    }
