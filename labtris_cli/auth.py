"""Session persistence for the CLI.

The API's `/auth/login` returns a 7-day JWT. The browser reads it from an
httponly cookie; the CLI reads it from the response body (which is why
`token` is now in the JSON envelope). We store the resulting token — plus
the URL it was minted against — in `~/.config/labtris/auth.json`.

No PAT / service-account concept, no keyring dependency. If the file is
readable only by the user (0600 on write) that matches what every other
tool does with a JWT."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from labtris_client import Api, ApiError

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "labtris"
_AUTH_PATH = _CONFIG_DIR / "auth.json"


@dataclass
class Session:
    url: str
    token: str
    username: str
    expires_at: str  # ISO 8601

    @property
    def is_expired(self) -> bool:
        try:
            when = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        return when <= datetime.now(UTC)


def load_session() -> Session | None:
    """Return the persisted session, or None. Silent on any read error."""
    try:
        raw = json.loads(_AUTH_PATH.read_text())
        return Session(
            url=raw["url"],
            token=raw["token"],
            username=raw["username"],
            expires_at=raw["expires_at"],
        )
    except (OSError, ValueError, KeyError):
        return None


def save_session(sess: Session) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _AUTH_PATH.write_text(
        json.dumps(
            {
                "url": sess.url,
                "token": sess.token,
                "username": sess.username,
                "expires_at": sess.expires_at,
            },
            indent=2,
        )
    )
    # 0600 on the file, 0700 on the directory. A JWT is a bearer credential:
    # anyone who reads it before it expires can impersonate the user for the
    # rest of the TTL. Same treatment ~/.aws/credentials and ~/.docker/config.json
    # get on any modern install.
    _AUTH_PATH.chmod(0o600)
    _CONFIG_DIR.chmod(0o700)


def forget_session() -> None:
    try:
        _AUTH_PATH.unlink()
    except FileNotFoundError:
        pass


def login(url: str, username: str, password: str) -> Session:
    """POST /auth/login, persist the returned session, return it.

    Raises `ApiError` on any HTTP failure — the caller prints the message
    and exits nonzero."""
    api = Api(base=url)
    resp = api.call("POST", "/api/v1/auth/login", {"username": username, "password": password})
    expires_in = int(resp.get("expires_in") or 0)
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    sess = Session(
        url=url.rstrip("/"),
        token=resp["token"],
        username=resp["user"]["username"],
        expires_at=expires_at,
    )
    save_session(sess)
    return sess


def logout(sess: Session | None) -> None:
    """Tell the server to invalidate the cookie AND drop the local file.

    The server's /auth/logout endpoint only clears the browser cookie today;
    the token itself is stateless (JWT with an exp claim) and cannot be
    revoked server-side. So the local file is the actual credential — we
    remove it, and best-effort call the endpoint for hygiene."""
    if sess is not None:
        try:
            Api(base=sess.url, token=sess.token).call("POST", "/api/v1/auth/logout")
        except ApiError:
            pass
    forget_session()


def require_session() -> Session:
    """The auth check every non-login subcommand runs first.

    Returns the live session or exits with a clear "run labtris login"
    message. Environment override: LABTRIS_TOKEN + LABTRIS_API_URL work as
    a service-account shape for CI/scripts — the file is not consulted
    when both are set."""
    env_token = os.environ.get("LABTRIS_TOKEN")
    env_url = os.environ.get("LABTRIS_API_URL")
    if env_token and env_url:
        return Session(url=env_url.rstrip("/"), token=env_token, username="(env)", expires_at="")
    sess = load_session()
    if sess is None:
        import sys

        sys.stderr.write(
            "No Labtris session — run `labtris login --url URL --user USER` first.\n"
        )
        raise SystemExit(2)
    if sess.is_expired:
        import sys

        sys.stderr.write(
            f"Session for {sess.username} at {sess.url} expired — run "
            "`labtris login` again.\n"
        )
        raise SystemExit(2)
    return sess


def api_for(sess: Session) -> Api:
    return Api(base=sess.url, token=sess.token)
