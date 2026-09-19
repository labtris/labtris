from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.auth import User, forbidden, get_current_user
from labtris_api.db import get_session
from labtris_api.errors import conflict, not_found, unprocessable
from labtris_api.lifecycle import (
    allocate_mac,
    get_lab,
    get_node,
    get_unlocked_lab,
    guest_name,
    iface_scheme_for,
    new_id,
    next_free_iface_slot,
    next_iface_idx,
    release_ifnames,
    require_lab_owner,
    start_node,
    stop_node,
)
from labtris_api.models import Interface, Lab, Node, Template
from labtris_api.runtime.base import RuntimeHandle, StopMode
from labtris_api.runtime.containers import needs_privilege
from labtris_api.runtime.registry import get_runtime
from labtris_api.schemas import (
    ConfigIn,
    ConfigOut,
    ConsoleExecIn,
    ConsoleExecOut,
    ConsoleOut,
    InterfaceIn,
    InterfaceOut,
    NodeConsoleIn,
    NodeCreate,
    NodeDetail,
    NodeOut,
    NodePatch,
    NodeResourcesIn,
    NodeStyleIn,
    SnapshotIn,
    StopBody,
    TemplateIn,
    TemplateOut,
    VncMouseIn,
    VncMouseOut,
    VncReadIn,
    VncReadOut,
    VncTypeIn,
    VncTypeOut,
    VncWaitIn,
    VncWaitOut,
)

router = APIRouter(tags=["nodes"])


def _caps(runtime_kind: str) -> list[str]:
    return sorted(c.value for c in get_runtime(runtime_kind).capabilities)


async def _saved_image_spec(session: AsyncSession, image: str) -> dict[str, Any]:
    """The template spec behind a `custom:` image, or {} for anything else.

    Looked up by image rather than passed as a template id so it holds however
    the node was created — the palette, a lab import, the API, the assistant.
    A spec that only applied on one path would be a default that is true most
    of the time, which is worse than none.
    """
    from labtris_api.runtime.qemu import CUSTOM_PREFIX

    if not image.startswith(CUSTOM_PREFIX):
        return {}
    row = (
        await session.execute(select(Template).where(Template.image == image))
    ).scalars().first()
    return dict(row.spec or {}) if row else {}


@router.post("/labs/{lab_id}/nodes", response_model=NodeOut, status_code=201)
async def create_node(
    lab_id: str,
    body: NodeCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Node:
    await get_unlocked_lab(session, lab_id)
    if body.runtime not in ("docker", "qemu"):
        raise unprocessable(f"runtime {body.runtime!r} is not available in this phase")
    # A privileged container can reach the host kernel, so placing one is a
    # decision about the server, not about the lab. Owning the lab is not
    # enough — on a shared box that would give every student host root.
    if body.runtime == "docker" and needs_privilege(body.image) and not user.is_admin:
        raise forbidden(
            f"{body.image} has to run privileged, which only an administrator may place"
        )
    # A saved image has no catalog entry to fall back on, so the defaults that
    # would normally size a QEMU node do not exist for it. Take them from the
    # template that produced the image, unless the caller said otherwise —
    # otherwise dragging a saved 8 GB appliance onto the canvas silently boots
    # it on 256 MB with a NIC its kernel cannot drive.
    tspec = await _saved_image_spec(session, body.image)
    node = Node(
        id=new_id(),
        lab_id=lab_id,
        name=body.name,
        runtime=body.runtime,
        image=body.image,
        env=body.env,
        cmd=body.cmd,
        cpu_limit=body.cpu_limit if body.cpu_limit is not None else tspec.get("cpus"),
        ram_mb=body.ram_mb if body.ram_mb is not None else tspec.get("ram_mb"),
        nic_model=body.nic_model or tspec.get("nic_model"),
        qemu_opts=tspec.get("qemu_opts") or {},
        console=body.console,
        state="defined",
    )
    session.add(node)
    try:
        # The (lab_id, name) unique violation fires here, at the flush that
        # gets the row an id for the interfaces below — not at the commit the
        # handler below guards, so it used to escape as a 500.
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"node name {body.name!r} already exists in this lab") from exc
    specs = body.interfaces or [InterfaceIn()]
    # A saved image has no catalog entry, so its port naming comes from the
    # template. Getting this wrong renames every port on the node: a guest that
    # really calls them ens3 would be drawn, and configured, as eth0.
    scheme = tspec.get("iface_scheme") or iface_scheme_for(body.runtime, body.image)
    for i, spec in enumerate(specs):
        idx = i
        session.add(
            Interface(
                id=(iface_id := new_id()),
                node_id=node.id,
                idx=idx,
                name=guest_name(idx, spec.name, scheme),
                mac=await allocate_mac(session, iface_id),
                network_id=spec.network_id,
            )
        )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"node name {body.name!r} already exists in this lab") from exc
    return await get_node(session, node.id)


@router.get("/nodes/{node_id}", response_model=NodeDetail)
async def read_node(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> NodeDetail:
    node = await get_node(session, node_id)
    base = NodeOut.model_validate(node)
    return NodeDetail(**base.model_dump(), capabilities=_caps(node.runtime))


@router.patch("/nodes/{node_id}", response_model=NodeOut)
async def patch_node(
    node_id: str,
    body: NodePatch,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    node = await get_node(session, node_id)
    if node.state == "running":
        raise conflict("cannot patch a running node")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(node, key, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict("node name already exists in this lab") from exc
    return await get_node(session, node.id)


@router.delete("/nodes/{node_id}", status_code=204)
async def delete_node(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Response:
    from labtris_api.lifecycle import destroy_node_runtime, release_macs

    node = await get_node(session, node_id)
    await require_lab_owner(session, node.lab_id, user)
    await get_unlocked_lab(session, node.lab_id)
    await destroy_node_runtime(session, node)
    # Before the cascade takes the interfaces with it, hand their addresses
    # back — otherwise the pool only ever shrinks.
    await release_macs(session, [i.id for i in node.interfaces])
    # The data volume outlives a wipe on purpose; it must not outlive the node.
    from labtris_api.runtime.qemu import remove_data_volume

    remove_data_volume(node.id)
    # destroy_node_runtime deletes the devices but leaves their names reserved,
    # so deleting nodes shrank the pool forever. Wipe deliberately does not do
    # this — a wiped node keeps its identity, and with it its device names.
    await release_ifnames(session, node)
    await session.delete(node)
    await session.commit()
    return Response(status_code=204)


@router.post("/nodes/{node_id}/wipe", response_model=NodeOut)
async def wipe_node(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Node:
    """Throw the node's disk away and keep the node.

    The topology — name, links, addresses, position on the canvas — is the part
    worth keeping when an experiment has made a mess of the guest. Deleting the
    overlay means the next start builds a fresh one from the shared base image,
    so this costs a boot rather than a re-download."""
    from labtris_api.lifecycle import destroy_node_runtime

    node = await get_node(session, node_id)
    await require_lab_owner(session, node.lab_id, user)
    await get_unlocked_lab(session, node.lab_id)
    await destroy_node_runtime(session, node)
    node.state = "defined"
    node.last_error = None
    await session.commit()
    return await get_node(session, node.id)


@router.post("/nodes/{node_id}/start", response_model=NodeOut)
async def do_start(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    node = await get_node(session, node_id)
    return await start_node(session, node)


@router.post("/nodes/{node_id}/stop", response_model=NodeOut)
async def do_stop(
    node_id: str,
    body: StopBody | None = None,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    node = await get_node(session, node_id)
    mode = StopMode(body.mode if body else "graceful")
    return await stop_node(session, node, mode)


@router.get("/nodes/{node_id}/console", response_model=ConsoleOut)
async def console(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> ConsoleOut:
    node = await get_node(session, node_id)
    if not node.runtime_ref:
        raise not_found("node has no runtime handle")
    endpoint = await get_runtime(node.runtime).console(
        RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
    )
    return ConsoleOut(kind=endpoint.kind, target=endpoint.target, meta=endpoint.meta)


@router.post("/nodes/{node_id}/console/exec", response_model=ConsoleExecOut)
async def console_exec(
    node_id: str,
    body: ConsoleExecIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> ConsoleExecOut:
    """Run one command inside a running node and return its output.

    Docker: `sh -c '<command>'` via `docker exec`; stdout, stderr, and
    exit code are all real. QEMU: writes `<command>\\n` to the serial
    console and drains whatever the guest produces within `timeout_s`.
    QEMU has no shell-prompt detection here — the caller gets whatever
    accumulated, plus a warning that says so. The kind of thing the
    agent can reason about but a human should read carefully."""
    import asyncio

    node = await get_node(session, node_id)
    if node.state != "running":
        raise unprocessable(
            f"node {node.name!r} is {node.state!r} — start it first"
        )
    if not node.runtime_ref:
        raise not_found("node has no runtime handle")

    if node.runtime == "docker":
        from labtris_api.runtime.docker import docker_runtime

        try:
            rc, out = await asyncio.wait_for(
                docker_runtime.exec_shell(
                    RuntimeHandle(node_id=node.id, ref=node.runtime_ref),
                    body.command,
                ),
                timeout=body.timeout_s,
            )
        except asyncio.TimeoutError:
            return ConsoleExecOut(
                stdout="",
                stderr="",
                exit_code=None,
                runtime="docker",
                warnings=[f"exec did not finish within {body.timeout_s}s"],
            )
        # exec_shell merges stdout and stderr in the aiodocker demux. That
        # is what the underlying `stream.read_out()` produces without a
        # separator — we surface the joined bytes as stdout for now.
        return ConsoleExecOut(
            stdout=out[:8000],
            stderr="",
            exit_code=rc,
            runtime="docker",
            warnings=(
                [f"output truncated to 8 KB (was {len(out)} bytes)"]
                if len(out) > 8000
                else []
            ),
        )

    if node.runtime == "qemu":
        from pathlib import Path

        from labtris_api.runtime.qemu import attach_session

        sess = await attach_session(node.id, Path(node.runtime_ref))
        if sess is None:
            raise not_found("qemu serial session is not open — restart the node")
        q = sess.subscribe()
        # Two consecutive read timeouts of a quarter of the budget each
        # is the heuristic: an idle prompt returns fast, a slow guest
        # can still deliver a full response before the total budget.
        per_read = max(0.25, body.timeout_s / 4)
        deadline = asyncio.get_event_loop().time() + body.timeout_s
        collected = bytearray()
        idle_streak = 0
        try:
            await sess.send((body.command + "\n").encode())
            while asyncio.get_event_loop().time() < deadline:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=per_read)
                    collected += chunk
                    idle_streak = 0
                except asyncio.TimeoutError:
                    idle_streak += 1
                    # Two idle reads in a row → probably done. The
                    # exceptions are guests still typing at their leisurely
                    # BIOS-era serial speed, which need the full budget.
                    if idle_streak >= 2:
                        break
        finally:
            sess.unsubscribe(q)
        return ConsoleExecOut(
            stdout=collected.decode("utf-8", errors="replace")[:8000],
            stderr="",
            exit_code=None,
            runtime="qemu",
            warnings=[
                "qemu serial exec has no shell-prompt detection — output "
                "may be partial or include unrelated console text."
            ],
        )

    raise unprocessable(f"console_exec not supported for runtime {node.runtime!r}")


@router.post("/nodes/{node_id}/vnc/screenshot")
async def vnc_screenshot(
    node_id: str,
    region: str | None = None,
    grid: int = 0,
    scale: float = 0.5,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Response:
    """Grab a PNG of the QEMU display right now.

    Only QEMU nodes have a display; the endpoint refuses container nodes
    with 422. The response body is `image/png`, one to a few tens of KB
    for a typical 1024×768 framebuffer.

    - `region=x,y,w,h` (query string) crops after capture. Useful when
      the model wants to inspect one dialog on a 1080p display without
      spending tokens on the whole frame.
    - `grid=N` (query string, integer pixel pitch, 0 = off) overlays a
      light grid with axis labels so coordinate-picking is easy. 100 is
      a good default for a 1024x768 display; 50 for a smaller one.
    - `scale=0.5` (default) downscales the returned image by half. A
      1280x800 framebuffer becomes 640x400 — the model still reads a
      login prompt just fine and pays a quarter of the vision tokens.
      Pass `scale=1.0` when you need full detail (small text, tight
      pixel-picking).
    """
    node = await get_node(session, node_id)
    if node.state != "running":
        raise unprocessable(f"node {node.name!r} is {node.state!r} — start it first")
    if node.runtime != "qemu":
        raise unprocessable(f"vnc screenshot not supported for runtime {node.runtime!r}")
    if not node.runtime_ref:
        raise not_found("node has no runtime handle")
    from labtris_api.runtime.qemu import screendump_png

    region_tuple: tuple[int, int, int, int] | None = None
    if region:
        try:
            parts = [int(p) for p in region.split(",")]
        except ValueError as exc:
            raise unprocessable("region must be four comma-separated integers x,y,w,h") from exc
        if len(parts) != 4 or any(p < 0 for p in parts) or parts[2] <= 0 or parts[3] <= 0:
            raise unprocessable("region must be x,y,w,h with w>0 and h>0")
        region_tuple = (parts[0], parts[1], parts[2], parts[3])
    if scale <= 0 or scale > 4:
        raise unprocessable("scale must be in (0, 4]")
    png = await screendump_png(
        Path(node.runtime_ref), region=region_tuple, grid=grid or False, scale=scale,
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/nodes/{node_id}/vnc/type", response_model=VncTypeOut)
async def vnc_type(
    node_id: str,
    body: VncTypeIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> VncTypeOut:
    """Type text or send raw key combos into a QEMU node's display.

    Two shapes, one endpoint:
    - `text: "root\\nubuntu\\n"` — types each character; use for
      passwords, hostnames, short answers at prompts.
    - `keys: ["ctrl-alt-f2"]` — sends one raw HMP `sendkey` combo per
      list element; use for function keys, ctrl/alt sequences, tty
      switches, GRUB commands.

    QEMU has no idea whether the guest was listening — you'll usually
    want a screenshot before and after to confirm. This is the pair
    with `vnc_screenshot` for driving a graphical console."""
    node = await get_node(session, node_id)
    if node.state != "running":
        raise unprocessable(f"node {node.name!r} is {node.state!r} — start it first")
    if node.runtime != "qemu":
        raise unprocessable(f"vnc type not supported for runtime {node.runtime!r}")
    if not node.runtime_ref:
        raise not_found("node has no runtime handle")
    import asyncio
    import base64 as b64

    from labtris_api.runtime.qemu import screendump_png, vnc_send_keys, vnc_send_text

    vm_dir = Path(node.runtime_ref)
    if body.text is not None:
        sent = await vnc_send_text(vm_dir, body.text, hold_ms=body.hold_ms)
    else:
        assert body.keys is not None  # enforced by VncTypeIn.model_post_init
        sent = await vnc_send_keys(vm_dir, body.keys, hold_ms=body.hold_ms)
    # Optional after-shot. `settle_ms` covers the frame or two the guest
    # takes to redraw after a menu selection or a click.
    shot_b64: str | None = None
    if body.screenshot:
        if body.settle_ms:
            await asyncio.sleep(body.settle_ms / 1000)
        png = await screendump_png(vm_dir, scale=body.scale)
        shot_b64 = b64.b64encode(png).decode()
    return VncTypeOut(
        keys_sent=sent,
        screenshot=shot_b64,
        mime_type="image/png" if shot_b64 else None,
    )


@router.post("/nodes/{node_id}/vnc/mouse", response_model=VncMouseOut)
async def vnc_mouse(
    node_id: str,
    body: VncMouseIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> VncMouseOut:
    """Move the pointer, or move-and-click, on a QEMU node's display.

    Coordinates are framebuffer pixels (0..fb_width-1, 0..fb_height-1)
    — the dimensions are returned in the response and in every
    screenshot's IHDR. Uses QEMU's QMP `input-send-event` under the
    hood so absolute coordinates land pixel-accurately, unlike HMP's
    relative `mouse_move`. Requires a QMP socket on the VM (nodes
    started before mouse support landed will need to be stopped and
    started to gain one)."""
    import asyncio
    import base64 as b64

    node = await get_node(session, node_id)
    if node.state != "running":
        raise unprocessable(f"node {node.name!r} is {node.state!r} — start it first")
    if node.runtime != "qemu":
        raise unprocessable(f"vnc mouse not supported for runtime {node.runtime!r}")
    if not node.runtime_ref:
        raise not_found("node has no runtime handle")
    from labtris_api.runtime.qemu import (
        fb_dims,
        screendump_png,
        vnc_mouse_click,
        vnc_mouse_move,
    )

    vm_dir = Path(node.runtime_ref)
    fb_w, fb_h = await fb_dims(vm_dir)
    if body.x >= fb_w or body.y >= fb_h:
        raise unprocessable(
            f"({body.x}, {body.y}) is outside the {fb_w}x{fb_h} framebuffer"
        )
    if body.action == "move":
        await vnc_mouse_move(vm_dir, body.x, body.y, fb_w, fb_h)
    else:
        await vnc_mouse_click(
            vm_dir, body.x, body.y, fb_w, fb_h,
            button=body.button, double=body.action == "double_click",
        )
    shot_b64: str | None = None
    if body.screenshot:
        if body.settle_ms:
            await asyncio.sleep(body.settle_ms / 1000)
        png = await screendump_png(vm_dir, scale=body.scale)
        shot_b64 = b64.b64encode(png).decode()
    return VncMouseOut(
        fb_width=fb_w,
        fb_height=fb_h,
        screenshot=shot_b64,
        mime_type="image/png" if shot_b64 else None,
    )


@router.post("/nodes/{node_id}/vnc/wait_for_change", response_model=VncWaitOut)
async def vnc_wait_for_change(
    node_id: str,
    body: VncWaitIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> VncWaitOut:
    """Block until the display stops changing, then return the final PNG.

    One call replaces the "screenshot in a loop" pattern the model
    falls into after a click. Polls every `poll_ms`, considers the
    screen settled once the same frame has been seen for `stable_ms`.
    Returns `settled: false` (with the latest frame) if `timeout_ms`
    elapses first."""
    import base64 as b64

    node = await get_node(session, node_id)
    if node.state != "running":
        raise unprocessable(f"node {node.name!r} is {node.state!r} — start it first")
    if node.runtime != "qemu":
        raise unprocessable(f"vnc wait not supported for runtime {node.runtime!r}")
    if not node.runtime_ref:
        raise not_found("node has no runtime handle")
    from labtris_api.runtime.qemu import screendump_png, wait_for_screen_change

    vm_dir = Path(node.runtime_ref)
    result = await wait_for_screen_change(
        vm_dir,
        poll_ms=body.poll_ms,
        stable_ms=body.stable_ms,
        timeout_ms=body.timeout_ms,
    )
    # Result carries the full-res PNG (we hash at full res for accuracy).
    # If the caller asked for a scaled preview, re-render at that scale
    # rather than shipping the full frame — no need to spend the tokens.
    if abs(body.scale - 1.0) >= 0.01:
        png = await screendump_png(vm_dir, scale=body.scale)
    else:
        png = result["png"]
    return VncWaitOut(settled=result["settled"], screenshot=b64.b64encode(png).decode())


@router.post("/nodes/{node_id}/vnc/read", response_model=VncReadOut)
async def vnc_read(
    node_id: str,
    body: VncReadIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> VncReadOut:
    """OCR the QEMU display and return only the text.

    Two orders of magnitude fewer tokens than `vnc_screenshot` when the
    model just needs to read what's on screen — a login prompt, an
    error dialog, which menu item is highlighted. Uses tesseract-ocr;
    if it's not installed the response is a clear error naming the
    apt package."""
    node = await get_node(session, node_id)
    if node.state != "running":
        raise unprocessable(f"node {node.name!r} is {node.state!r} — start it first")
    if node.runtime != "qemu":
        raise unprocessable(f"vnc read not supported for runtime {node.runtime!r}")
    if not node.runtime_ref:
        raise not_found("node has no runtime handle")
    from labtris_api.runtime.qemu import ocr_image_bytes, screendump_png

    region_tuple: tuple[int, int, int, int] | None = None
    if body.region:
        parts = [int(p) for p in body.region.split(",")]
        region_tuple = (parts[0], parts[1], parts[2], parts[3])
    # Full resolution for OCR — tesseract wants pixels.
    png = await screendump_png(Path(node.runtime_ref), region=region_tuple, scale=1.0)
    return VncReadOut(text=ocr_image_bytes(png))


@router.post("/nodes/{node_id}/interfaces", response_model=InterfaceOut, status_code=201)
async def add_interface(
    node_id: str,
    body: InterfaceIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Interface:
    node = await get_node(session, node_id)
    await get_unlocked_lab(session, node.lab_id)
    # A saved-image node's port naming comes from the template, not the
    # built-in QEMU_CATALOG — iface_scheme_for only knows about catalog
    # entries and falls through to "eth" for a custom: image, which
    # renames every port added after create. Mirror the create-time
    # resolution at line 131.
    tspec = await _saved_image_spec(session, node.image)
    scheme = tspec.get("iface_scheme") or iface_scheme_for(node.runtime, node.image)
    # Free idx and free name have to agree — see next_free_iface_slot.
    # Explicit body.name is kept as-is: a caller who names an interface owns
    # the collision, and the IntegrityError below still catches it.
    idx = next_free_iface_slot(node, scheme) if not body.name else next_iface_idx(node)
    iface = Interface(
        id=(iface_id := new_id()),
        node_id=node.id,
        idx=idx,
        name=guest_name(idx, body.name, scheme),
        mac=await allocate_mac(session, iface_id),
        network_id=body.network_id,
    )
    session.add(iface)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict("interface name already exists on node") from exc
    await session.refresh(iface)
    return iface


@router.patch("/nodes/{node_id}/style", response_model=NodeOut)
async def set_style(
    node_id: str,
    body: NodeStyleIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    """EVE-NG's `PUT .../nodes/{nodeId}/style` — per-node icon/color override."""
    node = await get_node(session, node_id)
    style = dict(node.style or {})
    data = body.model_dump(exclude_unset=True)
    style.update({k: v for k, v in data.items() if v is not None})
    node.style = style
    await session.commit()
    return await get_node(session, node.id)


@router.patch("/nodes/{node_id}/console", response_model=NodeOut)
async def set_console(
    node_id: str,
    body: NodeConsoleIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    """Point this node's VNC/RDP tunnel somewhere. Separate from PATCH /nodes
    because it's the one thing you need to change *while* the guest is up —
    you rarely know the guest's RDP address before it has booted."""
    node = await get_node(session, node_id)
    console = dict(node.console or {})
    settings = dict(console.get(body.protocol) or {})
    for key, value in body.model_dump(exclude_unset=True).items():
        if key == "protocol":
            continue
        if value is None or value == "":
            settings.pop(key, None)
        else:
            settings[key] = value
    console[body.protocol] = settings
    node.console = console
    await session.commit()
    return await get_node(session, node.id)


@router.get("/qemu/options")
async def qemu_options(_user: object = Depends(get_current_user)) -> dict[str, Any]:
    """The machine knobs a node may set, described well enough for the UI to
    render the form without knowing what any of them mean."""
    from labtris_api.config import settings as cfg
    from labtris_api.runtime.qemu import QEMU_OPTIONS

    return {
        "options": QEMU_OPTIONS,
        "extra_args_enabled": cfg.qemu_allow_extra_args,
    }


@router.patch("/nodes/{node_id}/qemu-options", response_model=NodeOut)
async def set_qemu_options(
    node_id: str,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    """How the machine is built, as opposed to what runs inside it.

    None of these can change on a live guest, so they take effect on the next
    start — the same contract as RAM and vCPU."""
    from labtris_api.runtime.qemu import QEMU_OPTIONS

    node = await get_node(session, node_id)
    if node.runtime != "qemu":
        raise unprocessable(f"{node.name} is a {node.runtime} node — it has no QEMU machine")
    unknown = set(body) - set(QEMU_OPTIONS)
    if unknown:
        raise unprocessable(f"unknown option(s): {', '.join(sorted(unknown))}")
    node.qemu_opts = {**(node.qemu_opts or {}), **body}
    await session.commit()
    return await get_node(session, node.id)


@router.patch("/nodes/{node_id}/resources", response_model=NodeOut)
async def set_resources(
    node_id: str,
    body: NodeResourcesIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    """RAM and vCPU count, editable after the node exists.

    Separate from PATCH /nodes because that refuses a running node outright,
    and because the QEMU backend keeps its own copy of these in the VM's
    config.json — changing the row alone would move the number on screen and
    nothing else. Neither can change on a live guest, so it takes effect on
    the next start."""
    node = await get_node(session, node_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("ram_mb") is not None:
        node.ram_mb = data["ram_mb"]
    if data.get("cpu_limit") is not None:
        node.cpu_limit = data["cpu_limit"]
    if data.get("nic_model") is not None:
        node.nic_model = data["nic_model"]
    await session.commit()

    runtime = get_runtime(node.runtime)
    resize = getattr(runtime, "resize", None)
    if resize is not None and node.runtime_ref:
        await resize(
            RuntimeHandle(node_id=node.id, ref=node.runtime_ref),
            node.ram_mb,
            round(node.cpu_limit) if node.cpu_limit else None,
            node.nic_model,
        )
    return await get_node(session, node.id)


@router.get("/nodes/{node_id}/config", response_model=ConfigOut)
async def read_config(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> ConfigOut:
    node = await get_node(session, node_id)
    return ConfigOut(content=node.startup_config)


@router.put("/nodes/{node_id}/config", response_model=ConfigOut)
async def save_config(
    node_id: str,
    body: ConfigIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> ConfigOut:
    """Save a node's startup-config — EVE-NG's `PUT api/labs{path}/configs/{nodeId}`."""
    node = await get_node(session, node_id)
    node.startup_config = body.content
    # The lab no longer matches whatever set was applied. Saying "running
    # `solution`" after someone edited one node is worse than saying nothing,
    # so the claim is dropped rather than quietly kept.
    lab = await session.get(Lab, node.lab_id)
    if lab is not None:
        lab.active_configset = None
    await session.commit()
    return ConfigOut(content=node.startup_config)


@router.post("/nodes/{node_id}/config/push")
async def push_config(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, str]:
    """Push the saved startup-config into the running container."""
    node = await get_node(session, node_id)
    if node.startup_config is None:
        raise conflict("no startup-config saved for this node")
    if node.state != "running" or not node.runtime_ref:
        raise conflict("node must be running to push config")
    runtime = get_runtime(node.runtime)
    handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
    await runtime.write_file(handle, "/config/startup-config", node.startup_config)
    return {"status": "pushed", "path": "/config/startup-config"}


@router.get("/nodes/{node_id}/logs")
async def node_logs(
    node_id: str,
    lines: int = 200,
    pattern: str = "",
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, list[str]]:
    """EVE-NG's `GET api/logs/{path}/{lines}/{pattern}`."""
    import re

    node = await get_node(session, node_id)
    if not node.runtime_ref:
        return {"lines": []}
    runtime = get_runtime(node.runtime)
    handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
    raw = await runtime.logs(handle, max(1, min(lines, 5000)))
    rows = raw.splitlines()
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise unprocessable(f"bad pattern: {exc}") from exc
        rows = [r for r in rows if rx.search(r)]
    return {"lines": rows}


@router.post("/nodes/{node_id}/suspend", response_model=NodeOut)
async def suspend_node(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    node = await get_node(session, node_id)
    if node.state != "running" or not node.runtime_ref:
        raise conflict("node must be running to suspend")
    runtime = get_runtime(node.runtime)
    if "suspend" not in _caps(node.runtime):
        raise unprocessable(f"runtime {node.runtime!r} does not support suspend")
    await runtime.suspend(RuntimeHandle(node_id=node.id, ref=node.runtime_ref))
    node.paused = True
    await session.commit()
    return await get_node(session, node.id)


@router.post("/nodes/{node_id}/resume", response_model=NodeOut)
async def resume_node(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Node:
    node = await get_node(session, node_id)
    if not node.paused or not node.runtime_ref:
        raise conflict("node is not suspended")
    runtime = get_runtime(node.runtime)
    await runtime.resume(RuntimeHandle(node_id=node.id, ref=node.runtime_ref))
    node.paused = False
    await session.commit()
    return await get_node(session, node.id)


@router.post("/nodes/{node_id}/snapshot")
async def save_snapshot(
    node_id: str,
    body: SnapshotIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, str]:
    """Real point-in-time VM state (QEMU `savevm`) — Docker has no analogue,
    so this 404s cleanly there instead of pretending to snapshot a container."""
    node = await get_node(session, node_id)
    if "snapshot" not in _caps(node.runtime):
        raise unprocessable(f"runtime {node.runtime!r} does not support snapshots")
    if node.state != "running" or not node.runtime_ref:
        raise conflict("node must be running to snapshot")
    runtime = get_runtime(node.runtime)
    await runtime.save_snapshot(RuntimeHandle(node_id=node.id, ref=node.runtime_ref), body.name)
    return {"status": "saved", "name": body.name}


@router.get("/nodes/{node_id}/snapshots")
async def list_snapshots(
    node_id: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, list[str]]:
    node = await get_node(session, node_id)
    if "snapshot" not in _caps(node.runtime) or not node.runtime_ref:
        return {"snapshots": []}
    runtime = get_runtime(node.runtime)
    names = await runtime.list_snapshots(RuntimeHandle(node_id=node.id, ref=node.runtime_ref))
    return {"snapshots": names}


@router.post("/nodes/{node_id}/snapshot/{name}/restore")
async def restore_snapshot(
    node_id: str,
    name: str,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> dict[str, str]:
    node = await get_node(session, node_id)
    if "snapshot" not in _caps(node.runtime):
        raise unprocessable(f"runtime {node.runtime!r} does not support snapshots")
    if not node.runtime_ref:
        raise conflict("node has no runtime handle")
    runtime = get_runtime(node.runtime)
    await runtime.load_snapshot(RuntimeHandle(node_id=node.id, ref=node.runtime_ref), name)
    return {"status": "restored", "name": name}


@router.post("/nodes/{node_id}/export", response_model=TemplateOut, status_code=201)
async def export_node_template(
    node_id: str,
    body: TemplateIn,
    session: AsyncSession = Depends(get_session),
    _user: object = Depends(get_current_user),
) -> Template:
    """EVE-NG's `PUT api/labs{path}/nodes/{id}/export` — save a node as a reusable
    template so future labs can drag it from the catalog with no `.yml` authoring.

    For Docker that is metadata only: the image reference already names
    immutable content, so a template is a name and a pointer.

    For QEMU it is not. The node's image is a base that its overlay sits on, so
    recording the reference would reproduce a pristine guest and throw away
    everything that was done to this one. So the disk is flattened into a new
    standalone image first, and the template points at that — along with the
    sizing and device facts the base catalog used to supply, which no longer
    have a catalog entry behind them.
    """
    node = await get_node(session, node_id)
    spec: dict[str, Any] = {}
    image = node.image

    if node.runtime == "qemu":
        # A qcow2 being written by a live guest has no consistent point to copy:
        # the result is a disk torn mid-write, which boots into a filesystem
        # check at best. Refused rather than snapshotted-and-hoped, because the
        # damage shows up days later on a template people have started trusting.
        if node.state == "running":
            raise unprocessable(
                f"stop {node.name!r} before saving it as a template — flattening a disk "
                "that a running guest is still writing produces a corrupt image"
            )
        from labtris_api.runtime.qemu import QEMU_CATALOG, flatten_node_disk

        saved = await flatten_node_disk(node.id)
        image = saved["image"]
        base = QEMU_CATALOG.get(node.image)
        spec = {
            "from_image": node.image,
            "bytes": saved["bytes"],
            # The node's own overrides win; the base catalog fills the rest.
            # Without this a saved desktop appliance would boot on the 256 MB
            # default and no driveable NIC.
            "ram_mb": node.ram_mb or (base.ram_mb if base else None),
            "cpus": int(node.cpu_limit) if node.cpu_limit else (base.cpus if base else None),
            "nic_model": node.nic_model or (base.nic_model if base else None),
            "graphical": base.graphical if base else False,
            "iface_scheme": node.iface_scheme,
            "qemu_opts": node.qemu_opts or {},
        }

    tmpl = Template(
        id=new_id(),
        name=body.name,
        runtime=node.runtime,
        image=image,
        cmd=node.cmd,
        env=node.env,
        icon=body.icon or (node.style or {}).get("icon"),
        spec=spec,
        description=body.description,
    )
    session.add(tmpl)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(f"template name {body.name!r} already exists") from exc
    await session.refresh(tmpl)
    return tmpl


async def _qemu_console(websocket: WebSocket, node: Node) -> None:
    """Bridge to the VM's always-connected SerialSession — replay the buffered
    boot log, then stream live output while forwarding typed lines as input."""
    import asyncio
    from pathlib import Path

    from labtris_api.runtime.qemu import attach_session

    session = await attach_session(node.id, Path(node.runtime_ref or ""))
    if session is None:
        await websocket.send_text("qemu session not found — (re)start the node first\r\n")
        await websocket.close()
        return
    await websocket.send_text(f"serial console {node.name} ({node.image})\r\n")
    await websocket.send_text(session.buffered().decode(errors="replace"))
    queue = session.subscribe()

    async def _pump_out() -> None:
        while True:
            chunk = await queue.get()
            await websocket.send_text(chunk.decode(errors="replace"))

    pump = asyncio.create_task(_pump_out())
    try:
        import json as _json
        while True:
            frame = await websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                break
            # xterm.js sends each keystroke as its own text frame, already
            # in the exact byte shape a serial line expects: `\r` for
            # Enter, `\x03` for Ctrl-C, VT100 escape sequences for the
            # arrow keys.
            data = frame.get("bytes")
            if data is None:
                text = frame.get("text") or ""
                # The browser also sends control JSON frames on resize
                # ({"resize":true,"cols":80,"rows":24}), matching what the
                # container-shell path already handles. A serial line has
                # no PTY to resize, so we recognise the shape and drop it —
                # otherwise the JSON bytes get echoed into the guest and
                # NX-OS spends the next screen full of "invalid command"
                # errors on `{"resize":...}`.
                if text.startswith("{"):
                    try:
                        ctl = _json.loads(text)
                    except ValueError:
                        ctl = None
                    if isinstance(ctl, dict) and ctl.get("resize"):
                        continue
                data = text.encode()
            if data:
                await session.send(data)
    finally:
        pump.cancel()
        session.unsubscribe(queue)


# guacamole-common-js sends its own keep-alive pings with an empty opcode
# ("0." on the wire). Those are tunnel-level, not protocol-level: guacd has no
# handler for them, and — more importantly — the browser closes the tunnel with
# "Server timeout" after 15s of silence, which an idle desktop easily hits. The
# reference Java tunnel answers them itself; so do we.
GUAC_PING_PREFIX = "0.,"

# Guacamole.Status codes (guacamole-common-js `Guacamole.Status.Code`).
GUAC_SERVER_ERROR = "512"
GUAC_UPSTREAM_ERROR = "515"
GUAC_RESOURCE_NOT_FOUND = "516"

# Sensible defaults for an RDP guest on a lab network: self-signed cert, let
# the server pick a security mode, and let the guest resize to the browser.
RDP_DEFAULTS: dict[str, str] = {
    "port": "3389",
    "security": "any",
    "ignore-cert": "true",
    "resize-method": "display-update",
}


async def _console_settings(node: Node, protocol: str) -> dict[str, str]:
    """Guacamole connection parameters for one node.

    VNC defaults to the display QEMU published for this VM on loopback. RDP
    has no such default — nothing on the host listens for the guest — so it
    comes from the node's `console` config: the guest's own address on a lab
    network, or a hostfwd. Either can be overridden per node, which is also
    how a Docker node running its own VNC/RDP server gets a console."""
    from labtris_api.runtime.qemu import vnc_port

    configured = (node.console or {}).get(protocol) or {}
    overrides = {str(k): str(v) for k, v in configured.items() if v is not None}

    if protocol == "vnc":
        settings = {"hostname": "127.0.0.1", "color-depth": "24"}
        if node.runtime == "qemu" and node.runtime_ref:
            port = await vnc_port(Path(node.runtime_ref))
            if port is not None:
                settings["port"] = str(port)
        settings.update(overrides)
        if not settings.get("port"):
            raise unprocessable(
                "no VNC display for this node — start a qemu node, or PATCH it "
                'with console={"protocol": "vnc", "hostname": "...", "port": ...}'
            )
    else:
        settings = dict(RDP_DEFAULTS)
        settings.update(overrides)
        if not settings.get("hostname"):
            raise unprocessable(
                "RDP has no default target — PATCH the node with "
                'console={"protocol": "rdp", "hostname": "...", "username": "...", '
                '"password": "..."} pointing at the guest\'s RDP listener'
            )
    return settings


async def guac_tunnel_to(
    websocket: WebSocket,
    settings_map: dict[str, str] | None,
    failure: str | None,
    protocol: str = "vnc",
) -> None:
    """Tunnel to an arbitrary VNC/RDP endpoint. Split out of the node console
    so the Wireshark windows ride the same implementation rather than a second
    copy of the handshake and relay."""
    import asyncio

    from labtris_api.errors import ApiError
    from labtris_api.guac import GuacSocket, connect_guacd, encode_instruction

    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="guacamole" if "guacamole" in offered else None)

    async def fail(message: str, status: str = GUAC_SERVER_ERROR) -> None:
        try:
            await websocket.send_text(encode_instruction("error", message, status))
            await websocket.close()
        except Exception:  # noqa: BLE001 - the socket may already be gone
            pass

    if failure or settings_map is None:
        await fail(failure or "no target", GUAC_RESOURCE_NOT_FOUND)
        return

    params = websocket.query_params
    try:
        width = max(320, min(7680, int(params.get("width") or 1024)))
        height = max(240, min(4320, int(params.get("height") or 768)))
        dpi = max(32, min(480, int(params.get("dpi") or 96)))
    except ValueError:
        width, height, dpi = 1024, 768, 96

    try:
        sock, connection_id = await connect_guacd(
            protocol, settings_map, width=width, height=height, dpi=dpi,
            timezone=params.get("timezone") or None,
        )
    except ApiError as exc:
        await fail(exc.message, GUAC_UPSTREAM_ERROR)
        return

    async def pump_from_guacd(sock: GuacSocket) -> None:
        reason = "connection closed"
        try:
            while True:
                instruction = await sock.read()
                await websocket.send_text(encode_instruction(*instruction))
                if instruction and instruction[0] == "disconnect":
                    await websocket.close()
                    return
        except (asyncio.IncompleteReadError, OSError, ApiError) as exc:
            reason = str(exc) or reason
        except WebSocketDisconnect:
            return
        try:
            await websocket.send_text(
                encode_instruction("error", f"guacd: {reason}", GUAC_UPSTREAM_ERROR)
            )
            await websocket.close()
        except Exception:  # noqa: BLE001 - browser may have left first
            pass

    await websocket.send_text(encode_instruction("ready", connection_id))
    pump = asyncio.create_task(pump_from_guacd(sock))
    try:
        while True:
            msg = await websocket.receive_text()
            if msg.startswith(GUAC_PING_PREFIX):
                await websocket.send_text(msg)
                continue
            await sock.send_raw(msg)
    except (WebSocketDisconnect, OSError, RuntimeError):
        pass
    finally:
        pump.cancel()
        sock.close()


async def _guac_tunnel(websocket: WebSocket, node_id: str, protocol: str) -> None:
    """A node's own console. Resolves where to dial, then hands off to the
    shared tunnel."""
    from labtris_api.auth import resolve_ws_user
    from labtris_api.db import SessionLocal
    from labtris_api.errors import ApiError

    async with SessionLocal() as session:
        # A WebSocket has no Depends chain, so no auth checked
        # automatically. Without this an unauthenticated client (a
        # different tab, a `curl`, a browser after its cookie expired)
        # could open a live VNC/RDP session to any node on the box. Fall
        # into guac_tunnel_to with a synthetic failure so the client
        # sees a real "sign in first" message instead of a mysterious
        # tunnel-519, which is what Guacamole shows when the wire dies
        # without a preceding `error` instruction.
        user = await resolve_ws_user(websocket, session)
        if user is None:
            await guac_tunnel_to(
                websocket, None, "sign in to open a console", protocol,
            )
            return
        try:
            node = await get_node(session, node_id)
        except ApiError as exc:
            await guac_tunnel_to(websocket, None, exc.message, protocol)
            return
    try:
        settings_map = await _console_settings(node, protocol)
    except ApiError as exc:
        await guac_tunnel_to(websocket, None, exc.message, protocol)
        return
    await guac_tunnel_to(websocket, settings_map, None, protocol)


@router.websocket("/nodes/{node_id}/vnc/ws")
async def vnc_ws(websocket: WebSocket, node_id: str) -> None:
    await _guac_tunnel(websocket, node_id, "vnc")


@router.websocket("/nodes/{node_id}/rdp/ws")
async def rdp_ws(websocket: WebSocket, node_id: str) -> None:
    """Same tunnel, guacd's RDP client instead — for guests that speak RDP
    (a Windows VM, or xrdp on a Linux desktop) rather than exposing a
    framebuffer to the hypervisor."""
    await _guac_tunnel(websocket, node_id, "rdp")


async def _container_shell(websocket: WebSocket, node: Node, handle: RuntimeHandle) -> None:
    """Pump a real PTY in the container both ways until one end stops.

    Two tasks rather than one loop, because both directions block: waiting on
    the browser would stall the guest's output, and waiting on the guest would
    swallow the keystroke that was going to stop it. Whichever finishes first
    cancels the other.

    Control frames are JSON, plain text is keystrokes. That split keeps a window
    resize from being typed into the shell, which is what happens if you send
    the size as text and the guest is at a prompt.
    """
    import asyncio
    import json

    from labtris_api.runtime.docker import docker_runtime

    stream, proc, docker = await docker_runtime.open_shell(handle)

    async def guest_to_browser() -> None:
        while True:
            msg = await stream.read_out()
            if msg is None:
                break
            await websocket.send_bytes(msg.data)

    async def browser_to_guest() -> None:
        while True:
            frame = await websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                break
            data = frame.get("bytes")
            if data is None:
                text = frame.get("text") or ""
                if text.startswith("{"):
                    try:
                        ctl = json.loads(text)
                    except ValueError:
                        ctl = {}
                    if ctl.get("resize"):
                        await docker_runtime.resize_shell(
                            proc, int(ctl.get("cols", 100)), int(ctl.get("rows", 30))
                        )
                        continue
                data = text.encode()
            await stream.write_in(data)

    pump = [asyncio.create_task(guest_to_browser()), asyncio.create_task(browser_to_guest())]
    try:
        done, pending = await asyncio.wait(pump, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            # Surface a genuine failure; a closed socket is not one.
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                raise exc
    finally:
        with contextlib.suppress(Exception):
            await stream.close()
        with contextlib.suppress(Exception):
            await docker.close()


@router.websocket("/nodes/{node_id}/console/ws")
async def console_ws(websocket: WebSocket, node_id: str) -> None:
    """Browser terminal. Cookie-authed on the upgrade request."""
    from labtris_api.auth import resolve_ws_user
    from labtris_api.db import SessionLocal
    from labtris_api.runtime.docker import docker_runtime

    await websocket.accept()
    async with SessionLocal() as session:
        # Auth first. A bare accept + close was silent to the browser
        # (the xterm just went blank); a "sign in first" line + close
        # tells the person what to do.
        user = await resolve_ws_user(websocket, session)
        if user is None:
            await websocket.send_text("sign in to open a console\r\n")
            await websocket.close(code=4401, reason="unauthorized")
            return
        try:
            node = await get_node(session, node_id)
            # A stopped QEMU node keeps its runtime_ref — it is the VM's
            # directory, not a handle to a live process — so runtime_ref alone
            # is not "there is something to attach to". Without the state check
            # this fell through to the serial attach and failed there, with the
            # close looking to the client like a dropped session.
            if node.state != "running" or not node.runtime_ref:
                # Close with a reason, not just a close. A bare close is
                # indistinguishable from the guest dying mid-session, so the
                # client cannot tell "there is nothing here to attach to yet"
                # from "you were attached and lost it" — and reconnecting is
                # right for one and a pointless loop for the other.
                await websocket.send_text(f"{node.name} is not running\r\n")
                await websocket.close(code=4404, reason="node is not running")
                return
            if node.runtime == "qemu":
                await _qemu_console(websocket, node)
                return
            handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
            await _container_shell(websocket, node, handle)
        except WebSocketDisconnect:
            return
        except Exception as exc:
            try:
                await websocket.send_text(f"error: {exc}\r\n")
            except Exception:
                pass


def _parse_ip_brief(out: str) -> dict[str, list[str]]:
    """Turn `ip -br -4 addr` into {ifname: [cidr, ...]}.

    Brief format is `NAME  STATE  ADDR [ADDR...]`, whitespace separated. Parsed
    rather than asked for as JSON because `ip -j` is absent from busybox, and
    Alpine is the most common node image in a lab.
    """
    found: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "lo":
            continue
        addrs = [p for p in parts[2:] if "/" in p and ":" not in p]
        if addrs:
            found[parts[0]] = addrs
    return found


@router.get("/labs/{lab_id}/addresses")
async def lab_addresses(
    lab_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> dict[str, dict[str, list[str]]]:
    """What each running guest thinks its own addresses are.

    The topology knows which port is wired to which, but not what anyone
    configured inside the guest — so "is this link actually up" cannot be
    answered from our own tables. This asks the guests.

    Docker only. A QEMU guest has no agent to ask and no exec channel; driving
    its serial console to run a command would mean typing into whatever is
    already on that console, which is not something to do behind the user's
    back. Those nodes are simply absent from the result, and the UI shows them
    as unknown rather than as having no address.
    """
    lab = await get_lab(session, lab_id)
    await session.refresh(lab, ["nodes"])
    out: dict[str, dict[str, list[str]]] = {}
    for node in lab.nodes:
        if node.runtime != "docker" or node.state != "running" or not node.runtime_ref:
            continue
        try:
            from labtris_api.runtime.docker import docker_runtime

            handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
            rc, text = await docker_runtime.exec_shell(handle, "ip -br -4 addr")
            if rc == 0:
                out[node.id] = _parse_ip_brief(text)
        except Exception:
            # One unreachable guest must not fail the whole map: the point of
            # the view is showing which nodes are addressed and which are not.
            continue
    return out
