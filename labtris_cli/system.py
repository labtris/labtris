"""`labtris system *` — instance-level queries and admin verbs.

`labtris system status` is a network-facing summary — it hits the API's
`/health` endpoint and the diagnostics collector when reachable, and
prints one Rich panel with the highlights. `labtris system doctor` still
exists as the deep-dive command that requires local access (it reads /proc,
the DB, and docker) — it delegates to the existing labtris-doctor logic."""

from __future__ import annotations

import typer

from labtris_client import ApiError
from labtris_cli.auth import api_for, require_session
from labtris_cli.format import console, error, format_option, print_object

app = typer.Typer(help="Instance-level status and admin.", no_args_is_help=True)


@app.command("status")
def cmd_status(fmt: str = format_option()) -> None:
    """Show the API's health + version + rough usage."""
    sess = require_session()
    api = api_for(sess)
    try:
        health = api.get("/api/v1/health")
    except ApiError as exc:
        error(f"health check failed: {exc.message}")
        raise typer.Exit(1) from None
    # `/health` payload varies by version; be tolerant. Add the URL the
    # session is against so a `status` in a scripting context tells you
    # which instance the answer is for.
    payload = {"url": sess.url, "user": sess.username, **(health or {})}
    print_object(fmt, payload)


@app.command("doctor")
def cmd_doctor() -> None:
    """Run the local diagnostics collector (reads /proc, DB, docker).

    Requires local access to the labtris install — same as `labtris-doctor`
    on its own. Anything a remote CLI can check via HTTP goes through
    `labtris system status`; doctor is the deep-dive that needs to be on
    the machine."""
    try:
        from labtris_api.diagnose import main as doctor_main
    except ImportError:
        console().print(
            "[red]error:[/red] labtris-doctor requires the labtris_api package to be "
            "installed locally. Install labtris on this machine or SSH into the "
            "server and run `labtris system doctor` there."
        )
        raise typer.Exit(2) from None
    doctor_main()
