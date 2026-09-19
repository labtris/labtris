"""Output formatting: Rich tables for humans, raw JSON for scripts.

Every subcommand carries its own `--format` / `-o` option (see
`format_option`). Values: 'table' (default, human), 'json' (scripts —
`--format json | jq` is the whole point), 'yaml' (pods and hooks
specifically — human-readable representations of what is already YAML).

The option lives on each subcommand rather than the root because Click
consumes options positionally: pre-verb options belong to the root parser,
post-verb to the subcommand, and `kubectl get pods -o json` shape is what
users type. A duplicate on both would work but doubles the surface for no
gain."""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

import typer
from rich.console import Console
from rich.table import Table


def format_option() -> Any:
    """Reusable `--format`/`-o` option for every command that emits data.

    Kept as a factory (returning a fresh `typer.Option` each call) because
    Typer/Click stores per-command metadata on the object; sharing one
    across commands would bleed help text between them."""
    return typer.Option(
        "table",
        "--format",
        "-o",
        help="Output: 'table' (default), 'json' (scripts), 'yaml'.",
    )

_console: Console | None = None


def console() -> Console:
    global _console
    if _console is None:
        # Force terminal detection to be honest about non-TTY output — colored
        # ANSI in a pipe is the wrong default. Rich's own logic gets this right
        # already; instantiating the Console here just centralizes the choice.
        _console = Console(soft_wrap=True)
    return _console


def print_table(
    fmt: str,
    rows: Iterable[dict[str, Any]],
    columns: list[tuple[str, str]],
    *,
    empty_message: str = "(nothing to show)",
) -> None:
    """Render `rows` as a table for humans, raw JSON otherwise.

    `columns` is a list of (header, key) pairs — the header goes on the
    table, the key is looked up in each row dict. Missing keys render as
    the empty string. `rows` is materialised for the `--format json` path,
    so upstream generators are fine."""
    rows_list = list(rows)
    if fmt == "json":
        json.dump(rows_list, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
        return
    if fmt == "yaml":
        import yaml

        yaml.safe_dump(rows_list, sys.stdout, sort_keys=False)
        return

    if not rows_list:
        console().print(f"[dim]{empty_message}[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    for header, _ in columns:
        table.add_column(header)
    for row in rows_list:
        table.add_row(*(str(row.get(key, "")) for _, key in columns))
    console().print(table)


def print_object(fmt: str, obj: Any) -> None:
    """One-off rendering for a single-object response (a `show` verb)."""
    if fmt == "json":
        json.dump(obj, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
        return
    if fmt == "yaml":
        import yaml

        yaml.safe_dump(obj, sys.stdout, sort_keys=False)
        return
    # For table format on a single object, print key/value pairs as a two-column
    # table. Nested dicts get JSON-serialised — a `show` on a lab with a big
    # geometry blob should stay legible instead of flattening into columns
    # nobody would want to read.
    if isinstance(obj, dict):
        table = Table(show_header=False)
        table.add_column("key", style="cyan")
        table.add_column("value")
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                v_str = json.dumps(v, indent=2, sort_keys=True, default=str)
            else:
                v_str = str(v)
            table.add_row(str(k), v_str)
        console().print(table)
    else:
        console().print(str(obj))


def error(msg: str) -> None:
    console().print(f"[red]error:[/red] {msg}")


def warn(msg: str) -> None:
    console().print(f"[yellow]warn:[/yellow] {msg}")
