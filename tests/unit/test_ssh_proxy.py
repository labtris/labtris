"""Unit tests for the SSH proxy — module-level plumbing only.

A full end-to-end asyncssh client-against-server test needs a running
DB, a SerialSession, and a live QEMU pump. That belongs in an acceptance
test on the real API, not here. What this file pins is the smaller stuff
that has to be right BEFORE a client connects: the disabled default, the
status shape, and the username → (lab_slug, node_name) split. Those are
the parts that most easily rot without noticing."""

from __future__ import annotations

import pytest


def test_default_disabled_ships_disabled():
    """Off by default. If someone flips this default, they should have to
    edit this test — the "server listens on port 2222 out of the box" is a
    security-relevant claim we do not want to make quietly."""
    from labtris_api.config import Settings

    assert Settings().ssh_proxy_enabled is False


def test_status_shape():
    """/system/status reads this — the keys need to be stable so the CLI
    and any external monitor can rely on them."""
    from labtris_api import ssh_proxy

    s = ssh_proxy.status()
    assert set(s.keys()) == {"enabled", "port", "bind", "listening", "host_key_path"}
    assert isinstance(s["port"], int)
    assert isinstance(s["listening"], bool)


def test_slug_matches_frontend_convention():
    """The `lab-slug:node-name` disambiguator only works if the lab-slug
    the SSH client types matches the slug this module derives from the
    stored lab name. Kept dependency-free (no `python-slugify`) so it
    matches even when the front-end does its own kebab-case."""
    from labtris_api.ssh_proxy import _slug

    assert _slug("GPU Lab #1") == "gpu-lab-1"
    assert _slug("pvlan-test") == "pvlan-test"
    assert _slug("Lab (staging)") == "lab-staging"
    assert _slug("---") == ""


@pytest.mark.asyncio
async def test_verify_jwt_rejects_garbage():
    """Malformed / expired / rotated-key JWTs all reach `read_token` as
    None and must return None here. If this ever raises, the SSH proxy
    would 500-equivalent on a probe attempt instead of denying."""
    from labtris_api.ssh_proxy import _verify_jwt

    assert await _verify_jwt("") is None
    assert await _verify_jwt("not.a.jwt") is None
    # A syntactically valid header/payload but signed with a random secret
    # will fail signature verification via read_token.
    fake = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiJub25lIn0."
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert await _verify_jwt(fake) is None
