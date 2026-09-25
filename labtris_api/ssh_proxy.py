"""SSH proxy — every running node looks like an SSH-accessible device.

The idea, from LocalStack: netmiko / scrapli / ansible-network / napalm — every
tool that already speaks Cisco / Junos / Arista / FRR CLIs — is a client. Give
them an SSH endpoint they can point at and Labtris nodes stop being "browser
console tabs" and start being "devices in a lab". This module is the endpoint.

Design points:
* One asyncssh server per API process, listening on the port from settings.
  Off by default; opt in with LABTRIS_SSH_PROXY_ENABLED=true.
* SSH username = the Labtris node name (matches how containerlab and openssh
  ProxyJump map "which node am I addressing" onto the username slot). When a
  node name is ambiguous across labs, form `<lab-slug>:<node-name>` and the
  first `:` in the username is the split.
* SSH password = a Labtris JWT (7-day token from /auth/login). Every existing
  tool takes a password over SSH, so this needs no per-tool integration. The
  JWT identifies the Labtris user; ANY authenticated user can currently reach
  any node — Labtris is single-tenant, and multi-tenant is separate work.
* Each SSH channel is bridged to the node's transport: SerialSession for QEMU
  (the same one the browser console tab uses; multiple subscribers are
  supported already), and open_shell() for Docker in the K1b follow-on.
* Host key is ed25519, generated once at first start and persisted so restarts
  don't rotate the fingerprint (that would break every known_hosts entry).

This module is deliberately a transport, not a protocol translator: it doesn't
parse `show ip route`, it doesn't know what a Cisco enable prompt is. Any
scripted CLI work uses the tools that already know that (netmiko et al.);
Labtris just makes their existing SSH transport work.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import structlog

from labtris_api.config import settings

logger = structlog.get_logger(__name__)

_server: Any = None  # asyncssh.SSHAcceptor once started; None otherwise.


async def _safe_close(obj: Any) -> None:
    """Best-effort close on aiodocker streams / clients — nothing worth
    surfacing if the socket is already gone by the time we get here."""
    try:
        await obj.close()
    except Exception:  # noqa: BLE001
        pass


def _host_key_path() -> Path:
    p = Path(settings.ssh_proxy_host_key_path).expanduser()
    return p


def _ensure_host_key() -> Path:
    """Generate the server's persistent ed25519 host key on first use.

    If the file exists we trust it — regenerating on every start would rotate
    the fingerprint on every deploy and break `known_hosts` for every client
    that ever connected. If it does not exist, create it 0600 and log the
    fingerprint so an operator can pin it in ~/.ssh/known_hosts.
    """
    import asyncssh  # local import: keeps `import labtris_api.ssh_proxy` cheap.

    path = _host_key_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519")
    path.write_bytes(key.export_private_key())
    path.chmod(0o600)
    fp = key.get_fingerprint()
    logger.info("ssh_proxy.hostkey.generated", path=str(path), fingerprint=fp)
    return path


async def _resolve_node(username: str) -> tuple[str, str, str, str] | None:
    """`<labtris-user>@<node>` — the node-name is the SSH username. Returns
    `(node_id, node_name, runtime, runtime_ref)` or None.

    When the same name is used across labs (e.g. `r1` in two different labs),
    the caller can write `lab-slug:node-name` and we pick that specific pair.
    Otherwise a bare name matches the single node with that name; two matches
    return None so the client sees `Permission denied` rather than randomly
    landing in one of the two labs.
    """
    from sqlalchemy import select

    from labtris_api.db import SessionLocal
    from labtris_api.models import Lab, Node

    lab_slug: str | None = None
    name = username
    if ":" in username:
        lab_slug, name = username.split(":", 1)

    async with SessionLocal() as session:
        stmt = select(Node.id, Node.name, Node.runtime, Node.runtime_ref, Lab.name).join(
            Lab, Lab.id == Node.lab_id
        ).where(Node.name == name)
        rows = (await session.execute(stmt)).all()
        if not rows:
            return None
        if lab_slug is not None:
            rows = [r for r in rows if _slug(r[4]) == lab_slug]
            if len(rows) != 1:
                return None
        elif len(rows) > 1:
            return None
        node_id, node_name, runtime, runtime_ref, _lab_name = rows[0]
        return (node_id, node_name, runtime, runtime_ref or "")


def _slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


async def _verify_jwt(token: str) -> str | None:
    """Return the Labtris user_id the JWT belongs to, or None if invalid.

    The two failure modes we care about — expired token and rotated signing
    key — both come back from `read_token` as None, so any None means the
    login prompt should re-appear on the client. A user row that has been
    disabled since the token was issued also fails closed here."""
    from labtris_api.auth import read_token
    from labtris_api.db import SessionLocal
    from labtris_api.models import User

    claims = read_token(token)
    if not claims:
        return None
    user_id = claims.get("sub")
    if not user_id:
        return None
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        if row is None or row.disabled:
            return None
    return user_id


async def _lookup_pubkey(fingerprint: str) -> str | None:
    """Return the Labtris user_id that owns this pubkey fingerprint.

    The SshKey table's unique constraint on `fingerprint` means at most one
    hit is possible; a disabled user's key still matches here so the
    check-and-reject happens in one place with a friendly log line."""
    from sqlalchemy import select

    from labtris_api.db import SessionLocal
    from labtris_api.models import SshKey, User

    async with SessionLocal() as session:
        row = (
            await session.execute(select(SshKey).where(SshKey.fingerprint == fingerprint))
        ).scalar_one_or_none()
        if row is None:
            return None
        user = await session.get(User, row.user_id)
        if user is None or user.disabled:
            return None
        return user.id


def _build_classes():
    """Build the asyncssh Server + Session subclasses, once asyncssh is on
    the path. Kept in a factory so `import labtris_api.ssh_proxy` does not
    pay the asyncssh import cost when the proxy is disabled (which is the
    default). The classes have to actually inherit from asyncssh's
    protocol classes or asyncssh treats them as unknown callbacks and
    hangs up after the service-accept."""
    import asyncssh

    class _Server(asyncssh.SSHServer):
        def __init__(self) -> None:
            self._node: tuple[str, str, str, str] | None = None
            self._user_id: str | None = None

        def connection_made(self, conn: Any) -> None:
            self._conn = conn

        def connection_lost(self, exc: BaseException | None) -> None:
            pass

        def begin_auth(self, username: str) -> bool:
            # True = "auth required" — the default of False lets anyone in.
            return True

        def password_auth_supported(self) -> bool:
            return True

        def public_key_auth_supported(self) -> bool:
            return True

        async def validate_public_key(self, username: str, key: Any) -> bool:
            """Match the presented pubkey's fingerprint against SshKey rows.

            The fingerprint is derived here (server side) so the client cannot
            claim ownership of a key it doesn't actually have — asyncssh has
            already verified the signature at this point, so the key we're
            fingerprinting IS the one the client authenticated with."""
            try:
                fp = key.get_fingerprint()
            except Exception:  # noqa: BLE001
                return False
            user_id = await _lookup_pubkey(fp)
            if user_id is None:
                logger.info(
                    "ssh_proxy.auth.pubkey_unknown", username=username, fingerprint=fp
                )
                return False
            node = await _resolve_node(username)
            if node is None:
                logger.info(
                    "ssh_proxy.auth.node_unknown",
                    username=username,
                    user_id=user_id,
                )
                return False
            self._user_id = user_id
            self._node = node
            # Bump last_used_at so `labtris ssh-keys list` shows liveness.
            try:
                from labtris_api.routers.ssh_keys import touch_last_used

                await touch_last_used(fp)
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                "ssh_proxy.auth.pubkey_ok",
                user_id=user_id,
                node_id=node[0],
                node_name=node[1],
                fingerprint=fp,
            )
            return True

        async def validate_password(self, username: str, password: str) -> bool:
            """SSH `username` = node name, `password` = Labtris JWT.

            Single generic outcome on failure (matches real devices).
            Diagnose from the labtris-api journal — the log lines below
            carry the specific reason."""
            user_id = await _verify_jwt(password)
            if user_id is None:
                logger.info("ssh_proxy.auth.jwt_invalid", username=username)
                return False
            node = await _resolve_node(username)
            if node is None:
                logger.info(
                    "ssh_proxy.auth.node_unknown",
                    username=username,
                    user_id=user_id,
                )
                return False
            self._user_id = user_id
            self._node = node
            logger.info(
                "ssh_proxy.auth.ok",
                user_id=user_id,
                node_id=node[0],
                node_name=node[1],
            )
            return True

        def session_requested(self) -> Any:
            return _Session(self._node, self._user_id)

    class _Session(asyncssh.SSHServerSession):
        def __init__(
            self,
            node: tuple[str, str, str, str] | None,
            user_id: str | None,
        ) -> None:
            self._node = node
            self._user_id = user_id
            self._chan: Any = None
            self._serial: Any = None
            self._queue: asyncio.Queue[bytes] | None = None
            self._pump: asyncio.Task[None] | None = None
            # Docker branch:
            self._docker_stream: Any = None
            self._docker_proc: Any = None
            self._docker_client: Any = None
            self._docker_pump: asyncio.Task[None] | None = None
            # Requested PTY size — captured in pty_requested, forwarded to
            # the docker exec after open_shell (open_shell takes cols/rows
            # at spawn) and re-applied whenever the client's window changes.
            self._cols = 100
            self._rows = 30

        def connection_made(self, chan: Any) -> None:
            self._chan = chan
            # session_started isn't awaited on asyncssh's server sessions —
            # kick off the attach-and-pump task from here as soon as the
            # channel is up. Any error inside emits over the channel and
            # exits with status 1, matching how a real device would tell
            # a client "sorry, not this time".
            asyncio.get_event_loop().create_task(self._attach())

        def connection_lost(self, exc: BaseException | None) -> None:
            if self._pump is not None:
                self._pump.cancel()
            if self._docker_pump is not None:
                self._docker_pump.cancel()
            if self._serial is not None and self._queue is not None:
                try:
                    self._serial.unsubscribe(self._queue)
                except Exception:  # noqa: BLE001
                    pass
            if self._docker_stream is not None:
                asyncio.get_event_loop().create_task(_safe_close(self._docker_stream))
            if self._docker_client is not None:
                asyncio.get_event_loop().create_task(_safe_close(self._docker_client))

        def pty_requested(
            self,
            term_type: str,
            term_size: tuple[int, int, int, int],
            term_modes: dict[int, int],
        ) -> bool:
            # term_size = (cols, rows, pixwidth, pixheight)
            try:
                self._cols = int(term_size[0]) or self._cols
                self._rows = int(term_size[1]) or self._rows
            except (IndexError, TypeError, ValueError):
                pass
            return True

        def terminal_size_changed(
            self, width: int, height: int, pixwidth: int, pixheight: int
        ) -> None:
            """Client resized its window mid-session. Forward to the docker
            PTY; QEMU serial has no window concept and safely ignores this."""
            self._cols = width or self._cols
            self._rows = height or self._rows
            if self._docker_proc is not None:
                from labtris_api.runtime.docker import docker_runtime

                asyncio.get_event_loop().create_task(
                    docker_runtime.resize_shell(self._docker_proc, self._cols, self._rows)
                )

        def shell_requested(self) -> bool:
            return True

        def exec_requested(self, command: str) -> bool:
            # No reliable prompt-detection over serial in K1a; force
            # interactive so scripted clients (netmiko et al.) apply their
            # own prompt handling.
            return False

        def subsystem_requested(self, subsystem: str) -> bool:
            # NETCONF subsystem is a later phase.
            return False

        def break_received(self, msec: int) -> bool:
            if self._serial is not None:
                asyncio.get_event_loop().create_task(self._serial.send(b"\x03"))
            return True

        async def _attach(self) -> None:
            assert self._chan is not None
            node = self._node
            if node is None:
                self._chan.write("labtris: no node resolved\r\n")
                self._chan.exit(1)
                return

            node_id, node_name, runtime, runtime_ref = node
            if runtime == "qemu":
                await self._attach_qemu(node_id, node_name, runtime_ref)
            elif runtime == "docker":
                await self._attach_docker(node_id, node_name, runtime_ref)
            else:
                self._chan.write(
                    f"labtris: {node_name!r} has runtime {runtime!r} — SSH proxy "
                    "supports qemu and docker only.\r\n"
                )
                self._chan.exit(1)

        async def _attach_qemu(self, node_id: str, node_name: str, runtime_ref: str) -> None:
            from labtris_api.runtime.qemu import attach_session

            serial = await attach_session(node_id, Path(runtime_ref))
            if serial is None:
                self._chan.write(
                    "labtris: no serial session for this node — (re)start it first.\r\n"
                )
                self._chan.exit(1)
                return

            self._serial = serial
            buf = serial.buffered()
            if buf:
                self._chan.write(buf.decode(errors="replace"))
            self._queue = serial.subscribe()
            self._pump = asyncio.get_event_loop().create_task(self._pump_qemu_out())

        async def _pump_qemu_out(self) -> None:
            assert self._queue is not None and self._chan is not None
            try:
                while True:
                    chunk = await self._queue.get()
                    self._chan.write(chunk.decode(errors="replace"))
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        async def _attach_docker(
            self, node_id: str, node_name: str, container_ref: str
        ) -> None:
            """Bridge to `docker exec -it <sh>` via the existing runtime.

            Same open_shell / resize_shell pair the browser terminal already
            uses (see routers/nodes.py:1361). What differs is the transport —
            SSH channel bytes instead of a websocket — so the pump loops on
            aiodocker's stream.read_out / stream.write_in, and PTY resize is
            piped from asyncssh's terminal_size_changed callback.
            """
            if not container_ref:
                self._chan.write(
                    "labtris: this container has no runtime handle — start it first.\r\n"
                )
                self._chan.exit(1)
                return

            from labtris_api.runtime.base import RuntimeHandle
            from labtris_api.runtime.docker import docker_runtime

            handle = RuntimeHandle(node_id=node_id, ref=container_ref)
            try:
                stream, proc, docker = await docker_runtime.open_shell(
                    handle, cols=self._cols, rows=self._rows
                )
            except Exception as exc:  # noqa: BLE001
                self._chan.write(
                    f"labtris: could not open shell in {node_name!r}: {exc}\r\n"
                )
                self._chan.exit(1)
                return

            self._docker_stream = stream
            self._docker_proc = proc
            self._docker_client = docker
            self._docker_pump = asyncio.get_event_loop().create_task(self._pump_docker_out())

        async def _pump_docker_out(self) -> None:
            assert self._docker_stream is not None and self._chan is not None
            try:
                while True:
                    msg = await self._docker_stream.read_out()
                    if msg is None:
                        break
                    self._chan.write(msg.data.decode(errors="replace"))
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            finally:
                # Container exec ended — close the channel cleanly so the
                # client sees the disconnect and their shell prompt returns.
                try:
                    self._chan.exit(0)
                except Exception:  # noqa: BLE001
                    pass

        def data_received(self, data: str, datatype: int | None) -> None:
            payload = data.encode()
            if self._serial is not None:
                asyncio.get_event_loop().create_task(self._serial.send(payload))
                return
            if self._docker_stream is not None:
                asyncio.get_event_loop().create_task(self._docker_stream.write_in(payload))

        def eof_received(self) -> bool:
            # Half-open: client did EOF but we can still emit output. A
            # real UART works the same way.
            return True

    return _Server


async def start() -> None:
    """Start the SSH proxy listener. No-op if disabled in settings.

    Failures are logged but never raise: an SSH-proxy port collision must
    not stop labtris-api from booting, or the entire lab manager dies
    because 2222 was busy. Users diagnose from /api/v1/system/status."""
    global _server
    if _server is not None:
        return
    if not settings.ssh_proxy_enabled:
        logger.info("ssh_proxy.disabled")
        return
    try:
        import asyncssh
    except ImportError:
        logger.warning("ssh_proxy.asyncssh_missing")
        return

    # asyncssh chats over its own logger at DEBUG. Silence auth messages
    # in the default log unless the operator raised the labtris log level.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)

    host_key = _ensure_host_key()
    server_cls = _build_classes()
    try:
        _server = await asyncssh.listen(
            host=settings.ssh_proxy_bind,
            port=settings.ssh_proxy_port,
            server_factory=server_cls,
            server_host_keys=[str(host_key)],
            # Refuse SFTP subsystem explicitly — this is an interactive
            # transport, not a file store.
            sftp_factory=None,
            # Accept algorithms modern OpenSSH negotiates; asyncssh's
            # defaults are already conservative.
            reuse_address=True,
            reuse_port=False,
        )
    except OSError as exc:
        logger.error(
            "ssh_proxy.listen_failed",
            bind=settings.ssh_proxy_bind,
            port=settings.ssh_proxy_port,
            error=str(exc),
        )
        _server = None
        return

    logger.info(
        "ssh_proxy.listening",
        bind=settings.ssh_proxy_bind,
        port=settings.ssh_proxy_port,
        host_key=str(host_key),
    )


async def stop() -> None:
    """Stop the SSH listener during API shutdown. Idempotent."""
    global _server
    if _server is None:
        return
    try:
        _server.close()
        await _server.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    _server = None
    logger.info("ssh_proxy.stopped")


def status() -> dict[str, Any]:
    """Shape surfaced by /api/v1/system/status so operators can tell whether
    the proxy is actually listening."""
    return {
        "enabled": settings.ssh_proxy_enabled,
        "port": settings.ssh_proxy_port,
        "bind": settings.ssh_proxy_bind,
        "listening": _server is not None,
        "host_key_path": str(_host_key_path()),
    }
