"""`labtris-image` — add, list, and remove QEMU images from outside the browser.

The HTTP upload path is fine for a laptop with the file on hand, but
sideloading a 40 GB vendor appliance that is already on the box is faster
without going through multipart. This CLI reads the file straight from disk
and registers it as a `Template` row, using the same helpers the upload
endpoint uses so the two paths converge on the same shape:
`custom-<sha>.qcow2` under the image cache, `image="custom:<sha>"` in the
DB.

Runs against the same Postgres the API talks to (reads `LABTRIS_DATABASE_URL`
via `labtris_api.config`). It writes to `~/.cache/labtris/qemu-images/`,
which is owned by the `labtris` service user — so invoke as:

    sudo -u labtris /opt/labtris/.venv/bin/labtris-image add ...

or as root. Running as `labtris-admin` (or any other user) gets a permissions
error on the cache directory; the CLI catches EACCES and prints the sudo
form it should have been called with.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from labtris_api.db import SessionLocal
from labtris_api.lifecycle import new_id
from labtris_api.models import Template
from labtris_api.runtime.qemu import (
    CUSTOM_PREFIX,
    IMAGE_PROGRESS,
    QEMU_CATALOG,
    _cache_dir,
    _custom_path,
    _ensure_qcow2,
    _install_custom,
    _resolve_base_image,
    image_status,
)


def _die(message: str, code: int = 1) -> NoReturn:
    print(f"labtris-image: {message}", file=sys.stderr)
    sys.exit(code)


def _permissions_hint() -> NoReturn:
    _die(
        "cannot write to the image cache. Re-run under the labtris service user:\n"
        f"  sudo -u labtris {' '.join(sys.argv)}",
        code=2,
    )


async def _add(args: argparse.Namespace) -> None:
    src = Path(args.path).expanduser().resolve()
    if not src.is_file():
        _die(f"{src}: not a file")

    lower = src.name.lower()
    if lower.endswith(".iso"):
        _die("iso is not supported yet — install once, then add the resulting qcow2")

    try:
        cache = _cache_dir()
    except PermissionError:
        _permissions_hint()

    # Copy into the cache first so _ensure_qcow2's rename stays on the same
    # filesystem. shutil.copyfile is fast enough — this CLI runs local to
    # the file, so the copy is a same-disk operation.
    upload_tmp = cache / f"cli-{new_id()}"
    normalised_tmp = cache / f"cli-{new_id()}.qcow2"
    try:
        try:
            shutil.copyfile(src, upload_tmp)
        except PermissionError:
            _permissions_hint()
        await _ensure_qcow2(upload_tmp, normalised_tmp)
        stored = await _install_custom(normalised_tmp)
    except Exception as exc:  # noqa: BLE001 — CLI top-level cleanup
        for stray in (upload_tmp, normalised_tmp):
            stray.unlink(missing_ok=True)
        _die(str(exc))

    spec: dict[str, Any] = {
        "origin": "cli",
        "bytes": stored["bytes"],
        "ram_mb": args.ram_mb,
        "cpus": args.cpus,
        "nic_model": args.nic_model,
        "disk_bus": args.disk_bus,
        "iface_scheme": args.iface_scheme,
        "graphical": args.graphical,
    }
    if args.description:
        spec["description"] = args.description

    async with SessionLocal() as session:
        tmpl = Template(
            id=new_id(),
            name=args.name,
            runtime="qemu",
            image=stored["image"],
            cmd=None,
            env={},
            icon=None,
            spec=spec,
            description=args.description,
        )
        session.add(tmpl)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            # File is already on disk under its content-addressed name;
            # leave it, a re-run with a different --name will dedup onto
            # the same bytes.
            _die(f"template name {args.name!r} already exists")

    print(
        f"added {args.name!r} → {stored['image']} "
        f"({stored['bytes'] // (1024 * 1024)} MiB)"
    )


async def _list(_args: argparse.Namespace) -> None:
    async with SessionLocal() as session:
        rows = list(
            (await session.execute(select(Template).order_by(Template.name))).scalars()
        )
    if not rows:
        print("(no templates)")
        return
    # id | name | runtime | image | origin | MiB
    print(f"{'name':<32} {'runtime':<7} {'origin':<9} {'MiB':>8}  image")
    for t in rows:
        spec = t.spec or {}
        origin = spec.get("origin", "-")
        mib = (spec.get("bytes") or 0) // (1024 * 1024) or "-"
        print(f"{t.name:<32} {t.runtime:<7} {origin:<9} {mib:>8}  {t.image}")


async def _pull(args: argparse.Namespace) -> None:
    """Download a catalog image now, print a progress bar, exit when cached."""
    image: str = args.image
    if image not in QEMU_CATALOG and not image.startswith(("http://", "https://", "custom-")):
        # Not fatal — a bare path is legal for _resolve_base_image — but
        # the common case is a typoed catalog id and it helps to say so.
        print(
            f"note: {image!r} is not a known catalog id; treating as a URL or path.",
            file=sys.stderr,
        )

    status = image_status(image)
    if status.get("cached"):
        print(f"{image}: already cached — nothing to do.")
        return

    # Kick off the pull and a progress-print loop in parallel.
    pull_task = asyncio.create_task(_resolve_base_image(image))
    try:
        last_line = ""
        while not pull_task.done():
            st = IMAGE_PROGRESS.get(image) or {}
            phase = st.get("phase", "starting")
            if phase == "downloading":
                done = int(st.get("done") or 0)
                total = int(st.get("total") or 0)
                pct = int(st.get("percent") or 0)
                if total:
                    line = f"\rdownloading  {pct:3d}%  {done / (1 << 20):8.1f} / {total / (1 << 20):.1f} MB"
                else:
                    line = f"\rdownloading  {done / (1 << 20):8.1f} MB (size unknown)"
            elif phase == "extracting":
                line = "\rextracting…                                        "
            elif phase == "converting":
                line = "\rconverting to qcow2…                               "
            else:
                line = f"\r{phase}…                                          "
            if line != last_line:
                sys.stdout.write(line)
                sys.stdout.flush()
                last_line = line
            await asyncio.sleep(0.5)
        # Clear the last progress line and let the exception (if any)
        # from the pull surface below.
        sys.stdout.write("\r" + " " * len(last_line) + "\r")
        sys.stdout.flush()
        path = pull_task.result()
        size = path.stat().st_size / (1 << 20)
        print(f"{image}: cached ({size:.1f} MB at {path})")
    except PermissionError:
        _permissions_hint()
    except KeyboardInterrupt:
        pull_task.cancel()
        _die("cancelled", code=130)


async def _rm(args: argparse.Namespace) -> None:
    async with SessionLocal() as session:
        row = (
            await session.execute(select(Template).where(Template.name == args.name))
        ).scalar_one_or_none()
        if row is None:
            _die(f"no template named {args.name!r}")
        image = row.image
        # A saved/uploaded/cli image may be referenced by nodes; refuse
        # rather than orphaning those nodes at their next start.
        if image.startswith(CUSTOM_PREFIX):
            from labtris_api.models import Node

            users = (
                await session.execute(select(Node).where(Node.image == image))
            ).scalars().all()
            if users:
                _die(
                    f"{len(users)} node(s) still use this image "
                    f"({', '.join(sorted(n.name for n in users)[:5])}). "
                    "Delete them first."
                )
        await session.delete(row)
        await session.commit()

        if image.startswith(CUSTOM_PREFIX):
            # Only remove the file if no other template still points at it —
            # two rows can share bytes via dedup.
            still = (
                await session.execute(select(Template).where(Template.image == image))
            ).scalar_one_or_none()
            if still is None:
                path = _custom_path(image[len(CUSTOM_PREFIX):])
                try:
                    path.unlink(missing_ok=True)
                except PermissionError:
                    print(
                        f"removed template row, but the disk file "
                        f"{path} could not be unlinked (permission).",
                        file=sys.stderr,
                    )
                    return

    print(f"removed {args.name!r}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labtris-image",
        description="Add, list, and remove Labtris templates (bring-your-own images).",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    add = subs.add_parser("add", help="register a qcow2 (or convertible disk) as a template")
    add.add_argument("path", help="path to the disk image on disk")
    add.add_argument("--name", required=True, help="display name for the new template")
    add.add_argument("--ram-mb", type=int, default=256, help="default guest RAM (MB)")
    add.add_argument("--cpus", type=int, default=1, help="default vCPUs")
    add.add_argument(
        "--nic-model", default="virtio-net-pci",
        help="QEMU NIC model — e1000 for old guests, virtio-net-pci otherwise",
    )
    add.add_argument(
        "--disk-bus", default="virtio",
        help="ide when the guest kernel has no virtio-blk driver, virtio otherwise",
    )
    add.add_argument(
        "--iface-scheme", default="ens",
        help="ens (systemd), eth (busybox/older), enp, vmware (ens192/224/…), "
             "srl (SR Linux), ios (GigE0/N), paloalto (mgmt + eth1/N), "
             "nxos (Mgmt0 + E1/N). See naming.IFACE_SCHEMES.",
    )
    add.add_argument("--graphical", action="store_true", help="console is framebuffer (VNC)")
    add.add_argument("--description", default=None)
    add.set_defaults(func=_add)

    lst = subs.add_parser("list", help="list templates")
    lst.set_defaults(func=_list)

    pull = subs.add_parser(
        "pull",
        help="download a catalog image now with a progress bar (no clicking around)",
    )
    pull.add_argument(
        "image",
        help="catalog id (e.g. cirros-0.6.2, ubuntu-24.04), https URL, or local path",
    )
    pull.set_defaults(func=_pull)

    rm = subs.add_parser("rm", help="remove a template (and its disk if unreferenced)")
    rm.add_argument("name", help="template name to remove")
    rm.set_defaults(func=_rm)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    # Everything below the CLI is async: DB, subprocess calls, file hashing.
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
