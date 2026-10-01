#!/usr/bin/env python3
"""Migrate labs and images from a running EVE-NG box. Run this ON Labtris.

    sudo -u labtris ./packaging/migrate-from-eveng.py
    sudo -u labtris ./packaging/migrate-from-eveng.py --host 10.0.0.9 --user root

It asks for the EVE-NG address, looks over SSH at what is there — the image
library, the .unl labs, and how big each is — shows you the inventory, and
lets you pick what to bring across by name. Then it copies only the disks the
things you picked actually need, registers them, and imports the labs.

ON PASSWORDS: this never reads, stores or forwards one. It opens a single SSH
connection up front and lets ssh do its own authentication, so a password goes
from your terminal to ssh and nowhere near this script. Every later operation
reuses that one connection, so you authenticate once.

COLD AND HOT

  cold (default)  the topology and the pristine base images. You get the
                  lab's shape and factory-default devices, and you configure
                  them yourself. Small, fast, and what you want when the
                  point is the topology.

  hot             the topology and each node's WORKING disk — the one with
                  the configuration actually applied to it. Someone forty
                  hours into a CCIE lab does not want the shape; they want
                  their configuration. Much larger, because a working disk
                  has to be flattened to stand alone.

Hot is not simply "copy a different file". EVE-NG's runtime disks are qcow2
deltas whose backing file is the base image at an /opt/unetlab path that does
not exist here, so copying a delta alone produces a disk that will not open.
They are flattened with `qemu-img convert` ON the EVE-NG box first, which
needs free space there and takes time proportional to what the guest has
written.

WHY ONE COMMAND AND NOT THREE

The manual route is: scan on EVE-NG, rsync the tree, scan the copy, register.
That works and it asks someone to move the whole library — commonly hundreds
of gigabytes — before they can tell whether one lab comes across. Picking a
lab first and copying only its disks turns an afternoon into a few minutes,
which is the difference between evaluating Labtris and not bothering.
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

G, R, Y, D, N = "\033[0;32m", "\033[1;31m", "\033[1;33m", "\033[0;90m", "\033[0m"


def say(msg: str) -> None:
    print(f"\n{D}==>{N} {msg}")


def die(msg: str) -> int:
    print(f"{R}ERROR:{N} {msg}", file=sys.stderr)
    return 2


class Remote:
    """One authenticated SSH connection, reused for everything.

    ControlMaster is the whole point: without it each scan, each rsync and
    each file read prompts again, and a migration that asks for a password
    eleven times is one nobody finishes. The socket lives in a private temp
    directory and the connection is closed on exit.
    """

    def __init__(self, host: str, user: str) -> None:
        self.target = f"{user}@{host}"
        self.dir = tempfile.mkdtemp(prefix="labtris-eveng-")
        self.sock = os.path.join(self.dir, "ctl")
        atexit.register(self.close)

    def _base(self) -> list[str]:
        return ["ssh", "-o", f"ControlPath={self.sock}"]

    def connect(self) -> bool:
        print(f"\nConnecting to {self.target} — ssh will ask for your password or key.")
        rc = subprocess.call([
            "ssh", "-M", "-S", self.sock, "-o", "ControlPersist=600",
            "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new",
            "-f", "-N", self.target,
        ])
        return rc == 0

    def run(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        p = subprocess.run(
            self._base() + [self.target, cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout

    def pull(self, src: str, dst: Path) -> int:
        dst.parent.mkdir(parents=True, exist_ok=True)
        return subprocess.call([
            "rsync", "-aP", "--info=progress2",
            "-e", f"ssh -o ControlPath={self.sock}",
            f"{self.target}:{src}", str(dst),
        ])

    def close(self) -> None:
        if os.path.exists(self.sock):
            subprocess.call(self._base() + ["-O", "exit", self.target],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.dir, ignore_errors=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def scan_images(r: Remote, root: str) -> list[dict]:
    """Image directories with their boot disk and total size, in one SSH call.

    `du` per directory would be one round trip each; on a library of fifty
    images over a slow link that is the difference between instant and a
    minute of silence.
    """
    cmd = (
        f"find {shlex.quote(root)}/addons/qemu -mindepth 1 -maxdepth 1 -type d "
        f"-printf '%f\\n' 2>/dev/null | while read -r d; do "
        f"  sz=$(du -sb {shlex.quote(root)}/addons/qemu/\"$d\" 2>/dev/null | cut -f1); "
        f"  dk=$(ls {shlex.quote(root)}/addons/qemu/\"$d\"/*.qcow2 "
        f"      {shlex.quote(root)}/addons/qemu/\"$d\"/*.vmdk 2>/dev/null | head -1); "
        f"  printf '%s\\t%s\\t%s\\n' \"$d\" \"${{sz:-0}}\" \"$dk\"; done"
    )
    rc, out = r.run(cmd, timeout=180)
    images = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0]:
            continue
        name, size, disk = parts
        images.append({
            "name": name, "bytes": int(size or 0), "disk": disk,
            "has_disk": bool(disk.strip()),
        })
    return sorted(images, key=lambda i: i["name"])


def scan_labs(r: Remote, root: str) -> list[dict]:
    """.unl files, with the EVE-NG templates each one uses.

    The templates matter more than the lab name: they are what decides which
    disks have to come across, and a lab whose templates are all unknown is
    one that will import as placeholders however it is handled.
    """
    rc, out = r.run(
        f"find {shlex.quote(root)}/labs -name '*.unl' -type f -printf '%p\\t%s\\n' 2>/dev/null",
        timeout=120,
    )
    labs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        path, size = parts
        rc2, xml = r.run(f"cat {shlex.quote(path)}", timeout=60)
        templates = sorted(set(re.findall(r'template="([^"]+)"', xml)))
        nodes = len(re.findall(r"<node\b", xml))
        # The lab's own id is how a runtime directory is tied back to it:
        # EVE-NG names the working-state directory after this uuid.
        m = re.search(r'<lab\b[^>]*\bid="([^"]+)"', xml)
        labs.append({
            "path": path,
            "name": Path(path).stem,
            "bytes": int(size or 0),
            "nodes": nodes,
            "templates": templates,
            "uuid": m.group(1) if m else "",
        })
    return sorted(labs, key=lambda l: l["name"])


def scan_runtime_disks(r: Remote, root: str) -> list[dict]:
    """Per-node working disks, found rather than assumed.

    EVE-NG puts running state under /opt/unetlab/tmp/<pod>/<lab-uuid>/<node>/,
    but that shape has changed across versions and some installs relocate it.
    So: find the qcow2 files and ask qemu-img what each one is. A disk with a
    backing file is a delta over a base image and is the interesting case; one
    without is already standalone.

    `qemu-img info` per file is a round trip each, which is why this only runs
    when hot mode is actually asked for.
    """
    rc, out = r.run(
        f"find {shlex.quote(root)}/tmp -name '*.qcow2' -type f 2>/dev/null | head -400",
        timeout=120,
    )
    paths = [l.strip() for l in out.splitlines() if l.strip()]
    if not paths:
        return []

    # One shell loop rather than one ssh per file.
    script = (
        "for f in " + " ".join(shlex.quote(p) for p in paths) + "; do "
        "  b=$(qemu-img info --output=json \"$f\" 2>/dev/null | "
        "      sed -n 's/.*\"backing-filename\": \"\\([^\"]*\\)\".*/\\1/p' | head -1); "
        "  s=$(stat -c %s \"$f\" 2>/dev/null); "
        "  printf '%s\\t%s\\t%s\\n' \"$f\" \"${s:-0}\" \"${b:-}\"; done"
    )
    rc, out = r.run(script, timeout=300)
    disks = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        path, size = parts[0], parts[1]
        backing = parts[2] if len(parts) > 2 else ""
        pp = Path(path)
        disks.append({
            "path": path,
            "bytes": int(size or 0),
            "backing": backing,
            # The parent directory is the node id; its parent is the lab uuid.
            "node": pp.parent.name,
            "lab_uuid": pp.parent.parent.name,
        })
    return disks


def images_for_templates(templates: list[str], images: list[dict]) -> list[dict]:
    """Which image directories serve these EVE-NG templates.

    EVE-NG names an image directory `<template>-<version>`, so the template
    is a prefix — but the next character has to be a separator, not another
    letter. A plain startswith() made a lab using `vios` also pull
    `viosl2-adventerprisek9-m`, which is a different image and commonly
    gigabytes, so selecting one small lab started copying things it never
    referenced.

    Strictness is right rather than merely safer: EVE-NG labs name
    `vmxvcp` and `vmxvfp` directly as separate templates, so nothing depends
    on `vmx` loosely matching them.
    """
    out = []
    for t in templates:
        for im in images:
            name = im["name"]
            if not name.startswith(t) or im in out:
                continue
            rest = name[len(t):]
            if rest and rest[0] not in "-._":
                continue
            out.append(im)
    return out


def letter_labels(n: int) -> list[str]:
    """a, b, ... z, aa, ab — spreadsheet style.

    Images are lettered and labs are numbered so a selection can only mean
    one list. Typing `3` when the images were also numbered 1..n was the
    problem: it was impossible to tell from the input which list was meant,
    and the only recovery was asking twice.
    """
    out = []
    for i in range(n):
        label, k = "", i
        while True:
            label = chr(ord("a") + k % 26) + label
            k = k // 26 - 1
            if k < 0:
                break
        out.append(label)
    return out


def choose(prompt: str, items: list[str], labels: list[str]) -> list[int]:
    """Pick by label, range, name substring, 'all', or nothing.

    Labels are matched case-insensitively and a range works for either kind:
    `2-4` over numbers and `a-d` over letters, by position in the label list
    rather than by arithmetic — `y-ab` has to work as well as `a-c`.
    """
    kind = "numbers" if labels and labels[0].isdigit() else "letters"
    example = "2-4" if kind == "numbers" else "a-d"
    print(f"\n{prompt}")
    print(f"  {D}{kind} ({' '.join(labels[:3])}), a range ({example}), "
          f"names, 'all', or Enter for none{N}")
    raw = input("  > ").strip()
    if not raw:
        return []
    if raw.lower() == "all":
        return list(range(len(items)))

    index = {l.lower(): i for i, l in enumerate(labels)}
    picked: list[int] = []
    for tok in raw.replace(",", " ").split():
        low = tok.lower()
        if "-" in low:
            a, b = low.split("-", 1)
            if a in index and b in index:
                lo, hi = sorted((index[a], index[b]))
                picked += list(range(lo, hi + 1))
                continue
        if low in index:
            picked.append(index[low])
            continue
        # Not a label — treat it as a name search, which is how someone
        # selects six related labs without reading off six labels.
        hits = [i for i, name in enumerate(items) if low in name.lower()]
        if hits:
            picked += hits
        else:
            print(f"  {Y}ignored{N} {tok!r} — not a label and matches no name")
    return sorted(set(picked))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", help="EVE-NG address (asked for if omitted)")
    ap.add_argument("--user", default="root", help="SSH user on EVE-NG (default: root)")
    ap.add_argument("--eveng-root", default="/opt/unetlab", help="EVE-NG install root")
    ap.add_argument("--dest", default="/var/lib/labtris/eveng-import",
                    help="where to put the copied disks on this host")
    ap.add_argument("--image-cmd", default="/opt/labtris/.venv/bin/labtris-image")
    ap.add_argument("--labtris-cmd", default="/opt/labtris/.venv/bin/labtris")
    ap.add_argument("--mode", choices=("cold", "hot"), default=None,
                    help="skip the question: cold is topology + base images, "
                         "hot also brings each node's configured disk.")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and show the plan, copy and import nothing")
    args = ap.parse_args()

    for tool in ("ssh", "rsync"):
        if not shutil.which(tool):
            return die(f"{tool} is not installed, and this needs it")

    host = args.host or input("EVE-NG address: ").strip()
    if not host:
        return die("no address given")

    # Asked rather than flagged. Someone running this for the first time does
    # not know the word "hot" means their running configuration, and a
    # default that silently leaves it behind is the wrong one to be quiet
    # about.
    if args.mode is None:
        print("\nWhat do you want to bring across?")
        print(f"  {G}1{N}  the labs and their base images")
        print(f"     {D}the topology, with devices at factory defaults. Small and quick.{N}")
        print(f"  {G}2{N}  that, plus each node's configured disk")
        print(f"     {D}what you actually built — the running configuration. Much larger,{N}")
        print(f"     {D}and each disk is flattened on the EVE-NG box before it moves.{N}")
        pick = input("  [1] > ").strip() or "1"
        args.mode = "hot" if pick.startswith("2") else "cold"

    r = Remote(host, args.user)
    if not r.connect():
        return die("could not connect. Check the address, the user, and that\n"
                   "       sshd on the EVE-NG box accepts your key or password.")

    rc, _ = r.run(f"test -d {shlex.quote(args.eveng_root)}/addons/qemu && echo yes")
    if rc != 0:
        return die(f"{args.eveng_root}/addons/qemu is not there.\n"
                   f"       Point --eveng-root at the directory holding addons/qemu.")

    say("Reading the image library")
    images = scan_images(r, args.eveng_root)
    say("Reading the labs")
    labs = scan_labs(r, args.eveng_root)

    runtime: list[dict] = []
    if args.mode == "hot":
        say("Looking for working disks (hot mode)")
        runtime = scan_runtime_disks(r, args.eveng_root)
        if not runtime:
            print(f"  {Y}none found{N} — no lab has been started on that box, or its")
            print(f"  runtime directory is elsewhere. Falling back to cold.")
            args.mode = "cold"

    if not images and not labs:
        return die("found no images and no labs — is this an EVE-NG install?")

    # ---------------------------------------------------------------- show
    if labs:
        print(f"\n{G}Labs{N} ({len(labs)})\n")
        print(f"  {'#':>3}  {'name':<34} {'nodes':>5}  templates")
        for i, l in enumerate(labs, 1):
            t = ", ".join(l["templates"][:4]) or "—"
            if len(l["templates"]) > 4:
                t += f" +{len(l['templates']) - 4}"
            print(f"  {i:>3}  {l['name'][:34]:<34} {l['nodes']:>5}  {t[:46]}")

    img_labels = letter_labels(len(images))
    if images:
        total = sum(i["bytes"] for i in images)
        print(f"\n{G}Images{N} ({len(images)}, {human(total)} total)\n")
        print(f"  {'id':>3}  {'directory':<42} {'size':>9}")
        for lbl, im in zip(img_labels, images):
            flag = "" if im["has_disk"] else f"  {Y}no disk file{N}"
            print(f"  {lbl:>3}  {im['name'][:42]:<42} {human(im['bytes']):>9}{flag}")

    # -------------------------------------------------------------- choose
    lab_pick = choose("Which labs? Their images are copied automatically.",
                      [l["name"] for l in labs],
                      [str(i) for i in range(1, len(labs) + 1)]) if labs else []
    chosen_labs = [labs[i] for i in lab_pick]

    needed = images_for_templates(
        sorted({t for l in chosen_labs for t in l["templates"]}), images
    )
    if needed:
        print(f"\n{D}Those labs need:{N} " + ", ".join(i["name"] for i in needed))

    hot_for_labs: list[dict] = []
    if args.mode == "hot" and chosen_labs:
        uuids = {l["uuid"] for l in chosen_labs if l["uuid"]}
        hot_for_labs = [d for d in runtime if d["lab_uuid"] in uuids]
        if hot_for_labs:
            hot_bytes = sum(d["bytes"] for d in hot_for_labs)
            print(f"\n{G}Working disks{N} for the labs you picked: "
                  f"{len(hot_for_labs)} node(s), {human(hot_bytes)} before flattening")
            print(f"  {D}flattened copies are larger — a delta only holds what the"
                  f" guest wrote{N}")
        else:
            print(f"\n  {Y}No working disks for those labs.{N} They have not been")
            print(f"  started on that box, so there is no configured state to bring.")
            print(f"  The topology and base images still come across.")

    extra_pick = choose("Any other images? (beyond the ones above)",
                        [i["name"] for i in images], img_labels) if images else []
    for i in extra_pick:
        if images[i] not in needed:
            needed.append(images[i])

    if not chosen_labs and not needed:
        print("\nNothing selected. Nothing done.")
        return 0

    copy_bytes = sum(i["bytes"] for i in needed)
    print(f"\n{G}Plan{N}  ({args.mode})")
    print(f"  labs to import:   {len(chosen_labs)}")
    print(f"  images to copy:   {len(needed)}  ({human(copy_bytes)})")
    if hot_for_labs:
        print(f"  working disks:    {len(hot_for_labs)}  "
              f"({human(sum(d['bytes'] for d in hot_for_labs))} before flattening)")
        print(f"  {D}each is flattened on the EVE-NG box first, which needs free"
              f" space there{N}")
    print(f"  destination:      {args.dest}")

    if args.dry_run:
        print(f"\n{Y}--dry-run — nothing copied or imported.{N}")
        return 0

    if input("\nGo ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Stopped.")
        return 0

    # ----------------------------------------------------------- copy + add
    dest_q = Path(args.dest) / "addons" / "qemu"
    failed: list[str] = []
    for im in needed:
        if not im["has_disk"]:
            print(f"  {Y}skip{N} {im['name']}: no disk file in the directory")
            continue
        say(f"Copying {im['name']} ({human(im['bytes'])})")
        if r.pull(f"{args.eveng_root}/addons/qemu/{im['name']}/", dest_q / im["name"]) != 0:
            failed.append(f"copy {im['name']}")
            continue
        if shutil.which(args.image_cmd) or Path(args.image_cmd).exists():
            local_disk = dest_q / im["name"] / Path(im["disk"]).name
            say(f"Registering {im['name']}")
            if subprocess.call([args.image_cmd, "add", str(local_disk),
                                "--name", im["name"]]) != 0:
                failed.append(f"register {im['name']}")
        else:
            print(f"  {Y}not registered{N}: {args.image_cmd} not found — the disk is at")
            print(f"    {dest_q / im['name']}")

    # -------------------------------------------------- working disks (hot)
    #
    # Flattened on the EVE-NG side, not here. A delta's backing file is an
    # absolute /opt/unetlab path; copy the delta alone and qemu cannot open
    # it, and copying base+delta would mean rewriting the backing path after
    # transfer. `qemu-img convert` produces a disk that stands on its own,
    # which is the only form worth carrying to another machine.
    for d in hot_for_labs:
        label = f"{d['lab_uuid'][:8]}/{d['node']}"
        say(f"Flattening working disk {label}")
        flat = f"/tmp/labtris-hot-{d['lab_uuid'][:8]}-{d['node']}.qcow2"
        rc, _ = r.run(
            f"qemu-img convert -O qcow2 {shlex.quote(d['path'])} {shlex.quote(flat)}",
            timeout=3600,
        )
        if rc != 0:
            failed.append(f"flatten {label}")
            print(f"  {R}failed{N} — out of space on the EVE-NG box, or qemu-img missing")
            continue

        lab_name = next((l["name"] for l in chosen_labs
                         if l["uuid"] == d["lab_uuid"]), d["lab_uuid"][:8])
        name = f"{lab_name}-{d['node']}"
        dest = Path(args.dest) / "hot" / f"{name}.qcow2"
        say(f"Copying {label}")
        if r.pull(flat, dest) != 0:
            failed.append(f"copy {label}")
        elif Path(args.image_cmd).exists():
            if subprocess.call([args.image_cmd, "add", str(dest), "--name", name]) != 0:
                failed.append(f"register {label}")
        # Remove the flattened copy either way: it is a full-size duplicate
        # sitting in /tmp on someone else's machine.
        r.run(f"rm -f {shlex.quote(flat)}", timeout=60)

    # -------------------------------------------------------------- labs
    for l in chosen_labs:
        say(f"Importing lab {l['name']}")
        rc, xml = r.run(f"cat {shlex.quote(l['path'])}", timeout=60)
        if rc != 0 or not xml.strip():
            failed.append(f"read {l['name']}")
            continue
        tmp = Path(tempfile.gettempdir()) / f"{l['name']}.unl"
        tmp.write_text(xml)
        if Path(args.labtris_cmd).exists():
            if subprocess.call([args.labtris_cmd, "lab", "import", str(tmp)]) != 0:
                failed.append(f"import {l['name']}")
        else:
            print(f"  {Y}not imported{N}: {args.labtris_cmd} not found. The file is at {tmp}")

    print()
    if failed:
        print(f"{R}{len(failed)} step(s) failed:{N} " + ", ".join(failed))
        return 1
    print(f"{G}Done.{N} Open the Labtris canvas — the imported labs are there.")
    print(f"{D}Vendor nodes whose image was not copied import as placeholders;{N}")
    print(f"{D}re-run and pick those images to replace them.{N}")
    if hot_for_labs:
        print(f"{D}Working disks were registered as <lab>-<node>. Point each node at{N}")
        print(f"{D}its own disk to get the configuration back, rather than the base.{N}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
