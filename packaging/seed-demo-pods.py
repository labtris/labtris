#!/usr/bin/env python3
"""Load the bundled demo pods as real labs, once, on first boot.

Shipping `packaging/demo-pods/*.pod.tar.gz` on the image is not the same
as bundling the labs: a fresh install shows an empty lab list, and the
pods only become labs if somebody knows they exist and runs `lab load`.
This closes that gap — after the first boot the AI-fabric labs are simply
there, on the canvas, wired.

Runs locally against the database rather than over HTTP, because the API
requires a session and no account exists yet on a fresh machine. That is
also why it is safe: it creates labs, and touches nothing about auth.

Idempotent by lab name. A pod whose lab already exists is skipped, so a
re-run after someone renames or deletes one does not resurrect it under a
second name. Failure of one pod does not stop the others — a demo lab is
a nicety, and an install that half-fails because a pod is malformed is
worse than one missing a lab.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

#: Which pods become labs on a fresh machine. Deliberately a list rather
#: than a glob: the directory also holds older pods kept for reference
#: (rdma-uet-demo arrives unwired and points at the superseded ue-stack
#: image), and seeding those would ship a broken first impression.
SEED = (
    "uet-pair",
    "rdma-pair",
    "p4-trim",
    "pfc-classes",
    "ai-fabric-uet",
)

#: Which pods this machine has already been offered, one name per line.
#:
#: It used to be an empty file whose mere existence meant "done", and the
#: unit carried ConditionPathExists=! on it. That made the first boot work
#: and every later release fail quietly: a pod added in 0.13 never reached a
#: machine installed on 0.12, because the stamp from the first boot said the
#: job was finished.
#:
#: Recording the names instead separates the two things that file was being
#: asked to mean. A pod listed here has been offered once and is never
#: offered again, so a lab someone deleted on purpose stays deleted. A pod
#: NOT listed here is new to this machine and gets seeded, which is what an
#: upgrade should do. Old empty stamps are read as "all of 0.12's pods",
#: which is what they meant.
STAMP = Path("/var/lib/labtris/demo-pods-seeded")

#: What an empty (pre-0.13) stamp file stands for: the set that existed when
#: the stamp was only a marker. Without this, upgrading a 0.12 machine would
#: re-offer all five and resurrect any the operator had deleted.
LEGACY_STAMP_MEANS = (
    "uet-pair",
    "rdma-pair",
    "p4-trim",
    "pfc-classes",
    "ai-fabric-uet",
)


def already_offered() -> set[str]:
    """Pod names this machine has been offered before."""
    try:
        text = STAMP.read_text()
    except OSError:
        return set()
    names = {line.strip() for line in text.splitlines() if line.strip()}
    # An empty stamp is the old format and means the 0.12 set.
    return names or set(LEGACY_STAMP_MEANS)


async def main() -> int:
    from sqlalchemy import select

    from labtris_api import pods
    from labtris_api.db import SessionLocal
    from labtris_api.models import Lab

    # Same resolution problem as the P4 builtins: on the container the
    # package is a wheel in site-packages and packaging/ lives at
    # /opt/labtris, so a __file__-relative path finds nothing.
    from labtris_api.config import packaging_dir

    pod_dir = packaging_dir("demo-pods")
    if not pod_dir.is_dir():
        print(f"no pod directory at {pod_dir}; nothing to seed")
        return 0

    offered = already_offered()
    loaded, skipped, failed = [], [], []
    async with SessionLocal() as session:
        existing = set(
            (await session.execute(select(Lab.name))).scalars().all()
        )
        for name in SEED:
            archive = pod_dir / f"{name}.pod.tar.gz"
            if not archive.exists():
                failed.append(f"{name}: missing {archive.name}")
                continue
            if name in offered:
                # Offered before. Whether it is still there is the operator's
                # business — this is the line that stops an upgrade undoing
                # someone's deletion.
                skipped.append(name)
                continue
            if name in existing:
                skipped.append(f"{name} (a lab of that name already exists)")
                offered.add(name)
                continue
            try:
                info = await pods.load(session, archive)
                loaded.append(f"{info['name']} ({info['node_count']} nodes)")
                offered.add(name)
            except Exception as exc:  # noqa: BLE001 — one bad pod must not stop the rest
                failed.append(f"{name}: {exc}")

    for label, items in (("loaded", loaded), ("skipped", skipped), ("failed", failed)):
        if items:
            print(f"{label}: {', '.join(items)}")

    # Record what has now been offered, including pods skipped because a lab
    # of that name already existed — those have been accounted for and should
    # not be offered again either. A pod that FAILED is deliberately not
    # recorded, so the next run retries it.
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text("\n".join(sorted(offered)) + "\n")
    except OSError as exc:
        print(f"could not write {STAMP}: {exc}")

    return 0


if __name__ == "__main__":
    # LABTRIS_PLUGINS_DISABLED is the name plugins.py actually reads;
    # LABTRIS_SKIP_PLUGINS, which this set before, is read by nothing.
    os.environ.setdefault("LABTRIS_PLUGINS_DISABLED", "aws")
    raise SystemExit(asyncio.run(main()))
