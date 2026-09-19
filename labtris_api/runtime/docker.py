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


docker_runtime = DockerRuntime()
