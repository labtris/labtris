"""`labtris node *` — list, inspect, and drive individual nodes."""

from __future__ import annotations

from pathlib import Path

import typer

from labtris_client import ApiError
from labtris_cli.auth import api_for, require_session
from labtris_cli.format import console, error, format_option, print_object, print_table

app = typer.Typer(help="Nodes — the routers, switches, VMs, and containers.", no_args_is_help=True)

p4_app = typer.Typer(
    help="P4 program on a bmv2 switch node (Phase E1).",
    no_args_is_help=True,
)
app.add_typer(p4_app, name="p4")


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


@app.command("start")
def cmd_start(node_id: str) -> None:
    """Start a single node by id."""
    api = api_for(require_session())
    try:
        api.post(f"/api/v1/nodes/{node_id}/start")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None


@app.command("stop")
def cmd_stop(
    node_id: str,
    mode: str = typer.Option("graceful", "--mode", help="graceful | force"),
) -> None:
    """Stop a single node by id."""
    api = api_for(require_session())
    try:
        api.post(f"/api/v1/nodes/{node_id}/stop", {"mode": mode})
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None


@app.command("wipe")
def cmd_wipe(node_id: str) -> None:
    """Wipe a single node's disk state — starts from scratch next time."""
    api = api_for(require_session())
    try:
        api.post(f"/api/v1/nodes/{node_id}/wipe")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None


@p4_app.command("show")
def p4_show(node_id: str, fmt: str = format_option()) -> None:
    """Show the P4 program currently mounted on a bmv2 node."""
    api = api_for(require_session())
    try:
        payload = api.get(f"/api/v1/nodes/{node_id}/p4")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_object(fmt, payload)


@p4_app.command("builtins")
def p4_builtins(fmt: str = format_option()) -> None:
    """List the curated P4 programs a bmv2 node can pick from."""
    api = api_for(require_session())
    try:
        rows = api.get("/api/v1/p4/builtins") or []
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_table(
        fmt,
        rows,
        [("Name", "name"), ("Description", "description"), ("Bytes", "bytes")],
        empty_message="no built-ins packaged — reinstall labtris",
    )


@p4_app.command("set")
def p4_set(
    node_id: str,
    builtin: str = typer.Option(..., "--builtin", "-b", help="Built-in name (basic_switch|ecmp|ecn|trim)."),
    fmt: str = format_option(),
) -> None:
    """Point a bmv2 node at one of the curated built-in P4 programs."""
    api = api_for(require_session())
    try:
        payload = api.call("PUT", f"/api/v1/nodes/{node_id}/p4", {"builtin": builtin})
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    print_object(fmt, payload)


@p4_app.command("upload")
def p4_upload(node_id: str, path: Path, fmt: str = format_option()) -> None:
    """Upload a custom .p4 file. Replaces whatever program was there."""
    if not path.exists():
        error(f"{path} does not exist")
        raise typer.Exit(1)
    sess = require_session()
    # Multipart upload — Api's `call` doesn't do multipart, and adding
    # it there would drag in requests. Do it with urllib manually.
    import mimetypes
    import urllib.error
    import urllib.request
    import uuid

    boundary = f"----labtris{uuid.uuid4().hex}"
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
    ).encode()
    ct = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    body += f"Content-Type: {ct}\r\n\r\n".encode()
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{sess.url}/api/v1/nodes/{node_id}/p4",
        data=bytes(body),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {sess.token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            import json

            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        error(f"upload failed: HTTP {exc.code}: {raw[:200]}")
        raise typer.Exit(1) from None
    print_object(fmt, payload)


@p4_app.command("clear")
def p4_clear(node_id: str) -> None:
    """Drop the mounted P4 program (falls back to default on next start)."""
    api = api_for(require_session())
    try:
        api.delete(f"/api/v1/nodes/{node_id}/p4")
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    console().print(f"cleared P4 program on {node_id}")
