from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from labtris_api.models import Interface, Link, Node
from labtris_api.netd_client import NetdError, netd

# Runtime imports are lazy (inside apply_link_qos) because this module
# is loaded during `labtris_api.routers.ai` import chain, and
# runtime.qemu / runtime.registry pull in a lot of the same tree —
# top-level references produce a circular import that only surfaces
# on the first uvicorn boot with the new import graph.

PRESETS: dict[str, dict[str, Any]] = {
    "clear": {},
    "lan": {"delay_ms": 2, "jitter_ms": 0, "loss_pct": 0, "rate_kbit": 100000},
    "wifi": {"delay_ms": 15, "jitter_ms": 8, "loss_pct": 0.5, "rate_kbit": 20000},
    "3g": {"delay_ms": 150, "jitter_ms": 40, "loss_pct": 1.0, "rate_kbit": 384},
    "satellite": {"delay_ms": 550, "jitter_ms": 50, "loss_pct": 0.2, "rate_kbit": 2048},
    "lossy-wan": {"delay_ms": 40, "jitter_ms": 10, "loss_pct": 5.0, "rate_kbit": 10000},
    # Phase F3: a DC-fabric-shape link that marks ECN under queue depth
    # instead of dropping. ~5 us delay is closer to a real leaf-to-spine
    # hop than the "lan" preset's 2 ms; 25 Gbps as the rate; RED marks
    # ECN in [50KB, 150KB] queue depth. Composes with the ecn.p4 built-in
    # for end-to-end DCTCP / DCQCN / UET CC prototyping.
    "datacenter": {
        "delay_ms": 0, "jitter_ms": 0, "loss_pct": 0,
        "rate_kbit": 25_000_000,
        "ecn": True, "ecn_min_bytes": 50_000, "ecn_max_bytes": 150_000,
    },
}


async def apply_spec(ifname: str | None, spec: dict[str, Any] | None) -> None:
    if not ifname:
        return
    spec = spec or {}
    try:
        if spec:
            await netd.call("tc.set", {"name": ifname, "spec": spec})
        else:
            await netd.call("tc.clear", {"name": ifname})
    except NetdError as exc:
        if exc.code == "ENOENT":
            return
        raise


async def apply_link_qos(
    link: Link,
    a: Interface | None,
    b: Interface | None,
    session: AsyncSession | None = None,
) -> None:
    await apply_spec(a.host_ifname if a else None, link.impair_ab)
    await apply_spec(b.host_ifname if b else None, link.impair_ba)
    want_up = link.admin_up is not False
    for iface in (a, b):
        if iface and iface.host_ifname:
            try:
                await netd.call("iface.set_state", {"name": iface.host_ifname, "up": want_up})
            except NetdError as exc:
                if exc.code != "ENOENT":
                    raise
    # Tell the guest driver its carrier changed. Setting the host tap down
    # is only half of "unplug the cable" — virtio-net reports carrier from
    # the netdev attachment, not from the host tap's L1, so the guest keeps
    # reporting the interface UP unless the QMP set_link command runs too.
    # Best-effort: skipped silently when we lack the session to look up the
    # node's runtime (older callers that predate this arg), or when the
    # node isn't running (the runtime's set_guest_link no-ops in that case
    # anyway — the next boot picks up admin_up via apply_link_qos on start).
    if session is None:
        return
    # Lazy imports: see module docstring — the runtime tree is not
    # safe to reach at import time from here.
    from labtris_api.runtime.base import IfaceSpec, RuntimeHandle
    from labtris_api.runtime.registry import get_runtime

    for iface in (a, b):
        if iface is None or not iface.host_ifname:
            continue
        node = await session.get(Node, iface.node_id)
        if node is None or node.state != "running" or not node.runtime_ref:
            continue
        try:
            runtime = get_runtime(node.runtime)
            handle = RuntimeHandle(node_id=node.id, ref=node.runtime_ref)
            spec = IfaceSpec(
                iface_id=iface.id,
                idx=iface.idx,
                guest_name=iface.name,
                host_ifname=iface.host_ifname,
                mac=str(iface.mac),
            )
            await runtime.set_guest_link(handle, spec, want_up)
        except Exception:  # noqa: BLE001 — host tap change is the authoritative signal
            pass
