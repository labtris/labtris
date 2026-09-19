from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Api:
    """Thin JSON client for a Labtris instance.

    urllib rather than httpx so the server can be run straight from a checkout
    with nothing installed. It talks to whatever instance LABTRIS_API_URL points
    at, so an agent on a laptop can drive a lab host over the network — which
    is the whole point of shipping this separately from the API process."""

    def __init__(
        self,
        base: str | None = None,
        user: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ) -> None:
        resolved = base or os.environ.get("LABTRIS_API_URL") or "http://127.0.0.1:8080"
        self.base = resolved.rstrip("/")
        user = user if user is not None else os.environ.get("LABTRIS_API_USER")
        password = password if password is not None else os.environ.get("LABTRIS_API_PASSWORD")
        self.auth = (
            base64.b64encode(f"{user}:{password}".encode()).decode() if user and password else None
        )
        # A Labtris session, distinct from any HTTP Basic credentials a proxy
        # in front of the instance may also want. Without it every call is
        # anonymous, which since authentication landed means every call is a
        # 401 — the tools would report success having done nothing.
        self.token = token if token is not None else os.environ.get("LABTRIS_TOKEN")

    def call(self, method: str, path: str, body: Any = None) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        # Bearer wins: it is the identity Labtris itself checks. Basic, when
        # present, is for a reverse proxy and would be consumed before the API
        # ever saw it.
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        elif self.auth:
            req.add_header("Authorization", f"Basic {self.auth}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                message = json.loads(raw)["error"]["message"]
            except Exception:  # noqa: BLE001 - not every error is our envelope
                message = raw[:400] or exc.reason
            raise ApiError(exc.code, message) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"cannot reach {self.base}: {exc.reason}") from None

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body if body is not None else {})

    def patch(self, path: str, body: Any) -> Any:
        return self.call("PATCH", path, body)

    def delete(self, path: str) -> Any:
        return self.call("DELETE", path)

    def post_bytes(self, path: str, body: Any = None) -> tuple[bytes, str]:
        """POST and return the raw response body + Content-Type.

        Used for endpoints that return binary data (vnc/screenshot returns
        PNG bytes). Sidesteps `call()`'s json-decode step, which would
        crash on non-JSON responses."""
        url = f"{self.base}{path}"
        data = json.dumps(body if body is not None else {}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        elif self.auth:
            req.add_header("Authorization", f"Basic {self.auth}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                message = json.loads(raw)["error"]["message"]
            except Exception:  # noqa: BLE001
                message = raw[:400] or exc.reason
            raise ApiError(exc.code, message) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"cannot reach {self.base}: {exc.reason}") from None
