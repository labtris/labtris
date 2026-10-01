#!/usr/bin/env python3
"""Import an EVE-NG .unl straight into the database.

    /opt/labtris/.venv/bin/python packaging/import-unl.py <file.unl> [--name NAME]

There is no `labtris lab import` — the importer is an API endpoint
(POST /labs/import/topology) and reaching it needs a logged-in session. The
migration runs as the labtris service user with the database to hand and no
token, exactly like packaging/seed-demo-pods.py, so it does the same thing:
talk to the database directly and reuse the API's own parse_unl and
realize_plan rather than keeping a second implementation of either.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("unl", help="the .unl file")
    ap.add_argument("--name", help="name for the imported lab (default: from the file)")
    args = ap.parse_args()

    from labtris_api.db import SessionLocal
    from labtris_api.migrate import parse_unl
    from labtris_api.routers.labs import realize_plan

    try:
        xml = Path(args.unl).read_text()
    except OSError as exc:
        print(f"could not read {args.unl}: {exc}", file=sys.stderr)
        return 1

    try:
        plan = parse_unl(xml, args.name or Path(args.unl).stem)
    except ValueError as exc:
        print(f"could not parse: {exc}", file=sys.stderr)
        return 1

    if args.name:
        plan.name = args.name
    if not plan.nodes:
        print("that topology has no nodes we could import", file=sys.stderr)
        return 1

    async with SessionLocal() as session:
        lab, warnings = await realize_plan(session, plan)
        await session.commit()

    print(f"imported '{lab.name}' ({len(plan.nodes)} nodes, {len(plan.links)} links)")
    # Warnings are the useful part: they name every node that came across as a
    # placeholder, which is exactly the list of images still to bring over.
    for w in warnings[:12]:
        print(f"  note: {w}")
    if len(warnings) > 12:
        print(f"  ... and {len(warnings) - 12} more")
    return 0


if __name__ == "__main__":
    # The real name. LABTRIS_SKIP_PLUGINS, which this used to set, is read by
    # nothing — plugins.py looks at LABTRIS_PLUGINS_DISABLED, a comma list of
    # plugin names. The old spelling silently did nothing, which is why every
    # invocation printed 150 lines of AWS service registration.
    os.environ.setdefault("LABTRIS_PLUGINS_DISABLED", "aws")
    raise SystemExit(asyncio.run(main()))
