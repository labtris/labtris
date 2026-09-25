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

from labtris_cli.profiles import (
    get_profile,
    profile_is_expired,
    profile_name,
    save_profile,
)
from labtris_client import Api, ApiError

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "labtris"
#: Retained for backwards compatibility — pre-K2 the single active session
#: lived here. K2 stores per-profile sessions in profiles.json but keeps
#: writing the mirror file so `cat ~/.config/labtris/auth.json | jq
#: .token` in existing scripts continues to work for the currently-
#: selected profile.
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
    """Return the currently-selected profile's session, or None.

    Consults profiles.json via `get_profile()`; that function handles the
    one-time import from the legacy auth.json for existing users. Silent
    on any read error — a missing/corrupt profiles.json prompts a login
    at the next `require_session()` check."""
    profile = get_profile()
    if not profile or not profile.get("token"):
        return None
    return Session(
        url=profile.get("url", ""),
        token=profile["token"],
        username=profile.get("username", ""),
        expires_at=profile.get("expires_at", ""),
    )


def save_session(sess: Session, profile: str | None = None) -> None:
    """Persist a session against a profile name.

    Also mirrors the current profile into the legacy `auth.json` so
    scripts that already read that file keep working. The mirror stays
    in sync with whichever profile is `current`."""
    name = profile or profile_name()
    save_profile(name, sess.url, sess.token, sess.username, sess.expires_at)
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
    _AUTH_PATH.chmod(0o600)
    _CONFIG_DIR.chmod(0o700)


def forget_session(profile: str | None = None) -> None:
    """Drop the profile's stored credentials from profiles.json + the
    legacy mirror. If the profile being forgotten is the current one,
    also drop the flat auth.json mirror so `LABTRIS_TOKEN`-style
    consumers stop finding it."""
    from labtris_cli.profiles import delete_profile

    name = profile or profile_name()
    delete_profile(name)
    if profile is None:
        try:
            _AUTH_PATH.unlink()
        except FileNotFoundError:
            pass


def login(url: str, username: str, password: str, profile: str | None = None) -> Session:
    """POST /auth/login, persist the returned session, return it.

    Raises `ApiError` on any HTTP failure — the caller prints the message
    and exits nonzero. `profile` names the slot the credentials are saved
    to — defaults to whatever `profile_name()` resolves to at call time.
    """
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
    save_session(sess, profile=profile)
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

    Returns the live session or exits with a clear "run labtris login /
    labtris configure" message. Env overrides: LABTRIS_TOKEN +
    LABTRIS_API_URL work as a service-account shape for CI/scripts — the
    profile store is not consulted when both are set. LABTRIS_PROFILE
    picks which named profile to read otherwise."""
    env_token = os.environ.get("LABTRIS_TOKEN")
    env_url = os.environ.get("LABTRIS_API_URL")
    if env_token and env_url:
        return Session(url=env_url.rstrip("/"), token=env_token, username="(env)", expires_at="")
    name = profile_name()
    profile = get_profile(name)
    if profile is None or not profile.get("token"):
        import sys

        sys.stderr.write(
            f"No Labtris session in profile {name!r} — run "
            "`labtris configure --profile "
            f"{name}` to set one up, or "
            "`labtris login --url URL --user USER`.\n"
        )
        raise SystemExit(2)
    if profile_is_expired(profile):
        import sys

        sys.stderr.write(
            f"Session in profile {name!r} at {profile.get('url','')} expired — "
            f"run `labtris login --url {profile.get('url','')} --user "
            f"{profile.get('username','')}` again.\n"
        )
        raise SystemExit(2)
    return Session(
        url=profile.get("url", ""),
        token=profile["token"],
        username=profile.get("username", ""),
        expires_at=profile.get("expires_at", ""),
    )


def api_for(sess: Session) -> Api:
    return Api(base=sess.url, token=sess.token)
