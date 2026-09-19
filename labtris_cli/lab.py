"""`labtris lab *` — read + basic operations against labs."""

from __future__ import annotations

import typer

from labtris_client import ApiError
from labtris_cli.auth import api_for, require_session
from labtris_cli.format import error, format_option, print_object, print_table

app = typer.Typer(help="Labs — the top-level topology unit.", no_args_is_help=True)


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
