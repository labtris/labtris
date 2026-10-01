#!/usr/bin/env python3
"""Register an existing EVE-NG image library as Labtris templates.

    ./packaging/adopt-eveng-images.py                      # dry run, default
    ./packaging/adopt-eveng-images.py --root /opt/unetlab
    ./packaging/adopt-eveng-images.py --apply              # create templates

WHY THIS EXISTS

`.unl` import already turns an EVE-NG topology into a Labtris lab. The nodes
then do not boot, because the disks those nodes name are not registered as
templates — and an EVE-NG user's image library is commonly tens or hundreds of
gigabytes of licensed vendor images. Nobody re-downloads that to evaluate a
new tool, so without this the importer is a demo rather than a migration path.

WHAT EVE-NG'S LAYOUT ACTUALLY ENCODES

    /opt/unetlab/addons/qemu/<template>-<version>/<disk>.qcow2

Two things are inferable and one is not:

  * The directory name's PREFIX is EVE-NG's template id — `vios`, `csr1000vng`,
    `veos`, `vmx`, `nxosv9k`. That is what decides RAM, CPU count and NIC
    model in EVE-NG, so it is what we map from. Longest-prefix wins, because
    `viosl2` and `vios` are different templates and `vios` is a prefix of both.
  * The DISK FILENAME encodes the bus: virtioa.qcow2 is virtio, hda.qcow2 is
    ide, sataa/scsia/nvme-a likewise. Getting this wrong gives a guest that
    boots to a disk it cannot see.
  * Nothing in the tree records the version in a parseable field, the licence
    status, or whether the image was ever made to work. Those stay the
    operator's business and this does not pretend otherwise.

The sizing table below covers the templates people actually have. An unknown
prefix is still adopted — with conservative defaults and marked GUESS, because
an image that appears with a note to check its RAM is more use than one that
does not appear at all.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)

#: prefix -> (ram_mb, cpus, nic_model, graphical)
#: Values are EVE-NG's own template defaults for the common images. Where a
#: vendor ships several sizes the floor is used: a guest that boots slowly is
#: recoverable, one that is OOM-killed on start looks like a broken import.
TEMPLATES: dict[str, tuple[int, int, str, bool]] = {
    "viosl2":      (1024, 1, "e1000",         False),
    "vios":        (1024, 1, "e1000",         False),
    "csr1000vng":  (4096, 2, "virtio-net-pci", False),
    "csr1000v":    (4096, 2, "virtio-net-pci", False),
    "xrv9k":       (16384, 4, "virtio-net-pci", False),
    "xrv":         (4096, 2, "virtio-net-pci", False),
    "nxosv9k":     (8192, 2, "e1000",         False),
    "nxosv":       (4096, 2, "e1000",         False),
    "veos":        (2048, 1, "virtio-net-pci", False),
    "vmxvcp":      (2048, 1, "virtio-net-pci", False),
    "vmxvfp":      (4096, 3, "virtio-net-pci", False),
    "vmx":         (2048, 1, "virtio-net-pci", False),
    "vsrxng":      (4096, 2, "virtio-net-pci", False),
    "vsrx":        (2048, 2, "virtio-net-pci", False),
    "vqfxre":      (1024, 1, "virtio-net-pci", False),
    "vqfxpfe":     (2048, 1, "virtio-net-pci", False),
    "vjunosswitch":(5120, 4, "virtio-net-pci", False),
    "timos":       (2048, 2, "virtio-net-pci", False),
    "vyos":        (1024, 1, "virtio-net-pci", False),
    "paloalto":    (4096, 2, "virtio-net-pci", True),
    "fortinet":    (1024, 1, "virtio-net-pci", True),
    "vwlc":        (2048, 1, "e1000",         True),
    "linux":       (1024, 1, "virtio-net-pci", True),
    "win":         (4096, 2, "virtio-net-pci", True),
}

#: disk filename stem -> qemu bus. The letter suffix is the slot (a, b, c).
DISK_BUSES: dict[str, str] = {
    "virtio": "virtio",
    "hd":     "ide",
    "sata":   "sata",
    "scsi":   "scsi",
    "megasas": "scsi",
    "nvme":   "nvme",
}

DEFAULTS = (1024, 1, "virtio-net-pci", False)


def template_for(dirname: str) -> tuple[str, tuple[int, int, str, bool], bool]:
    """EVE-NG template id and sizing for a directory name.

    Longest prefix first: `viosl2-...` must not match `vios`.
    """
    for prefix in sorted(TEMPLATES, key=len, reverse=True):
        if dirname.startswith(prefix):
            return prefix, TEMPLATES[prefix], False
    return dirname.split("-")[0], DEFAULTS, True


def bus_for(disk: Path) -> str:
    """The bus a disk filename implies, or virtio if it says nothing.

    Prefix match, longest first — NOT stripping the slot letter off the end.
    `hda` has every one of its characters in [a-h], so rstrip("abcdefgh")
    reduced it to the empty string and the disk came back as virtio: an IDE
    guest handed a virtio disk it cannot see. `sataa` lost both trailing a's
    and became `sat`. Both looked plausible in the output and were wrong.
    """
    stem = disk.stem
    for prefix in sorted(DISK_BUSES, key=len, reverse=True):
        if stem.startswith(prefix):
            return DISK_BUSES[prefix]
    return "virtio"


def scan(root: Path) -> tuple[list[dict], list[str]]:
    qemu_dir = root / "addons" / "qemu"
    if not qemu_dir.is_dir():
        return [], [f"no {qemu_dir} — is --root the EVE-NG install root?"]

    found: list[dict] = []
    skipped: list[str] = []
    for d in sorted(p for p in qemu_dir.iterdir() if p.is_dir()):
        disks = sorted(
            f for f in d.iterdir()
            if f.is_file() and f.suffix in (".qcow2", ".vmdk", ".img")
        )
        if not disks:
            skipped.append(f"{d.name}: no disk image in the directory")
            continue
        kind, (ram, cpus, nic, graphical), guessed = template_for(d.name)
        # The first disk is the boot disk in every EVE-NG layout seen; extra
        # disks are recorded so a multi-disk guest is not silently halved.
        found.append({
            "name": d.name,
            "eveng_template": kind,
            "guessed": guessed,
            "image": str(disks[0]),
            "extra_disks": [str(x) for x in disks[1:]],
            "bytes": sum(f.stat().st_size for f in disks),
            "spec": {
                "ram_mb": ram, "cpus": cpus, "nic_model": nic,
                "disk_bus": bus_for(disks[0]), "graphical": graphical,
            },
        })
    return found, skipped


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default="/opt/unetlab",
                    help="EVE-NG install root (default: /opt/unetlab)")
    ap.add_argument("--apply", action="store_true",
                    help="create the templates. Without this, nothing is written.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--print-commands", action="store_true",
                    help="show the labtris-image commands and stop")
    ap.add_argument("--image-cmd",
                    default="/opt/labtris/.venv/bin/labtris-image",
                    help="path to labtris-image on the Labtris host")
    args = ap.parse_args()

    found, skipped = scan(Path(args.root))

    if args.json:
        print(json.dumps({"found": found, "skipped": skipped}, indent=2))
        return 0 if found else 1

    if not found:
        for s in skipped:
            print(f"  skipped  {s}")
        print("\nnothing to adopt.")
        return 1

    total = sum(f["bytes"] for f in found)
    print(f"\n{len(found)} image(s) under {args.root}/addons/qemu, {human(total)} total\n")
    print(f"  {'directory':<36} {'template':<13} {'RAM':>7} {'cpu':>3} {'bus':<7} note")
    print(f"  {'-'*36} {'-'*13} {'-'*7} {'-'*3} {'-'*7} ----")
    for f in found:
        s = f["spec"]
        note = "GUESS — check RAM/NIC" if f["guessed"] else ""
        if f["extra_disks"]:
            note = (note + f" +{len(f['extra_disks'])} disk(s)").strip()
        print(f"  {f['name'][:36]:<36} {f['eveng_template'][:13]:<13} "
              f"{s['ram_mb']:>6}M {s['cpus']:>3} {s['disk_bus']:<7} {note}")
    for s in skipped:
        print(f"\n  skipped  {s}")

    guesses = [f for f in found if f["guessed"]]
    if guesses:
        print(f"\n  {len(guesses)} directory name(s) matched no known EVE-NG template.")
        print("  They are still adoptable; their RAM and NIC are defaults, not facts.")

    # Emit `labtris-image add` rather than talking to the API directly.
    # That command already exists, is documented, stores the disk under its
    # content hash and writes into the cache the service actually reads. A
    # second path into the image store would be a second thing to keep
    # correct, and the first one to drift.
    cmds = []
    for f in found:
        s = f["spec"]
        cmds.append(
            f'{args.image_cmd} add {shlex.quote(f["image"])}'
            f' --name {shlex.quote(f["name"])}'
            f' --ram-mb {s["ram_mb"]}'
            f' --nic-model {s["nic_model"]}'
            f' --disk-bus {s["disk_bus"]}'
        )

    if args.print_commands or not args.apply:
        print("\nCommands to register these (run on the Labtris host):\n")
        for c in cmds:
            print(f"  sudo -u labtris {c}")
        if not args.apply:
            print("\nDry run — nothing was written.")
            print("Review the list above, then re-run with --apply on the Labtris host.")
        return 0

    # --apply only makes sense where the disks and the image store both are.
    if not Path(args.image_cmd.split()[0]).exists():
        die(f"{args.image_cmd.split()[0]} not found.\n"
            "       --apply has to run ON the Labtris host, after the images\n"
            "       have been copied there. On the EVE-NG box, use the default\n"
            "       dry run and copy the commands over.")

    ok = failed = 0
    for f, c in zip(found, cmds):
        print(f"\n==> {f['name']}")
        rc = subprocess.call(c, shell=True)
        if rc == 0:
            ok += 1
        else:
            failed += 1
            print(f"    FAILED (exit {rc}) — continuing with the rest")
    print(f"\n{ok} registered, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
