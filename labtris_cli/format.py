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
import os
import sys
from typing import Any, Iterable

import typer
from rich.console import Console
from rich.table import Table

from labtris_cli.query import apply_query


def format_option() -> Any:
    """Reusable `--format`/`-o` option for every command that emits data.

    Kept as a factory (returning a fresh `typer.Option` each call) because
    Typer/Click stores per-command metadata on the object; sharing one
    across commands would bleed help text between them.

    'text' mode (K2) is TSV-shaped output for pipelines that don't want
    JSON — `labtris nodes list -o text | awk '{print $2}'` gives you node
    names without a jq dependency."""
    default = os.environ.get("LABTRIS_OUTPUT") or "table"
    return typer.Option(
        default,
        "--format",
        "--output",
        "-o",
        help="Output: 'table' (default), 'json' (scripts), 'text' (tsv), 'yaml'.",
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


def _emit(fmt: str, data: Any, *, columns: list[tuple[str, str]] | None = None,
          empty_message: str = "(nothing to show)") -> None:
    """Format-and-print worker shared by `print_table` and `print_object`.

    Applies the `--query` filter first (LABTRIS_QUERY env var, or whatever
    the root Typer callback stashed there) so `--query` reshapes the raw
    payload BEFORE the formatter renders it. Reshaping in the formatter
    means the same query works regardless of whether the caller went
    through print_table or print_object.

    Root-level `--output` wins over the per-command `-o` default. Typer
    reads options positionally, so `labtris --output json lab list` and
    `labtris lab list -o json` are two different code paths; the env var
    the root callback stashes bridges them here.
    """
    override = os.environ.get("LABTRIS_OUTPUT")
    if override:
        fmt = override
    filtered = apply_query(data)

    if fmt == "json":
        json.dump(filtered, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
        return
    if fmt == "yaml":
        import yaml

        yaml.safe_dump(filtered, sys.stdout, sort_keys=False)
        return
    if fmt == "text":
        _emit_text(filtered, columns)
        return

    # Fallback (table)
    if isinstance(filtered, list):
        if not filtered:
            console().print(f"[dim]{empty_message}[/dim]")
            return
        cols = columns or _autodetect_columns(filtered)
        table = Table(show_header=True, header_style="bold")
        for header, _ in cols:
            table.add_column(header)
        for row in filtered:
            if isinstance(row, dict):
                table.add_row(*(_stringify(row.get(key, "")) for _, key in cols))
            else:
                table.add_row(_stringify(row))
        console().print(table)
    elif isinstance(filtered, dict):
        table = Table(show_header=False)
        table.add_column("key", style="cyan")
        table.add_column("value")
        for k, v in filtered.items():
            table.add_row(str(k), _stringify(v))
        console().print(table)
    else:
        console().print(str(filtered))


def _autodetect_columns(rows: list[Any]) -> list[tuple[str, str]]:
    """When a `--query` reshape produced dicts we didn't pre-declare columns
    for, fall back to the first row's keys. Table output is human-facing so
    stable-ish column ordering matters — use insertion order."""
    first = next((r for r in rows if isinstance(r, dict)), None)
    if not first:
        return [("value", "")]
    return [(k, k) for k in first.keys()]


def _stringify(value: Any) -> str:
    """Nested dicts/lists → indented JSON so they stay legible in a table
    cell. Everything else stringifies with str()."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    return str(value)


def _emit_text(data: Any, columns: list[tuple[str, str]] | None) -> None:
    """TSV. Header row + one line per record. Scalars go on their own line.

    Matches `aws --output text` well enough for `awk '{print $2}'` style
    pipelines. Every field is stripped of newlines/tabs so a single row
    never spans multiple lines."""
    if isinstance(data, list):
        if not data:
            return
        if all(isinstance(r, dict) for r in data):
            cols = columns or _autodetect_columns(data)
            sys.stdout.write("\t".join(h for h, _ in cols) + "\n")
            for row in data:
                sys.stdout.write(
                    "\t".join(_flat_str(row.get(k, "")) for _, k in cols) + "\n"
                )
        else:
            for item in data:
                sys.stdout.write(_flat_str(item) + "\n")
    elif isinstance(data, dict):
        for k, v in data.items():
            sys.stdout.write(f"{k}\t{_flat_str(v)}\n")
    else:
        sys.stdout.write(_flat_str(data) + "\n")


def _flat_str(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str)
    text = str(value)
    return text.replace("\t", " ").replace("\n", " ")


def print_table(
    fmt: str,
    rows: Iterable[dict[str, Any]],
    columns: list[tuple[str, str]],
    *,
    empty_message: str = "(nothing to show)",
) -> None:
    """List-of-dicts renderer. See `_emit` for details."""
    _emit(fmt, list(rows), columns=columns, empty_message=empty_message)


def print_object(fmt: str, obj: Any) -> None:
    """Single-object renderer. See `_emit` for details."""
    _emit(fmt, obj)


def error(msg: str) -> None:
    console().print(f"[red]error:[/red] {msg}")


def warn(msg: str) -> None:
    console().print(f"[yellow]warn:[/yellow] {msg}")
