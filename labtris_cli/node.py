"""`labtris node *` — list, inspect, and drive individual nodes."""

from __future__ import annotations

import typer

from labtris_client import ApiError
from labtris_cli.auth import api_for, require_session
from labtris_cli.format import error, format_option, print_table

app = typer.Typer(help="Nodes — the routers, switches, VMs, and containers.", no_args_is_help=True)


@app.command("list")
def cmd_list(
    lab: str | None = typer.Option(None, "--lab", help="Restrict to one lab id."),
    fmt: str = format_option(),
) -> None:
    """List nodes across every lab, or a single lab with --lab."""
    api = api_for(require_session())
    try:
        if lab:
            labs = [api.get(f"/api/v1/labs/{lab}")]
        else:
            labs = [api.get(f"/api/v1/labs/{s['id']}") for s in (api.get("/api/v1/labs") or [])]
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    rows = []
    for detail in labs:
        lab_name = detail.get("name", "")
        for n in detail.get("nodes", []) or []:
            rows.append(
                {
                    "id": n.get("id"),
                    "name": n.get("name"),
                    "runtime": n.get("runtime"),
                    "image": n.get("image", ""),
                    "state": n.get("state"),
                    "lab": lab_name,
                }
            )
    print_table(
        fmt,
        rows,
        [
            ("ID", "id"),
            ("Name", "name"),
            ("Runtime", "runtime"),
            ("Image", "image"),
            ("State", "state"),
            ("Lab", "lab"),
        ],
        empty_message="no nodes — draw one in the UI or import a topology",
    )
