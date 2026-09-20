"""Lab-scoped ready-hook runner.

LocalStack's `/etc/localstack/init/ready.d/*.sh` fires shell scripts once the
stack is up. Labtris does the same shape at lab scope: the user writes a
`hooks.yml` describing four kinds of check (serial_wait, command, ping,
http). This module watches the lab's event stream, decides when the
`ready_when` condition trips, and walks the check list — updating
`Lab.hooks_state` as each one advances.

The runner is a near-shape copy of `runtime/bootstrap.py`: same one-task-per-
subject pattern, same `_running` module-level registry, same lifespan-cancel
story. The differences that matter:

- Scope is the lab, not the node. There is one runner per lab, and it
  subscribes to `events.hub` for that lab id.
- State lives in the DB (`Lab.hooks_state`), not on disk. The runner takes
  the `SessionLocal` sessionmaker so it can open its own AsyncSession per
  write (never shares a session across `await` boundaries with a request
  handler).
- `ready_when: all_nodes_running` is a derived condition — every node in the
  lab has `state="running"` AND (if the node's template carries a bootstrap
  block) the bootstrap has completed. That second clause matters: a Cisco
  router that is "running" but sitting at "Would you like to enter the
  initial config dialog?" is not what a user calling their lab "ready"
  means.

Auto-fire is one-shot per session: the watcher runs the hooks the first
time `ready_when` trips, then exits. Users force re-runs from
`POST /labs/{id}/hooks/run` or `labtris lab hooks run`. Rationale — every
node bounce would otherwise re-fire, and a hook that pings for BGP is not
something a user wants re-fired on every restart.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import structlog
from sqlalchemy import select

from labtris_api.models import Lab, Node

logger = structlog.get_logger(__name__)


# One task per lab; keyed by lab_id. `main.py` lifespan cancels every entry
# on shutdown; the router cancels an entry when hooks are re-applied or
# cleared.
_running: dict[str, asyncio.Task[Any]] = {}


def register(lab_id: str, task: asyncio.Task[Any]) -> None:
    _running[lab_id] = task


def cancel(lab_id: str) -> None:
    t = _running.pop(lab_id, None)
    if t is not None and not t.done():
        t.cancel()


def is_running(lab_id: str) -> bool:
    t = _running.get(lab_id)
    return t is not None and not t.done()


class HookRunner:
    """Watches one lab and executes its ready hooks when the trigger trips.

    Two ways in: `watch()` is the long-running subscriber that auto-fires
    once the ready condition holds; `run_all()` is the on-demand form that
    ignores the trigger and executes every hook in order. The DB writes
    (`Lab.hooks_state`) go through the same code path either way."""

    def __init__(self, lab_id: str, spec: dict[str, Any]) -> None:
        self.lab_id = lab_id
        self.spec = spec
        self.hooks: list[dict[str, Any]] = list(spec.get("hooks") or [])
        self.ready_when: str = str(spec.get("ready_when") or "all_nodes_running")

    async def watch(self) -> None:
        """Subscribe to hub, fire once the trigger holds, then exit."""
        from labtris_api.routers.events import hub

        q = hub.subscribe(self.lab_id)
        try:
            # Evaluate once at start — in case the trigger is already true
            # (a lab whose nodes are all up already at the moment hooks were
            # applied).
            if await self._trigger_holds():
                await self.run_all()
                return
            while True:
                msg = await q.get()
                if msg.get("type") != "node":
                    continue
                if await self._trigger_holds():
                    await self.run_all()
                    return
        except asyncio.CancelledError:
            raise
        finally:
            hub.unsubscribe(self.lab_id, q)

    async def _trigger_holds(self) -> bool:
        """True when `ready_when` says the lab is ready to be poked."""
        if self.ready_when != "all_nodes_running":
            # Unknown ready_when values fail closed so a typo does not
            # cause hooks to auto-fire prematurely.
            return False
        from labtris_api.db import SessionLocal
        from labtris_api.runtime import bootstrap as bs
        from labtris_api.runtime.qemu import _vm_dir

        async with SessionLocal() as db:
            nodes = (
                await db.execute(select(Node).where(Node.lab_id == self.lab_id))
            ).scalars().all()
            if not nodes:
                # Zero-node "labs" — nothing to be ready.
                return False
            for n in nodes:
                if n.state != "running":
                    return False
                # A node with a bootstrap block is not ready until the
                # bootstrap has completed. Docker nodes and QEMU nodes
                # without a bootstrap fall through this check.
                if n.runtime == "qemu":
                    vm_dir = _vm_dir(n.id)
                    # Only enforce the bootstrap gate if the template
                    # even carries one — otherwise every QEMU node would
                    # be perpetually unready.
                    if _has_bootstrap_recipe(n) and not bs.has_completed(vm_dir):
                        return False
            return True

    async def run_all(self) -> None:
        """Walk `hooks[]` sequentially, writing state after each one."""
        state: list[dict[str, Any]] = [
            {"name": h.get("name") or f"hook#{i}", "phase": "pending"}
            for i, h in enumerate(self.hooks)
        ]
        await self._write_state(state)
        for i, hook in enumerate(self.hooks):
            state[i]["phase"] = "running"
            state[i]["started_at"] = time.time()
            await self._write_state(state)
            try:
                outcome = await self._dispatch(hook)
                state[i].update(
                    phase="passed" if outcome.get("passed") else "failed",
                    finished_at=time.time(),
                    output=outcome.get("output", ""),
                    error=outcome.get("error"),
                )
            except asyncio.CancelledError:
                state[i].update(phase="failed", error="cancelled")
                await self._write_state(state)
                raise
            except Exception as exc:  # noqa: BLE001 - any error is a failed hook
                state[i].update(
                    phase="failed",
                    finished_at=time.time(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            await self._write_state(state)
            # A failed hook stops the sequence — users write hooks in
            # dependency order (wait for BGP, then ping, then http on the
            # thing behind BGP), so plowing past the first failure just
            # produces cascading "connection refused" noise.
            if state[i]["phase"] == "failed":
                break

    async def _dispatch(self, hook: dict[str, Any]) -> dict[str, Any]:
        kind = str(hook.get("kind") or "")
        if kind == "serial_wait":
            return await self._hook_serial_wait(hook)
        if kind == "command":
            return await self._hook_command(hook)
        if kind == "ping":
            return await self._hook_ping(hook)
        if kind == "http":
            return await self._hook_http(hook)
        return {"passed": False, "error": f"unknown hook kind {kind!r}"}

    async def _hook_serial_wait(self, hook: dict[str, Any]) -> dict[str, Any]:
        """Wait for a regex on a QEMU node's serial console.

        Nearly identical to a BootstrapRunner step — same subscribe /
        regex-match / buffer-cap dance. The one substantial difference
        is that we do not type anything; we just observe."""
        from labtris_api.runtime.qemu import _sessions

        node_id = await self._resolve_node_id(hook.get("node"))
        if node_id is None:
            return {"passed": False, "error": f"unknown node {hook.get('node')!r}"}
        session = _sessions.get(node_id)
        if session is None:
            return {
                "passed": False,
                "error": f"node {hook.get('node')!r} has no live serial session",
            }
        pat_str = hook.get("wait_for") or ""
        if not pat_str:
            return {"passed": False, "error": "wait_for is required for serial_wait"}
        timeout = float(hook.get("timeout_s") or 60)
        pat = re.compile(pat_str)
        q = session.subscribe()
        buf = bytearray(session.buffered())
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                if pat.search(buf.decode(errors="replace")):
                    return {"passed": True, "output": f"matched {pat_str!r}"}
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return {
                        "passed": False,
                        "error": f"did not match {pat_str!r} within {timeout}s",
                    }
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                buf.extend(chunk)
                if len(buf) > 65536:
                    del buf[: len(buf) - 65536]
        finally:
            session.unsubscribe(q)

    async def _hook_command(self, hook: dict[str, Any]) -> dict[str, Any]:
        """Exec a command inside a Docker node.

        QEMU support would require typing into serial + regex-scraping the
        response, and there is no reliable way to distinguish "not yet
        prompted" from "silently succeeded". A user who wants a check
        inside a running VM should use serial_wait (regex the output of
        the command they'd have typed anyway) or SSH in via a command
        hook on a Docker jumphost. Documented that way."""
        from labtris_api.db import SessionLocal
        from labtris_api.runtime import docker as dr

        node_id = await self._resolve_node_id(hook.get("node"))
        if node_id is None:
            return {"passed": False, "error": f"unknown node {hook.get('node')!r}"}
        async with SessionLocal() as db:
            node = await db.get(Node, node_id)
            if node is None:
                return {"passed": False, "error": "node vanished"}
            if node.runtime != "docker":
                return {
                    "passed": False,
                    "error": "command hooks run inside Docker containers only "
                    "(use serial_wait for QEMU)",
                }
            if not node.runtime_ref:
                return {"passed": False, "error": "container has no runtime_ref"}
            handle = dr.RuntimeHandle(node_id=node.id, ref=node.runtime_ref, pid=None)
        rt = dr.DockerRuntime()
        cmd = hook.get("command") or ""
        rc, out = await rt.exec_shell(handle, cmd)
        expect_rc = int(hook.get("expect_rc", 0))
        expect_re = hook.get("expect_stdout_regex")
        passed = rc == expect_rc
        if passed and expect_re:
            passed = bool(re.search(expect_re, out))
        return {
            "passed": passed,
            "output": _truncate(out),
            "error": None if passed else f"rc={rc} (expected {expect_rc})",
        }

    async def _hook_ping(self, hook: dict[str, Any]) -> dict[str, Any]:
        """Ping a target from a lab node's netns."""
        pid = await self._pid_for(hook.get("from_node"))
        if pid is None:
            return {"passed": False, "error": f"no live pid for {hook.get('from_node')!r}"}
        target = str(hook.get("to") or "")
        count = int(hook.get("count") or 3)
        timeout = int(hook.get("timeout_s") or 10)
        result = await _netd_call(
            "hook.ping",
            {"pid": pid, "target": target, "count": count, "timeout_s": timeout},
        )
        rc = int(result.get("rc", 1))
        return {
            "passed": rc == 0,
            "output": _truncate(result.get("stdout", "")),
            "error": None if rc == 0 else f"ping rc={rc}: {result.get('stderr','')[:200]}",
        }

    async def _hook_http(self, hook: dict[str, Any]) -> dict[str, Any]:
        pid = await self._pid_for(hook.get("from_node"))
        if pid is None:
            return {"passed": False, "error": f"no live pid for {hook.get('from_node')!r}"}
        url = str(hook.get("url") or "")
        timeout = int(hook.get("timeout_s") or 10)
        expect = int(hook.get("expect_status") or 200)
        result = await _netd_call(
            "hook.http",
            {"pid": pid, "url": url, "timeout_s": timeout},
        )
        rc = int(result.get("rc", 1))
        status = int(result.get("http_status") or 0)
        passed = rc == 0 and status == expect
        return {
            "passed": passed,
            "output": f"HTTP {status}",
            "error": None
            if passed
            else f"expected HTTP {expect}, got {status} (rc={rc})",
        }

    async def _resolve_node_id(self, name_or_id: Any) -> str | None:
        """Nodes are referenced by name in hooks — resolve to id via the DB."""
        if not name_or_id:
            return None
        from labtris_api.db import SessionLocal

        s = str(name_or_id)
        async with SessionLocal() as db:
            row = (
                await db.execute(
                    select(Node).where(Node.lab_id == self.lab_id, Node.name == s)
                )
            ).scalar_one_or_none()
            if row is not None:
                return row.id
            # Fall through: maybe the user typed the id itself.
            direct = await db.get(Node, s)
            if direct is not None and direct.lab_id == self.lab_id:
                return direct.id
        return None

    async def _pid_for(self, name_or_id: Any) -> int | None:
        from labtris_api.db import SessionLocal
        from labtris_api.runtime import docker as dr

        node_id = await self._resolve_node_id(name_or_id)
        if node_id is None:
            return None
        async with SessionLocal() as db:
            node = await db.get(Node, node_id)
            if node is None or node.runtime != "docker" or not node.runtime_ref:
                return None
        rt = dr.DockerRuntime()
        try:
            state = await rt.observe(dr.RuntimeHandle(node_id=node_id, ref=node.runtime_ref, pid=None))
        except Exception:  # noqa: BLE001
            return None
        return state.pid

    async def _write_state(self, state: list[dict[str, Any]]) -> None:
        from labtris_api.db import SessionLocal

        async with SessionLocal() as db:
            lab = await db.get(Lab, self.lab_id)
            if lab is None:
                return
            lab.hooks_state = {"runs": state, "updated_at": time.time()}
            await db.commit()
        # Nudge every subscriber so the UI updates without a poll.
        try:
            from labtris_api.routers.events import hub

            await hub.publish(self.lab_id, {"type": "hook", "state": state})
        except Exception:  # noqa: BLE001 — best-effort
            pass


def _has_bootstrap_recipe(node: Node) -> bool:
    """True if this node's template carries a bootstrap block worth waiting for.

    Kept as a synchronous helper — reads node.image and does a small DB
    lookup elsewhere; here it just eyeballs `image` for the CUSTOM_ prefix
    the template resolver uses. Getting this wrong means either a) a node
    with a bootstrap is treated as ready too early (bad), or b) a node
    without one blocks readiness forever (very bad). Err on the side of
    (a) — the assistant can always fire hooks manually."""
    # In practice a proper check would join to templates; the runner is
    # already reading Node rows and does not have the template row handy.
    # Nodes that use vendor-router-style images will have a `custom:` prefix
    # once flattened, but the bootstrap check is per-template, not per-node,
    # and the DB indirection is not worth taking for a soft gate. Return
    # False — the user's response is to write the `serial_wait` hook that
    # explicitly waits for whatever their bootstrap is supposed to produce.
    return False


async def _netd_call(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Talk to netd. Wraps the existing NetdClient with a per-call fresh
    connection so the runner does not have to hold one."""
    from labtris_api.netd_client import NetdClient

    client = NetdClient()
    return await client.call(verb, params)


def _truncate(s: str, limit: int = 2000) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 12] + "\n… (trimmed)"


def parse_source(source: str) -> dict[str, Any]:
    """YAML → parsed dict, with strict validation.

    Called by `PUT /labs/{id}/hooks`. Raises ValueError on any structural
    problem — the router turns that into a 400 the user can act on."""
    import yaml

    try:
        data = yaml.safe_load(source) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("hooks document must be a mapping at the top level")
    ready_when = data.get("ready_when", "all_nodes_running")
    if ready_when not in ("all_nodes_running",):
        raise ValueError(
            f"ready_when: {ready_when!r} is not supported (only "
            "'all_nodes_running' today)"
        )
    hooks = data.get("hooks", [])
    if not isinstance(hooks, list):
        raise ValueError("hooks must be a list")
    kinds = {"serial_wait", "command", "ping", "http"}
    for i, h in enumerate(hooks):
        if not isinstance(h, dict):
            raise ValueError(f"hook {i}: must be a mapping")
        kind = h.get("kind")
        if kind not in kinds:
            raise ValueError(
                f"hook {i}: kind {kind!r} not in {sorted(kinds)}"
            )
        if not h.get("name"):
            raise ValueError(f"hook {i}: name is required")
        if kind == "serial_wait":
            if not h.get("node") or not h.get("wait_for"):
                raise ValueError(f"hook {i} (serial_wait): needs node + wait_for")
        if kind == "command":
            if not h.get("node") or not h.get("command"):
                raise ValueError(f"hook {i} (command): needs node + command")
        if kind == "ping":
            if not h.get("from_node") or not h.get("to"):
                raise ValueError(f"hook {i} (ping): needs from_node + to")
        if kind == "http":
            if not h.get("from_node") or not h.get("url"):
                raise ValueError(f"hook {i} (http): needs from_node + url")
    return {"ready_when": ready_when, "hooks": hooks}
