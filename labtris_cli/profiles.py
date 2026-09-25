"""Named-profile store for the CLI (Phase K2 — aws-cli-shape).

`aws` gets its "which endpoint / which credentials" from a named profile:
`~/.aws/credentials` + `~/.aws/config`, keyed by `--profile <name>` or
`AWS_PROFILE`. This is that for Labtris.

Storage — one file, JSON, at `~/.config/labtris/profiles.json`:
    { "current": "home",
      "profiles": {
          "home":  {"url": "http://home:8080",  "token": "...",
                    "username": "raj", "expires_at": "..."},
          "lab":   {"url": "http://10.0.0.5",   "token": "...", ...},
          "prod":  {"url": "https://labtris.corp", "token": "...", ...}
      } }

The old flat `~/.config/labtris/auth.json` from before K2 is imported into
the `default` slot on first access so nobody loses a session on upgrade.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "labtris"
_PROFILES_PATH = _CONFIG_DIR / "profiles.json"
#: Legacy single-session file kept working for one upgrade so scripts that
#: `cat ~/.config/labtris/auth.json | jq .token` don't break the day K2
#: ships. Migrated on first profile-store read.
_LEGACY_AUTH_PATH = _CONFIG_DIR / "auth.json"

DEFAULT_PROFILE = "default"


def _load_raw() -> dict[str, Any]:
    if _PROFILES_PATH.exists():
        try:
            return json.loads(_PROFILES_PATH.read_text())
        except (OSError, ValueError):
            return {"current": DEFAULT_PROFILE, "profiles": {}}
    # First read after upgrade: import the legacy single-file session as
    # the `default` profile so no one is signed out by the upgrade.
    if _LEGACY_AUTH_PATH.exists():
        try:
            legacy = json.loads(_LEGACY_AUTH_PATH.read_text())
        except (OSError, ValueError):
            return {"current": DEFAULT_PROFILE, "profiles": {}}
        return {
            "current": DEFAULT_PROFILE,
            "profiles": {
                DEFAULT_PROFILE: {
                    "url": legacy.get("url", ""),
                    "token": legacy.get("token", ""),
                    "username": legacy.get("username", ""),
                    "expires_at": legacy.get("expires_at", ""),
                }
            },
        }
    return {"current": DEFAULT_PROFILE, "profiles": {}}


def _write_raw(data: dict[str, Any]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PROFILES_PATH.write_text(json.dumps(data, indent=2))
    # 0600/0700: this file holds N JWTs — one bearer credential per profile.
    _PROFILES_PATH.chmod(0o600)
    _CONFIG_DIR.chmod(0o700)


def profile_name() -> str:
    """Return the profile name for this invocation.

    Precedence — most explicit wins:
        1. `--profile <name>` (set by the root Typer callback into
           LABTRIS_PROFILE at parse time)
        2. `LABTRIS_PROFILE=<name>` in the environment
        3. `current` field of profiles.json
        4. Literal `default`
    """
    env = os.environ.get("LABTRIS_PROFILE")
    if env:
        return env
    return _load_raw().get("current") or DEFAULT_PROFILE


def get_profile(name: str | None = None) -> dict[str, Any] | None:
    """Return the profile dict, or None. `None` for name means "current"."""
    resolved = name or profile_name()
    return _load_raw().get("profiles", {}).get(resolved)


def list_profiles() -> list[str]:
    data = _load_raw()
    return sorted(data.get("profiles", {}).keys())


def save_profile(name: str, url: str, token: str, username: str, expires_at: str) -> None:
    data = _load_raw()
    data.setdefault("profiles", {})[name] = {
        "url": url.rstrip("/"),
        "token": token,
        "username": username,
        "expires_at": expires_at,
    }
    # First save doubles as "set as current" — matches aws-cli's default
    # behaviour when only one profile exists.
    if not data.get("current"):
        data["current"] = name
    _write_raw(data)


def set_current(name: str) -> None:
    data = _load_raw()
    if name not in data.get("profiles", {}):
        raise KeyError(name)
    data["current"] = name
    _write_raw(data)


def delete_profile(name: str) -> None:
    data = _load_raw()
    data.get("profiles", {}).pop(name, None)
    if data.get("current") == name:
        remaining = list(data.get("profiles", {}).keys())
        data["current"] = remaining[0] if remaining else DEFAULT_PROFILE
    _write_raw(data)


def profile_is_expired(profile: dict[str, Any]) -> bool:
    """True if `expires_at` (ISO 8601) is in the past. Missing/malformed
    date counts as expired — no session on that profile."""
    raw = profile.get("expires_at") or ""
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return when <= datetime.now(UTC)
