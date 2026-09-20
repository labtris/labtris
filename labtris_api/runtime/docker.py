from __future__ import annotations

from typing import Any

import aiodocker
from aiodocker.exceptions import DockerError

from labtris_api.config import settings
from labtris_api.errors import runtime_error
from labtris_api.netd_client import NetdError, netd
from labtris_api.runtime.base import (
    Capability,
    ConsoleEndpoint,
    IfaceSpec,
    NodeSpec,
    RuntimeHandle,
    RuntimeState,
    StopMode,
)
from labtris_api.runtime.containers import BASE_CAPS, profile_for


def _docker() -> aiodocker.Docker:
    url = settings.docker_host
    if url.startswith("unix://"):
        url = url
    return aiodocker.Docker(url)


def _per_node_dir(node_id: str, subdir: str) -> str:
    """Where a node's per-instance bind-mount subdir lives on the host.

    Layout: ~/.local/share/labtris/node-mounts/<node_id>/<subdir>/. Made on
    demand so the P4-upload endpoint (or any future uploader that talks to
    this path) does not have to know the container hasn't been created yet.
    Returned as a string because Docker's HostConfig.Binds wants strings."""
    import os
    from pathlib import Path

    root = Path(
        os.environ.get("LABTRIS_NODE_MOUNTS_DIR")
        or "~/.local/share/labtris/node-mounts"
    ).expanduser()
    p = root / node_id / subdir
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def per_node_dir(node_id: str, subdir: str) -> str:
    """Public re-export so the P4 uploader endpoint can land bytes here
    without importing a leading-underscore name."""
    return _per_node_dir(node_id, subdir)


def _seed_p4_default(node_id: str) -> None:
    """Copy `basic_switch.p4` into the node's /p4 mount if empty.

    Called from `create()` for a bmv2 node so a fresh drag-drop lands a
    working switch. If the file already exists (a re-create after a
    wipe kept the host dir), leave it alone."""
    import shutil
    from pathlib import Path

    dst = Path(_per_node_dir(node_id, "p4")) / "prog.p4"
    if dst.exists():
        return
    src = Path(__file__).resolve().parents[2] / "packaging" / "p4-programs" / "basic_switch.p4"
    if src.exists():
        shutil.copyfile(src, dst)


class DockerRuntime:
    kind = "docker"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.EXEC, Capability.HOTPLUG_NIC, Capability.SUSPEND}
    )

    async def create(self, spec: NodeSpec) -> RuntimeHandle:
        docker = _docker()
        try:
            await self._ensure_image(docker, spec.image)
            # An image the catalog knows about may need more than the default
            # two capabilities to reach its first prompt. Anything BYO gets the
            # unprivileged default, which is the safe answer when we have no
            # evidence either way.
            profile = profile_for(spec.image)
            host_config: dict[str, Any] = {
                "NetworkMode": "none",
                "CapAdd": [*BASE_CAPS, *(profile.cap_add if profile else ())],
            }
            if profile is not None and profile.privileged:
                host_config["Privileged"] = True
            if spec.cpu_limit is not None:
                host_config["NanoCpus"] = int(spec.cpu_limit * 1_000_000_000)
            if spec.ram_mb is not None:
                host_config["Memory"] = int(spec.ram_mb) * 1024 * 1024
            # Per-instance bind mounts declared by the profile (e.g. bmv2
            # gets /p4 pointed at ~/.local/share/labtris/node-mounts/<id>/p4).
            # Directories are created on demand — the loader / uploader
            # writes into them and the container reads on start. No shared
            # cache: each node has its own copy so editing one node's
            # program never touches another's.
            if profile is not None and profile.per_node_mounts:
                binds: list[str] = []
                for subdir, container_path in profile.per_node_mounts:
                    host_dir = _per_node_dir(spec.node_id, subdir)
                    binds.append(f"{host_dir}:{container_path}:rw")
                host_config["Binds"] = binds
                # Seed a bmv2 node's /p4 with basic_switch.p4 on first
                # create, so a drag-and-drop from the palette produces
                # a switch that actually boots rather than one whose
                # compile step immediately fails. Users can swap the
                # program later via PUT /nodes/{id}/p4 or upload.
                if profile.id == "bmv2":
                    _seed_p4_default(spec.node_id)
            config: dict[str, Any] = {
                "Image": spec.image,
                "Hostname": spec.name,
                "Env": [f"{k}={v}" for k, v in spec.env.items()],
                "Labels": {
                    "pnl.node_id": spec.node_id,
                    "pnl.lab_id": spec.lab_id,
                },
                "HostConfig": host_config,
            }
            if spec.cmd is not None:
                config["Cmd"] = spec.cmd
            elif profile is not None and profile.cmd is not None:
                config["Cmd"] = profile.cmd
            if profile is not None and profile.user is not None:
                config["User"] = profile.user
            container = await docker.containers.create(config=config)
            info = await container.show()
            return RuntimeHandle(node_id=spec.node_id, ref=info["Id"], pid=None)
        except DockerError as exc:
            raise runtime_error(f"docker create failed: {exc}") from exc
        finally:
            await docker.close()

    async def start(self, h: RuntimeHandle) -> None:
        docker = _docker()
        try:
            container = docker.containers.container(h.ref)
            await container.start()
        except DockerError as exc:
            raise runtime_error(f"docker start failed: {exc}") from exc
        finally:
            await docker.close()

    async def stop(self, h: RuntimeHandle, mode: StopMode) -> None:
        docker = _docker()
        try:
            container = docker.containers.container(h.ref)
            timeout = 0 if mode is StopMode.FORCE else 10
            await container.stop(t=timeout)
        except DockerError as exc:
            if exc.status != 404:
                raise runtime_error(f"docker stop failed: {exc}") from exc
        finally:
            await docker.close()

    async def destroy(self, h: RuntimeHandle) -> None:
        docker = _docker()
        try:
            container = docker.containers.container(h.ref)
            try:
                await container.stop(t=0)
            except DockerError:
                pass
            await container.delete(force=True)
        except DockerError as exc:
            if exc.status != 404:
                raise runtime_error(f"docker destroy failed: {exc}") from exc
        finally:
            await docker.close()

    async def attach_iface(self, h: RuntimeHandle, i: IfaceSpec, bridge: str) -> None:
        if not i.peer_ifname:
            raise runtime_error("docker attach requires a peer veth name")
        pid = await self._pid(h)
        try:
            await netd.call("iface.delete", {"name": i.host_ifname})
        except NetdError:
            pass
        try:
            await netd.call("iface.delete", {"name": i.peer_ifname})
        except NetdError:
            pass
        try:
            await netd.call("veth.create", {"name": i.host_ifname, "peer": i.peer_ifname})
            await netd.call("iface.attach", {"name": i.host_ifname, "bridge": bridge})
            await netd.call(
                "netns.move",
                {
                    "name": i.peer_ifname,
                    "pid": pid,
                    "rename_to": i.guest_name,
                    "mac": i.mac,
                    "up": True,
                },
            )
            await netd.call("iface.set_state", {"name": i.host_ifname, "up": True})
        except NetdError as exc:
            raise runtime_error(f"dataplane attach failed: {exc.message}") from exc

    async def detach_iface(self, h: RuntimeHandle, i: IfaceSpec) -> None:
        try:
            await netd.call("iface.delete", {"name": i.host_ifname})
        except NetdError:
            pass

    async def sync_interfaces(self, h: RuntimeHandle, ifaces: list[IfaceSpec]) -> None:
        """Docker has no per-node config file to refresh — a container's
        network namespace picks up veths as they land. No-op."""
        return

    async def set_guest_link(self, h: RuntimeHandle, i: IfaceSpec, up: bool) -> None:
        """Container veth carrier already reflects the host peer's state
        directly, so the host-side iface.set_state down is what the guest
        sees. Nothing extra to do here."""
        return

    async def console(self, h: RuntimeHandle) -> ConsoleEndpoint:
        return ConsoleEndpoint(kind="exec", target=h.ref, meta={})

    async def observe(self, h: RuntimeHandle) -> RuntimeState:
        docker = _docker()
        try:
            container = docker.containers.container(h.ref)
            info = await container.show()
            state = info.get("State") or {}
            running = bool(state.get("Running"))
            pid = state.get("Pid") if running else None
            return RuntimeState(
                exists=True,
                running=running,
                pid=int(pid) if pid else None,
                exit_code=state.get("ExitCode"),
                detail=str(state.get("Status") or ""),
            )
        except DockerError as exc:
            if exc.status == 404:
                return RuntimeState(exists=False, running=False, pid=None, exit_code=None)
            raise runtime_error(f"docker inspect failed: {exc}") from exc
        finally:
            await docker.close()

    async def exec_shell(self, h: RuntimeHandle, command: str) -> tuple[int, str]:
        docker = _docker()
        try:
            container = docker.containers.container(h.ref)
            proc = await container.exec(
                ["sh", "-c", command],
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
            )
            stream = proc.start()
            chunks: list[bytes] = []
            try:
                while True:
                    msg = await stream.read_out()
                    if msg is None:
                        break
                    chunks.append(msg.data)
            finally:
                await stream.close()
            inspect = await proc.inspect()
            rc = int((inspect.get("ExitCode") if inspect else 0) or 0)
            out = b"".join(chunks).decode("utf-8", errors="replace")
            return rc, out
        except DockerError as exc:
            # 409 = container is not in a state the exec API can act on
            # (usually stopped/exited/dead). The DB thinking it's
            # "running" is the state drift we cover in the router;
            # here we just surface a message the model can act on
            # rather than the raw aiodocker repr.
            if exc.status == 409:
                raise runtime_error(
                    "container is not running — its state has drifted "
                    "from Labtris. Start the node again."
                ) from exc
            if exc.status == 404:
                raise runtime_error(
                    "container no longer exists — it was removed outside "
                    "Labtris. Recreate the node."
                ) from exc
            raise runtime_error(f"docker exec failed: {exc}") from exc
        finally:
            await docker.close()

    async def commit_and_save(
        self, h: RuntimeHandle, image_tag: str, dst_tar: str
    ) -> dict[str, Any]:
        """Snapshot a running container's filesystem: `docker commit` to
        `image_tag`, then `docker save` to `dst_tar` (raw uncompressed
        tar — the pod archive wraps it in gzip).

        Used by hot lab snapshots (labtris_api/pods.py Phase D). Not on
        the DockerRuntime abstract base because only pods use it.

        Dirty-layer commit is normal for Docker: a write in-flight at
        commit time either lands in the new image or is committed
        partially. We inherit the same semantics — a hot snapshot of a
        container mid-write is the container mid-write, matching what
        `docker commit` on the CLI does. Callers should quiesce the
        workload themselves if that matters.
        """
        import shutil
        import subprocess

        docker = _docker()
        try:
            container = docker.containers.container(h.ref)
            # aiodocker's .commit() call returns the new image id.
            commit_info = await container.commit(repository=image_tag)
            image_id = str(commit_info.get("Id") or "").strip()
        except DockerError as exc:
            raise runtime_error(f"docker commit failed: {exc}") from exc
        finally:
            await docker.close()

        # aiodocker has no `save` — the endpoint streams a tarball. Shell
        # out to the docker CLI instead: it is guaranteed to be present
        # anywhere the API can talk to Docker, and the streaming write
        # goes straight to disk without buffering multi-GB in memory.
        if not shutil.which("docker"):
            raise runtime_error(
                "the `docker` CLI is not on PATH — required for hot pod snapshots"
            )
        with open(dst_tar, "wb") as fh:
            proc = subprocess.run(
                ["docker", "save", image_tag],
                stdout=fh,
                stderr=subprocess.PIPE,
                check=False,
            )
        if proc.returncode != 0:
            raise runtime_error(
                f"docker save failed: {proc.stderr.decode(errors='replace').strip()}"
            )
        import os

        return {
            "image_tag": image_tag,
            "image_id": image_id,
            "bytes": os.path.getsize(dst_tar),
            "tar_path": dst_tar,
        }

    async def open_shell(self, h: RuntimeHandle, cols: int = 100, rows: int = 30):
        """A real interactive shell in the container, with a TTY behind it.

        exec_shell runs one command and returns: no session, so `cd` does not
        persist, an interactive prompt cannot be answered, and Ctrl-C has
        nothing to interrupt because nothing is still running. That is fine for
        the assistant, which asks one question at a time, and wrong for a person.

        This keeps one exec alive for as long as the socket is open, with
        tty=True so the shell line-edits, colours its prompt and handles signals
        itself — the same reason the QEMU consoles feel different: they were
        always a real stream.

        Returns (stream, proc). The caller pumps it; closing the stream ends the
        exec, and the shell dying ends the stream.
        """
        docker = _docker()
        container = docker.containers.container(h.ref)
        # sh is the only shell guaranteed to exist; bash is nicer where present,
        # and a container without either is one where no shell was ever going
        # to work. `-i` is what makes the shell interactive so it draws a prompt
        # — without it the terminal opens blank and the user cannot tell "am I
        # connected" from "is this thing broken".
        #
        # The fallback picks the shell inside the container so a busybox image
        # gets `sh -i` instead of a bash-not-found error. `command -v bash`
        # short-circuits the check without spawning bash if it's missing, and
        # `exec` replaces the sh process so the interactive shell owns the tty
        # cleanly.
        #
        # A previous version had `exec /bin/bash -i 2>/dev/null || exec /bin/sh
        # -i` and produced a permanently blank terminal for every user. The
        # reason took a debug session to find: bash's readline writes the
        # prompt to *stderr*, and the `2>/dev/null` was swallowing it. Every
        # keystroke worked; the guest just had no visible prompt. Do not
        # reintroduce the stderr redirect.
        proc = await container.exec(
            [
                "/bin/sh",
                "-c",
                "if command -v bash >/dev/null 2>&1; then exec bash -i; "
                "else exec /bin/sh -i; fi",
            ],
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            environment=[
                "TERM=xterm-256color",
                f"COLUMNS={cols}",
                f"LINES={rows}",
                # Busybox-sh fallback: it does not set a prompt in interactive
                # mode unless one is in the env. Bash ignores this and uses
                # its own default.
                "PS1=\\u@\\h:\\w\\$ ",
            ],
        )
        stream = proc.start(detach=False)
        return stream, proc, docker

    async def resize_shell(self, proc: Any, cols: int, rows: int) -> None:
        """Tell the PTY its new size.

        Without this the shell wraps at whatever width it started with, so
        resizing the window corrupts every line longer than the original — the
        classic "my terminal is broken" that is really a stale winsize.
        """
        try:
            await proc.resize(h=rows, w=cols)
        except Exception:
            # A container that will not resize still works at its original
            # size, which is far better than dropping the session.
            return

    async def suspend(self, h: RuntimeHandle) -> None:
        """Docker has no snapshot/suspend primitive; `pause` freezes the cgroup
        (SIGSTOP-equivalent) which is the closest analogue and is instant/free."""
        docker = _docker()
        try:
            await docker.containers.container(h.ref).pause()
        except DockerError as exc:
            raise runtime_error(f"docker pause failed: {exc}") from exc
        finally:
            await docker.close()

    async def resume(self, h: RuntimeHandle) -> None:
        docker = _docker()
        try:
            await docker.containers.container(h.ref).unpause()
        except DockerError as exc:
            raise runtime_error(f"docker unpause failed: {exc}") from exc
        finally:
            await docker.close()

    async def logs(self, h: RuntimeHandle, lines: int) -> str:
        docker = _docker()
        try:
            container = docker.containers.container(h.ref)
            chunks = await container.log(stdout=True, stderr=True, tail=str(lines))
            return "".join(chunks)
        except DockerError as exc:
            raise runtime_error(f"docker logs failed: {exc}") from exc
        finally:
            await docker.close()

    async def write_file(self, h: RuntimeHandle, path: str, content: str) -> None:
        marker = f"LABTRIS_EOF_{abs(hash(content)) % 10**8}"
        script = (
            f"mkdir -p $(dirname '{path}') && cat > '{path}' << '{marker}'\n{content}\n{marker}\n"
        )
        rc, out = await self.exec_shell(h, script)
        if rc != 0:
            raise runtime_error(f"writing {path} failed: {out}")

    async def save_snapshot(self, h: RuntimeHandle, name: str) -> None:
        raise runtime_error("docker backend has no VM snapshot — Capability.SNAPSHOT is unset")

    async def load_snapshot(self, h: RuntimeHandle, name: str) -> None:
        raise runtime_error("docker backend has no VM snapshot — Capability.SNAPSHOT is unset")

    async def list_snapshots(self, h: RuntimeHandle) -> list[str]:
        return []

    async def ping_engine(self) -> bool:
        docker = _docker()
        try:
            await docker.version()
            return True
        except Exception:
            return False
        finally:
            await docker.close()

    async def _pid(self, h: RuntimeHandle) -> int:
        state = await self.observe(h)
        if not state.running or not state.pid:
            raise runtime_error("container is not running; cannot attach interface")
        return state.pid

    async def _ensure_image(self, docker: aiodocker.Docker, image: str) -> None:
        try:
            await docker.images.inspect(image)
            return
        except DockerError:
            pass
        repo, _, tag = image.partition(":")
        tag = tag or "latest"
        try:
            await docker.images.pull(from_image=repo, tag=tag)
        except DockerError as exc:
            raise runtime_error(f"docker pull {image} failed: {exc}") from exc


async def is_image_cached(image: str) -> bool:
    """Cheap yes/no: does `docker inspect` know this image already?

    Called from `system.catalog()` to surface a Pull button in the
    palette next to any docker image that would otherwise stall the
    first spawn on a registry pull."""
    d = _docker()
    try:
        await d.images.inspect(image)
        return True
    except DockerError:
        return False
    finally:
        await d.close()


#: Per-image live pull progress, updated by `pull_image_now` as
#: aiodocker's pull stream emits per-layer events. Shape matches what
#: `image_status` returns for QEMU pulls (done/total bytes, phase,
#: percent) so the palette can render the same progress bar for both.
#: Cleared to `{"cached": True}` on completion.
_docker_pull_progress: dict[str, dict[str, object]] = {}


def docker_pull_progress(image: str) -> dict[str, object] | None:
    """Snapshot of an in-flight docker pull, or None if nothing is
    happening. Called by routers/images.py's status endpoint so the
    palette's poll loop gets real byte-level progress."""
    return _docker_pull_progress.get(image)


async def pull_image_now(image: str) -> None:
    """Force a `docker pull` in the background with layer-level
    progress accounting.

    aiodocker's `pull(..., stream=True)` returns an async iterator of
    per-layer status dicts — `{status: 'Downloading', progressDetail:
    {current: N, total: M}, id: layer_id}` — that mirror what the
    docker CLI prints. We aggregate across layers to compute a whole-
    image percent so the palette shows one growing bar instead of a
    fistful of per-layer ones."""
    d = _docker()
    repo, _, tag = image.partition(":")
    tag = tag or "latest"
    layers: dict[str, dict[str, int]] = {}
    try:
        _docker_pull_progress[image] = {
            "phase": "starting", "done": 0, "total": 0, "percent": 0,
        }
        stream = await d.images.pull(from_image=repo, tag=tag, stream=True)
        async for line in stream:
            # aiodocker sometimes hands back bytes rows, sometimes
            # already-decoded dicts; handle both defensively.
            if isinstance(line, (bytes, str)):
                import json as _json

                try:
                    line = _json.loads(line)
                except (ValueError, TypeError):
                    continue
            if not isinstance(line, dict):
                continue
            status = str(line.get("status") or "")
            layer_id = line.get("id")
            detail = line.get("progressDetail") or {}
            if layer_id and status in ("Downloading", "Extracting"):
                layers[layer_id] = {
                    "current": int(detail.get("current") or 0),
                    "total": int(detail.get("total") or 0),
                }
            done = sum(int(l.get("current") or 0) for l in layers.values())
            total = sum(int(l.get("total") or 0) for l in layers.values())
            pct = int(done * 100 / total) if total else 0
            _docker_pull_progress[image] = {
                "phase": status.lower() or "pulling",
                "done": done,
                "total": total,
                "percent": pct,
            }
    except DockerError as exc:
        _docker_pull_progress.pop(image, None)
        raise runtime_error(f"docker pull {image} failed: {exc}") from exc
    else:
        _docker_pull_progress[image] = {
            "phase": "cached", "done": 0, "total": 0, "percent": 100,
        }
        # Leave the "cached" sentinel in for one status poll so the
        # UI sees the 100% frame before we clear.
        import asyncio as _asyncio
        await _asyncio.sleep(2)
        _docker_pull_progress.pop(image, None)
    finally:
        await d.close()


docker_runtime = DockerRuntime()
