from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.config import settings
from labtris_api.db import SessionLocal, get_session
from labtris_api.errors import not_found
from labtris_api.lifecycle import get_lab, new_id, start_node, stop_node
from labtris_api.models import Interface, Node, Task
from labtris_api.runtime.base import StopMode
from labtris_api.runtime.docker import _docker as _new_docker_client
from labtris_api.runtime.docker import docker_runtime
from labtris_api.runtime.qemu import _resolve_base_image, image_status
from labtris_api.schemas import TaskIn, TaskOut

router = APIRouter(tags=["tasks"])


async def _set_progress(task_id: str, progress: int, total: int, message: str) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return
        task.progress = progress
        task.total = total
        task.message = message
        await session.commit()
        try:
            from labtris_api.routers.events import hub

            if task.lab_id:
                await hub.publish(
                    task.lab_id,
                    {
                        "type": "task",
                        "id": task.id,
                        "status": task.status,
                        "progress": progress,
                        "total": total,
                        "message": message,
                    },
                )
        except Exception:
            pass


async def _finish(task_id: str, status: str, message: str) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return
        task.status = status
        task.message = message
        await session.commit()


async def _report_image_progress(task_id: str, image: str, done: int, total: int) -> None:
    """Push the image's own download percentage into the task's message while
    it runs, so the progress bar moves during the fifteen minutes one qemu
    image takes rather than jumping at the end."""
    while True:
        status = image_status(image)
        phase = status.get("phase")
        if phase == "downloading" and status.get("total"):
            got, want = status["done"] // 2**20, status["total"] // 2**20
            msg = f"{image}: {status['percent']}% ({got}/{want} MB)"
            await _set_progress(task_id, done, total, msg)
        elif phase:
            await _set_progress(task_id, done, total, f"{image}: {phase}")
        await asyncio.sleep(2)


async def _run_task(task_id: str, lab_id: str, kind: str, opts: TaskIn) -> None:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return
        task.status = "running"
        await session.commit()
        nodes = list((await session.execute(select(Node).where(Node.lab_id == lab_id))).scalars())
    total = len(nodes) if kind != "pull_images" else len({n.image for n in nodes})
    await _set_progress(task_id, 0, total, "starting")
    try:
        if kind == "pull_images":
            done = 0
            # A qemu image is a multi-GB download, an extract and a convert —
            # exactly the thing worth doing ahead of time from the task queue
            # rather than discovering inside a node start that appears to hang.
            for image, runtime_kind in {(n.image, n.runtime) for n in nodes}:
                if runtime_kind == "qemu":
                    watch = asyncio.create_task(_report_image_progress(task_id, image, done, total))
                    try:
                        await _resolve_base_image(image)
                    finally:
                        watch.cancel()
                else:
                    docker = _new_docker_client()
                    try:
                        await docker_runtime._ensure_image(docker, image)  # noqa: SLF001
                    finally:
                        await docker.close()
                done += 1
                await _set_progress(task_id, done, total, f"pulled {image}")
        elif kind in ("start_all", "stop_all"):
            done, failed, reasons = await _run_in_waves(task_id, kind, nodes, total, opts)
            if failed and not done:
                await _finish(task_id, "failed", f"nothing {kind.split('_')[0]}ed; {failed} failed")
                return
            note = f"{done} of {total}"
            if failed:
                # The reasons themselves, commonest first — not a pointer to a
                # field that may well be empty.
                top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
                note += f", {failed} failed: " + "; ".join(f"{n}x {why}" for why, n in top)
            if done < total - failed:
                note += f", {total - done - failed} not attempted — host was low on memory"
            await _finish(task_id, "done", note)
            return
        await _finish(task_id, "done", "complete")
    except Exception as exc:
        await _finish(task_id, "failed", str(exc))


#: Grouping key for failure reasons — see _run_in_waves.
_QUOTED = re.compile(r"'[^']*'")


def _mem_available_mb() -> int | None:
    """MemAvailable, which is the only figure worth gating on.

    MemFree excludes reclaimable page cache and so reads catastrophically low
    on a host that is merely warm; gating on it would refuse to start a lab
    that fits perfectly well.
    """
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


async def _ordered_for_start(nodes: list[Node]) -> list[Node]:
    """Most-connected first, so a node's upstream is up before it is.

    A host whose edge switch is not running yet comes up, finds no peer, and
    backs off — so the fabric converges on BGP's retry timer rather than on
    how fast the nodes started. Ordering by interface count is the
    topology-agnostic way to get that right: in a fat tree it yields
    core, then aggregation, then edge, then hosts, without the task runner
    needing to know what a fat tree is. A spine-leaf gets spines first for
    the same reason, and a topology where it means nothing loses nothing.
    """
    ids = [n.id for n in nodes]
    degree: dict[str, int] = {}
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Interface.node_id, func.count(Interface.id))
                .where(Interface.node_id.in_(ids))
                .group_by(Interface.node_id)
            )
        ).all()
        degree = {r[0]: r[1] for r in rows}
    return sorted(nodes, key=lambda n: (-degree.get(n.id, 0), n.name))


async def _run_in_waves(
    task_id: str, kind: str, nodes: list[Node], total: int, opts: TaskIn
) -> tuple[int, int, dict[str, int]]:
    """Start or stop in waves of `batch`, pausing `stagger_ms` between them."""
    starting = kind == "start_all"
    # Each node in flight holds a database session for as long as its start
    # takes, so an unbounded wave can drain the pool and starve the HTTP
    # handlers — which is how a large batch first showed up: not as a slow
    # start, but as the interface getting 500s from /system/diagnostics with
    # "QueuePool limit of size 5 overflow 10 reached", and nginx turning the
    # backlog into 502s. The pool is bigger now; this keeps a wave from
    # taking all of it however large a batch someone asks for.
    budget = max(1, (settings.db_pool_size + settings.db_max_overflow) // 2)
    width = min(opts.batch, budget)
    ordered = await _ordered_for_start(nodes) if starting else list(nodes)
    done = 0
    failed = 0
    #: Why nodes failed, counted by reason. The first version of this threw
    #: the exception away and the task said "N failed (each node's last_error
    #: says why)" — which was wrong: start_node records last_error for a
    #: failure *during* a start, but refuses outright for a node already in
    #: `starting`, before there is anything to record. So 6 nodes failed, no
    #: node carried an error, and the message sent you somewhere empty.
    reasons: dict[str, int] = {}

    async def one(node_id: str) -> bool:
        async with SessionLocal() as session:
            n = await session.get(Node, node_id)
            if n is None:
                reasons["node deleted mid-run"] = reasons.get("node deleted mid-run", 0) + 1
                return False
            try:
                if starting:
                    await start_node(session, n)
                else:
                    await stop_node(session, n, StopMode.FORCE)
                return True
            except Exception as exc:
                # One node that will not start is not a reason to abandon the
                # other three thousand.
                why = getattr(exc, "message", None) or str(exc) or type(exc).__name__
                # Any quoted identifier, not just this node's own name: the
                # message may name a different node (a conflict names the one
                # already starting), and substituting only n.name left two
                # near-identical strings counted as two distinct reasons.
                why = _QUOTED.sub("<node>", why)[:120]
                reasons[why] = reasons.get(why, 0) + 1
                return False

    for i in range(0, len(ordered), opts.batch):
        if starting and opts.min_free_mb:
            free = _mem_available_mb()
            if free is not None and free < opts.min_free_mb:
                await _set_progress(
                    task_id, done, total,
                    f"stopped at {done}: {free} MB free, under the {opts.min_free_mb} MB floor",
                )
                break
        wave = ordered[i : i + opts.batch]
        results: list[object] = []
        for j in range(0, len(wave), width):
            results.extend(
                await asyncio.gather(
                    *(one(n.id) for n in wave[j : j + width]), return_exceptions=True
                )
            )
        for r in results:
            if r is True:
                done += 1
            else:
                failed += 1
        await _set_progress(
            task_id, done + failed, total,
            f"{kind} {done + failed}/{total}" + (f", {failed} failed" if failed else ""),
        )
        if opts.stagger_ms and i + opts.batch < len(ordered):
            await asyncio.sleep(opts.stagger_ms / 1000)

    return done, failed, reasons


@router.post("/labs/{lab_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    lab_id: str,
    body: TaskIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Task:
    """Async job queue with progress — EVE-NG's `POST api/labs{path}/task[s]`."""
    await get_lab(session, lab_id)
    task = Task(id=new_id(), lab_id=lab_id, kind=body.kind, status="pending")
    session.add(task)
    await session.commit()
    await session.refresh(task)
    asyncio.create_task(_run_task(task.id, lab_id, body.kind, body))
    return task


@router.get("/labs/{lab_id}/tasks", response_model=list[TaskOut])
async def list_tasks(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> list[Task]:
    result = await session.execute(
        select(Task).where(Task.lab_id == lab_id).order_by(Task.created_at.desc())
    )
    return list(result.scalars())


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def read_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise not_found(f"task {task_id} not found")
    return task


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Response:
    task = await session.get(Task, task_id)
    if task is None:
        raise not_found(f"task {task_id} not found")
    await session.delete(task)
    await session.commit()
    return Response(status_code=204)
