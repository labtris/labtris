"""`labtris pod *` — inspect and manage local pods on the server."""

from __future__ import annotations

import typer

from labtris_client import ApiError
from labtris_cli.auth import api_for, require_session
from labtris_cli.format import console, error, format_option, print_object, print_table

app = typer.Typer(help="Portable lab snapshots (pods) on the server host.", no_args_is_help=True)


@app.command("list")
def cmd_list(fmt: str = format_option()) -> None:
    """List every pod archive in the server's pod_dir."""
    api = api_for(require_session())
    try:
        rows = api.get("/api/v1/pods") or []
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_table(
        fmt,
        rows,
        [
            ("Pod ID", "pod_id"),
            ("Lab", "lab_name"),
            ("Mode", "mode"),
            ("Nodes", "node_count"),
            ("Bytes", "bytes"),
            ("Created", "created_at"),
        ],
        empty_message="no pods — save one with `labtris lab snapshot <id>`",
    )


@app.command("inspect")
def cmd_inspect(pod_id: str, fmt: str = format_option()) -> None:
    """Show a pod's summary metadata as reported by /api/v1/pods."""
    api = api_for(require_session())
    try:
        rows = api.get("/api/v1/pods") or []
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    match = next((p for p in rows if p.get("pod_id") == pod_id), None)
    if match is None:
        error(f"no pod {pod_id!r} — run `labtris pod list` to see what's local")
        raise typer.Exit(1)
    print_object(fmt, match)


@app.command("rm")
def cmd_rm(pod_id: str) -> None:
    """Delete a pod archive from the server host."""
    api = api_for(require_session())
    try:
        api.delete(f"/api/v1/pods/{pod_id}")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    console().print(f"removed pod {pod_id}")
