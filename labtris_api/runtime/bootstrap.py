"""Vendor first-boot bootstrap driver.

The gap vrnetlab fills for classical vendor images: an XRv, an IOSv, an
NX-OSv boots to a "Router>" (or a "Setup dialog: yes/no?") prompt and
waits for a human. vrnetlab has one `launch.py` per vendor that logs in
over serial telnet and types the initial config so the router comes up
ready to SSH into.

Labtris makes it data instead of code: a template carries an optional
`bootstrap` block naming a small list of {wait_for, type} steps. When
a node boots for the first time, this driver reads from the serial
console until each pattern matches, types the response, and moves on.

Kept intentionally small. Regex on the incoming byte stream, ASCII in
and out. Anything more elaborate (branching, environment interpolation
beyond `{name}`/`{node_id}`) waits for a case that needs it.

Bootstrap runs at most once per node — a marker file `.bootstrap-done`
in the VM dir is dropped on success. `POST /nodes/{id}/bootstrap/retry`
removes it and re-runs from the current console state.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Step:
    wait_for: str        # regex; matched against the console tail
    send: str            # what to type once matched
    timeout_s: float     # per-step timeout


def _parse_steps(spec: dict[str, Any]) -> list[Step]:
    """A `bootstrap` template block becomes a list of Steps.

    Shape:
        {
          "timeout_s": 300,        # overall cap; defaults to sum of steps
          "steps": [
            {"wait_for": "Username:", "type": "cisco\\n", "timeout_s": 60},
            ...
          ]
        }
    """
    raw = spec.get("steps") or []
    default_timeout = int(spec.get("step_timeout_s") or 60)
    out: list[Step] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ValueError(f"bootstrap step {i}: must be an object")
        wait_for = s.get("wait_for")
        send = s.get("type")
        if not isinstance(wait_for, str) or not isinstance(send, str):
            raise ValueError(f"bootstrap step {i}: needs string `wait_for` + `type`")
        out.append(
            Step(
                wait_for=wait_for,
                send=send,
                timeout_s=float(s.get("timeout_s") or default_timeout),
            )
        )
    return out


def _substitute(text: str, ctx: dict[str, str]) -> str:
    """Minimal {name} substitution — enough for the common
    "hostname {name}" case without pulling in a template engine."""
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", v)
    return text


class BootstrapRunner:
    """Drives one node's first-boot config sequence.

    Not a class per se — the state is small and only the lifecycle needs
    reasoning about. Kept as a class so the runner state (current step,
    accumulated buffer, cancellation flag) is easy to inspect from tests.
    """

    def __init__(
        self,
        node_id: str,
        vm_dir: Path,
        spec: dict[str, Any],
        session: Any,  # SerialSession — avoid the import cycle
        ctx: dict[str, str] | None = None,
    ) -> None:
        self.node_id = node_id
        self.vm_dir = vm_dir
        self.steps = _parse_steps(spec)
        self.session = session
        self.ctx = ctx or {}
        self.state_file = vm_dir / "bootstrap-state.json"
        self.done_marker = vm_dir / ".bootstrap-done"

    async def run(self) -> dict[str, Any]:
        """Execute every step. Returns the final state dict."""
        import json
        import time

        state: dict[str, Any] = {
            "phase": "running",
            "step": 0,
            "total": len(self.steps),
            "started_at": time.time(),
            "error": None,
        }
        self._write_state(state)

        queue = self.session.subscribe()
        buf = bytearray()
        # Bring existing history into the buffer so a step whose pattern
        # is already on screen (a getty that prompted before we hooked
        # in) doesn't time out. `buffered()` is the full session history.
        buf.extend(self.session.buffered())

        try:
            for idx, step in enumerate(self.steps):
                state["step"] = idx
                state["current"] = {"wait_for": step.wait_for, "type": _snippet(step.send)}
                self._write_state(state)
                pat = re.compile(step.wait_for)
                deadline = asyncio.get_event_loop().time() + step.timeout_s
                # Wait for the pattern in the accumulated buffer.
                while True:
                    if pat.search(buf.decode(errors="replace")):
                        break
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"step {idx} ({step.wait_for!r}) did not match "
                            f"within {step.timeout_s}s"
                        )
                    try:
                        chunk = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        continue
                    buf.extend(chunk)
                    # Cap the buffer so a chatty guest doesn't grow this
                    # unboundedly. 64 KB is more than enough context for
                    # any prompt-matching regex.
                    if len(buf) > 65536:
                        del buf[: len(buf) - 65536]
                # Match. Type the response.
                to_type = _substitute(step.send, self.ctx)
                await self.session.send(to_type.encode())
                # Consume the echo before the next step's regex runs
                # against the buffer (otherwise we would immediately
                # re-match our own sent bytes if they contained the
                # next step's pattern). Just clear — the next wait_for
                # will fill it again from real guest output.
                buf.clear()
                # Small breath so the guest can process the input before
                # the next wait_for starts polling.
                await asyncio.sleep(0.05)

            state.update(
                phase="done",
                step=len(self.steps),
                current=None,
                finished_at=time.time(),
            )
            self._write_state(state)
            self.done_marker.write_text(json.dumps({"finished_at": state["finished_at"]}))
            logger.info("bootstrap completed", node_id=self.node_id, steps=len(self.steps))
        except Exception as exc:  # noqa: BLE001 — any error becomes a state
            state.update(
                phase="failed",
                error=str(exc),
                finished_at=time.time(),
            )
            self._write_state(state)
            logger.warning(
                "bootstrap failed",
                node_id=self.node_id,
                step=state["step"],
                error=str(exc),
            )
        finally:
            self.session.unsubscribe(queue)
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        import json

        self.state_file.write_text(json.dumps(state))


def read_state(vm_dir: Path) -> dict[str, Any]:
    """Read the last-known bootstrap state for a node's VM dir.

    Callers should treat `phase` as one of {"idle","running","done",
    "failed"}. "idle" is the default when no bootstrap has ever run.
    """
    import json

    f = vm_dir / "bootstrap-state.json"
    if not f.exists():
        return {"phase": "idle"}
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {"phase": "idle"}


def has_completed(vm_dir: Path) -> bool:
    return (vm_dir / ".bootstrap-done").exists()


def reset(vm_dir: Path) -> None:
    """Wipe the bootstrap markers so the next start re-runs it."""
    for name in ("bootstrap-state.json", ".bootstrap-done"):
        (vm_dir / name).unlink(missing_ok=True)


def _snippet(s: str, limit: int = 40) -> str:
    """Compact preview of a `type` payload for state reporting.

    A step that types "cisco\\nCisco12345\\n" reads as `cisco\\nCisco…`
    in the state file — enough for a human to tell which step is in
    flight without spilling a password in full."""
    out = s.replace("\n", "\\n").replace("\r", "\\r")
    return out if len(out) <= limit else out[: limit - 1] + "…"


# Per-node in-flight tasks so a second start() or a stop() can find and
# stop the runner. Keyed by node_id.
_running: dict[str, asyncio.Task[Any]] = {}


def register(node_id: str, task: asyncio.Task[Any]) -> None:
    _running[node_id] = task


def cancel(node_id: str) -> None:
    t = _running.pop(node_id, None)
    if t is not None and not t.done():
        t.cancel()


def is_running(node_id: str) -> bool:
    t = _running.get(node_id)
    return t is not None and not t.done()
