"""`labtris ssh-keys` — register/list/remove SSH pubkeys for the SSH proxy."""

from __future__ import annotations

from pathlib import Path

import typer

from labtris_client import ApiError
from labtris_cli.auth import api_for, require_session
from labtris_cli.format import console, error, format_option, print_object, print_table

app = typer.Typer(
    help="SSH pubkeys the labtris SSH proxy (port 2222) will accept.",
    no_args_is_help=True,
)


@app.command("add")
def cmd_add(
    key_path: Path = typer.Argument(
        ...,
        help="Path to an OpenSSH-format pubkey (usually ~/.ssh/id_ed25519.pub).",
        exists=True, readable=True, dir_okay=False,
    ),
    name: str = typer.Option(
        None, "--name", "-n",
        help="Friendly label. Defaults to the comment in the key body, then to the filename stem.",
    ),
    fmt: str = format_option(),
) -> None:
    """Register a pubkey against your Labtris user.

    Once registered, `ssh -p 2222 <node>@<labtris-host>` picks the matching
    private key from your agent and signs in without a password."""
    body = key_path.read_text().strip()
    if not body:
        error("that file is empty")
        raise typer.Exit(1)
    # Derive a name from the comment portion of the key body if the user
    # didn't pass --name — every ssh-keygen output ends with the comment,
    # which is usually the most human-readable label available.
    parts = body.split()
    label = name or (parts[-1] if len(parts) > 2 else key_path.stem)
    api = api_for(require_session())
    try:
        payload = api.post("/api/v1/auth/ssh-keys", {"name": label, "body": body})
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_object(fmt, payload)


@app.command("list")
def cmd_list(fmt: str = format_option()) -> None:
    """List every pubkey registered under your account."""
    api = api_for(require_session())
    try:
        payload = api.get("/api/v1/auth/ssh-keys") or {}
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    rows = payload.get("keys", [])
    print_table(
        fmt,
        rows,
        [
            ("ID", "id"),
            ("Name", "name"),
            ("Algorithm", "algorithm"),
            ("Fingerprint", "fingerprint"),
            ("Last used", "last_used_at"),
        ],
        empty_message="no keys yet — labtris ssh-keys add ~/.ssh/id_ed25519.pub",
    )


@app.command("rm")
def cmd_rm(key_id: str) -> None:
    """Remove a pubkey by id (find the id in `labtris ssh-keys list`)."""
    api = api_for(require_session())
    try:
        api.delete(f"/api/v1/auth/ssh-keys/{key_id}")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    console().print(f"removed {key_id}")
