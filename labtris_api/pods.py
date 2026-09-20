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


HOT_SNAPSHOT_TAG_PREFIX = "labtris-pod-"


async def save(
    session: AsyncSession, lab_id: str, mode: str = "cold"
) -> dict[str, Any]:
    """Write a snapshot of `lab_id` to `pods_dir()`.

    mode='cold' — refuses if any node is running. QEMU nodes captured
        via flatten_node_disk (already-flattened qcow2 from cache).
        Docker nodes captured as topology only (image ref carries state).

    mode='hot' — running nodes accepted. QEMU nodes: HMP `savevm <tag>`
        into their live qcow2, then flatten (the internal snapshot rides
        along in the flattened bytes). Docker nodes: `docker commit` a
        running container to a scratch tag, `docker save` that image
        into `image.tar` inside the pod archive.

    Returns the pod's summary dict."""
    from labtris_api.routers.labs import export_lab_payload
    from labtris_api.runtime.docker import DockerRuntime
    from labtris_api.runtime.qemu import _install_custom, flatten_node_disk

    if mode not in ("cold", "hot"):
        raise bad_request(f"snapshot mode must be 'cold' or 'hot', got {mode!r}")

    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise not_found(f"lab {lab_id}")
    nodes = (
        await session.execute(select(Node).where(Node.lab_id == lab_id))
    ).scalars().all()

    if mode == "cold":
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

    pod_id = _mint_pod_id(lab.name)
    snapshot_tag = f"{HOT_SNAPSHOT_TAG_PREFIX}{pod_id[-8:]}"

    node_manifest: list[dict[str, Any]] = []
    disk_files: dict[str, Path] = {}    # node_id -> qcow2 path
    image_tars: dict[str, Path] = {}    # node_id -> docker image tar path

    for n in nodes:
        entry: dict[str, Any] = {
            "id": n.id,
            "name": n.name,
            "runtime": n.runtime,
            "image": n.image,
        }

        if n.runtime == "qemu":
            from labtris_api.runtime.qemu import _vm_dir, qemu_runtime

            overlay = _vm_dir(n.id) / "disk.qcow2"
            if not overlay.exists():
                entry["disk_status"] = "never_started"
            else:
                # Hot: capture live memory + device state into the qcow2
                # via savevm BEFORE flattening. The internal snapshot
                # travels with the flattened bytes; loadvm on the target
                # side wakes the guest at exactly this moment.
                if mode == "hot" and n.state == "running" and n.runtime_ref:
                    from labtris_api.runtime.base import RuntimeHandle

                    handle = RuntimeHandle(
                        node_id=n.id, ref=n.runtime_ref, pid=None
                    )
                    try:
                        await qemu_runtime.save_snapshot(handle, snapshot_tag)
                        entry["hot_snapshot_tag"] = snapshot_tag
                    except Exception as exc:  # noqa: BLE001
                        raise runtime_error(
                            f"savevm on node {n.name!r} failed: {exc}"
                        ) from exc
                try:
                    info = await flatten_node_disk(n.id)
                except Exception as exc:  # noqa: BLE001
                    raise runtime_error(
                        f"flattening node {n.name!r} failed: {exc}"
                    ) from exc
                entry["disk_sha256_prefix"] = info["digest"]
                entry["disk_bytes"] = info["bytes"]
                disk_files[n.id] = Path(info["path"])

        elif n.runtime == "docker":
            # Hot: commit + save the running container's filesystem into
            # image.tar inside the pod. Cold docker nodes carry only the
            # topology entry (the image ref reproduces them from scratch).
            if mode == "hot" and n.state == "running" and n.runtime_ref:
                tar_path = pods_dir() / "_staging" / f"{pod_id}-{n.id}.tar"
                tar_path.parent.mkdir(parents=True, exist_ok=True)
                # Docker image references must be lowercase; ULIDs are
                # uppercase alphanumeric, so `-{n.id[-6:]}` would fail
                # with "invalid reference format" without .lower().
                tag = (
                    f"labtris/pod-{lab_id[-6:].lower()}-{n.id[-6:].lower()}"
                    f":{snapshot_tag}"
                )
                from labtris_api.runtime.base import RuntimeHandle

                handle = RuntimeHandle(
                    node_id=n.id, ref=n.runtime_ref, pid=None
                )
                try:
                    dr = DockerRuntime()
                    info = await dr.commit_and_save(handle, tag, str(tar_path))
                    entry["hot_image_tag"] = tag
                    entry["hot_image_bytes"] = info["bytes"]
                    image_tars[n.id] = tar_path
                except Exception as exc:  # noqa: BLE001
                    raise runtime_error(
                        f"commit+save on node {n.name!r} failed: {exc}"
                    ) from exc

        node_manifest.append(entry)

    manifest = {
        "format": POD_FORMAT,
        "labtris_version": __version__,
        "pod_id": pod_id,
        "lab_id": lab_id,
        "lab_name": lab.name,
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(),
        "node_count": len(nodes),
        "nodes": node_manifest,
        "has_hooks": bool(hooks_source),
        "hot_snapshot_tag": snapshot_tag if mode == "hot" else None,
    }

    out_path = pod_path(pod_id)
    tmp_path = out_path.with_suffix(".tar.gz.tmp")
    try:
        _write_archive(
            tmp_path,
            manifest,
            payload,
            hooks_source,
            templates_payload,
            disk_files,
            image_tars,
        )
        tmp_path.rename(out_path)
    finally:
        # Clean the transient image tars — the pod archive now holds
        # their compressed copies. Leaving them under _staging would grow
        # unboundedly.
        for p in image_tars.values():
            p.unlink(missing_ok=True)
    size = out_path.stat().st_size
    logger.info(
        f"pod.save.{mode}",
        lab_id=lab_id,
        pod_id=pod_id,
        node_count=len(nodes),
        bytes=size,
    )
    _ = _install_custom  # linked for the loader path
    return {
        "pod_id": pod_id,
        "path": str(out_path),
        "bytes": size,
        "node_count": len(nodes),
        "mode": mode,
        "created_at": manifest["created_at"],
    }


# Backward-compat: some tests / earlier callers may still name this.
async def save_cold(session: AsyncSession, lab_id: str) -> dict[str, Any]:
    return await save(session, lab_id, mode="cold")


def _write_archive(
    dst: Path,
    manifest: dict[str, Any],
    lab_payload: dict[str, Any],
    hooks_source: str | None,
    templates_payload: list[dict[str, Any]],
    disk_files: dict[str, Path],
    image_tars: dict[str, Path] | None = None,
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
        for node_id, src in (image_tars or {}).items():
            if not src.exists():
                raise runtime_error(f"image tar vanished for {node_id}: {src}")
            tar.add(str(src), arcname=f"nodes/{node_id}/image.tar")


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

        # Prepare per-node restore intent. Two flavours:
        #   * cold: install disk into the custom-image cache; rewrite the
        #     node's `image` to `custom:<sha>` so first-start creates a
        #     fresh overlay on top of the frozen bytes.
        #   * hot: place disk directly into the new node's vm_dir as
        #     `disk.qcow2` and remember the savevm tag; the start path
        #     will `loadvm <tag>` right after the VM comes up. Hot disks
        #     are not shared across nodes so the copy-per-node cost is
        #     the price of resuming exactly where save happened.
        # Docker hot: `docker load` the image.tar, rewrite `image` to
        # the loaded tag so the new container starts from the same fs.
        disk_image_by_node: dict[str, str] = {}
        pending_loadvm: dict[str, tuple[str, Path]] = {}  # src_node_id -> (tag, disk_src)
        pending_docker_image: dict[str, str] = {}         # src_node_id -> loaded tag
        for entry in manifest.get("nodes", []):
            runtime = entry.get("runtime")
            if runtime == "qemu":
                if entry.get("disk_status") == "never_started":
                    continue
                disk_src = staging / "nodes" / entry["id"] / "disk.qcow2"
                if not disk_src.exists():
                    raise bad_request(
                        f"pod is incomplete: nodes/{entry['id']}/disk.qcow2 is missing"
                    )
                hot_tag = entry.get("hot_snapshot_tag")
                if hot_tag:
                    # Keep the source disk in staging; the vm_dir move
                    # happens after the new node's id is known.
                    pending_loadvm[entry["id"]] = (hot_tag, disk_src)
                else:
                    # Cold: dedup into the custom-image cache.
                    from labtris_api.runtime.qemu import _cache_dir

                    tmp = _cache_dir() / f"pod-restore-{uuid.uuid4().hex}.qcow2"
                    shutil.move(str(disk_src), str(tmp))
                    info = await _install_custom(tmp)
                    disk_image_by_node[entry["id"]] = info["image"]
            elif runtime == "docker" and entry.get("hot_image_tag"):
                image_tar = staging / "nodes" / entry["id"] / "image.tar"
                if not image_tar.exists():
                    raise bad_request(
                        f"pod is incomplete: nodes/{entry['id']}/image.tar is missing"
                    )
                loaded = await _docker_load(image_tar)
                pending_docker_image[entry["id"]] = loaded

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

        # Patch source nodes so `image` points at the restored bits.
        # For hot QEMU we leave `image` alone (start_node's overlay is
        # skipped because we pre-place disk.qcow2 in vm_dir).
        # For hot Docker we swap in the docker-loaded tag.
        src_nodes = list(src_lab.get("nodes") or [])
        for n in src_nodes:
            if new_image := disk_image_by_node.get(n["id"]):
                n["image"] = new_image
            elif loaded := pending_docker_image.get(n["id"]):
                n["image"] = loaded

        node_id_map, _ = await _clone_topology(
            session, lab, src_nodes, src_lab.get("links") or []
        )

        # Post-clone: for every hot QEMU node, move the pod disk into
        # the new node's vm_dir and record the loadvm tag on
        # `qemu_opts.load_snapshot_on_boot` so lifecycle.start_node
        # knows to invoke `loadvm` after the guest is up.
        if pending_loadvm:
            from labtris_api.models import Node as NodeRow
            from labtris_api.runtime.qemu import _vm_dir

            for old_id, (tag, disk_src) in pending_loadvm.items():
                new_id_ = node_id_map.get(old_id)
                if new_id_ is None:
                    continue
                vm_dir = _vm_dir(new_id_)
                vm_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(disk_src), str(vm_dir / "disk.qcow2"))
                new_node = await session.get(NodeRow, new_id_)
                if new_node is not None:
                    opts = dict(new_node.qemu_opts or {})
                    opts["load_snapshot_on_boot"] = tag
                    new_node.qemu_opts = opts
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


async def _docker_load(tar_path: Path) -> str:
    """`docker load < tar` and return the loaded image tag.

    Uses the docker CLI so we can stream from a file without buffering
    the whole tar in Python. Parses `Loaded image: <tag>` from stdout.
    """
    import asyncio
    import shutil

    if not shutil.which("docker"):
        raise runtime_error(
            "the `docker` CLI is not on PATH — required to restore hot Docker snapshots"
        )
    with tar_path.open("rb") as fh:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "load",
            stdin=fh,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise runtime_error(
            f"docker load failed: {stderr.decode(errors='replace').strip()}"
        )
    # Output shape: "Loaded image: <tag>\n"
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("Loaded image:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("Loaded image ID:"):
            # Rare fallback: `docker save` on an untagged image saves by
            # id. Callers should always tag first (commit_and_save does),
            # but keep this branch to give a useful error rather than
            # returning an empty string.
            raise runtime_error(
                "pod contains an untagged Docker image — cannot restore"
            )
    raise runtime_error("docker load produced no `Loaded image:` line")
