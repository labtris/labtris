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


async def resolve_placeholders(session, plan) -> list[str]:
    """Point vendor nodes at images this machine actually has.

    parse_unl maps a fixed set of EVE-NG templates to runtimes; anything else
    — every vendor appliance — becomes an alpine placeholder carrying
    LABTRIS_ORIGINAL_TEMPLATE. That is right for a bare import and wrong
    straight after a migration, which is the whole reason the images were
    copied. Without this step someone moves 100GB of Palo Alto disks and
    still gets five alpine containers.

    Matched on the template name being a prefix of the registered template,
    with a separator after it, so `paloalto` finds `paloalto-12.2.5-rel` and
    `vios` does not claim `viosl2-...`.

    Where several versions are registered one is picked and the others are
    NAMED, rather than claiming to have chosen well. Sorting by name is not
    version order — among 12.1.2-rel, 12.2.5-rel, 13.0-main and
    release-cosmos-nebula-11.1.main, the last sorts highest and is the
    oldest. There is no reliable version in these strings, so the honest
    thing is to pick deterministically, say what else was available, and let
    the node be switched on the canvas.
    """
    from sqlalchemy import select

    from labtris_api.models import Template

    rows = (await session.execute(select(Template))).scalars().all()
    if not rows:
        return []

    notes: list[str] = []
    for node in plan.nodes:
        want = node.env.get("LABTRIS_ORIGINAL_TEMPLATE")
        if not want:
            continue
        cands = [
            t for t in rows
            if t.name.startswith(want)
            and (len(t.name) == len(want) or t.name[len(want)] in "-._")
        ]
        if not cands:
            continue
        pick = sorted(cands, key=lambda t: t.name)[0]
        node.runtime = pick.runtime
        node.image = pick.image
        node.env.pop("LABTRIS_ORIGINAL_TEMPLATE", None)
        if len(cands) == 1:
            notes.append(f"{node.name}: using {pick.name}")
        else:
            others = ", ".join(t.name for t in sorted(cands, key=lambda t: t.name)[1:])
            notes.append(
                f"{node.name}: using {pick.name} "
                f"(also registered: {others} — switch it on the canvas if wrong)"
            )
    return notes


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
        resolved = await resolve_placeholders(session, plan)
        lab, warnings = await realize_plan(session, plan)
        await session.commit()

    for line in resolved:
        print(f"  {line}")

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
