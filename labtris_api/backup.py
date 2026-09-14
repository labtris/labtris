from __future__ import annotations

import io
import json
import tarfile
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.models import Lab, Setting

ARCHIVE_VERSION = "labtris-backup-v1"


async def build_archive(session: AsyncSession, include_settings: bool = True) -> bytes:
    """Every lab's topology, plus optionally the instance's own settings, as
    one gzipped tar.

    Deliberately configuration only. A qemu image is 0.7-6 GB and is a
    verbatim copy of something already on the internet with a known URL — the
    catalog id is worth backing up, the bytes are not, and putting them in
    here would turn a two-second export into an hour and make restore
    unusable. Images re-download on first start."""
    from labtris_api.routers.labs import export_lab_payload

    buf = io.BytesIO()
    labs = list((await session.execute(select(Lab).order_by(Lab.name))).scalars())
    manifest: dict[str, Any] = {
        "format": ARCHIVE_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "labs": [],
    }

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def add(name: str, payload: Any) -> None:
            data = json.dumps(payload, indent=2, default=str).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))

        for lab in labs:
            payload = await export_lab_payload(session, lab.id)
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in lab.name)
            add(f"labs/{safe}-{lab.id[-6:]}.json", payload)
            manifest["labs"].append({"id": lab.id, "name": lab.name, "file": safe})

        if include_settings:
            rows = (await session.execute(select(Setting))).scalars()
            # Secrets are excluded on purpose: a backup file is the thing most
            # likely to end up somewhere it should not be.
            add(
                "settings.json",
                {r.key: r.value for r in rows if "key" not in r.key and "password" not in r.key},
            )
        add("manifest.json", manifest)

    return buf.getvalue()


def read_manifest(blob: bytes) -> dict[str, Any]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        member = tar.extractfile("manifest.json")
        if member is None:
            raise ValueError("archive has no manifest.json")
        return json.loads(member.read())  # type: ignore[no-any-return]


def labs_in(blob: bytes) -> list[dict[str, Any]]:
    """Every lab payload in an archive, ready to hand to the importer."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for info in tar.getmembers():
            if not info.name.startswith("labs/") or not info.name.endswith(".json"):
                continue
            member = tar.extractfile(info)
            if member is not None:
                out.append(json.loads(member.read()))
    return out
