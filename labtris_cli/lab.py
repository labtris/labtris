"""`labtris lab *` — read + basic operations against labs."""

from __future__ import annotations

from pathlib import Path

import typer

from labtris_client import ApiError
from labtris_cli.auth import api_for, require_session
from labtris_cli.format import console, error, format_option, print_object, print_table

app = typer.Typer(help="Labs — the top-level topology unit.", no_args_is_help=True)

hooks_app = typer.Typer(
    help="Ready-hooks — YAML-defined checks that run when the lab is up.",
    no_args_is_help=True,
)
app.add_typer(hooks_app, name="hooks")


@app.command("list")
def cmd_list(fmt: str = format_option()) -> None:
    """List every lab this account can see."""
    api = api_for(require_session())
    try:
        labs = api.get("/api/v1/labs")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    # `/labs` returns `LabListItem` — `nodes` is a count, `running` is how many
    # of them are up right now. Owner surfaces on a shared instance; folder
    # helps in a workspace with many labs.
    rows = [
        {
            "id": lab.get("id"),
            "name": lab.get("name"),
            "nodes": lab.get("nodes", 0),
            "running": lab.get("running", 0),
            "folder": lab.get("folder", ""),
            "owner": lab.get("owner_name") or lab.get("owner") or "",
            "locked": "yes" if lab.get("locked") else "",
        }
        for lab in (labs or [])
    ]
    print_table(
        fmt,
        rows,
        [
            ("ID", "id"),
            ("Name", "name"),
            ("Nodes", "nodes"),
            ("Running", "running"),
            ("Folder", "folder"),
            ("Owner", "owner"),
            ("Locked", "locked"),
        ],
        empty_message="no labs yet — create one in the UI",
    )


@app.command("show")
def cmd_show(lab_id: str, fmt: str = format_option()) -> None:
    """Show one lab in full — nodes, networks, links, hooks."""
    api = api_for(require_session())
    try:
        detail = api.get(f"/api/v1/labs/{lab_id}")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_object(fmt, detail)


@hooks_app.command("show")
def hooks_show(lab_id: str, fmt: str = format_option()) -> None:
    """Show the hooks block + last-run state for a lab."""
    api = api_for(require_session())
    try:
        payload = api.get(f"/api/v1/labs/{lab_id}/hooks")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_object(fmt, payload)


@hooks_app.command("apply")
def hooks_apply(lab_id: str, path: Path, fmt: str = format_option()) -> None:
    """Upload a hooks.yml — replaces the lab's hooks block."""
    try:
        source = path.read_text()
    except OSError as exc:
        error(f"cannot read {path}: {exc}")
        raise typer.Exit(1) from None
    api = api_for(require_session())
    try:
        payload = api.call("PUT", f"/api/v1/labs/{lab_id}/hooks", {"source": source})
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_object(fmt, payload)


@hooks_app.command("run")
def hooks_run(lab_id: str, fmt: str = format_option()) -> None:
    """Force a hooks run now, ignoring the ready_when trigger."""
    api = api_for(require_session())
    try:
        payload = api.post(f"/api/v1/labs/{lab_id}/hooks/run")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_object(fmt, payload)


@hooks_app.command("clear")
def hooks_clear(lab_id: str) -> None:
    """Drop the lab's hooks block and state history."""
    api = api_for(require_session())
    try:
        api.delete(f"/api/v1/labs/{lab_id}/hooks")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    console().print(f"cleared hooks on {lab_id}")
