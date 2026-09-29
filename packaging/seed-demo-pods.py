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

STAMP = Path("/var/lib/labtris/demo-pods-seeded")


async def main() -> int:
    from sqlalchemy import select

    from labtris_api import pods
    from labtris_api.db import SessionLocal
    from labtris_api.models import Lab

    pod_dir = ROOT / "packaging" / "demo-pods"
    if not pod_dir.is_dir():
        print(f"no pod directory at {pod_dir}; nothing to seed")
        return 0

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
            if name in existing:
                skipped.append(name)
                continue
            try:
                info = await pods.load(session, archive)
                loaded.append(f"{info['name']} ({info['node_count']} nodes)")
            except Exception as exc:  # noqa: BLE001 — one bad pod must not stop the rest
                failed.append(f"{name}: {exc}")

    for label, items in (("loaded", loaded), ("skipped", skipped), ("failed", failed)):
        if items:
            print(f"{label}: {', '.join(items)}")

    # Stamp regardless: this runs once by design. An operator who wants it
    # again deletes the stamp, which is easier to discover than a unit that
    # silently re-seeds every boot and fights their deletions.
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text("")
    except OSError as exc:
        print(f"could not write {STAMP}: {exc}")

    return 0


if __name__ == "__main__":
    os.environ.setdefault("LABTRIS_SKIP_PLUGINS", "1")
    raise SystemExit(asyncio.run(main()))
