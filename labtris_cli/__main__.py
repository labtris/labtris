"""`labtris` — the command-line surface for a Labtris instance.

The subcommand tree matches the shape the plan approved:

    labtris login  --url URL --user USER
    labtris logout
    labtris lab   {list,show}                 # more verbs land in Phase B/C/D
    labtris node  {list}
    labtris system {status,doctor}

The global `--format` option toggles between Rich tables (human) and raw
JSON (scripts). Every non-login subcommand goes through
`labtris_cli.auth.require_session()`, which reads
`~/.config/labtris/auth.json` and exits with a "run labtris login" hint
when it isn't there. LABTRIS_TOKEN + LABTRIS_API_URL in the environment
override the file — the service-account shape for CI."""

from __future__ import annotations

import getpass

import typer

from labtris_client import ApiError
from labtris_cli import lab as lab_cmd
from labtris_cli import node as node_cmd
from labtris_cli import system as system_cmd
from labtris_cli.auth import load_session, login as do_login, logout as do_logout
from labtris_cli.format import console, error

app = typer.Typer(
    help="Labtris — the self-hosted network emulation platform.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(lab_cmd.app, name="lab")
app.add_typer(node_cmd.app, name="node")
app.add_typer(system_cmd.app, name="system")


@app.command("login")
def cmd_login(
    url: str = typer.Option(..., "--url", help="Labtris API base URL (e.g. http://host:8081)."),
    user: str = typer.Option(..., "--user", "-u", help="Username."),
    password: str | None = typer.Option(
        None,
        "--password",
        help="Password. If omitted, prompts on the terminal (recommended so it "
        "does not land in shell history).",
    ),
) -> None:
    """Sign in and save the token to ~/.config/labtris/auth.json.

    The token is a 7-day JWT — the same one the browser uses. It is stored
    in the caller's home directory with mode 0600; nothing on the server is
    kept about the session beyond what /auth/login already logs."""
    if password is None:
        password = getpass.getpass(f"password for {user}: ")
    try:
        sess = do_login(url, user, password)
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    console().print(f"signed in as [bold]{sess.username}[/bold] at {sess.url}")


@app.command("logout")
def cmd_logout() -> None:
    """Drop the local session file and tell the server to clear the cookie."""
    do_logout(load_session())
    console().print("signed out")


if __name__ == "__main__":  # pragma: no cover
    app()
