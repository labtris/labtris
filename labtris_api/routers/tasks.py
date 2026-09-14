from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import get_current_user
from labtris_api.db import SessionLocal, get_session
from labtris_api.errors import not_found
from labtris_api.lifecycle import get_lab, new_id, start_node, stop_node
from labtris_api.models import Node, Task
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


async def _run_task(task_id: str, lab_id: str, kind: str) -> None:
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
            for i, node in enumerate(nodes):
                async with SessionLocal() as session:
                    n = await session.get(Node, node.id)
                    if n is None:
                        continue
                    if kind == "start_all":
                        await start_node(session, n)
                    else:
                        await stop_node(session, n, StopMode.FORCE)
                await _set_progress(task_id, i + 1, total, f"{kind} {node.name}")
        await _finish(task_id, "done", "complete")
    except Exception as exc:
        await _finish(task_id, "failed", str(exc))


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
    asyncio.create_task(_run_task(task.id, lab_id, body.kind))
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
