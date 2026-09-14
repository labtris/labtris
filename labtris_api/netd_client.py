from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from labtris_api.config import settings
from labtris_api.errors import runtime_error
from labtris_api.models import Host
from labtris_netd.protocol import IFNAME_RE, decode_line, encode


class NetdError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NetdClient:
    """Talks to one netd — local over a unix socket, or a registered remote
    Host over TCP with a bearer token (see `labtris_api.models.Host`). Multi-host
    is this: the API is one control plane driving several of these."""

    def __init__(
        self, path: str | None = None, endpoint: str | None = None, token: str | None = None
    ) -> None:
        self.endpoint = endpoint or (f"unix://{path}" if path else f"unix://{settings.netd_socket}")
        self.token = token
        self._lock = asyncio.Lock()
        self._req_id = 0

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        parts = urlsplit(self.endpoint)
        if parts.scheme == "tcp":
            return await asyncio.open_connection(parts.hostname, parts.port)
        return await asyncio.open_unix_connection(
            parts.path or self.endpoint.removeprefix("unix://")
        )

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        verb: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self._req_id += 1
        req_id = f"r{self._req_id}"
        writer.write(encode({"id": req_id, "verb": verb, "params": params}))
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise runtime_error("netd closed the connection")
        msg = decode_line(line)
        if msg.get("id") != req_id:
            raise runtime_error("netd response id mismatch")
        return msg

    async def call(self, verb: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            try:
                reader, writer = await self._connect()
            except OSError as exc:
                raise runtime_error(f"netd unavailable ({self.endpoint}): {exc}") from exc
            try:
                if self.token:
                    auth = await self._send(writer, reader, "auth", {"token": self.token})
                    if not auth.get("ok"):
                        raise NetdError("EAUTH", "netd rejected the auth token")
                msg = await self._send(writer, reader, verb, params or {})
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        if not msg.get("ok"):
            err = msg.get("error") or {}
            raise NetdError(
                str(err.get("code", "EINTERNAL")), str(err.get("message", "netd error"))
            )
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    async def ping(self) -> bool:
        try:
            result = await self.call("ping")
            return bool(result.get("pong"))
        except Exception:
            return False


def client_for_endpoint(endpoint: str, token: str | None = None) -> NetdClient:
    return NetdClient(endpoint=endpoint, token=token)


def client_for_host(host: Host) -> NetdClient:
    if host.is_local:
        return netd
    return client_for_endpoint(host.endpoint, host.token)


netd = NetdClient()

_ = IFNAME_RE
