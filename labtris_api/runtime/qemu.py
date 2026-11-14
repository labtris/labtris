from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

from labtris_api.config import settings
from labtris_api.errors import ApiError, conflict, needs_restart, runtime_error, unprocessable
from labtris_api.netd_client import NetdError, netd
from labtris_api.runtime.base import (
    MAX_NIC_QUEUES,
    MAX_VCPUS,
    Capability,
    ConsoleEndpoint,
    IfaceSpec,
    NodeSpec,
    RuntimeHandle,
    RuntimeState,
    StopMode,
)

# osboxes.org publishes ready-to-run desktop/server installs of the mainstream
# distros as VirtualBox VDIs inside .7z archives — the same "just boot it"
# catalog EVE-NG users expect, without building images by hand. They cost a
# multi-GB download and a one-time qemu-img convert; that result is cached, so
# only the first node of a given image pays for it.
#
# Every osboxes image ships the same account: osboxes / osboxes.org (root
# password osboxes.org too).
OSBOXES_CREDENTIALS = "osboxes / osboxes.org"
_OSBOXES = "https://sourceforge.net/projects/osboxes/files/v/vb"
_UBUNTU_CLOUD = "https://cloud-images.ubuntu.com/releases"

#: What the generated cloud-init seed sets, so the credentials shown in the UI
#: are the credentials that actually work.
CLOUD_USER = "labtris"
CLOUD_PASSWORD = "labtris"
CLOUD_CREDENTIALS = f"{CLOUD_USER} / {CLOUD_PASSWORD}"


@dataclass(frozen=True)
class QemuImage:
    """One entry in the QEMU image catalog."""

    id: str
    label: str
    url: str
    #: Which OS this is, and which release of it. Two images of the same
    #: family are the same thing at different versions, and the UI should
    #: offer them that way rather than as unrelated entries — you pick Ubuntu,
    #: then you pick 24.04. Blank family means a one-off with no siblings.
    family: str = ""
    family_label: str = ""
    version: str = ""
    #: The version offered first when someone picks this family.
    default_version: bool = False
    #: A cloud image has no password at all until cloud-init sets one, so it
    #: needs a generated seed to be usable. It is also far smaller than a
    #: desktop install and already has a serial console.
    cloud_init: bool = False
    #: Archive format the download is wrapped in, or None for a bare disk.
    archive: str | None = None
    ram_mb: int = 256
    cpus: int = 1
    #: True when the guest's real console is its framebuffer (VNC), not a
    #: serial port — every desktop install, and anything without
    #: `console=ttyS0` in its bootloader config.
    graphical: bool = False
    credentials: str | None = None
    source: str = "osboxes.org"
    #: Emulated NIC this image's kernel is known to drive. virtio needs a
    #: driver the guest may not have; e1000 is the safe fallback for anything
    #: old, minimal, or shipped without virtio.
    nic_model: str = "virtio-net-pci"
    #: virtio queue pairs per NIC; None is one. Left unset for every shipped
    #: image — multi-queue is a per-lab decision, not an image property.
    nic_queues: int | None = None
    #: Which disk controller this image's kernel can boot from. Same problem as
    #: nic_model one layer down, and far less forgiving: a guest handed a
    #: virtio disk with no virtio-blk driver does not boot at all, and does not
    #: say so — QEMU starts, the console shows a bootloader or nothing, and it
    #: looks like the host is broken. 67 of the 182 QEMU appliances in the GNS3
    #: registry need ide rather than virtio.
    disk_bus: str = "virtio"
    #: What this guest will call its own ports. Anything with systemd renames
    #: them by PCI slot, so a virtio NIC on our machine type comes up as ens3 —
    #: not the eth0 the topology used to promise. See naming.IFACE_SCHEMES.
    iface_scheme: str = "ens"


QEMU_CATALOG: dict[str, QemuImage] = {
    image.id: image
    for image in [
        # A real NOS in the catalog, and a legally clean one: Cumulus VX is
        # NVIDIA's freely redistributable VX build, published as a Vagrant
        # libvirt box (a tar.gz around box.img). No account, no licence file,
        # no vendor image we are not allowed to fetch.
        QemuImage(
            id="cumulus-vx-5.10",
            label="Cumulus Linux VX 5.10 (switch NOS)",
            url=(
                "https://d2cd9e7ca6hntp.cloudfront.net/public/CumulusLinux-5.10.0/"
                "cumulus-linux-5.10.0-vx-amd64-libvirt.box"
            ),
            archive="tar.gz",
            iface_scheme="swp",
            ram_mb=2048,
            cpus=2,
            credentials="cumulus / cumulus (forced change on first login)",
            source="nvidia.com",
            family="cumulus-vx",
            family_label="Cumulus Linux VX",
            version="5.10",
            default_version=True,
        ),
        QemuImage(
            id="cirros",
            label="CirrOS 0.6.2 (tiny serial test VM)",
            # No systemd, so no predictable-name renaming: it really is eth0.
            iface_scheme="eth",
            url="https://download.cirros-cloud.net/0.6.2/cirros-0.6.2-x86_64-disk.img",
            ram_mb=256,
            credentials="cirros / gocubsgo",
            source="cirros-cloud.net",
        ),
        QemuImage(
            id="ubuntu-24.04",
            label="Ubuntu 24.04 Desktop",
            url=f"{_OSBOXES}/55-U-u/24.04/64bit.7z/download",
            archive="7z",
            ram_mb=4096,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
            family="ubuntu-desktop",
            family_label="Ubuntu Desktop",
            version="24.04 LTS",
            default_version=True,
        ),
        QemuImage(
            id="ubuntu-22.04",
            label="Ubuntu 22.04 Desktop",
            url=f"{_OSBOXES}/55-U-u/22.04/64bit.7z/download",
            archive="7z",
            ram_mb=4096,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
            family="ubuntu-desktop",
            family_label="Ubuntu Desktop",
            version="22.04 LTS",
        ),
        # Canonical's own cloud images. osboxes stops at 25.04, and these are
        # a tenth of the size, boot in seconds rather than minutes, and carry
        # console=ttyS0 already — which is what makes the serial console
        # useful without editing the guest first. The catch is that they have
        # no password until cloud-init sets one, hence the generated seed.
        QemuImage(
            id="ubuntu-cloud-26.04",
            label="Ubuntu 26.04 Server (cloud image)",
            url=f"{_UBUNTU_CLOUD}/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img",
            ram_mb=1024,
            cpus=1,
            credentials=CLOUD_CREDENTIALS,
            source="cloud-images.ubuntu.com",
            family="ubuntu-server",
            family_label="Ubuntu Server",
            version="26.04 LTS",
            default_version=True,
            cloud_init=True,
        ),
        QemuImage(
            id="ubuntu-cloud-24.04",
            label="Ubuntu 24.04 Server (cloud image)",
            url=f"{_UBUNTU_CLOUD}/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img",
            ram_mb=1024,
            cpus=1,
            credentials=CLOUD_CREDENTIALS,
            source="cloud-images.ubuntu.com",
            family="ubuntu-server",
            family_label="Ubuntu Server",
            version="24.04 LTS",
            cloud_init=True,
        ),
        QemuImage(
            id="debian-12",
            label="Debian 12.9 Desktop",
            url=f"{_OSBOXES}/14-D-b/12.9.0/64bit.7z/download",
            archive="7z",
            ram_mb=2048,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
        ),
        QemuImage(
            id="fedora-42-server",
            label="Fedora 42 Server",
            url=f"{_OSBOXES}/18-F-d/42/Server/64bit.7z/download",
            archive="7z",
            ram_mb=2048,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
        ),
        QemuImage(
            id="fedora-42",
            label="Fedora 42 Workstation",
            url=f"{_OSBOXES}/18-F-d/42/Workstation/64bit.7z/download",
            archive="7z",
            ram_mb=4096,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
        ),
        QemuImage(
            id="centos-9-server",
            label="CentOS Stream 9 Server",
            url=f"{_OSBOXES}/10-C-nt/9/Server/64bit.7z/download",
            archive="7z",
            ram_mb=2048,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
        ),
        QemuImage(
            id="opensuse-leap-15.6",
            label="openSUSE Leap 15.6",
            url=f"{_OSBOXES}/39-O-s-se/Leap/15.6/64bit.7z/download",
            archive="7z",
            ram_mb=2048,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
        ),
        QemuImage(
            id="linuxmint-22.1",
            label="Linux Mint 22.1 Cinnamon",
            url=f"{_OSBOXES}/31-Lx-M-t/22.1/Cinnamon/64bit.7z/download",
            archive="7z",
            ram_mb=4096,
            cpus=2,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
        ),
        QemuImage(
            id="kali-2025.2",
            label="Kali Linux 2025.2",
            url=f"{_OSBOXES}/25-Kl-l-x/2025.2/64bit.7z/download",
            archive="7z",
            ram_mb=4096,
            cpus=2,
            graphical=True,
            credentials="kali / kali",
        ),
        QemuImage(
            id="arch-cli",
            label="Arch Linux (CLI)",
            url=f"{_OSBOXES}/4-Ar---c-x/20240601/CLI/64bit.7z/download",
            archive="7z",
            ram_mb=1024,
            cpus=1,
            graphical=True,
            credentials=OSBOXES_CREDENTIALS,
        ),
    ]
}

# Disk images we know how to hand to qemu-img, in the order we prefer them when
# an archive contains several.
_DISK_SUFFIXES = (".qcow2", ".vdi", ".vmdk", ".vhdx", ".vhd", ".raw", ".img")


# Emulated NICs worth offering, most capable first. virtio-net-pci is fastest
# but invisible to a guest without virtio drivers — a stock Windows install, an
# older BSD, some appliance images — which boots with no network and nothing to
# explain why. e1000 is the compatibility answer: almost everything has driven
# an Intel gigabit NIC since 2000.
NIC_MODELS: dict[str, str] = {
    "virtio-net-pci": "virtio — fastest, needs virtio drivers in the guest",
    "e1000": "Intel 82540EM — the safe default for guests without virtio",
    "e1000e": "Intel 82574L — newer Intel, broad Windows support",
    "rtl8139": "Realtek 8139 — very old guests",
    "vmxnet3": "VMware vmxnet3 — images built for ESXi",
    "pcnet": "AMD PCnet — DOS-era and minimal guests",
}

_available_nics: set[str] | None = None


async def available_nic_models() -> set[str]:
    """Which of NIC_MODELS this qemu build actually has.

    Offering a device the local binary does not implement produces a device_add
    failure at hotplug time, long after the choice was made and nowhere near
    where the user made it."""
    global _available_nics
    if _available_nics is not None:
        return _available_nics
    if shutil.which("qemu-system-x86_64") is None:
        _available_nics = set(NIC_MODELS)
        return _available_nics
    rc, out = await _run("qemu-system-x86_64", "-device", "help", timeout=30)
    if rc != 0:
        _available_nics = set(NIC_MODELS)
    else:
        _available_nics = {m for m in NIC_MODELS if f'name "{m}"' in out}
    return _available_nics


def _seven_zip() -> str:
    for name in ("7z", "7za", "7zr"):
        found = shutil.which(name)
        if found:
            return found
    raise runtime_error(
        "extracting this image needs 7-Zip — install it with "
        "`apt install p7zip-full` (or `brew install p7zip`)"
    )


def _vm_dir(node_id: str) -> Path:
    d = Path(settings.qemu_vm_dir).expanduser() / node_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_dir() -> Path:
    d = Path(settings.qemu_image_cache_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bios_dir() -> Path:
    """Content-addressed store for companion BIOS blobs (OVMF-sata.fd etc).
    Sibling of _cache_dir so a single QEMU_IMAGE_CACHE_DIR still governs
    both — usual deploy has both under the labtris user's cache."""
    d = _cache_dir().parent / "qemu-bios"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cdrom_dir() -> Path:
    """Content-addressed store for companion CD-ROMs (NX-OSv config
    schema ISO, etc.). Same location shape as _bios_dir."""
    d = _cache_dir().parent / "qemu-cdrom"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _companion_ok(path_str: str | None, cache_dir: Path) -> Path | None:
    """Turn a persisted companion path into a validated Path, or None
    if not set / invalid. Refuses paths outside the cache dir — a
    template row could nominate any file on disk, and we don't want an
    admin to accidentally (or maliciously) point qemu at /etc/shadow."""
    if not path_str:
        return None
    p = Path(path_str)
    try:
        p = p.resolve(strict=True)
        p.relative_to(cache_dir.resolve())
    except (FileNotFoundError, ValueError):
        return None
    return p


async def _run(*args: str, timeout: float | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise runtime_error(f"{args[0]} timed out after {timeout}s") from None
    return proc.returncode or 0, out.decode(errors="replace")


# What each catalog image is doing right now, so a node that looks stuck at
# "starting" for twenty minutes can say "downloading 12% of 2060 MB" instead.
# Keyed by catalog id (or URL, for an image given directly).
IMAGE_PROGRESS: dict[str, dict[str, Any]] = {}


def image_status(image: str) -> dict[str, Any]:
    """Whether this image is ready to boot, still being fetched, or neither."""
    spec = QEMU_CATALOG.get(image)
    url = spec.url if spec else image
    status: dict[str, Any] = dict(IMAGE_PROGRESS.get(image) or {})
    if url.startswith(("http://", "https://")):
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        status["cached"] = (_cache_dir() / f"{digest}.qcow2").exists()
    else:
        status["cached"] = Path(url).expanduser().exists()
    return status


async def _content_length(url: str) -> int:
    rc, out = await _run("curl", "-sIL", "--max-time", "60", url)
    if rc != 0:
        return 0
    sizes = [
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.lower().startswith("content-length:")
    ]
    return int(sizes[-1]) if sizes and sizes[-1].isdigit() else 0


async def _download(url: str, dest: Path, key: str) -> None:
    """Multi-GB images over SourceForge mirrors: follow redirects, resume a
    partial `.part` rather than restarting, and only publish the final name
    once the transfer completed.

    curl's own progress meter goes to a pipe we'd have to parse; the `.part`
    file's size against Content-Length says the same thing and survives a
    resume."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = await _content_length(url)
    IMAGE_PROGRESS[key] = {"phase": "downloading", "done": 0, "total": total, "percent": 0}

    proc = await asyncio.create_subprocess_exec(
        "curl", "-fSL", "--retry", "3", "--retry-delay", "5", "-C", "-", "-o", str(tmp), url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _watch() -> None:
        while True:
            await asyncio.sleep(2)
            done = tmp.stat().st_size if tmp.exists() else 0
            IMAGE_PROGRESS[key] = {
                "phase": "downloading",
                "done": done,
                "total": total,
                "percent": round(done * 100 / total) if total else 0,
            }

    watcher = asyncio.create_task(_watch())
    try:
        out_bytes, _ = await proc.communicate()
    finally:
        watcher.cancel()
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        IMAGE_PROGRESS.pop(key, None)
        raise runtime_error(
            f"downloading qemu image {url!r} failed: {out_bytes.decode(errors='replace').strip()}"
        )
    tmp.rename(dest)


def _find_disk(root: Path) -> Path:
    """Pick the disk out of an extracted archive — osboxes .7z files hold the
    .vdi next to a README and sometimes a .vbox descriptor."""
    candidates = [f for f in root.rglob("*") if f.is_file()]
    for suffix in _DISK_SUFFIXES:
        matching = [f for f in candidates if f.suffix.lower() == suffix]
        if matching:
            return max(matching, key=lambda f: f.stat().st_size)
    raise runtime_error(f"no disk image found in archive (looked for {', '.join(_DISK_SUFFIXES)})")


async def _extract(archive: Path, kind: str, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    if kind == "7z":
        rc, out = await _run(_seven_zip(), "x", "-y", f"-o{into}", str(archive))
    elif kind == "zip":
        rc, out = await _run("unzip", "-o", "-q", str(archive), "-d", str(into))
    elif kind in ("tar.gz", "tgz"):
        # Vendor images that ship as a tarball, and Vagrant `.box` files —
        # which are a gzipped tar holding box.img next to a Vagrantfile and
        # metadata.json. _find_disk takes the largest disk-suffixed file, so
        # the descriptors alongside it are ignored.
        rc, out = await _run("tar", "-xzf", str(archive), "-C", str(into))
    else:
        raise unprocessable(f"unsupported archive format {kind!r}")
    if rc != 0:
        raise runtime_error(f"extracting {archive.name} failed: {out.strip()}")
    return _find_disk(into)


async def _to_qcow2(disk: Path, dest: Path) -> None:
    """VDI/VMDK straight from osboxes, or a raw cloud image — normalise to
    qcow2 so every VM's overlay can use a qcow2 backing file."""
    rc, info = await _run("qemu-img", "info", "--output=json", str(disk))
    fmt = json.loads(info).get("format") if rc == 0 else None
    if fmt == "qcow2":
        disk.replace(dest)
        return
    tmp = dest.with_suffix(".converting")
    rc, out = await _run("qemu-img", "convert", "-p", "-O", "qcow2", str(disk), str(tmp))
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise runtime_error(f"qemu-img convert of {disk.name} failed: {out.strip()}")
    tmp.rename(dest)


_resolving: dict[str, asyncio.Lock] = {}


#: How a saved image is named in a node's `image` field. A scheme rather than a
#: bare path keeps the filesystem layout out of the database — the cache can be
#: moved with LABTRIS_QEMU_IMAGE_CACHE_DIR and every template still resolves.
CUSTOM_PREFIX = "custom:"


def _custom_path(digest: str) -> Path:
    return _cache_dir() / f"custom-{digest}.qcow2"


async def _sha256(path: Path) -> str:
    """Hash off the event loop: these files are gigabytes."""

    def _read() -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    return await asyncio.to_thread(_read)


async def _install_custom(tmp: Path) -> dict[str, Any]:
    """Hash a qcow2, dedup against existing custom images, and move it into
    the cache under its content-addressed name.

    `tmp` must already be a qcow2 the caller no longer needs; on success it
    is moved into place, on dedup it is unlinked. Returns the same shape
    the saved-template flow has always returned so callers can be reused.
    """
    digest = (await _sha256(tmp))[:16]
    final = _custom_path(digest)
    if final.exists():
        # Same bytes as an image already stored — keep the original and drop
        # this copy rather than duplicating multi-GB of identical data.
        tmp.unlink(missing_ok=True)
    else:
        tmp.rename(final)
    return {
        "image": f"{CUSTOM_PREFIX}{digest}",
        "digest": digest,
        "bytes": final.stat().st_size,
        "path": str(final),
    }


async def _ensure_qcow2(src: Path, dst: Path) -> Path:
    """Normalise a disk image to qcow2 at `dst`. If `src` is already qcow2,
    just move it into place; otherwise `qemu-img convert` it. `src` is
    consumed either way — the caller does not need to clean it up on
    success.

    The move path is why `dst` must be on the same filesystem as `src`.
    Both should live under `_cache_dir()`.
    """
    dst.unlink(missing_ok=True)
    rc, info = await _run("qemu-img", "info", "--output=json", str(src))
    fmt = None
    if rc == 0:
        try:
            fmt = json.loads(info).get("format")
        except (json.JSONDecodeError, AttributeError):
            fmt = None
    if fmt == "qcow2":
        src.rename(dst)
        return dst
    rc, out = await _run("qemu-img", "convert", "-O", "qcow2", "-c", str(src), str(dst))
    src.unlink(missing_ok=True)
    if rc != 0:
        dst.unlink(missing_ok=True)
        raise runtime_error(f"qemu-img convert of {src.name} failed: {out.strip()}")
    return dst


async def flatten_node_disk(node_id: str) -> dict[str, Any]:
    """Turn a node's overlay into a standalone image, and return how to name it.

    `qemu-img convert` rather than `qemu-img commit`. Commit is what EVE-NG
    does — it merges the overlay down into the base in place — and it is the
    wrong move here for two reasons. Every node using that image shares the one
    base file, so committing would silently rewrite the disk under other
    people's running labs; and the cache is content-addressed, so a base whose
    bytes no longer match the digest in its own filename makes every later
    "is this cached?" answer wrong.

    Convert has neither problem. It reads the whole backing chain and writes a
    new, self-contained file that nothing else references.
    """
    overlay = _vm_dir(node_id) / "disk.qcow2"
    if not overlay.exists():
        raise unprocessable(
            "this node has no disk yet — start it at least once before saving it as a template"
        )

    cache = _cache_dir()
    tmp = cache / f"flatten-{node_id}.qcow2"
    tmp.unlink(missing_ok=True)
    rc, out = await _run(
        "qemu-img", "convert", "-O", "qcow2", "-c", str(overlay), str(tmp)
    )
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise runtime_error(f"flattening {overlay.name} failed: {out.strip()}")

    return await _install_custom(tmp)


async def _resolve_base_image(image: str) -> Path:
    """Turn a catalog id, URL, or local path into a qcow2 on this host.

    Downloading and converting a 2 GB osboxes archive takes minutes, so the
    result is cached under the URL's digest and concurrent starts of the same
    image wait on one another instead of racing on the same files."""
    if image.startswith(CUSTOM_PREFIX):
        path = _custom_path(image[len(CUSTOM_PREFIX) :])
        if not path.exists():
            raise unprocessable(
                f"saved image {image!r} is missing from the image cache. It was "
                "flattened from a node on this host; if the cache directory moved "
                "or was cleared, the template can no longer be started."
            )
        return path
    spec = QEMU_CATALOG.get(image)
    url = spec.url if spec else image
    if not url.startswith(("http://", "https://")):
        path = Path(url).expanduser()
        if not path.exists():
            raise unprocessable(
                f"qemu image {image!r} not found (catalog id, URL, or path expected)"
            )
        return path

    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    cache = _cache_dir()
    final = cache / f"{digest}.qcow2"
    if final.exists():
        return final

    async with _resolving.setdefault(digest, asyncio.Lock()):
        if final.exists():
            return final
        # SourceForge URLs end in "/download", so the archive kind from the
        # catalog is a better name for the cache file than the URL's tail.
        if spec and spec.archive:
            suffix = f".{spec.archive}"
        else:
            suffix = "".join(Path(url.split("?")[0]).suffixes[-1:]) or ".img"
        download = cache / f"{digest}{suffix}"
        if not download.exists():
            await _download(url, download, image)
        extracted = cache / f"{digest}.d"
        try:
            if spec and spec.archive:
                IMAGE_PROGRESS[image] = {"phase": "extracting", "percent": None}
                disk = await _extract(download, spec.archive, extracted)
            else:
                disk = download
            IMAGE_PROGRESS[image] = {"phase": "converting", "percent": None}
            await _to_qcow2(disk, final)
        finally:
            IMAGE_PROGRESS.pop(image, None)
            shutil.rmtree(extracted, ignore_errors=True)
            if final.exists():
                download.unlink(missing_ok=True)
    return final


#: Displays handed out recently, by VM directory. QEMU binds its VNC port
#: only after the serial socket is connected, a second or more after launch,
#: so a bind test alone gave two VMs started back to back the same display
#: and the second died with "Failed to find an available port"
#: (labtris-012, 2026-11-12). A claim holds for VNC_CLAIM_S.
VNC_CLAIM_S = 120.0
_vnc_claims: dict[int, tuple[str, float]] = {}


def _claimed_by_other(display: int, owner: str) -> bool:
    claim = _vnc_claims.get(display)
    if claim is None:
        return False
    who, at = claim
    if time.monotonic() - at > VNC_CLAIM_S:
        _vnc_claims.pop(display, None)
        return False
    return who != owner


def _claim_vnc_display(display: int, owner: str) -> None:
    _vnc_claims[display] = (owner, time.monotonic())


def _pick_free_vnc_display(low: int = 1, high: int = 900, owner: str = "") -> int:
    """QEMU's `-vnc :N` listens on TCP 5900+N. Bind-test to avoid handing out
    a display already taken by another VM on this host, and skip one just
    handed to a VM that has not bound it yet."""
    for display in range(low, high):
        if _claimed_by_other(display, owner):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", 5900 + display))
            except OSError:
                continue
        _claim_vnc_display(display, owner)
        return display
    raise runtime_error("no free VNC display available on this host")


logger = structlog.get_logger(__name__)


def _display_free(display: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 5900 + display))
            return True
        except OSError:
            return False


def _rebase_if_orphaned(vm_dir: Path) -> None:
    """Repoint a disk whose backing file moved.

    Each VM's disk is a thin overlay on a shared base image, and the path to
    that base is written into the overlay's header when it is created. Move the
    cache — renaming the project moved it from ~/.cache/pnl to ~/.cache/labtris
    — and every overlay built before the move points at nothing. QEMU then
    exits at launch with the reason in its own log and nowhere else. The base
    is content-addressed, so the same filename in the current cache is the same
    bytes: repoint the header and the VM is whole again."""
    disk = vm_dir / "disk.qcow2"
    if not disk.exists():
        return
    try:
        out = subprocess.run(
            ["qemu-img", "info", "--output=json", str(disk)],
            capture_output=True,
            timeout=30,
            check=True,
        )
        backing = json.loads(out.stdout).get("backing-filename")
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    if not backing or Path(backing).exists():
        return
    candidate = _cache_dir() / Path(backing).name
    if not candidate.exists():
        return
    try:
        subprocess.run(
            ["qemu-img", "rebase", "-u", "-F", "qcow2", "-b", str(candidate), str(disk)],
            capture_output=True,
            timeout=60,
            check=True,
        )
        logger.info("qemu.disk.rebased", vm=vm_dir.name, was=backing, now=str(candidate))
    except (OSError, subprocess.SubprocessError):
        return


async def _await_monitor(mon_sock: Path, proc: asyncio.subprocess.Process) -> bool:
    """True once the monitor accepts a connection; False if QEMU died first.

    Budget: 60s. The old 10s was fine for SeaBIOS+virtio (monitor ready
    almost instantly), but a template with a companion BIOS (OVMF-sata
    for NX-OSv) can spend 20-30s in firmware init before qemu opens the
    monitor for business — and Labtris was killing the process for
    "monitor timeout" while OVMF was still probing PCI. False positives
    from a long budget are cheap; false negatives (killing a live boot)
    look to the user like the VM boot-loops for no reason."""
    for _ in range(600):
        if proc.returncode is not None:
            return False
        if mon_sock.exists():
            try:
                _, writer = await asyncio.open_unix_connection(str(mon_sock))
                writer.close()
                return True
            except (ConnectionRefusedError, OSError):
                pass
        await asyncio.sleep(0.1)
    return proc.returncode is None


#: How long a VM must stay up after its monitor answers before start() calls it
#: started. QEMU opens the monitor before it realizes devices, so a VM that
#: dies on a bad drive ("Block node is read-only", labtris-012 2026-11-12)
#: passes the monitor check and exits a few milliseconds later.
SETTLE_S = 2.0


async def _await_settled(proc: Any, seconds: float = SETTLE_S) -> bool:
    """True if the process is still alive after `seconds`."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if proc.returncode is not None:
            return False
        await asyncio.sleep(0.1)
    return proc.returncode is None


#: Where qemu.log stood when each VM was last launched. The log is appended to
#: across boots, so without this an exit is "explained" by a line from an
#: earlier boot.
_log_starts: dict[Path, int] = {}


def _last_qemu_error(vm_dir: Path) -> str:
    """The most useful line QEMU wrote, this launch, before giving up."""
    try:
        with open(vm_dir / "qemu.log", "rb") as f:
            f.seek(_log_starts.get(vm_dir, 0))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return "no output"
    # The serial chardev announces itself on every boot; it is not an error.
    lines = [ln for ln in lines if "waiting for connection" not in ln]
    for line in reversed(lines):
        if line.startswith("qemu-system") or "rror" in line:
            return line.strip()[:400]
    return lines[-1].strip()[:400] if lines else "no output"


def _configured_vnc_port(vm_dir: Path) -> int | None:
    cfg_path = _config_path(vm_dir)
    if not cfg_path.exists():
        return None
    display = json.loads(cfg_path.read_text()).get("vnc_display")
    return 5900 + display if isinstance(display, int) else None


async def vnc_port(vm_dir: Path) -> int | None:
    """Where this VM's VNC server actually is.

    config.json holds the display the VM will use *next* boot, and it can be
    rewritten while the VM runs — so trusting it means the console can be
    pointed at a port nothing is listening on, which the browser reports only
    as a silent disconnect. A running VM is asked instead: `info vnc` is the
    address QEMU actually bound. The file is the fallback for a VM that is not
    up, where there is nothing to ask."""
    if _read_pid(vm_dir) is not None:
        try:
            out = await _hmp(vm_dir, "info vnc", wait=0.6)
        except Exception:  # noqa: BLE001 - a stale monitor just means fall back
            out = ""
        # "  Server: 127.0.0.1:5901 (ipv4)" — and "Server: disabled" when the
        # VM was started without one.
        found = re.search(r"Server:\s*\S+:(\d+)", _strip_ansi(out))
        if found:
            return int(found.group(1))
    return _configured_vnc_port(vm_dir)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(vm_dir: Path) -> int | None:
    pid_file = vm_dir / "qemu.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _pid_alive(pid) else None


class SerialSession:
    """Persistent connection to a VM's serial chardev, opened the moment the VM
    starts so boot output isn't dropped before a browser console attaches —
    the unix chardev discards writes while nobody is connected, same idea as
    netd's tcpdump ring buffer applied to the console instead of a capture."""

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        #: Bounded by bytes, not by chunk count: 4000 chunks of 4 KB would be
        #: 16 MB per node, and a hundred nodes is not a rounding error. This is
        #: the only place console history lives — nothing is written to disk.
        self.history: deque[bytes] = deque()
        self._history_bytes = 0
        self._history_max = 256 * 1024
        self._subs: list[asyncio.Queue[bytes]] = []
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        #: Set while the chardev is connected. start() only launches the pump,
        #: and the socket opens a moment later.
        self._connected = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        reader = None
        for _ in range(50):
            if self.sock_path.exists():
                try:
                    reader, writer = await asyncio.open_unix_connection(str(self.sock_path))
                    self._writer = writer
                    self._connected.set()
                    break
                except OSError:
                    pass
            await asyncio.sleep(0.2)
        if reader is None:
            return
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                self.history.append(chunk)
                self._history_bytes += len(chunk)
                while self._history_bytes > self._history_max and len(self.history) > 1:
                    self._history_bytes -= len(self.history.popleft())
                for q in list(self._subs):
                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            self._writer = None
            self._connected.clear()

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def buffered(self) -> bytes:
        return b"".join(self.history)

    async def send(self, data: bytes, wait_s: float = 10.0) -> None:
        """Write to the guest's console, waiting for the socket if it is
        still opening. Right after an API restart the session is re-attached
        and the first command arrived before the connection did: it was
        dropped without a word and the caller read 5 s of nothing
        (labtris-013, 2026-11-13)."""
        if self._writer is None and self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass
        if self._writer is None:
            raise runtime_error("the VM's serial console is not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._writer = None


_sessions: dict[str, SerialSession] = {}
# asyncio.subprocess.Process.__del__ kills the child if the transport is
# garbage-collected without being waited on — even with start_new_session=True,
# which only detaches the OS process group, not asyncio's own lifecycle
# tracking. Keeping a reference here is what makes "detached" actually stick.
_procs: dict[str, asyncio.subprocess.Process] = {}

#: One task per running VM that waits for its QEMU to exit. stop() cancels it
#: first, so the task only ever finishes for an exit Labtris did not ask for:
#: a crash, or the guest powering itself off.
_watchers: dict[str, asyncio.Task[None]] = {}
ExitListener = Callable[[str, "int | None", str], Awaitable[None]]
_exit_listeners: list[ExitListener] = []


def on_unexpected_exit(fn: ExitListener) -> None:
    """Call fn(node_id, returncode, reason) when a VM exits by itself.

    lifecycle registers one at startup to mark the node stopped; without it a
    node whose QEMU died kept saying "running" until the API restarted."""
    if fn not in _exit_listeners:
        _exit_listeners.append(fn)


async def _watch_exit(node_id: str, vm_dir: Path, proc: Any, pid: int | None) -> None:
    if proc is not None:
        rc: int | None = await proc.wait()
    else:
        # A VM this process did not launch (it survived an API restart): no
        # child to wait on, so ask the kernel now and then.
        while pid is not None and _pid_alive(pid):
            await asyncio.sleep(5)
        rc = None
    if _watchers.get(node_id) is asyncio.current_task():
        _watchers.pop(node_id, None)
    _procs.pop(node_id, None)
    (vm_dir / "qemu.pid").unlink(missing_ok=True)
    session = _sessions.pop(node_id, None)
    if session is not None:
        with contextlib.suppress(Exception):
            await session.close()
    _qmp_conns.pop(vm_dir, None)
    reason = "" if rc == 0 else _last_qemu_error(vm_dir)
    if reason == "no output":
        reason = ""
    logger.warning("qemu.exited", node=node_id, returncode=rc, reason=reason)
    for fn in list(_exit_listeners):
        try:
            await fn(node_id, rc, reason)
        except Exception as exc:  # noqa: BLE001 - one listener must not stop the rest
            logger.warning("qemu.exit_listener_failed", node=node_id, error=str(exc))


def _watch(node_id: str, vm_dir: Path, proc: Any, pid: int | None) -> None:
    old = _watchers.pop(node_id, None)
    if old is not None:
        old.cancel()
    _watchers[node_id] = asyncio.create_task(
        _watch_exit(node_id, vm_dir, proc, pid), name=f"qemu-exit-{node_id}"
    )


def adopt(node_id: str, vm_dir: Path) -> bool:
    """Watch a VM that was already running when this API process started.

    start() watches what it launches; a VM that outlived an API restart was
    launched by the previous process, so without this its later crash went
    unnoticed until the next restart. True if there was a live VM to watch."""
    pid = _read_pid(vm_dir)
    if pid is None:
        return False
    if node_id not in _watchers:
        _watch(node_id, vm_dir, _procs.get(node_id), pid)
    return True


def _unwatch(node_id: str) -> None:
    task = _watchers.pop(node_id, None)
    if task is not None:
        task.cancel()


def get_session(node_id: str) -> SerialSession | None:
    return _sessions.get(node_id)


async def attach_session(node_id: str, vm_dir: Path) -> SerialSession | None:
    """Get (or re-open) the serial session for a VM that is already running.

    The session lives in this process's memory, but the VM does not: restart
    the API and every running guest lost its console, with the only advertised
    remedy being to restart the node — rebooting a healthy machine because a
    web process bounced. QEMU's unix chardev takes one connection at a time and
    the old one died with the old process, so the socket is free to re-open.
    The scrollback starts from now, since nothing was listening in between."""
    existing = _sessions.get(node_id)
    if existing is not None:
        return existing
    if _read_pid(vm_dir) is None:
        return None
    sock = vm_dir / "serial.sock"
    if not sock.exists():
        return None
    session = SerialSession(sock)
    try:
        await session.start()
    except OSError:
        return None
    _sessions[node_id] = session
    return session


# HMP echoes each keystroke back with cursor-movement escapes, so a reply
# reads as "i[K[Din[K[D[Din..." before the part anyone wants.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b.|\[K|\[D")


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def resolve_accel(setting: str | None = None) -> str:
    """The accelerator a VM will actually start with.

    "auto" becomes "kvm" when /dev/kvm exists and this process may open it,
    and "tcg" otherwise; an explicit value is returned as given. Resolved at
    start and written into the VM's config.json, so what a running node got
    is a recorded fact rather than an inference from a setting that may have
    changed since.
    """
    want = (setting if setting is not None else settings.qemu_accel) or "auto"
    if want != "auto":
        return want
    kvm = Path("/dev/kvm")
    return "kvm" if kvm.exists() and os.access(kvm, os.W_OK) else "tcg"


async def _hmp(vm_dir: Path, command: str, wait: float = 0.4) -> str:
    """Send one Human Monitor Protocol command and return the raw reply text."""
    mon = vm_dir / "mon.sock"
    if not mon.exists():
        raise runtime_error("qemu monitor socket not found — is the VM running?")
    try:
        reader, writer = await asyncio.open_unix_connection(str(mon))
    except (ConnectionRefusedError, OSError) as exc:
        # The file outlives the process, so its presence says nothing about
        # whether anyone is listening.
        raise runtime_error(
            f"the VM is not running — its monitor socket is stale: {_last_qemu_error(vm_dir)}"
        ) from exc
    try:
        writer.write(b"\n")
        await writer.drain()
        try:
            await asyncio.wait_for(reader.read(4096), timeout=0.3)
        except TimeoutError:
            pass
        writer.write(command.encode() + b"\n")
        await writer.drain()
        await asyncio.sleep(wait)
        try:
            data = await asyncio.wait_for(reader.read(8192), timeout=1.5)
        except TimeoutError:
            data = b""
        return data.decode(errors="replace")
    finally:
        writer.close()


# --------------------------------------------------------------- QMP
#
# QMP is JSON-RPC over a Unix socket. Used for the commands HMP doesn't
# expose cleanly — currently just `input-send-event` for absolute mouse
# coordinates. On startup, QMP sends a `QMP` greeting; we must reply
# with `qmp_capabilities` before any other command is accepted.


# One persistent QMP connection per VM. Mouse tools call QMP two to
# four times per click (move + button down + button up + optional
# after-shot); reconnecting and re-handshaking each time cost ~100ms
# per call. Cached, the second and later calls are ~1-2ms round trip.
# Reset on error (broken pipe, EOF): the next call reconnects.
_qmp_conns: dict[Path, tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.Lock]] = {}


async def _qmp_open(vm_dir: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.Lock]:
    """Return a live QMP (reader, writer, lock) for this VM, opening one if
    necessary. Handshake is done once per connection; subsequent callers
    reuse the same stream serialized by the lock."""
    cached = _qmp_conns.get(vm_dir)
    if cached is not None:
        _, writer, _ = cached
        if not writer.is_closing():
            return cached
        # Fell out from under us — cook a new one below.
        _qmp_conns.pop(vm_dir, None)

    sock = vm_dir / "qmp.sock"
    if not sock.exists():
        raise runtime_error(
            "qemu QMP socket not found — this VM was started before mouse "
            "support landed; stop and start it to gain the socket."
        )
    reader, writer = await asyncio.open_unix_connection(str(sock))
    # Greeting + handshake.
    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
    if not line:
        raise runtime_error("QMP closed the connection before greeting")
    writer.write(b'{"execute":"qmp_capabilities"}\n')
    await writer.drain()
    _ = await asyncio.wait_for(reader.readline(), timeout=2.0)
    lock = asyncio.Lock()
    entry = (reader, writer, lock)
    _qmp_conns[vm_dir] = entry
    return entry


async def _qmp(
    vm_dir: Path,
    command: str,
    arguments: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> Any:
    """Send one QMP command and return the `return` payload (or raise on error).

    Uses a per-VM persistent connection cached in `_qmp_conns` — the
    handshake happens once per VM, not per call. A lock serializes
    writers so overlapping calls from different asyncio tasks don't
    interleave. On any I/O error the cached connection is dropped and
    the next call reopens.
    """
    reader, writer, lock = await _qmp_open(vm_dir)
    async with lock:
        try:
            payload = {"execute": command}
            if arguments:
                payload["arguments"] = arguments
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if not raw:
                    raise runtime_error("QMP closed the connection before replying")
                msg = json.loads(raw)
                if "return" in msg:
                    return msg["return"]
                if "error" in msg:
                    err = msg["error"]
                    raise runtime_error(f"QMP {command}: {err.get('desc', err)}")
                # Event — keep reading.
        except (TimeoutError, ConnectionError, OSError):
            _qmp_conns.pop(vm_dir, None)
            with contextlib.suppress(Exception):
                writer.close()
            raise


#: Snapshotting a VM with real memory in it is not a sub-second operation,
#: and the only honest timeout is one longer than any lab node should take.
_SNAPSHOT_TIMEOUT = 900.0


async def _hmp_qmp(vm_dir: Path, command: str, timeout: float = 10.0) -> str:
    """Run one HMP command through QMP and return just its output.

    The raw monitor socket echoes back what you "type", character by
    character, with cursor-movement escapes between them. A reply read from
    it therefore contains the command as well as the answer — which is how
    `savevm before-error-repro` came to be reported as a failure while
    succeeding: the check looked for "error" and found it in the echo of the
    name. `human-monitor-command` returns the command's own output and
    nothing else.

    It also returns only once the command has finished, which is the other
    half of the problem: the old path slept a fixed interval and then read
    whatever had arrived, so a long savevm reported success while QEMU was
    still writing.
    """
    out = await _qmp(vm_dir, "human-monitor-command", {"command-line": command},
                     timeout=timeout)
    return out if isinstance(out, str) else ""


#: Cursor movement, erase-to-end and friends, which the raw monitor socket
#: sprays between the characters it echoes back.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")


def _hmp_error(out: str) -> str | None:
    """The error line from an HMP reply, or None if it succeeded.

    QEMU prefixes monitor failures with "Error"; a command that worked
    usually says nothing at all. Matched per line and at the start of one,
    which is what separates a diagnosis from a mention: `savevm
    before-error-repro` echoes the word back without having failed.

    Escape sequences are stripped first so this is equally correct on the raw
    monitor socket, where the echoed command arrives interleaved with cursor
    movement, and on `human-monitor-command`, where it does not.
    """
    for raw in out.splitlines():
        line = _ANSI.sub("", raw).strip()
        if line.lower().startswith(("error", "unable")):
            return line
    return None


# QMP's tablet axis range is fixed at 0..32767 regardless of framebuffer
# size — the display maps it to pixels internally. Caller supplies
# framebuffer-space coordinates plus the framebuffer dimensions; we
# scale.
_QMP_ABS_MAX = 32767


def _png_dims(data: bytes) -> tuple[int, int]:
    """Read (width, height) from a PNG's IHDR chunk. Bytes 16-24."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise runtime_error("screendump did not return a PNG")
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return w, h


async def fb_dims(vm_dir: Path) -> tuple[int, int]:
    """Query the current framebuffer size via a throwaway screenshot.

    QMP has no cheap way to ask; `query-display-options` and
    `query-vnc-servers` return everything except dimensions. Taking a
    screenshot and reading the PNG header is ~5 KB + 50 ms and always
    works.

    Explicitly `scale=1.0` — mouse coordinate translation needs the real
    framebuffer size, not a scaled-down preview.
    """
    return _png_dims(await screendump_png(vm_dir, scale=1.0))


async def wait_for_screen_change(
    vm_dir: Path,
    *,
    poll_ms: int = 200,
    stable_ms: int = 400,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Poll the framebuffer until it stops changing, then return the final PNG.

    Every `poll_ms` we take a screenshot and compare its SHA-1 to the
    previous one. Once the same hash has been seen for `stable_ms`
    consecutively (i.e. `stable_ms // poll_ms` iterations), we call it
    stable and return. If `timeout_ms` elapses first, we return whatever
    the latest frame is with `settled=false`.

    This replaces the "screenshot in a poll loop" pattern the model
    naturally falls into after a click or a keystroke: one call blocks
    on the backend and returns a single result instead of five round
    trips through the LLM.
    """
    poll_s = poll_ms / 1000
    stable_needed = max(1, stable_ms // poll_ms)
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
    last_hash: str | None = None
    stable_count = 0
    last_png: bytes = b""
    while True:
        last_png = await screendump_png(vm_dir, scale=1.0)
        h = hashlib.sha1(last_png).hexdigest()
        if h == last_hash:
            stable_count += 1
            if stable_count >= stable_needed:
                return {"png": last_png, "settled": True, "polls": stable_count}
        else:
            stable_count = 0
            last_hash = h
        if asyncio.get_event_loop().time() >= deadline:
            return {"png": last_png, "settled": False, "polls": stable_count}
        await asyncio.sleep(poll_s)


def ocr_image_bytes(png: bytes) -> str:
    """OCR the given PNG and return the recognized text.

    Lazy import of pytesseract so the API starts even without it. Raises
    a runtime error with the fix if pytesseract or the tesseract binary
    is missing — the assistant can then fall back to a full screenshot.
    """
    try:
        import pytesseract  # type: ignore[import-untyped]
    except ImportError as exc:
        raise runtime_error(
            "pytesseract not installed. `sudo apt install tesseract-ocr "
            "tesseract-ocr-eng` and `pip install pytesseract` in the venv."
        ) from exc
    from io import BytesIO

    from PIL import Image

    try:
        img = Image.open(BytesIO(png))
        return pytesseract.image_to_string(img)
    except pytesseract.TesseractNotFoundError as exc:
        raise runtime_error(
            "tesseract binary not found. `sudo apt install tesseract-ocr "
            "tesseract-ocr-eng`."
        ) from exc


async def vnc_mouse_move(vm_dir: Path, x: int, y: int, fb_w: int, fb_h: int) -> None:
    """Move the pointer to (x, y) in framebuffer pixels."""
    ax = round(x * _QMP_ABS_MAX / max(1, fb_w - 1))
    ay = round(y * _QMP_ABS_MAX / max(1, fb_h - 1))
    ax = max(0, min(_QMP_ABS_MAX, ax))
    ay = max(0, min(_QMP_ABS_MAX, ay))
    await _qmp(
        vm_dir,
        "input-send-event",
        {
            "events": [
                {"type": "abs", "data": {"axis": "x", "value": ax}},
                {"type": "abs", "data": {"axis": "y", "value": ay}},
            ]
        },
    )


async def vnc_mouse_click(
    vm_dir: Path,
    x: int,
    y: int,
    fb_w: int,
    fb_h: int,
    button: str = "left",
    double: bool = False,
) -> None:
    """Move to (x, y) and click.  button ∈ {left, right, middle}."""
    if button not in ("left", "right", "middle"):
        raise unprocessable(f"unknown mouse button {button!r}")
    await vnc_mouse_move(vm_dir, x, y, fb_w, fb_h)
    for _ in range(2 if double else 1):
        await _qmp(
            vm_dir,
            "input-send-event",
            {"events": [{"type": "btn", "data": {"button": button, "down": True}}]},
        )
        await _qmp(
            vm_dir,
            "input-send-event",
            {"events": [{"type": "btn", "data": {"button": button, "down": False}}]},
        )


# --------------------------------------------------------------- VNC helpers
#
# QEMU exposes two HMP commands that together let us drive a graphical
# console: `screendump filename -f png` captures the current framebuffer,
# `sendkey <combo>` types one key at a time. Both are older than time,
# universally present, and cost us zero new dependencies. The alternative
# — talking RFB from a Python client — would need a library we do not
# ship and would compete with Guacamole for the same port.


async def screendump_png(
    vm_dir: Path,
    *,
    region: tuple[int, int, int, int] | None = None,
    grid: int | bool = False,
    scale: float = 0.5,
) -> bytes:
    """Grab the QEMU display as PNG bytes.

    `screendump <path> -f png` in a temp file next to the VM's own state
    directory (labtris owns it; /tmp under systemd hardening might not
    work), then read + delete. QEMU writes synchronously inside the
    command dispatch, so the file is complete by the time HMP returns
    the prompt.

    `region=(x, y, w, h)` crops after capture — useful when the model
    wants to look at one dialog on a 1080p display without spending
    tokens on the whole frame.

    `grid=True` overlays a 100-pixel grid with axis labels so the model
    can name coordinates. Pass an int for a custom pitch (`grid=50`).

    `scale=0.5` (default) downscales the final image by 50%. A 1280x800
    framebuffer becomes 640x400 — the model still reads a `login:`
    prompt just as well but pays roughly a quarter of the vision
    tokens per screenshot. Pass `scale=1.0` for full resolution.

    Any of `region`, `grid`, or a non-1.0 `scale` requires Pillow.
    """
    if not vm_dir.exists() or _read_pid(vm_dir) is None:
        raise runtime_error("VM is not running")

    # RFB cache path. A persistent VNC client per VM keeps the current
    # framebuffer in memory and hands out screenshots in ~1 ms. First
    # call for a VM opens the connection and waits for the initial
    # frame; subsequent calls are memcache-fast. Any failure — protocol
    # error, port not yet listening, cache eviction — falls back to the
    # HMP screendump path below, which always works.
    raw: bytes | None = None
    port = _configured_vnc_port(vm_dir)
    if port is not None:
        try:
            from labtris_api.runtime.vnc_cache import get_cached_client

            client = await get_cached_client(vm_dir, port, node_id=vm_dir.name)
            raw = await client.screenshot_png()
        except Exception as exc:  # noqa: BLE001 — any cache issue falls through
            logger.debug("vnc cache miss, falling back to screendump", exc_info=exc)
            raw = None

    if raw is None:
        dest = vm_dir / f".shot-{os.urandom(4).hex()}.png"
        try:
            # 200ms is empirically fine for a 1280x800 dump — measured
            # ~50ms on the reference host. The old 1s was defensive
            # against long-loading BIOS displays, but every screenshot
            # paid the penalty.
            await _hmp(vm_dir, f"screendump {dest} -f png", wait=0.2)
            if not dest.exists() or dest.stat().st_size == 0:
                raise runtime_error("screendump produced no file — is the display attached?")
            raw = dest.read_bytes()
        finally:
            with contextlib.suppress(FileNotFoundError):
                dest.unlink()

    if region is None and not grid and abs(scale - 1.0) < 0.01:
        return raw

    # Only import Pillow when the caller asks for a transformation —
    # the common "raw framebuffer" path stays dep-free at call time.
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.open(BytesIO(raw))
    if region is not None:
        x, y, w, h = region
        img = img.crop((x, y, x + w, y + h))
    if scale > 0 and abs(scale - 1.0) >= 0.01:
        w, h = img.size
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    if grid:
        pitch = 100 if grid is True else int(grid)
        # Grid coordinates are drawn in the CURRENT (post-scale) pixel
        # space, so `vnc_mouse` clicks the model derives from them line
        # up when the caller uses the same scale.
        overlay = img.convert("RGB").copy()
        d = ImageDraw.Draw(overlay)
        gw, gh = overlay.size
        # Semi-transparent black lines are the least distracting — draw
        # with a mid-grey stroke that reads on both dark and light
        # backgrounds without dominating the image.
        for gx in range(0, gw, pitch):
            d.line([(gx, 0), (gx, gh)], fill=(160, 160, 160), width=1)
            d.text((gx + 2, 2), str(gx), fill=(160, 160, 160))
        for gy in range(0, gh, pitch):
            d.line([(0, gy), (gw, gy)], fill=(160, 160, 160), width=1)
            d.text((2, gy + 2), str(gy), fill=(160, 160, 160))
        img = overlay
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


#: Map printable ASCII → QEMU HMP key name. Missing entries either need
#: shift (upper-case letters, most punctuation) or are just the character
#: itself lower-cased.
_ASCII_TO_HMP: dict[str, str] = {
    " ": "spc",
    "\n": "ret",
    "\r": "ret",
    "\t": "tab",
    "-": "minus",
    "=": "equal",
    "[": "bracket_left",
    "]": "bracket_right",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "dot",
    "/": "slash",
    "\\": "backslash",
    "`": "grave_accent",
}
#: The shifted layer — what a US-ASCII keyboard produces with shift held.
_SHIFTED: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "minus", "+": "equal",
    "{": "bracket_left", "}": "bracket_right",
    ":": "semicolon", '"': "apostrophe",
    "<": "comma", ">": "dot", "?": "slash",
    "|": "backslash", "~": "grave_accent",
}


def _text_to_sendkeys(text: str) -> list[str]:
    """Break a string into a list of HMP `sendkey` combos.

    Each element is one command payload — `a`, `shift-a`, `spc`, `ret`,
    `ctrl-c`. Callers that want raw HMP combos (`ctrl-alt-f2`, `esc`)
    should build the list themselves; this only handles the "type this
    string as if a human were typing" case.
    """
    keys: list[str] = []
    for ch in text:
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            keys.append(ch)
        elif "A" <= ch <= "Z":
            keys.append(f"shift-{ch.lower()}")
        elif ch in _ASCII_TO_HMP:
            keys.append(_ASCII_TO_HMP[ch])
        elif ch in _SHIFTED:
            keys.append(f"shift-{_SHIFTED[ch]}")
        else:
            # Unrepresentable in the sendkey vocabulary (non-ASCII,
            # control chars). Skip rather than fail — a hex dump in
            # the reply for one weird byte is worse than typing what
            # we can.
            continue
    return keys


async def vnc_send_text(vm_dir: Path, text: str, hold_ms: int = 40) -> int:
    """Type `text` into the guest via HMP `sendkey`. Returns keys sent.

    ASCII only; non-ASCII characters are skipped. This is intended for
    passwords, hostnames, and short interactive answers — not for
    pasting a config file. Every keystroke round-trips through HMP,
    which is roughly 20ms; a 100-character password is ~2s of typing.
    """
    if not vm_dir.exists() or _read_pid(vm_dir) is None:
        raise runtime_error("VM is not running")
    keys = _text_to_sendkeys(text)
    for k in keys:
        await _hmp(vm_dir, f"sendkey {k} {hold_ms}", wait=0.05)
    return len(keys)


async def vnc_send_keys(vm_dir: Path, keys: list[str], hold_ms: int = 40) -> int:
    """Send raw HMP `sendkey` combos: `ctrl-alt-f2`, `esc`, `f1`, `ret`.

    Trusts the caller — a bad combo is refused by HMP and the exception
    surfaces. Returns keys sent.
    """
    if not vm_dir.exists() or _read_pid(vm_dir) is None:
        raise runtime_error("VM is not running")
    for k in keys:
        await _hmp(vm_dir, f"sendkey {k} {hold_ms}", wait=0.05)
    return len(keys)


def _config_path(vm_dir: Path) -> Path:
    return vm_dir / "config.json"


def _seed_path(vm_dir: Path) -> Path:
    return vm_dir / "seed.iso"


#: vhost-net moves the virtio-net data path out of QEMU's main loop and into a
#: kernel thread, which is most of the throughput difference between a lab NIC
#: and a real one. It needs the character device open-able by *this* process:
#: the one that spawns qemu, unprivileged. Checked per start rather than once,
#: because the compose file and the installer are where the device gets
#: granted, and a host that gains it should not need an API restart.
_VHOST_NET = Path("/dev/vhost-net")


def vhost_available() -> bool:
    return _VHOST_NET.exists() and os.access(_VHOST_NET, os.R_OK | os.W_OK)


def nic_queue_count(cfg: dict[str, Any]) -> int:
    """How many virtio queue pairs this VM's NICs get, from its config.json.

    1 unless nic_queues is set above it *and* the model is virtio: e1000 and
    the rest have no multi-queue, and QEMU rejects `mq=on` on them outright
    rather than ignoring it."""
    try:
        q = int(cfg.get("nic_queues") or 1)
    except (TypeError, ValueError):
        q = 1
    model = cfg.get("nic_model") or "virtio-net-pci"
    if not model.startswith("virtio-net"):
        return 1
    return max(1, min(q, MAX_NIC_QUEUES))


def port_nic(cfg: dict[str, Any], model: str | None) -> tuple[str, int]:
    """One port's card and its queue count: the port's own model when it has
    one, else the node's. Queues follow the card, so an e1000 port on a
    four-queue virtio node gets one, and its tap is made single-queue."""
    model = model or cfg.get("nic_model") or "virtio-net-pci"
    return model, nic_queue_count({**cfg, "nic_model": model})


def netdev_args(net_id: str, host_ifname: str, queues: int, vhost: bool) -> str:
    """The -netdev / netdev_add argument string, one function for both so the
    cold-boot NIC and a hot-plugged one cannot drift apart."""
    s = f"tap,id={net_id},ifname={host_ifname},script=no,downscript=no"
    if queues > 1:
        s += f",queues={queues}"
    if vhost:
        s += ",vhost=on"
    return s


def device_args(
    model: str, net_id: str, mac: str, nic_id: str, queues: int, viommu: bool = False
) -> str:
    """The -device / device_add argument string. `vectors` is the MSI-X count
    virtio-net wants for N queue pairs: one per RX queue, one per TX queue,
    one for config and one for control — 2N+2.

    Under a vIOMMU a virtio NIC is only *behind* the IOMMU if it says so:
    `iommu_platform=on` makes the device honour the guest's DMA mappings
    (otherwise vfio in the guest binds it, DPDK programs IOVAs, and the
    device DMAs to the untranslated address). `ats=on` lets vhost cache
    translations through the device IOTLB. Both are refused on the default
    transitional device — "VIRTIO_F_IOMMU_PLATFORM was supported by neither
    legacy nor transitional device", QEMU 10 — hence `disable-legacy=on`,
    which makes it a modern-only device (PCI 1af4:1041 instead of 1000).
    Any kernel since 4.x and DPDK's virtio PMD drive both IDs."""
    s = f"{model},netdev={net_id},mac={mac},id={nic_id}"
    if queues > 1:
        s += f",mq=on,vectors={2 * queues + 2}"
    if viommu and model.startswith("virtio-net"):
        s += ",iommu_platform=on,ats=on,disable-legacy=on"
    return s


#: Id prefix of the spare PCIe slots on a q35 VM (`lt-hp0`, `lt-hp1`, ...).
HOTPLUG_PORT_PREFIX = "lt-hp"
#: Chassis numbers for those slots. Every pcie-root-port needs a unique
#: (chassis, slot) pair; starting high keeps clear of any root port a
#: template's extra arguments add, which by convention count up from 1.
_HOTPLUG_CHASSIS_BASE = 200


def is_q35(opts: dict[str, Any]) -> bool:
    return str(opts.get("machine") or "pc").split(",")[0] == "q35"


def hotplug_slot_count(opts: dict[str, Any]) -> int:
    """How many spare PCIe slots this VM boots with: only q35 needs them."""
    if not is_q35(opts):
        return 0
    try:
        n = int(opts.get("hotplug_slots", 4))
    except (TypeError, ValueError):
        n = 4
    return max(0, min(n, 32))


def hotplug_port_args(opts: dict[str, Any]) -> list[str]:
    """Empty pcie-root-ports for a q35 VM to hot-plug NICs into.

    A NIC present at boot sits on `pcie.0`. PCIe treats devices there as part
    of the chipset, so QEMU cannot add or remove one while the VM runs, and
    `device_del` on it does nothing at all. A root port is a slot with a
    hot-plug controller: a card added to it appears in the guest, and one
    removed from it goes away.

    Appended after every other device, so the PCI addresses of devices that
    were already there do not move: a guest whose configuration names its
    NICs by PCI address (DPDK, VPP) still finds them where they were."""
    out: list[str] = []
    for n in range(hotplug_slot_count(opts)):
        out += [
            "-device",
            f"pcie-root-port,id={HOTPLUG_PORT_PREFIX}{n},"
            f"chassis={_HOTPLUG_CHASSIS_BASE + n},slot=0",
        ]
    return out


_NET_LINE = re.compile(
    r"^\s*(?:\\\s*)?(?P<id>[\w.-]+):\s*index=\d+,type=(?P<type>\w+),(?P<rest>.*)$"
)


def parse_info_network(text: str) -> dict[str, dict[str, Any]]:
    """`info network` as {nic id: {"netdev": id, "ifname": tap or None}}.

    QEMU prints each NIC with its backend indented under it:

        nic6: index=0,type=nic,model=virtio-net-pci,macaddr=52:54:00:..
         \\ net6: index=0,type=tap,ifname=vperffeth6-1du2,script=no,..

    A NIC whose backend was deleted prints with no line under it."""
    out: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        m = _NET_LINE.match(line)
        if not m:
            continue
        kind, rest = m.group("type"), m.group("rest")
        is_peer = line.lstrip().startswith("\\")
        if kind == "nic" and not is_peer:
            current = m.group("id")
            out[current] = {"netdev": None, "ifname": None}
        elif is_peer and current is not None:
            out[current]["netdev"] = m.group("id")
            fm = re.search(r"(?:^|,)ifname=([^,]+)", rest)
            out[current]["ifname"] = fm.group(1) if fm else None
        elif not is_peer:
            current = None
    return out


#: Per-boot record of which spare slot holds which hot-plugged NIC.
_HOTPLUG_STATE = "hotplug.json"


def _hotplug_ports_used(vm_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((vm_dir / _HOTPLUG_STATE).read_text())
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _free_hotplug_port(vm_dir: Path, slots: int) -> str | None:
    used = set(_hotplug_ports_used(vm_dir).values())
    for n in range(slots):
        port = f"{HOTPLUG_PORT_PREFIX}{n}"
        if port not in used:
            return port
    return None


def _record_hotplug_port(vm_dir: Path, nic_id: str, port: str | None) -> None:
    data = _hotplug_ports_used(vm_dir)
    if port is None:
        data.pop(nic_id, None)
    else:
        data[nic_id] = port
    (vm_dir / _HOTPLUG_STATE).write_text(json.dumps(data))


async def _inspect_tap(name: str) -> dict[str, Any]:
    try:
        got = await netd.call("iface.inspect", {"names": [name]})
    except NetdError:
        return {}
    return dict((got.get("interfaces") or {}).get(name) or {})


async def _ensure_tap(
    vm_dir: Path, name: str, fresh: bool = False, multi_queue: bool | None = None,
    model: str | None = None,
) -> None:
    """Make sure the tap exists; with `fresh`, replace it."""
    if multi_queue is None:
        cfg_path = _config_path(vm_dir)
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        multi_queue = port_nic(cfg, model)[1] > 1
    if fresh:
        await _drop_tap(name)
    elif (await _inspect_tap(name)).get("exists"):
        return
    await netd.call(
        "tap.create", {"name": name, "owner_uid": os.getuid(), "multi_queue": multi_queue}
    )


async def _drop_tap(name: str) -> None:
    with contextlib.suppress(NetdError):
        await netd.call("iface.delete", {"name": name})


async def _wire_tap(name: str, bridge: str, current: str | None) -> None:
    """Move a tap onto `bridge`, off whatever it was on."""
    if current and current != bridge:
        with contextlib.suppress(NetdError):
            await netd.call("iface.detach", {"name": name})
    if current != bridge:
        await netd.call("iface.attach", {"name": name, "bridge": bridge})
    await netd.call("iface.set_state", {"name": name, "up": True})


#: How long a hot-unplug may take before it counts as refused. Linux guests
#: acknowledge in well under a second; the rest is margin.
UNPLUG_WAIT_S = 5.0


async def _unplug(vm_dir: Path, nic_id: str) -> bool:
    """Ask for the NIC to be removed and wait for it to be gone.

    `device_del` only *requests* removal; the guest acknowledges through the
    slot's hot-plug controller, which takes a moment. On a slot that cannot
    hot-unplug (q35's pcie.0) QEMU refuses or nothing ever happens, so the
    answer is whether the device actually disappeared."""
    out = await _hmp_qmp(vm_dir, f"device_del {nic_id}")
    if _hmp_error(out) is not None:
        return False
    deadline = asyncio.get_running_loop().time() + UNPLUG_WAIT_S
    while True:
        nics = parse_info_network(await _hmp_qmp(vm_dir, "info network"))
        if nic_id not in nics:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.25)


def machine_args(opts: dict[str, Any]) -> list[str]:
    """`-M` and, with the IOMMU on, the device that provides it.

    Interrupt remapping is what makes the guest's MSI-X work through the
    IOMMU, and QEMU refuses it with the in-kernel irqchip ("Interrupt
    Remapping cannot work with kernel-irqchip=on, please use 'split|off'"),
    so the machine gets kernel-irqchip=split. The intel-iommu device comes
    before every other PCI device on the command line, which is where QEMU
    wants it. caching-mode is what lets a vhost backend learn about the
    guest's mappings; device-iotlb pairs with ats=on on the NICs."""
    machine = str(opts.get("machine") or "pc")
    props = ""
    if opts.get("firmware") == "uefi-secure":
        if machine != "q35":
            raise unprocessable("firmware=uefi-secure needs the q35 chipset")
        props = ",smm=on"
    if not opts.get("viommu"):
        return ["-M", machine + props]
    if machine != "q35":
        raise unprocessable(
            "the emulated IOMMU needs the q35 chipset — intel-iommu is a "
            f"q35 device and the node's chipset is {machine!r}"
        )
    return [
        "-M", f"q35{props},kernel-irqchip=split",
        "-device", "intel-iommu,intremap=on,caching-mode=on,device-iotlb=on",
    ]


def _serialize_ifaces(ifaces: list[IfaceSpec]) -> list[dict[str, Any]]:
    """Persist the shape start() needs to emit -netdev/-device pairs.

    Kept minimal on purpose: idx, host_ifname, and mac are the only fields
    the qemu command line uses. peer_ifname/bridge are runtime concerns
    handled by netd and lifecycle.py, not the guest device."""
    return [
        {"idx": i.idx, "host_ifname": i.host_ifname, "mac": i.mac,
         **({"nic_model": i.nic_model} if i.nic_model else {})}
        for i in ifaces
        if i.host_ifname  # a name is required to bind a tap
    ]


#: What a node may set about its own machine. Everything is optional and the
#: defaults are what QEMU nodes did before this existed, so an untouched node
#: behaves exactly as it did.
#: What `-drive if=` actually accepts, measured against qemu-system-x86_64
#: 8.2 rather than assumed: virtio, ide, scsi, sd and none are taken; **sata,
#: nvme and usb are rejected outright** with "unsupported bus type".
#:
#: That matters because the vocabularies do not match. The GNS3 registry — and
#: EVE-NG's filename convention — happily say `sata`, and three of those names
#: would produce a command line qemu refuses to start, turning an imported
#: appliance into a node that dies at launch with a message about a bus nobody
#: chose.
#:
#: So the unrepresentable ones are mapped to the nearest bus that boots. sata
#: to ide because both are ATA and a guest that wants AHCI almost always still
#: has IDE support; real AHCI needs `-device ich9-ahci` plus `if=none`, which
#: is a larger change than this. nvme to virtio as the closest modern block
#: interface. An approximation, but a documented one — silently passing the
#: name through is how you get a VM that will not start.
DRIVE_IF: dict[str, str] = {
    "virtio": "virtio",
    "ide": "ide",
    "scsi": "scsi",
    "sd": "sd",
    "sata": "ide",
    "usb": "ide",
    "nvme": "virtio",
}


def drive_if(bus: str | None) -> str:
    """The `-drive if=` value for a bus name from any of our sources."""
    return DRIVE_IF.get((bus or "").strip().lower(), "virtio")


QEMU_OPTIONS: dict[str, dict[str, Any]] = {
    "machine": {
        "label": "Chipset",
        "type": "choice",
        "choices": ["pc", "q35"],
        "default": "pc",
        "help": "q35 gives PCIe and a modern chipset, which some appliances require. "
        "pc is the older i440FX and boots almost anything.",
    },
    "cpu": {
        "label": "CPU model",
        "type": "choice",
        "choices": ["qemu64", "max", "host"],
        "default": "qemu64",
        "help": "max exposes every feature this QEMU can emulate — slower under TCG "
        "but needed by guests that check for specific instructions. host requires KVM.",
    },
    "boot": {
        "label": "Boot order",
        "type": "choice",
        "choices": ["c", "d", "n", "dc"],
        "default": "c",
        "help": "c=disk, d=cdrom, n=network. dc tries the cdrom then the disk, "
        "which is what an install from ISO needs.",
    },
    "firmware": {
        "label": "Firmware",
        "type": "choice",
        # "" keeps what nodes always did: SeaBIOS, or the template's companion
        # -bios file when it has one.
        "choices": ["", "uefi", "uefi-secure"],
        "default": "",
        "help": "Blank boots SeaBIOS, or the template's BIOS file if it has one. "
        "uefi boots the host's OVMF with a variable store kept per node, so boot "
        "entries a guest writes (efibootmgr, an installer) survive a restart, and "
        "revert with a snapshot. uefi-secure adds Secure Boot with Microsoft keys "
        "enrolled and needs the q35 chipset. Wipe resets the variables.",
    },
    "disk_bus": {
        "label": "Disk controller",
        "type": "choice",
        # "" means "whatever the image says", which is the right default: the
        # catalog knows, and hard-coding virtio here would override it. Only
        # values qemu accepts as `-drive if=` are offered — see DRIVE_IF.
        "choices": ["", "virtio", "ide", "scsi"],
        "default": "",
        "help": "Leave blank to use the image's own setting. A guest whose kernel "
        "has no virtio-blk driver will not boot from a virtio disk — and it fails "
        "silently, at a blank console or a bootloader that never continues. If a "
        "VM will not boot and everything else checks out, try ide.",
    },
    "viommu": {
        "label": "Emulated IOMMU",
        "type": "bool",
        "default": False,
        "help": "An Intel VT-d IOMMU in the guest, with every virtio NIC behind "
        "it, so the guest can bind NICs to vfio-pci and run DPDK, VPP or "
        "TRex on them with a real IOMMU rather than unsafe no-IOMMU mode. "
        "Needs the q35 chipset, and the guest kernel booted with "
        "intel_iommu=on (iommu=pt is the usual companion). Set CPU model to "
        "host as well: DPDK refuses to start on qemu64, which lacks SSE4.2.",
    },
    "hotplug_slots": {
        "label": "Spare hot-plug slots (q35)",
        "type": "int",
        "default": 4,
        "min": 0,
        "max": 32,
        "help": "Empty PCIe slots a q35 VM boots with, so a network card can be "
        "added or removed while it runs. Cards present at boot sit on the "
        "chipset's own bus, which PCIe does not allow to change while running; "
        "only a card in one of these slots can come and go. Ignored on pc "
        "(i440FX), whose bus is hot-pluggable already. Takes effect at the next "
        "start.",
    },
    "usb_tablet": {
        "label": "Absolute pointer",
        "type": "bool",
        "default": True,
        "help": "Without a tablet the guest gets relative mouse deltas and the "
        "pointer drifts away from the browser's. Turn it off only if a guest "
        "cannot drive USB.",
    },
    "mgmt_nic": {
        "label": "Management NIC (user-mode)",
        "type": "bool",
        "default": False,
        "help": "Adds a NIC that NATs to the internet through the host, for "
        "installing packages. It is not part of the topology and captures on lab "
        "links will not see its traffic — which is why it is off by default.",
    },
    "data_volume": {
        "label": "Persistent data volume",
        "type": "bool",
        "default": False,
        "help": "A small disk kept outside the VM directory, so Wipe does not "
        "touch it. Mount it in the guest with `mount LABEL=LABTRIS /mnt`.",
    },
    "data_volume_mb": {
        "label": "Data volume size (MB)",
        "type": "int",
        "default": 256,
        "min": 16,
        "max": 65536,
        "help": "Only used when the data volume is enabled.",
    },
    "extra_args": {
        "label": "Extra QEMU arguments",
        "type": "list",
        "default": [],
        "help": "Appended verbatim to the command line. Disabled by default "
        "because arbitrary arguments are arbitrary code on this host.",
    },
}


def option_defaults() -> dict[str, Any]:
    return {k: v["default"] for k, v in QEMU_OPTIONS.items()}


def resolved_options(opts: dict[str, Any] | None) -> dict[str, Any]:
    """Node options over the defaults, ignoring anything unrecognised."""
    merged = option_defaults()
    for key, value in (opts or {}).items():
        if key in QEMU_OPTIONS:
            merged[key] = value
    return merged


def check_option_values(opts: dict[str, Any]) -> None:
    """Refuse values an option cannot take, at the moment someone sets them.

    Only the keys were checked before, so a typo in a value was saved and
    then ignored at every start, which reads as the option not working."""
    problems = []
    for key, value in opts.items():
        spec = QEMU_OPTIONS.get(key)
        if spec is None:
            continue
        kind = spec["type"]
        if kind == "choice" and value not in spec["choices"]:
            choices = ", ".join(repr(c) for c in spec["choices"])
            problems.append(f"{key}={value!r} is not one of {choices}")
        elif kind == "bool" and not isinstance(value, bool):
            problems.append(f"{key} must be true or false, not {value!r}")
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"{key} must be a whole number, not {value!r}")
            elif not spec.get("min", value) <= value <= spec.get("max", value):
                problems.append(f"{key} must be between {spec['min']} and {spec['max']}")
        elif kind == "list" and not isinstance(value, list):
            problems.append(f"{key} must be a list")
    merged = {**option_defaults(), **opts}
    if merged.get("firmware") == "uefi-secure" and merged.get("machine") != "q35":
        problems.append("firmware=uefi-secure needs machine=q35: Secure Boot relies on "
                        "SMM, which the pc chipset does not provide")
    if problems:
        raise unprocessable("; ".join(problems))


#: Split OVMF builds, code then variable template, in the places distributions
#: install them. Ubuntu and Debian: the ovmf package. Fedora: edk2-ovmf. The
#: combined OVMF.fd is not used: its variables live in the same file as the
#: code, which is why a read-only -bios forgets every boot entry.
_OVMF: dict[str, list[tuple[str, str]]] = {
    "uefi": [
        ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
        ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
        ("/usr/share/edk2/ovmf/OVMF_CODE.fd", "/usr/share/edk2/ovmf/OVMF_VARS.fd"),
    ],
    "uefi-secure": [
        ("/usr/share/OVMF/OVMF_CODE_4M.secboot.fd", "/usr/share/OVMF/OVMF_VARS_4M.ms.fd"),
        ("/usr/share/OVMF/OVMF_CODE.secboot.fd", "/usr/share/OVMF/OVMF_VARS.ms.fd"),
        ("/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd", "/usr/share/edk2/ovmf/OVMF_VARS.secboot.fd"),
    ],
}
EFIVARS = "efivars.qcow2"
_EFIVARS_SOURCE = "efivars.source"


def ovmf_files(kind: str) -> tuple[Path, Path]:
    for code, template in _OVMF[kind]:
        if Path(code).is_file() and Path(template).is_file():
            return Path(code), Path(template)
    raise runtime_error(
        f"firmware={kind} needs OVMF and this host has none: install the ovmf "
        "package (edk2-ovmf on Fedora)"
    )


async def ensure_efivars(vm_dir: Path, kind: str) -> tuple[Path, Path]:
    """The OVMF code for `kind` and this node's own variable store.

    The store is made from the distribution's empty template on first use and
    kept in the VM directory, so Wipe resets it with everything else. qcow2,
    not the template's raw: savevm refuses to run while any writable drive
    cannot hold a snapshot, and with qcow2 a revert brings the variables back
    to the same moment as the disk. Switching between uefi and uefi-secure
    starts a fresh store, because the two use different key databases."""
    code, template = ovmf_files(kind)
    store = vm_dir / EFIVARS
    source = vm_dir / _EFIVARS_SOURCE
    if store.exists() and source.exists() and source.read_text().strip() == str(template):
        return code, store
    if store.exists():
        logger.warning("qemu.efivars.reset", vm_dir=str(vm_dir), firmware=kind,
                       reason="made from a different OVMF variable template")
    tmp = vm_dir / (EFIVARS + ".tmp")
    rc, out = await _run("qemu-img", "convert", "-f", "raw", "-O", "qcow2",
                         str(template), str(tmp))
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise runtime_error(f"could not create the UEFI variable store: {out.strip()}")
    tmp.replace(store)
    source.write_text(str(template))
    return code, store


async def firmware_args(opts: dict[str, Any], vm_dir: Path, bios_path: Path | None) -> list[str]:
    kind = str(opts.get("firmware") or "")
    if not kind:
        return ["-bios", str(bios_path)] if bios_path else []
    if bios_path:
        logger.info("qemu.firmware.overrides_bios", vm_dir=str(vm_dir), firmware=kind,
                    bios=str(bios_path))
    code, store = await ensure_efivars(vm_dir, kind)
    args = []
    if kind == "uefi-secure":
        # Without this the variable store is writable from outside SMM and
        # Secure Boot's keys mean nothing.
        args += ["-global", "driver=cfi.pflash01,property=secure,value=on"]
    return args + [
        "-drive", f"if=pflash,unit=0,format=raw,readonly=on,file={code}",
        "-drive", f"if=pflash,unit=1,format=qcow2,file={store}",
    ]


def _data_volume_path(node_id: str) -> Path:
    """Deliberately NOT under the VM directory.

    Wipe removes the VM directory wholesale, and the entire point of this
    volume is that it is what survives that. Keeping it somewhere else is what
    makes the guarantee structural rather than a rule someone has to remember.
    """
    root = Path(settings.qemu_vm_dir).expanduser().parent / "node-data"
    d = root / node_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "data.img"


def remove_data_volume(node_id: str) -> None:
    """Only ever called when the node itself goes away.

    Never from wipe — surviving a wipe is the whole reason this volume exists,
    and the two paths share destroy_node_runtime(), so the distinction has to
    live at the call site rather than in the runtime.
    """
    d = _data_volume_path(node_id).parent
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def ensure_data_volume(node_id: str, size_mb: int) -> Path | None:
    """Create the node's data volume once, formatted and labelled.

    Raw rather than qcow2 so mkfs can run against the file directly — no nbd,
    no loop device, no root. The label is how the guest finds it without
    caring which /dev/vdX it landed on.
    """
    path = _data_volume_path(node_id)
    if path.exists():
        return path
    size = max(16, min(65536, int(size_mb or 256)))
    try:
        with open(path, "wb") as fh:
            fh.truncate(size * 1024 * 1024)
        subprocess.run(
            ["mkfs.ext4", "-q", "-L", "LABTRIS", "-F", str(path)],
            capture_output=True,
            timeout=120,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("qemu.data_volume.failed", node=node_id, error=str(exc))
        path.unlink(missing_ok=True)
        return None
    logger.info("qemu.data_volume.created", node=node_id, mb=size)
    return path


def write_cloud_init_seed(vm_dir: Path, hostname: str) -> Path | None:
    """Build the NoCloud seed a cloud image needs to be usable at all.

    Canonical's images ship with no password and no user — without a seed you
    get a login prompt that nothing can satisfy. This sets a known account and
    the node's own name, which is also where generated network configuration
    will go rather than being written into the disk by hand.

    Returns None when cloud-localds is missing, so a host without it degrades
    to "this image has no password" instead of failing to start.
    """
    if shutil.which("cloud-localds") is None:
        logger.warning("qemu.seed.no_cloud_localds", vm=vm_dir.name)
        return None
    seed = _seed_path(vm_dir)
    user_data = "\n".join(
        [
            "#cloud-config",
            f"hostname: {hostname}",
            "ssh_pwauth: true",
            "users:",
            f"  - name: {CLOUD_USER}",
            "    groups: [sudo]",
            "    shell: /bin/bash",
            "    sudo: ['ALL=(ALL) NOPASSWD:ALL']",
            "    lock_passwd: false",
            f"    plain_text_passwd: {CLOUD_PASSWORD}",
            "chpasswd:",
            "  expire: false",
            "",
        ]
    )
    meta_data = f"instance-id: {vm_dir.name}\nlocal-hostname: {hostname}\n"
    tmp = vm_dir / "cloud-init"
    tmp.mkdir(exist_ok=True)
    (tmp / "user-data").write_text(user_data)
    (tmp / "meta-data").write_text(meta_data)
    try:
        subprocess.run(
            ["cloud-localds", str(seed), str(tmp / "user-data"), str(tmp / "meta-data")],
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("qemu.seed.failed", vm=vm_dir.name, error=str(exc))
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return seed


class QemuRuntime:
    kind = "qemu"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.SERIAL, Capability.HOTPLUG_NIC, Capability.SUSPEND, Capability.SNAPSHOT}
    )

    async def create(self, spec: NodeSpec) -> RuntimeHandle:
        if shutil.which("qemu-system-x86_64") is None:
            raise runtime_error("qemu-system-x86_64 is not installed")
        vm_dir = _vm_dir(spec.node_id)
        base = await _resolve_base_image(spec.image)
        overlay = vm_dir / "disk.qcow2"
        if not overlay.exists():
            proc = await asyncio.create_subprocess_exec(
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(base),
                str(overlay),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise runtime_error(f"qemu-img create failed: {err.decode(errors='replace')}")
        # A desktop image booted with CirrOS's 256 MB will thrash or OOM before
        # it draws anything, so the catalog entry supplies the floor when the
        # node itself doesn't ask for a size.
        catalog = QEMU_CATALOG.get(spec.image)
        ram = spec.ram_mb or (catalog.ram_mb if catalog else 256)
        cpus = max(1, min(MAX_VCPUS, round(spec.cpu_limit or (catalog.cpus if catalog else 1))))
        nic = spec.nic_model or (catalog.nic_model if catalog else "virtio-net-pci")
        if nic not in await available_nic_models():
            raise unprocessable(
                f"this qemu build has no {nic!r} device; available: "
                + ", ".join(sorted(await available_nic_models()))
            )
        # No vnc_display here on purpose. A display picked at create() is a
        # reservation nothing enforces: the port is only bound when the VM
        # starts, and by then another VM may hold it — which surfaces as
        # "Failed to find an available port" and a VM that dies at launch.
        # start() picks one at the moment it binds it.
        if catalog is not None and catalog.cloud_init:
            write_cloud_init_seed(vm_dir, spec.name or spec.node_id[-8:])
        # Disk bus and graphical: spec first (populated from the template
        # for saved images), catalog for built-in image ids, and finally
        # virtio/False as the last-resort defaults. Without consulting
        # spec, an NX-OSv template that specified sata was silently
        # booted with virtio here — the guest kernel found no root disk,
        # panicked, and the node boot-looped. Same shape as nic_model /
        # ram_mb / cpus already use.
        disk_bus_val = spec.disk_bus or (catalog.disk_bus if catalog else "virtio")
        graphical_val = (
            spec.graphical if spec.graphical is not None
            else bool(catalog and catalog.graphical)
        )
        _config_path(vm_dir).write_text(
            json.dumps(
                {
                    "ram_mb": ram,
                    "cpus": cpus,
                    "graphical": graphical_val,
                    "nic_model": nic,
                    "nic_queues": spec.nic_queues or (catalog.nic_queues if catalog else None),
                    "disk_bus": disk_bus_val,
                    "cloud_init": bool(catalog and catalog.cloud_init),
                    "opts": resolved_options(spec.qemu_opts),
                    # Cold-plugged at start(). Wired-vs-unwired is not recorded
                    # here — bridging is a runtime concern the tap picks up
                    # through netd. What matters at boot is that the guest sees
                    # every declared NIC, so an appliance that enumerates PCI
                    # once (NX-OS, PAN-OS, most physical-shape images) does
                    # not need to be rebooted after every link.
                    "interfaces": _serialize_ifaces(spec.interfaces),
                    # Companion files + template extras — see runtime/base.py
                    # NodeSpec. None/empty means no -bios/-cdrom/extra_args
                    # emitted; existing templates are unaffected.
                    "bios": spec.bios,
                    "cdrom": spec.cdrom,
                    "extra_args": list(spec.extra_args or []),
                }
            )
        )
        return RuntimeHandle(node_id=spec.node_id, ref=str(vm_dir))

    async def resize(
        self,
        h: RuntimeHandle,
        ram_mb: int | None,
        cpus: int | None,
        nic_model: str | None = None,
        nic_queues: int | None = None,
    ) -> None:
        """Change what the VM will boot with next time.

        create() writes ram/cpus into the VM's config.json, so editing the node
        row alone would change the number on screen and nothing about the
        machine. QEMU cannot change either on a live guest, so this is
        deliberately a next-boot change rather than a hotplug."""
        cfg_path = _config_path(Path(h.ref))
        if not cfg_path.exists():
            return
        cfg = json.loads(cfg_path.read_text())
        if ram_mb:
            cfg["ram_mb"] = int(ram_mb)
        if cpus:
            cfg["cpus"] = max(1, min(MAX_VCPUS, int(cpus)))
        if nic_model:
            if nic_model not in await available_nic_models():
                raise unprocessable(f"this qemu build has no {nic_model!r} device")
            cfg["nic_model"] = nic_model
        if nic_queues is not None:
            cfg["nic_queues"] = max(1, min(MAX_NIC_QUEUES, int(nic_queues)))
        cfg_path.write_text(json.dumps(cfg))

    async def sync_from_spec(self, h: RuntimeHandle, spec: NodeSpec) -> None:
        """Refresh persisted config.json fields from the current NodeSpec so
        template edits (or bug-fix backfills) take effect at the next start.

        Called from lifecycle before every start:
        - `interfaces` — so a node created before cold-plug existed still
          gets its NICs on the command line, and ports added while stopped
          are picked up on the next boot.
        - `disk_bus` — critical for NX-OSv-style images whose kernel needs
          a specific controller; without this sync, a bug-created-with-
          virtio node stayed broken even after the template was corrected.
        - `nic_model`, `ram_mb`, `cpus`, `graphical` — a template edit
          (edit-template modal) propagates on the next start.
        - `opts` — the QEMU options. These were only ever written at
          create, so `PATCH /nodes/{id}/qemu-options` saved to the row and
          changed nothing: chipset, CPU model, vIOMMU, mgmt NIC all stayed
          as the node was born. Found by a user who edited config.json by
          hand while the node was stopped.

        Anything the spec doesn't explicitly say (None) is left alone.
        """
        cfg_path = _config_path(Path(h.ref))
        if not cfg_path.exists():
            return
        cfg = json.loads(cfg_path.read_text())
        cfg["interfaces"] = _serialize_ifaces(spec.interfaces)
        if spec.disk_bus:
            cfg["disk_bus"] = spec.disk_bus
        if spec.nic_model:
            cfg["nic_model"] = spec.nic_model
        if spec.nic_queues is not None:
            cfg["nic_queues"] = spec.nic_queues
        if spec.ram_mb:
            cfg["ram_mb"] = int(spec.ram_mb)
        if spec.cpu_limit:
            cfg["cpus"] = max(1, min(MAX_VCPUS, round(float(spec.cpu_limit))))
        if spec.graphical is not None:
            cfg["graphical"] = bool(spec.graphical)
        # The node's options are the whole set (create copied the template's
        # into the row), resolved over the defaults the same way create does.
        cfg["opts"] = resolved_options(spec.qemu_opts)
        # Companion files + template extras. Set to None/[] explicitly
        # so a template edit that clears a companion actually clears it
        # in the persisted config (a `.get` fallback would leave the
        # previous value in place forever).
        cfg["bios"] = spec.bios
        cfg["cdrom"] = spec.cdrom
        cfg["extra_args"] = list(spec.extra_args or [])
        cfg_path.write_text(json.dumps(cfg))

    # Kept for callers that only have a list of interfaces on hand; new
    # code should prefer sync_from_spec, which covers more.
    async def sync_interfaces(self, h: RuntimeHandle, ifaces: list[IfaceSpec]) -> None:
        cfg_path = _config_path(Path(h.ref))
        if not cfg_path.exists():
            return
        cfg = json.loads(cfg_path.read_text())
        cfg["interfaces"] = _serialize_ifaces(ifaces)
        cfg_path.write_text(json.dumps(cfg))

    async def set_guest_link(self, h: RuntimeHandle, i: IfaceSpec, up: bool) -> None:
        """Simulate the cable being (un)plugged, as the guest sees it.

        Setting the host tap DOWN is not enough on its own — QEMU's virtio
        (and e1000) reports carrier from the netdev backend attachment,
        not the L1 state of the host tap. So a `ip link set … down` on the
        host leaves the guest's driver convinced the NIC is still up, and
        `show interfaces` inside PAN-OS/VyOS/anything else keeps saying
        "up". `set_link net{idx} off/on` on the QEMU monitor propagates
        through virtio's NETDEV_F_STATUS so the guest driver sees the
        expected LINK-DOWN interrupt.

        Silently no-op if the VM isn't running — the netdev doesn't exist
        yet, and the next boot will pick up the current admin_up via
        apply_link_qos anyway.
        """
        vm_dir = Path(h.ref)
        if _read_pid(vm_dir) is None:
            return
        net_id = f"net{i.idx}"
        try:
            await _hmp(vm_dir, f"set_link {net_id} {'on' if up else 'off'}")
        except Exception:  # noqa: BLE001 — best-effort; the host tap change is authoritative
            pass

    async def start(self, h: RuntimeHandle) -> None:
        vm_dir = Path(h.ref)
        if (running_pid := _read_pid(vm_dir)) is not None:
            if h.node_id not in _sessions:
                session = SerialSession(vm_dir / "serial.sock")
                await session.start()
                _sessions[h.node_id] = session
            if h.node_id not in _watchers:
                _watch(h.node_id, vm_dir, _procs.get(h.node_id), running_pid)
            return
        cfg: dict[str, Any] = {}
        if _config_path(vm_dir).exists():
            cfg = json.loads(_config_path(vm_dir).read_text())
        # A base image that moved leaves the overlay pointing at nothing.
        _rebase_if_orphaned(vm_dir)
        # Companion BIOS and CD-ROM, when the template points at one, and only
        # from inside the companion cache (a template row shouldn't be able to
        # nominate arbitrary host files). One that is set but missing stops the
        # start: booting without it used to put a UEFI image on SeaBIOS with no
        # error at all, which is far harder to diagnose than a refusal that
        # names the file.
        bios_path = _companion_ok(cfg.get("bios"), _bios_dir())
        cdrom_path = _companion_ok(cfg.get("cdrom"), _cdrom_dir())
        for kind, wanted, found in (
            ("bios", cfg.get("bios"), bios_path),
            ("cdrom", cfg.get("cdrom"), cdrom_path),
        ):
            if wanted and found is None:
                raise runtime_error(
                    f"the template's {kind} file {wanted} is missing; upload it again "
                    f"with POST /images/companion kind={kind} and set the template's "
                    f"{kind} to the path that returns"
                )
        # Claim a display now, next to the launch that binds it. A stored one
        # is only a hint: it is right most of the time and catastrophically
        # wrong when another VM took the port, so it is checked, not trusted.
        display = cfg.get("vnc_display")
        owner = str(vm_dir)
        if (
            not isinstance(display, int)
            or _claimed_by_other(display, owner)
            or not _display_free(display)
        ):
            display = _pick_free_vnc_display(owner=owner)
            cfg["vnc_display"] = display
            _config_path(vm_dir).write_text(json.dumps(cfg))
        else:
            _claim_vnc_display(display, owner)
        # Decided here, next to the launch that uses it, and recorded so
        # `labtris node perf` can say which VMs are on software emulation.
        accel = resolve_accel()
        if cfg.get("accel") != accel:
            cfg["accel"] = accel
            _config_path(vm_dir).write_text(json.dumps(cfg))
        serial_sock = vm_dir / "serial.sock"
        mon_sock = vm_dir / "mon.sock"
        # QMP is opened as a *second* monitor alongside HMP. HMP is our
        # working shell (screendump, sendkey, set_link, info vnc — none
        # of which have a comparable QMP path). QMP is JSON-only and
        # exposes commands HMP doesn't (input-send-event with absolute
        # tablet coordinates for mouse events). Two sockets, one QEMU
        # process, no cost.
        qmp_sock = vm_dir / "qmp.sock"
        for sock in (serial_sock, mon_sock, qmp_sock):
            sock.unlink(missing_ok=True)
        opts = resolved_options(cfg.get("opts"))
        data_volume = (
            ensure_data_volume(h.node_id, opts.get("data_volume_mb", 256))
            if opts.get("data_volume")
            else None
        )
        # The node's explicit choice, then what the image asked for at create,
        # then virtio. The node option defaults to "" rather than "virtio"
        # precisely so that leaving it alone does not override the image.
        # "sata" is honoured with a real AHCI controller (see below) rather
        # than falling through drive_if's approximation to IDE, because
        # NX-OSv 9000 and other appliances that declare sata actually need
        # AHCI — IDE gives them a "no root disk" panic and boot-loops.
        requested_bus = (str(opts.get("disk_bus") or "") or str(cfg.get("disk_bus") or "virtio")).strip().lower()
        want_sata = requested_bus == "sata"
        disk_bus = "none" if want_sata else drive_if(requested_bus)
        common_disk_flags = "cache=writeback,aio=threads,discard=unmap,detect-zeroes=unmap"
        disk_path = vm_dir / "disk.qcow2"
        if want_sata:
            # Real AHCI: attach an ich9-ahci controller and hang the disk
            # off it as ide-hd on port 0. bootindex=1 makes SeaBIOS/OVMF
            # pick this disk over any companion CD-ROM the template
            # attaches — without it, a template with -cdrom present
            # boots the CD instead of the installed system.
            drive_and_ctrl = [
                "-device", "ich9-ahci,id=ahci0",
                "-drive", f"id=disk0,if=none,file={disk_path},format=qcow2,{common_disk_flags}",
                "-device", "ide-hd,bus=ahci0.0,drive=disk0,bootindex=1",
            ]
        elif disk_bus in ("virtio", "ide"):
            # Split form: -drive if=none + -device carries the disk.
            # `bootindex` is a device-level option and QEMU 8.2+ refuses
            # it on the shortcut `-drive if=<bus>` form ("Block format
            # 'qcow2' does not support the option 'bootindex'"). It has
            # to sit on -device instead, so we build the device pair
            # here regardless of whether a companion CD-ROM is present.
            # For virtio the device is `virtio-blk-pci`; for ide,
            # `ide-hd` on the default IDE bus. PAN-OS's dhpm still sees
            # a `pci.0` slot-0 topology from virtio-blk-pci, so the
            # "this is a normal VM" heuristic that bus=0,unit=0 used to
            # serve is still met.
            device = "virtio-blk-pci" if disk_bus == "virtio" else "ide-hd"
            drive_and_ctrl = [
                "-drive",
                f"id=disk0,if=none,file={disk_path},format=qcow2,{common_disk_flags}",
                "-device",
                f"{device},drive=disk0,bootindex=1",
            ]
        else:
            # Buses we don't emit a matching -device for (scsi, sd) — keep
            # the shortcut form and skip bootindex. Without a competing
            # CD-ROM the guest still boots the disk via -boot c; a
            # scsi/sd node that also carries a companion CD-ROM would
            # need a full HBA + scsi-hd wiring pass, which is a bigger
            # change than what this file has ever needed.
            drive_and_ctrl = [
                "-drive",
                f"file={disk_path},if={disk_bus},bus=0,unit=0,{common_disk_flags},format=qcow2",
            ]
        bios_args = await firmware_args(opts, vm_dir, bios_path)
        cdrom_args = ["-cdrom", str(cdrom_path)] if cdrom_path else []

        args = [
            "qemu-system-x86_64",
            *machine_args(opts),
            "-cpu",
            str(opts.get("cpu") or "qemu64"),
            "-boot",
            str(opts.get("boot") or "c"),
            "-accel",
            accel,
            "-m",
            str(cfg.get("ram_mb", 256)),
            "-smp",
            str(cfg.get("cpus", 1)),
            *bios_args,
            *cdrom_args,
            *drive_and_ctrl,
            *(
                # The cloud-init seed CD still rides IDE — that's what
                # every cloud-image expects to find its no-cloud data on.
                # media=cdrom matters: without it QEMU makes an IDE hard
                # disk, which cannot be read-only, and the VM exits at once
                # with "Block node is read-only" (0.13.13 ISO, 2026-11-12).
                ["-drive",
                 f"file={_seed_path(vm_dir)},if=ide,media=cdrom,format=raw,readonly=on"]
                if _seed_path(vm_dir).exists()
                else []
            ),
            # `-display none` rather than `-nographic`: the latter also sets
            # the machine's `graphics=off` and rewires the default serial and
            # monitor to stdio, which fights the explicit chardevs below.
            # Lab interfaces are cold-plugged just below (one -netdev/-device
            # pair per known interface, wired or not). What remains here is
            # the optional user-mode NAT nic and the "and nothing else"
            # baseline. Without any -netdev at all QEMU auto-adds a default
            # user-mode NIC, so when no interfaces are declared the baseline
            # is still explicitly `nic none` — a network lab shouldn't come
            # up with an undeclared interface on 10.0.2.15.
            *(
                ["-nic", "user"]
                if opts.get("mgmt_nic")
                else ([] if cfg.get("interfaces") else ["-nic", "none"])
            ),
            "-display",
            "none",
            "-vga",
            "std",
            # Without an absolute pointing device the VNC guest gets relative
            # mouse deltas and the cursor drifts away from the browser's.
            *(["-usb", "-device", "usb-tablet"] if opts.get("usb_tablet", True) else []),
            "-rtc",
            "base=utc",
            # wait=on, not off. With wait=off QEMU boots the guest the instant
            # it starts, and everything printed before anything connects to
            # this socket — the entire kernel boot — is written to a socket
            # with no reader and discarded. wait=on holds the guest at the
            # starting line until the console session below has attached, so
            # the first byte the kernel emits is the first byte we receive.
            #
            # Nothing is written to disk: the buffer lives in the session.
            "-serial",
            f"unix:{serial_sock},server=on,wait=on",
            "-monitor",
            f"unix:{mon_sock},server=on,wait=off",
            "-qmp",
            f"unix:{qmp_sock},server=on,wait=off",
            "-vnc",
            # share=force-shared lets a new client take over the display
            # even when the previous one hasn't fully closed. QEMU's
            # default is share=allow-exclusive, which holds the display
            # for the first attached client until it cleanly disconnects
            # — and a browser tab that Chrome dropped without a proper
            # WebSocket close (a Chrome quirk on backgrounded tabs and
            # some page navigations) leaves the display "in use" for
            # minutes. The next VNC open from any tab gets refused by
            # QEMU and guacd surfaces that as tunnel 519. force-shared
            # also lets two people watch the same guest, which is what
            # you want in a shared lab tool.
            f"127.0.0.1:{display},share=force-shared",
            *(
                [
                    "-drive",
                    f"file={data_volume},if=virtio,format=raw",
                ]
                if data_volume is not None
                else []
            ),
        ]
        # Cold-plug every declared interface. Whether a tap is enslaved to a
        # bridge is a runtime concern netd handles separately (in
        # lifecycle.py's post-boot loop, only when network_id is set) — an
        # unbridged tap shows up in the guest as a NIC with no carrier,
        # which is exactly how a real router presents an unplugged port.
        # The taps themselves must already exist on the host when qemu
        # launches: the pre-boot loop in lifecycle.start_node calls
        # netd.tap.create for every interface, wired or not.
        # vhost only with multi-queue, on purpose: the single-queue path is
        # the one every existing lab runs on, and a host that lacks the
        # device must keep starting exactly as before. Asking for queues is
        # the opt-in; vhost comes with it when the host allows it.
        if nic_queue_count(cfg) > 1 and not vhost_available():
            logger.warning(
                "qemu.vhost_unavailable", vm_dir=str(vm_dir), queues=nic_queue_count(cfg),
                hint="/dev/vhost-net is not open-able by the API process",
            )
        for iface in cfg.get("interfaces") or []:
            net_id = f"net{iface['idx']}"
            nic_id = f"nic{iface['idx']}"
            nic_model, queues = port_nic(cfg, iface.get("nic_model"))
            vhost = queues > 1 and vhost_available()
            args.extend([
                "-netdev", netdev_args(net_id, iface["host_ifname"], queues, vhost),
                "-device", device_args(
                    nic_model, net_id, iface["mac"], nic_id, queues,
                    viommu=bool(opts.get("viommu")),
                ),
            ])
        # Template-level extras: authored by an admin at template
        # registration (same trust as picking the disk image itself),
        # applied automatically. Used for -smbios strings that Cisco
        # appliances check, IvyBridge CPU features vjunos wants,
        # -nvram sizes IOS needs. Ungated — the trust decision was
        # made when the template was created. Anything added here
        # ends up on every node from that template.
        template_extras = cfg.get("extra_args") or []
        if template_extras:
            args.extend(str(a) for a in template_extras)

        # Per-NODE arbitrary arguments are a different story — a random
        # lab owner slipping args onto the command line runs code as
        # the qemu process. Kept behind the settings gate.
        extra = opts.get("extra_args") or []
        if extra and settings.qemu_allow_extra_args:
            args.extend(str(a) for a in extra)
        elif extra:
            logger.warning("qemu.extra_args.refused", vm=vm_dir.name, count=len(extra))
        # Last, so nothing above changes PCI address because of them.
        args.extend(hotplug_port_args(opts))
        # Which spare slot holds which hot-plugged NIC is about this boot
        # only: everything in config.json is cold-plugged again from here.
        (vm_dir / _HOTPLUG_STATE).unlink(missing_ok=True)
        log = open(vm_dir / "qemu.log", "ab")
        _log_starts[vm_dir] = log.tell()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        (vm_dir / "qemu.pid").write_text(str(proc.pid))
        _procs[h.node_id] = proc

        # QEMU creates its chardev sockets before it validates the rest of its
        # arguments, so the socket existing proved nothing: a VM that died on a
        # missing disk or a taken VNC port still left the files behind, start()
        # reported success, and the first monitor command failed with a bare
        # ECONNREFUSED several layers away. Wait for a monitor that actually
        # answers, and if the process is gone, report what it said on its way
        # out — the reason is always in its log and was never being read.
        # Before the monitor, deliberately. QEMU is holding the guest until
        # something connects to the serial socket, and the monitor handshake
        # below can take seconds — seconds of boot output that would be gone.
        # Connecting here is also what releases QEMU to start the guest at all.
        session = SerialSession(serial_sock)
        await session.start()
        _sessions[h.node_id] = session

        if not await _await_monitor(mon_sock, proc) or not await _await_settled(proc):
            raise runtime_error(f"qemu exited at startup: {_last_qemu_error(vm_dir)}")
        _watch(h.node_id, vm_dir, proc, proc.pid)


    async def stop(self, h: RuntimeHandle, mode: StopMode) -> None:
        vm_dir = Path(h.ref)
        # First, before any await: this exit is asked for, not news.
        _unwatch(h.node_id)
        pid = _read_pid(vm_dir)
        if pid is None:
            return
        if mode is StopMode.GRACEFUL:
            try:
                await _hmp(vm_dir, "system_powerdown")
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    if _read_pid(vm_dir) is None:
                        break
            except ApiError:
                pass  # best effort — fall through to a hard kill below
        pid = _read_pid(vm_dir)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            # SIGKILL is a request. Until the process is gone it holds the
            # qcow2's write lock, and "stopped" followed at once by a
            # qemu-img on the disk failed with "Failed to get write lock"
            # (labtris-013, 2026-11-14).
            proc = _procs.get(h.node_id)
            for _ in range(100):
                if proc is not None and proc.returncode is not None:
                    break
                if proc is None and not _pid_alive(pid):
                    break
                await asyncio.sleep(0.1)
        (vm_dir / "qemu.pid").unlink(missing_ok=True)
        session = _sessions.pop(h.node_id, None)
        if session is not None:
            await session.close()
        _procs.pop(h.node_id, None)
        # Tear down the RFB framebuffer cache — its socket is dead the
        # moment QEMU exits, and a stale entry would return an error on
        # the next screenshot instead of transparently reconnecting.
        _qmp_conns.pop(vm_dir, None)
        from labtris_api.runtime.vnc_cache import drop_cached_client

        await drop_cached_client(vm_dir)

    async def destroy(self, h: RuntimeHandle) -> None:
        await self.stop(h, StopMode.FORCE)
        shutil.rmtree(h.ref, ignore_errors=True)

    async def attach_iface(self, h: RuntimeHandle, i: IfaceSpec, bridge: str) -> None:
        """Put this interface on `bridge`, live.

        Unlike Docker's veth pair, a QEMU guest NIC is backed directly by a
        persistent tap: the same device is both the bridge port and the
        guest's virtio-net backend. netd creates it owned by *our* uid so the
        qemu child can open it by name with no capabilities of its own.

        Every interface a VM has at boot is cold-plugged with its tap, wired
        or not. So re-pointing a link on a running VM is only a matter of
        moving that tap to the new bridge: the guest sees its cable pulled
        and plugged back in, nothing more. That is the common case and it
        never touches the guest's PCI devices.

        It used to delete and recreate the tap instead, then ask QEMU to swap
        the NIC. QEMU keeps the deleted tap open, and on q35 a boot-time NIC
        sits on `pcie.0`, which cannot be hot-unplugged, so the swap silently
        did nothing: the new tap sat in the right bridge, the guest's NIC
        stayed on the dead one, and the API said 201 (perf-fw, 2026-11-05).

        A NIC is hot-plugged only when the guest has no working one for this
        interface: one added after boot, or one whose tap was lost. The
        result is checked, and anything that did not take raises
        needs_restart rather than reporting success."""
        vm_dir = Path(h.ref)
        if _read_pid(vm_dir) is None:
            # Not running: the next start cold-plugs it.
            await _ensure_tap(vm_dir, i.host_ifname, model=i.nic_model)
            await _wire_tap(i.host_ifname, bridge, None)
            return
        tap = await _inspect_tap(i.host_ifname)
        nic_id = f"nic{i.idx}"
        nics = parse_info_network(await _hmp_qmp(vm_dir, "info network"))
        nic = nics.get(nic_id)
        if (
            tap.get("exists")
            and tap.get("carrier")
            and nic is not None
            and nic.get("ifname") == i.host_ifname
        ):
            await _wire_tap(i.host_ifname, bridge, tap.get("master"))
            await self.set_guest_link(h, i, True)
            return
        await self._hotplug_nic(vm_dir, i, bridge, nic_present=nic is not None)

    async def _hotplug_nic(
        self, vm_dir: Path, i: IfaceSpec, bridge: str, nic_present: bool
    ) -> None:
        cfg_path = _config_path(vm_dir)
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        opts = resolved_options(cfg.get("opts"))
        model, queues = port_nic(cfg, i.nic_model)
        vhost = queues > 1 and vhost_available()
        net_id, nic_id = f"net{i.idx}", f"nic{i.idx}"
        restart = needs_restart(
            f"{i.guest_name} is saved and wired, but this VM cannot take a new "
            "network card while it runs; it takes effect when the node restarts",
            {"interface": i.guest_name, "node_ref": str(vm_dir)},
        )

        if nic_present:
            # A NIC left holding a tap that no longer exists. Only a card in a
            # hot-plug slot can be swapped; one on pcie.0 cannot be removed.
            if not await _unplug(vm_dir, nic_id):
                await _ensure_tap(vm_dir, i.host_ifname, fresh=False, model=i.nic_model)
                await _wire_tap(i.host_ifname, bridge, None)
                raise restart
        await _hmp_qmp(vm_dir, f"netdev_del {net_id}")
        await _ensure_tap(vm_dir, i.host_ifname, fresh=True, multi_queue=queues > 1)
        await _wire_tap(i.host_ifname, bridge, None)

        bus = ""
        if is_q35(opts):
            port = _free_hotplug_port(vm_dir, hotplug_slot_count(opts))
            if port is None:
                raise restart
            bus = f",bus={port}"
        out = await _hmp_qmp(
            vm_dir, "netdev_add " + netdev_args(net_id, i.host_ifname, queues, vhost)
        )
        if (err := _hmp_error(out)) is not None:
            raise runtime_error(f"qemu netdev_add failed: {err}")
        out = await _hmp_qmp(
            vm_dir,
            "device_add "
            + device_args(model, net_id, i.mac, nic_id, queues,
                          viommu=bool(opts.get("viommu")))
            + bus,
        )
        if _hmp_error(out) is not None:
            # Most often: a VM started before it had spare slots ("Bus
            # 'lt-hp0' not found"). Undo the backend and say so.
            await _hmp_qmp(vm_dir, f"netdev_del {net_id}")
            raise restart
        # Check what QEMU actually has, not what it was told.
        nic = parse_info_network(await _hmp_qmp(vm_dir, "info network")).get(nic_id)
        tap = await _inspect_tap(i.host_ifname)
        if nic is None or nic.get("ifname") != i.host_ifname or not tap.get("carrier"):
            raise restart
        if bus:
            _record_hotplug_port(vm_dir, nic_id, bus.split("=", 1)[1])

    async def detach_iface(self, h: RuntimeHandle, i: IfaceSpec) -> None:
        """Remove this interface's NIC from a running VM.

        Only a NIC in a hot-plug slot (or on a pc VM) can really leave. One
        that cannot is left in place with its cable pulled, and the caller
        is told the removal waits for a restart."""
        vm_dir = Path(h.ref)
        if _read_pid(vm_dir) is None:
            await _drop_tap(i.host_ifname)
            return
        nic_id = f"nic{i.idx}"
        if await _unplug(vm_dir, nic_id):
            await _hmp_qmp(vm_dir, f"netdev_del net{i.idx}")
            await _drop_tap(i.host_ifname)
            _record_hotplug_port(vm_dir, nic_id, None)
            return
        await self.set_guest_link(h, i, False)
        with contextlib.suppress(NetdError):
            await netd.call("iface.detach", {"name": i.host_ifname})
        raise needs_restart(
            f"{i.guest_name} is unplugged from its network; the card itself "
            "stays in the VM until it restarts",
            {"interface": i.guest_name},
        )

    async def console(self, h: RuntimeHandle) -> ConsoleEndpoint:
        return ConsoleEndpoint(kind="serial", target=str(Path(h.ref) / "serial.sock"), meta={})

    async def observe(self, h: RuntimeHandle) -> RuntimeState:
        vm_dir = Path(h.ref)
        pid = _read_pid(vm_dir)
        exists = vm_dir.exists()
        return RuntimeState(exists=exists, running=pid is not None, pid=pid, exit_code=None)

    async def suspend(self, h: RuntimeHandle) -> None:
        """A real VM pause via HMP `stop` — QEMU's actual analogue of EVE's
        suspend, unlike Docker's cgroup freeze."""
        await _hmp(Path(h.ref), "stop")

    async def resume(self, h: RuntimeHandle) -> None:
        await _hmp(Path(h.ref), "cont")

    async def logs(self, h: RuntimeHandle, lines: int) -> str:
        log_path = Path(h.ref) / "qemu.log"
        if not log_path.exists():
            return ""
        text = log_path.read_text(errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    async def write_file(self, h: RuntimeHandle, path: str, content: str) -> None:
        raise unprocessable("qemu backend has no in-guest file write — use the serial console")

    async def save_snapshot(self, h: RuntimeHandle, name: str) -> None:
        """Capture RAM and device state into the node's qcow2.

        Returns only once QEMU has finished writing. Callers rely on that:
        pods.py flattens the same disk immediately afterwards, and reading a
        qcow2 mid-savevm produces an archive whose snapshot is partial or
        absent — which surfaces much later as "snapshot does not exist" on
        the restore, a long way from the cause.

        Which network cards the VM had is recorded next to it: a card
        hot-plugged after this moment is unknown to the guest the snapshot
        brings back, and restore says so (labtris-013, 2026-11-14).
        """
        vm_dir = Path(h.ref)
        # Before savevm, so the last thing this does is the save itself; the
        # cards cannot change in between, the monitor runs one command at once.
        nics = sorted(parse_info_network(await _hmp_qmp(vm_dir, "info network")))
        out = await _hmp_qmp(vm_dir, f"savevm {name}", timeout=_SNAPSHOT_TIMEOUT)
        if (err := _hmp_error(out)) is not None:
            raise runtime_error(f"savevm failed: {err}")
        meta = _snapshot_meta(vm_dir)
        meta[name] = {"nics": nics, "saved_at": time.time()}
        _write_snapshot_meta(vm_dir, meta)

    async def snapshot_drift(self, h: RuntimeHandle, name: str) -> tuple[list[str], list[str]]:
        """Network cards the running VM has now that it did not have when
        `name` was taken, and cards it had then and has not now, as nic ids.

        Added cards in hot-plug slots load fine, but the guest restored from
        the snapshot has no driver bound to them, so they carry nothing until
        a restart. After a restart they are cold-plugged, and QEMU refuses
        to load the snapshot at all: a different set of devices is "Error
        -22 while loading VM state" and nothing more."""
        vm_dir = Path(h.ref)
        saved = _snapshot_meta(vm_dir).get(name, {}).get("nics")
        if saved is None or _read_pid(vm_dir) is None:
            return [], []
        now = parse_info_network(await _hmp_qmp(vm_dir, "info network"))
        return sorted(set(now) - set(saved)), sorted(set(saved) - set(now))

    async def load_snapshot(self, h: RuntimeHandle, name: str) -> None:
        vm_dir = Path(h.ref)
        if _read_pid(vm_dir) is None:
            raise conflict("the VM must be running to revert it; start it first")
        out = await _hmp_qmp(vm_dir, f"loadvm {name}", timeout=_SNAPSHOT_TIMEOUT)
        if (err := _hmp_error(out)) is not None:
            raise runtime_error(f"loadvm failed: {err}")

    async def list_snapshots(self, h: RuntimeHandle) -> list[str]:
        vm_dir = Path(h.ref)
        if _read_pid(vm_dir) is None:
            # Stopped: the snapshots are in the qcow2 all the same.
            disk = vm_dir / "disk.qcow2"
            if not disk.exists():
                return []
            rc, out = await _run("qemu-img", "info", "-U", "--output=json", str(disk), timeout=30)
            if rc != 0:
                raise runtime_error(f"qemu-img info failed: {out.strip()}")
            return [sn["name"] for sn in json.loads(out).get("snapshots", [])]
        out = await _hmp_qmp(vm_dir, "info snapshots")
        names = []
        for raw in out.splitlines():
            line = raw.strip()
            # No echo to skip any more, but the header lines are still there,
            # and "(qemu)" costs nothing to keep ignoring.
            if not line or line.startswith(("List of snapshots", "ID", "(qemu)")):
                continue
            parts = line.split()
            if len(parts) >= 2 and (parts[0] == "--" or parts[0].isdigit()):
                names.append(parts[1])
        return names

    async def delete_snapshot(self, h: RuntimeHandle, name: str) -> None:
        """delvm on a running VM; qemu-img on a stopped one, from the disk and
        the UEFI variable store alike, since savevm wrote to both."""
        vm_dir = Path(h.ref)
        if _read_pid(vm_dir) is not None:
            out = await _hmp_qmp(vm_dir, f"delvm {name}", timeout=_SNAPSHOT_TIMEOUT)
            if (err := _hmp_error(out)) is not None:
                raise runtime_error(f"delvm failed: {err}")
        else:
            for f in (vm_dir / "disk.qcow2", vm_dir / EFIVARS):
                if not f.exists():
                    continue
                rc, out = await _run("qemu-img", "snapshot", "-d", name, str(f), timeout=120)
                if rc != 0 and f.name == "disk.qcow2":
                    raise runtime_error(f"deleting snapshot {name!r} failed: {out.strip()}")
        meta = _snapshot_meta(vm_dir)
        if meta.pop(name, None) is not None:
            _write_snapshot_meta(vm_dir, meta)


_SNAPSHOT_META = "snapshots.json"


def _snapshot_meta(vm_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((vm_dir / _SNAPSHOT_META).read_text())
    except (OSError, ValueError):
        return {}


def _write_snapshot_meta(vm_dir: Path, meta: dict[str, Any]) -> None:
    """Best effort: the snapshot itself is in the qcow2; this only feeds the
    warning about cards added since."""
    try:
        (vm_dir / _SNAPSHOT_META).write_text(json.dumps(meta))
    except OSError as exc:
        logger.warning("qemu.snapshot_meta.unwritable", vm_dir=str(vm_dir), error=str(exc))


qemu_runtime = QemuRuntime()
