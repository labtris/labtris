from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from labtris_api.version import __version__
from labtris_mcp.client import Api, ApiError
from labtris_mcp.tools import TOOLS

PROTOCOL_VERSION = "2025-06-18"
SERVER = {"name": "labtris", "version": __version__}


def _result(request_id: Any, payload: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _text(payload: Any, is_error: bool = False) -> dict[str, Any]:
    # A tool may return content blocks directly (image, mixed) — pass those
    # through instead of json-dumping the dict. Detected by a `content` key
    # that is a list of blocks; anything else is treated as data to render.
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("content"), list)
        and payload["content"]
        and isinstance(payload["content"][0], dict)
        and "type" in payload["content"][0]
    ):
        return {"content": payload["content"], "isError": is_error}
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def handle(message: dict[str, Any], api: Api) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # Notifications carry no id and must never be answered.
    if request_id is None:
        return None

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER,
                "instructions": (
                    "Drives a labtris instance: build labs, start nodes, wire them "
                    "together or onto shared segments, impair links, and capture traffic. "
                    "Start with health and list_catalog. A QEMU image that is not already "
                    "cached costs a multi-GB download on its first start."
                ),
            },
        )

    if method == "tools/list":
        return _result(
            request_id,
            {
                "tools": [
                    {"name": name, "description": desc, "inputSchema": schema}
                    for name, (desc, schema, _) in sorted(TOOLS.items())
                ]
            },
        )

    if method == "tools/call":
        name = str(params.get("name") or "")
        entry = TOOLS.get(name)
        if entry is None:
            return _result(request_id, _text(f"unknown tool {name!r}", is_error=True))
        _, _, fn = entry
        try:
            return _result(request_id, _text(fn(api, params.get("arguments") or {})))
        except ApiError as exc:
            # Reported as a tool error rather than a protocol error: the model
            # should see "that NIC already backs another cloud" and adjust,
            # not have the conversation aborted.
            return _result(request_id, _text(f"{exc.status or 'error'}: {exc.message}", True))
        except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the session
            return _result(request_id, _text(f"{type(exc).__name__}: {exc}", is_error=True))

    if method == "ping":
        return _result(request_id, {})

    return _error(request_id, -32601, f"method not found: {method}")


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    api: Api | None = None,
) -> None:
    """MCP stdio transport: newline-delimited JSON-RPC 2.0, one message per line."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    api = api or Api()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = _error(None, -32700, "parse error")
        else:
            reply = handle(message, api)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main() -> None:
    serve()
