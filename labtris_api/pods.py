"""Lab snapshots — a lab's entire state packaged into `labtris-pod-v1.tar.gz`.

Modelled on LocalStack's `pod save/load`. A pod archive is a portable
document that reproduces a lab on any Labtris host: topology, hooks,
templates, and — for QEMU nodes — the flattened qcow2 disk state at the
moment of capture.

Phase C ships cold snapshots only: the lab must be stopped before save.
Cold covers the teaching use case ("here is a lab ready to run") fully
and keeps the writer / loader simple. Phase D adds hot snapshots
(QMP snapshot-save for QEMU + docker commit for containers) on top of
this same module.

Archive shape
-------------

    snapshot.json                # manifest — this module's schema
    lab.json                     # export_lab_payload output (labtris-lab-v1)
    hooks.yml                    # the lab's hooks_source (present iff set)
    templates.json               # every Template referenced by any node
    nodes/<node_id>/disk.qcow2   # QEMU: flattened qcow2 from _install_custom
                                 # Docker: absent (image ref carries the state)

Restore semantics
-----------------

Load always creates a NEW lab with fresh ids. The safer default —
mirrors `_clone_topology` in `routers/labs.py`, which is the existing
"import a labtris-lab-v1 payload" path this reuses. QEMU disks are
installed into the local `custom-<sha>.qcow2` cache and each new node's
`image` is rewritten to `custom:<sha>` so the very first start after
restore boots from the snapshot state. Subsequent changes accumulate
in a fresh overlay — the pod itself is never mutated.

Non-goals for v1
----------------

- Cross-host transport. Copy the .tar.gz yourself (scp, drive, USB).
- Proprietary vendor image bytes. If `node.image` is a catalog id, the
  target host must have it (or pull it) — the pod carries state, not
  licensed images.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.config import settings
from labtris_api.errors import bad_request, conflict, not_found, runtime_error
from labtris_api.models import Geometry, Lab, Node, Template
from labtris_api.version import __version__

logger = structlog.get_logger(__name__)


POD_FORMAT = "labtris-pod-v1"


def pods_dir() -> Path:
    p = Path(settings.pod_dir).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def pod_path(pod_id: str) -> Path:
    """Where a pod tarball lives on disk. `pod_id` is a stable id chosen
    at save time (a ULID-like string is fine); the file name carries the
    lab name for human legibility."""
    return pods_dir() / f"{pod_id}.tar.gz"


async def save_cold(session: AsyncSession, lab_id: str) -> dict[str, Any]:
    """Write a cold snapshot of `lab_id` to `pods_dir()`.

    Refuses if any node is running — cold means "at power-off state".
    Returns the pod's summary dict (`pod_id`, `path`, `bytes`, `node_count`).
    """
    from labtris_api.routers.labs import export_lab_payload
    from labtris_api.runtime.qemu import _install_custom, flatten_node_disk

    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise not_found(f"lab {lab_id}")
    nodes = (
        await session.execute(select(Node).where(Node.lab_id == lab_id))
    ).scalars().all()
    running = [n for n in nodes if n.state == "running"]
    if running:
        names = ", ".join(n.name for n in running[:5])
        raise bad_request(
            f"cold snapshot needs the lab stopped — still running: {names}"
            + (" (…)" if len(running) > 5 else "")
        )

    payload = await export_lab_payload(session, lab_id)
    hooks_source = lab.hooks_source

    # Collect referenced templates.
    template_ids = {n.image for n in nodes}
    tmpls = (
        await session.execute(select(Template).where(Template.image.in_(template_ids)))
    ).scalars().all()
    templates_payload = [
        {
            "id": t.id,
            "name": t.name,
            "image": t.image,
            "runtime": t.runtime,
            "spec": t.spec,
            "env": t.env,
            "description": t.description,
        }
        for t in tmpls
    ]

    # Flatten each QEMU node's disk. Record sha for the manifest.
    node_manifest: list[dict[str, Any]] = []
    disk_files: dict[str, Path] = {}  # node_id -> local path to the disk we'll tar in
    for n in nodes:
        entry: dict[str, Any] = {
            "id": n.id,
            "name": n.name,
            "runtime": n.runtime,
            "image": n.image,
        }
        if n.runtime == "qemu":
            # A QEMU node that has never been started has no disk to
            # capture — nothing to snapshot beyond its topology entry,
            # which is already in lab.json. On restore it will boot
            # fresh from its template, same as a new node. This is
            # also what "cold + never started" naturally means.
            from labtris_api.runtime.qemu import _vm_dir

            overlay = _vm_dir(n.id) / "disk.qcow2"
            if not overlay.exists():
                entry["disk_status"] = "never_started"
            else:
                try:
                    info = await flatten_node_disk(n.id)
                except Exception as exc:  # noqa: BLE001
                    raise runtime_error(
                        f"flattening node {n.name!r} failed: {exc}"
                    ) from exc
                entry["disk_sha256_prefix"] = info["digest"]
                entry["disk_bytes"] = info["bytes"]
                disk_files[n.id] = Path(info["path"])
        node_manifest.append(entry)

    pod_id = _mint_pod_id(lab.name)
    manifest = {
        "format": POD_FORMAT,
        "labtris_version": __version__,
        "pod_id": pod_id,
        "lab_id": lab_id,
        "lab_name": lab.name,
        "mode": "cold",
        "created_at": datetime.now(UTC).isoformat(),
        "node_count": len(nodes),
        "nodes": node_manifest,
        "has_hooks": bool(hooks_source),
    }

    out_path = pod_path(pod_id)
    tmp_path = out_path.with_suffix(".tar.gz.tmp")
    _write_archive(tmp_path, manifest, payload, hooks_source, templates_payload, disk_files)
    tmp_path.rename(out_path)
    size = out_path.stat().st_size
    logger.info(
        "pod.save.cold",
        lab_id=lab_id,
        pod_id=pod_id,
        node_count=len(nodes),
        bytes=size,
    )
    # `_install_custom` result path is the canonical location for these
    # flattened disks — deliberately don't unlink; other pods/templates
    # may point at the same content-addressed file.
    _ = _install_custom  # silence: imported only for its side-effect story
    return {
        "pod_id": pod_id,
        "path": str(out_path),
        "bytes": size,
        "node_count": len(nodes),
        "mode": "cold",
        "created_at": manifest["created_at"],
    }


def _write_archive(
    dst: Path,
    manifest: dict[str, Any],
    lab_payload: dict[str, Any],
    hooks_source: str | None,
    templates_payload: list[dict[str, Any]],
    disk_files: dict[str, Path],
) -> None:
    """Assemble the tar.gz atomically at `dst`. The caller renames the .tmp
    file into place on success — a partial write must never be observable
    as a valid pod."""
    with tarfile.open(dst, "w:gz") as tar:
        _add_json(tar, "snapshot.json", manifest)
        _add_json(tar, "lab.json", lab_payload)
        _add_json(tar, "templates.json", templates_payload)
        if hooks_source is not None:
            _add_text(tar, "hooks.yml", hooks_source)
        for node_id, src in disk_files.items():
            if not src.exists():
                # Shouldn't happen — flatten_node_disk verified this — but
                # a race with a manual delete deserves a real error rather
                # than a silent short tar.
                raise runtime_error(f"flattened disk vanished for {node_id}: {src}")
            tar.add(str(src), arcname=f"nodes/{node_id}/disk.qcow2")


def _add_json(tar: tarfile.TarFile, name: str, obj: Any) -> None:
    data = json.dumps(obj, indent=2, sort_keys=True, default=str).encode()
    _add_bytes(tar, name, data)


def _add_text(tar: tarfile.TarFile, name: str, text: str) -> None:
    _add_bytes(tar, name, text.encode())


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _mint_pod_id(lab_name: str) -> str:
    """Filesystem-safe id — the lab name for legibility plus a random tail
    so two pods of the same lab don't collide."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in lab_name)[:40] or "lab"
    return f"{safe}-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def list_pods() -> list[dict[str, Any]]:
    """Enumerate local pods with just enough metadata for a picker.

    Reads only `snapshot.json` from each archive — cheap on large pods.
    Silently skips archives that fail to parse; a malformed one is a bug
    the user cannot do anything about from a list view."""
    out: list[dict[str, Any]] = []
    for path in sorted(pods_dir().glob("*.tar.gz")):
        try:
            with tarfile.open(path, "r:gz") as tar:
                member = tar.getmember("snapshot.json")
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                manifest = json.loads(fh.read())
        except (tarfile.TarError, KeyError, json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "pod_id": manifest.get("pod_id") or path.stem.rstrip(".tar"),
                "lab_name": manifest.get("lab_name", ""),
                "mode": manifest.get("mode", "unknown"),
                "created_at": manifest.get("created_at", ""),
                "node_count": manifest.get("node_count", 0),
                "bytes": path.stat().st_size,
                "path": str(path),
            }
        )
    return out


def read_manifest(path: Path) -> dict[str, Any]:
    with tarfile.open(path, "r:gz") as tar:
        member = tar.getmember("snapshot.json")
        fh = tar.extractfile(member)
        if fh is None:
            raise runtime_error("pod has an empty snapshot.json")
        return json.loads(fh.read())


def delete_pod(pod_id: str) -> None:
    p = pod_path(pod_id)
    if not p.exists():
        raise not_found(f"pod {pod_id}")
    p.unlink()


async def load(
    session: AsyncSession,
    src: Path,
    name_override: str | None = None,
) -> dict[str, Any]:
    """Restore a pod archive as a new lab. Never overwrites in-place.

    Returns `{lab_id, name, node_count, mode, pod_id}`."""
    from labtris_api.lifecycle import new_id
    from labtris_api.routers.labs import _clone_topology, _remap_geometry
    from labtris_api.runtime.qemu import _install_custom

    if not src.exists():
        raise not_found(f"pod archive {src}")

    staging = pods_dir() / "_staging" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(src, "r:gz") as tar:
            _safe_extract(tar, staging)

        manifest = json.loads((staging / "snapshot.json").read_text())
        if manifest.get("format") != POD_FORMAT:
            raise bad_request(
                f"pod format is {manifest.get('format')!r}, expected {POD_FORMAT!r}"
            )
        lab_payload = json.loads((staging / "lab.json").read_text())
        if lab_payload.get("format") != "labtris-lab-v1":
            raise bad_request("pod's lab.json is not labtris-lab-v1")

        src_lab = lab_payload["lab"]
        base_name = name_override or src_lab.get("name") or "restored-lab"
        # Pick an unused name — same one-suffix-at-a-time pattern as
        # /labs/import so a manual re-load never collides.
        name = base_name
        for suffix in range(1, 100):
            exists = (
                await session.execute(select(Lab).where(Lab.name == name))
            ).scalar_one_or_none()
            if exists is None:
                break
            name = f"{base_name}-{suffix}"

        # Install each QEMU disk into the custom cache under its own sha.
        # Rewrite each node's `image` to point at the new custom entry so
        # the first start after load boots from the restored state.
        # Nodes recorded as "never_started" carry no disk — they restore
        # as fresh nodes booting from their template, same as at save.
        disk_image_by_node: dict[str, str] = {}
        for entry in manifest.get("nodes", []):
            if entry.get("runtime") != "qemu":
                continue
            if entry.get("disk_status") == "never_started":
                continue
            disk_src = staging / "nodes" / entry["id"] / "disk.qcow2"
            if not disk_src.exists():
                raise bad_request(
                    f"pod is incomplete: nodes/{entry['id']}/disk.qcow2 is missing"
                )
            # Move disk_src into a temp under the cache dir and let
            # _install_custom hash+dedup it.
            from labtris_api.runtime.qemu import _cache_dir

            tmp = _cache_dir() / f"pod-restore-{uuid.uuid4().hex}.qcow2"
            shutil.move(str(disk_src), str(tmp))
            info = await _install_custom(tmp)
            disk_image_by_node[entry["id"]] = info["image"]

        # Restore templates that don't already exist. Same-image match is
        # the dedup key: if the target host already has a Template that
        # points at the same image, keep the local one.
        templates_data = json.loads((staging / "templates.json").read_text())
        existing_images = set(
            (
                await session.execute(select(Template.image))
            ).scalars().all()
        )
        existing_names = set(
            (
                await session.execute(select(Template.name))
            ).scalars().all()
        )
        for t in templates_data:
            if t["image"] in existing_images:
                continue
            # Template.name is UNIQUE — pick a free "-restored" suffix.
            base = f"{t['name']}-restored"
            candidate = base
            for suffix in range(1, 100):
                if candidate not in existing_names:
                    break
                candidate = f"{base}-{suffix}"
            existing_names.add(candidate)
            session.add(
                Template(
                    id=new_id(),
                    name=candidate,
                    image=t["image"],
                    runtime=t.get("runtime", "docker"),
                    spec=t.get("spec") or {},
                    env=t.get("env") or {},
                    description=(t.get("description") or "")
                    + " (restored from pod)",
                )
            )

        # Create the fresh lab + topology.
        lab = Lab(id=new_id(), name=name, description=src_lab.get("description", ""))
        session.add(lab)
        await session.flush()

        # Patch source nodes so `image` points at the restored disk.
        src_nodes = list(src_lab.get("nodes") or [])
        for n in src_nodes:
            new_image = disk_image_by_node.get(n["id"])
            if new_image:
                n["image"] = new_image

        node_id_map, _ = await _clone_topology(
            session, lab, src_nodes, src_lab.get("links") or []
        )
        session.add(
            Geometry(
                lab_id=lab.id,
                data=_remap_geometry(lab_payload.get("geometry") or {}, node_id_map),
            )
        )

        # Restore hooks if the pod carries them.
        hooks_path = staging / "hooks.yml"
        if hooks_path.exists():
            try:
                from labtris_api.runtime import hooks as hk

                source = hooks_path.read_text()
                lab.hooks_source = source
                lab.hooks = hk.parse_source(source)
            except Exception:  # noqa: BLE001 — hooks are opt-in; a bad restore keeps the topology
                lab.hooks_source = None
                lab.hooks = None

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise conflict("pod load failed — a referenced name already exists") from exc

        logger.info(
            "pod.load",
            src=str(src),
            new_lab_id=lab.id,
            name=lab.name,
            node_count=len(src_nodes),
        )
        return {
            "lab_id": lab.id,
            "name": lab.name,
            "node_count": len(src_nodes),
            "mode": manifest.get("mode", "cold"),
            "pod_id": manifest.get("pod_id"),
        }
    finally:
        # Staging dir is always cleaned up — we only kept the extracted
        # disks long enough to install them into the cache.
        try:
            shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            pass


def _safe_extract(tar: tarfile.TarFile, path: Path) -> None:
    """Refuse any archive entry whose extraction would escape `path`.

    Python 3.12 added `data` as a safer default filter, but pinning to
    it explicitly protects instances that ended up with older tarfiles
    on some transitive dep path. This is exactly the CVE-2007-4559
    ("tar traversal") mitigation."""
    for member in tar.getmembers():
        target = (path / member.name).resolve()
        if not str(target).startswith(str(path.resolve())):
            raise bad_request(f"pod contains illegal path {member.name!r}")
    tar.extractall(path, filter="data")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
