"""Take a vrnetlab image apart and get the vendor disk back out of it.

vrnetlab solves a genuinely hard problem — turning a licensed vendor qcow2
into something container tooling can run — by wrapping QEMU in a container.
Labtris runs QEMU natively, so the wrapper is the one part we do not want.
What we do want is the disk inside it, and the launch parameters somebody
already worked out for that platform.

Both are recoverable. A vrnetlab image is built with

    COPY $IMAGE* /
    COPY *.py /

so the vendor disk sits at the root of the image filesystem, and the
per-platform `launch.py` beside it carries the RAM, vCPU, NIC model and NIC
count that platform needs. Those constants are the accumulated result of
somebody booting the thing repeatedly until it came up, which is exactly the
knowledge a bring-your-own-image workflow is missing.

Nothing is redistributed. This reads an image the user built themselves from
a qcow2 they are licensed to have; the bytes never leave their machine, and
Labtris ships no vendor disk of any kind.

The detected values are always printed and every one can be overridden,
because a heuristic that silently gets RAM wrong produces a VM that fails to
boot for no visible reason.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: vrnetlab's own defaults, from common/vrnetlab.py VM.__init__. Used only
#: when launch.py does not override them, which is common — a platform happy
#: with 4 GB and an IDE disk simply says nothing about either.
VRNETLAB_DEFAULTS: dict[str, Any] = {
    "ram": 4096,
    "smp": "1",
    "cpu": "host",
    "driveif": "ide",
    "nic_type": "e1000",
    "num_nics": 0,
    "arch": "x86_64",
}

#: Keys worth lifting out of a super().__init__() call.
_KWARGS = frozenset({"ram", "smp", "cpu", "driveif", "num_nics", "nic_type", "arch"})

#: vrnetlab's driveif values against the two Labtris supports. A guest whose
#: kernel has no virtio-blk driver will not find its own root disk, so when
#: in doubt this stays on ide: slow beats unbootable.
_DISK_BUS = {"virtio": "virtio", "virtio-blk": "virtio"}

#: Interface naming, guessed from the image name. Only the families where
#: getting it wrong is confusing rather than cosmetic — a Cisco guest that
#: calls its ports GigabitEthernet0/1 and a canvas that calls them ens4 do
#: not look like the same device. Anything unmatched falls through to the
#: `add` default rather than guessing.
_IFACE_SCHEME_HINTS: tuple[tuple[str, str], ...] = (
    (r"srlinux|_srl\b|/srl", "srl"),
    (r"panos|paloalto|_pan\b", "paloalto"),
    (r"n9kv|nxos|nexus", "nxos"),
    (r"csr|c8000v|xrv|iosv|vios|cat9kv|iol", "ios"),
)


class VrnetlabError(RuntimeError):
    """Something about this image is not what a vrnetlab image looks like."""


@dataclass
class Extracted:
    """A vendor disk, plus what vrnetlab knows about booting it."""

    disk: Path
    image: str
    ram_mb: int
    cpus: int
    nic_model: str
    disk_bus: str
    num_nics: int
    iface_scheme: str | None
    #: Where each value came from, so the CLI can show its working.
    source: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _docker(*args: str, binary: bool = False) -> Any:
    """Run the docker CLI.

    Shelling out rather than using aiodocker: this needs `docker cp`, which
    means pulling a path out of a created-but-never-started container, and
    the CLI does that in one call against whatever DOCKER_HOST is set — the
    host daemon on a source install, dind in the container one.
    """
    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=not binary,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise VrnetlabError(f"docker {' '.join(args)}: {err.strip()}")
    return proc.stdout


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def parse_launch(text: str) -> tuple[dict[str, Any], list[str]]:
    """Pull the VM constants out of a platform's launch.py.

    Returns what was found, and the names of the VM classes defined — a few
    platforms are more than one virtual machine and the caller needs to say
    so rather than quietly describe one of them.

    Two shapes carry the constants: `self.nic_type = "virtio-net-pci"` on
    the VM subclass, and keyword arguments to `super().__init__()`
    (`ram=8192`, `driveif="virtio"`). Only literals are taken. Cisco's
    csr1000v says `self.num_nics = nics`, reading it from an argument, and
    Nokia's sros computes RAM from the chassis type — neither is knowable
    here, and vrnetlab's own default is a better answer than a guess.

    First occurrence wins, which is right for the common case (a second
    class for an install-mode pass, written after the runtime one) and
    merely arbitrary for the rare one, hence the class list.
    """
    found: dict[str, Any] = {}
    classes: list[str] = []
    with warnings.catch_warnings():
        # Several real launch.py files contain regexes written as "\.foo$"
        # rather than r"\.foo$". Python warns when compiling them, which is
        # true and entirely not the user's problem — they did not write this
        # file and cannot fix it.
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                if name == "VM":
                    classes.append(node.name)
                    break
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr in _KWARGS
                ):
                    value = _literal(node.value)
                    if value is not None:
                        found.setdefault(target.attr, value)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in _KWARGS:
                    value = _literal(kw.value)
                    if value is not None:
                        found.setdefault(kw.arg, value)
    return found, classes


def _iface_scheme_for(image: str) -> str | None:
    lowered = image.lower()
    for pattern, scheme in _IFACE_SCHEME_HINTS:
        if re.search(pattern, lowered):
            return scheme
    return None


def _root_listing(image: str) -> list[tuple[str, int]]:
    """(name, size) for every regular file at the image root.

    The entrypoint is overridden because the real one boots a router. `find`
    with -printf would be neater but is not in every base image; `ls -ln`
    is.
    """
    out = _docker(
        "run", "--rm", "--entrypoint", "/bin/sh", image,
        "-c", "ls -lnL / 2>/dev/null || ls -ln /",
    )
    files = []
    for line in out.splitlines():
        parts = line.split(maxsplit=8)
        if len(parts) < 9 or not parts[0].startswith("-"):
            continue
        try:
            files.append((parts[8], int(parts[4])))
        except ValueError:
            continue
    return files


def extract(image: str, dest_dir: Path) -> Extracted:
    """Get the vendor disk and boot parameters out of `image`.

    `dest_dir` should be on the same filesystem as the image cache so the
    caller's rename into place stays a rename.
    """
    if shutil.which("docker") is None:
        raise VrnetlabError("the docker CLI is not on PATH; it is needed to read the image")

    files = _root_listing(image)
    disks = sorted(
        ((n, s) for n, s in files if n.lower().endswith(".qcow2")),
        key=lambda item: item[1],
        reverse=True,
    )
    if not disks:
        raise VrnetlabError(
            f"{image}: no .qcow2 at the image root. vrnetlab images are built "
            "with `COPY $IMAGE* /`, so this does not look like one"
        )

    notes: list[str] = []
    disk_name, disk_bytes = disks[0]
    if len(disks) > 1:
        # Some platforms ship an install-time disk alongside the real one.
        # The vendor image is invariably the larger.
        notes.append(
            f"{len(disks)} disks at the image root; took the largest "
            f"({disk_name}, {disk_bytes // (1024 * 1024)} MiB). Others: "
            + ", ".join(n for n, _ in disks[1:])
        )

    cid = _docker("create", image).strip()
    try:
        disk_path = dest_dir / f"vrnetlab-{disk_name}"
        _docker("cp", f"{cid}:/{disk_name}", str(disk_path))
        launch_path = dest_dir / "launch.py"
        try:
            _docker("cp", f"{cid}:/launch.py", str(launch_path))
            launch_text = launch_path.read_text(errors="replace")
        except VrnetlabError:
            launch_text = ""
            notes.append(
                "no /launch.py in the image — boot parameters fall back to "
                "vrnetlab's defaults, which may not suit this platform"
            )
        finally:
            launch_path.unlink(missing_ok=True)
    except Exception:
        _docker("rm", "-f", cid)
        raise
    _docker("rm", "-f", cid)

    detected: dict[str, Any] = {}
    if launch_text:
        try:
            detected, vm_classes = parse_launch(launch_text)
        except SyntaxError as exc:
            vm_classes = []
            notes.append(f"could not parse launch.py ({exc}); using vrnetlab defaults")
        if len(vm_classes) > 1:
            # Juniper's vMX is a control-plane VM plus a forwarding VM, with
            # different RAM and a different NIC count. Labtris models one
            # disk per template, so this cannot be imported faithfully and
            # saying which one was read beats appearing to handle it.
            notes.append(
                f"launch.py defines {len(vm_classes)} VMs ({', '.join(vm_classes)}); "
                f"settings were read from {vm_classes[0]}. Multi-VM platforms do "
                "not map onto one template — check the values and override them"
            )

    source = {}
    values = {}
    for key, fallback in VRNETLAB_DEFAULTS.items():
        if key in detected:
            values[key] = detected[key]
            source[key] = "launch.py"
        else:
            values[key] = fallback
            source[key] = "vrnetlab default"

    try:
        cpus = max(1, int(str(values["smp"]).split(",")[0]))
    except ValueError:
        cpus = 1
        source["smp"] = "unparseable, using 1"

    if values["arch"] != "x86_64":
        notes.append(
            f"launch.py targets {values['arch']}; Labtris runs x86_64 guests, "
            "so this image will not boot here"
        )

    return Extracted(
        disk=disk_path,
        image=image,
        ram_mb=int(values["ram"]),
        cpus=cpus,
        nic_model=str(values["nic_type"]),
        disk_bus=_DISK_BUS.get(str(values["driveif"]), "ide"),
        num_nics=int(values["num_nics"]),
        iface_scheme=_iface_scheme_for(image),
        source=source,
        warnings=notes,
    )


def describe(found: Extracted) -> str:
    """A short report of what was read and where each value came from."""
    rows = [
        ("RAM", f"{found.ram_mb} MB", found.source.get("ram", "")),
        ("vCPUs", str(found.cpus), found.source.get("smp", "")),
        ("NIC model", found.nic_model, found.source.get("nic_type", "")),
        ("Disk bus", found.disk_bus, found.source.get("driveif", "")),
        ("Data NICs", str(found.num_nics), found.source.get("num_nics", "")),
        ("Iface scheme", found.iface_scheme or "(default)", "guessed from the image name"),
    ]
    width = max(len(label) for label, _, _ in rows)
    lines = [f"  {label.ljust(width)}  {value}  [{origin}]" for label, value, origin in rows]
    return "\n".join(lines)


def to_json(found: Extracted) -> str:
    return json.dumps(
        {
            "image": found.image,
            "ram_mb": found.ram_mb,
            "cpus": found.cpus,
            "nic_model": found.nic_model,
            "disk_bus": found.disk_bus,
            "num_nics": found.num_nics,
            "iface_scheme": found.iface_scheme,
            "source": found.source,
            "warnings": found.warnings,
        },
        indent=2,
    )
