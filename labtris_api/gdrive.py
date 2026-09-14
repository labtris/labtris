from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: Overridable so the tests can point the whole flow at a stub. Google's real
#: endpoints are the defaults; nothing else in the module hardcodes a host.
OAUTH_DEVICE_URL = "https://oauth2.googleapis.com/device/code"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"

#: Only files this app created. The broad `drive` scope would let a backup
#: tool read a user's entire Drive, which it has no business doing.
SCOPE = "https://www.googleapis.com/auth/drive.file"


class DriveError(RuntimeError):
    pass


def _request(url: str, data: bytes | None = None, headers: dict[str, str] | None = None,
             method: str | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        raise DriveError(f"Google returned {exc.code}: {body}") from None
    except urllib.error.URLError as exc:
        raise DriveError(f"cannot reach Google: {exc.reason}") from None


def begin_device_auth(client_id: str) -> dict[str, Any]:
    """Start the device flow.

    A lab host is usually headless and often reached over SSH, so the browser
    redirect flow is the wrong shape: it needs a callback URL this instance
    may not have. The device flow gives the user a code to type on whatever
    machine they are actually sitting at."""
    payload = urllib.parse.urlencode({"client_id": client_id, "scope": SCOPE}).encode()
    return _request(
        OAUTH_DEVICE_URL, payload, {"Content-Type": "application/x-www-form-urlencoded"}
    )


def poll_device_auth(client_id: str, client_secret: str, device_code: str) -> dict[str, Any]:
    """Exchange the device code once the user has approved it.

    `authorization_pending` is the normal answer while they are still typing,
    and is reported as such rather than as a failure."""
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
    ).encode()
    try:
        return _request(
            OAUTH_TOKEN_URL, payload, {"Content-Type": "application/x-www-form-urlencoded"}
        )
    except DriveError as exc:
        if "authorization_pending" in str(exc) or "slow_down" in str(exc):
            return {"pending": True}
        raise


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    data = _request(
        OAUTH_TOKEN_URL, payload, {"Content-Type": "application/x-www-form-urlencoded"}
    )
    token = data.get("access_token")
    if not token:
        raise DriveError("Google did not return an access token; re-authorise this instance")
    return str(token)


def upload(token: str, name: str, blob: bytes, folder_id: str | None = None) -> dict[str, Any]:
    """Multipart upload of one archive.

    Multipart rather than resumable on purpose: a config backup is tens of KB
    to a few MB, and resumable costs an extra round trip per file for a
    transfer that will not be interrupted. If image backup is ever added this
    is the function that has to change."""
    meta: dict[str, Any] = {"name": name}
    if folder_id:
        meta["parents"] = [folder_id]
    boundary = "labtris-boundary-7c1f"
    body = b"".join(
        [
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
            json.dumps(meta).encode(),
            f"\r\n--{boundary}\r\nContent-Type: application/gzip\r\n\r\n".encode(),
            blob,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return _request(
        f"{UPLOAD_URL}?uploadType=multipart",
        body,
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
    )


def listing(token: str, folder_id: str | None = None) -> list[dict[str, Any]]:
    query = "name contains 'labtris-backup' and trashed = false"
    if folder_id:
        query += f" and '{folder_id}' in parents"
    url = f"{FILES_URL}?" + urllib.parse.urlencode(
        {"q": query, "fields": "files(id,name,size,createdTime)", "orderBy": "createdTime desc"}
    )
    data = _request(url, headers={"Authorization": f"Bearer {token}"})
    return list(data.get("files") or [])


def download(token: str, file_id: str) -> bytes:
    req = urllib.request.Request(
        f"{FILES_URL}/{file_id}?alt=media", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return bytes(resp.read())
    except urllib.error.HTTPError as exc:
        raise DriveError(f"Google returned {exc.code} fetching {file_id}") from None


def default_name() -> str:
    return f"labtris-backup-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.tar.gz"
