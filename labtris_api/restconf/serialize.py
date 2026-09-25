"""JSON encoding helpers for RESTCONF responses (Phase K3).

RFC 8040 says JSON responses use the module-name-prefixed key form —
`{"ietf-interfaces:interfaces": {...}}` at the top level, then bare
`{"interface": [...]}` inside. `envelope()` builds that once so every
handler doesn't repeat the prefix logic.

Error responses follow RFC 8040 §7.1: `{"ietf-restconf:errors":
{"error": [{"error-type": "...", "error-tag": "...", "error-message":
"..."}]}}`. `errors()` returns exactly that.
"""

from __future__ import annotations

from typing import Any

YANG_DATA_JSON = "application/yang-data+json"


def envelope(module: str, container: str, payload: Any) -> dict[str, Any]:
    """Wrap a payload in the RFC 8040 module-prefixed top-level key.

    module   e.g. "ietf-interfaces"
    container e.g. "interfaces", "interfaces-state"
    payload   whatever the container's schema says goes inside
    """
    return {f"{module}:{container}": payload}


def errors(
    tag: str,
    message: str,
    *,
    error_type: str = "application",
    path: str | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "error-type": error_type,
        "error-tag": tag,
        "error-message": message,
    }
    if path is not None:
        err["error-path"] = path
    return {"ietf-restconf:errors": {"error": [err]}}
