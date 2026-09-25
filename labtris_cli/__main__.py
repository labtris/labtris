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
import os

import typer

from labtris_client import ApiError
from labtris_cli import lab as lab_cmd
from labtris_cli import node as node_cmd
from labtris_cli import pod as pod_cmd
from labtris_cli import ssh_keys as ssh_keys_cmd
from labtris_cli import system as system_cmd
from labtris_cli.auth import load_session, login as do_login, logout as do_logout
from labtris_cli.format import console, error, print_table
from labtris_cli.profiles import (
    delete_profile,
    get_profile,
    list_profiles,
    profile_name,
    save_profile,
    set_current,
)

app = typer.Typer(
    help="Labtris — the self-hosted network emulation platform.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _root(
    profile: str | None = typer.Option(
        None, "--profile", "-p",
        help="Named profile to use (see `labtris configure`, `labtris profiles`).",
    ),
    output: str | None = typer.Option(
        None, "--output",
        help="Global output format: 'table', 'json', 'text', 'yaml'. Overrides per-command -o.",
    ),
    query: str | None = typer.Option(
        None, "--query",
        help="JMESPath expression applied to the response before formatting (aws-cli-shape).",
    ),
) -> None:
    """Root callback — stashes global flags into env vars so every
    downstream command reads the same profile / query / output setting
    without needing to thread them through every function signature.

    This matches how the aws CLI wires `--profile` / `--output` / `--query`
    at the root: users set them once, everything under it inherits.
    """
    if profile:
        os.environ["LABTRIS_PROFILE"] = profile
    if output:
        os.environ["LABTRIS_OUTPUT"] = output
    if query:
        os.environ["LABTRIS_QUERY"] = query


app.add_typer(lab_cmd.app, name="lab")
app.add_typer(node_cmd.app, name="node")
app.add_typer(system_cmd.app, name="system")
app.add_typer(pod_cmd.app, name="pod")
app.add_typer(ssh_keys_cmd.app, name="ssh-keys")

profiles_app = typer.Typer(
    help="Named endpoint + credential profiles (aws-cli-shape).",
    no_args_is_help=True,
)
app.add_typer(profiles_app, name="profiles")


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
    """Sign in and save the token to the current profile.

    The token is a 7-day JWT — the same one the browser uses. It is
    stored in `~/.config/labtris/profiles.json` under the current profile
    (default: `default`) with 0600 permissions; a mirror is kept at
    `~/.config/labtris/auth.json` for pre-K2 scripts. Nothing on the
    server is kept about the session beyond what /auth/login already logs.
    """
    if password is None:
        password = getpass.getpass(f"password for {user}: ")
    try:
        sess = do_login(url, user, password)
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    console().print(
        f"signed in as [bold]{sess.username}[/bold] at {sess.url} "
        f"(profile: {profile_name()})"
    )


@app.command("logout")
def cmd_logout() -> None:
    """Drop the current profile's stored session and best-effort tell the
    server. Other profiles are untouched."""
    do_logout(load_session())
    console().print("signed out")


@app.command("configure")
def cmd_configure(
    url: str | None = typer.Option(None, "--url", help="Labtris API base URL."),
    user: str | None = typer.Option(None, "--user", "-u", help="Username."),
    password: str | None = typer.Option(
        None, "--password", help="Password (prompts if omitted)."
    ),
    profile: str | None = typer.Option(
        None, "--profile", "-p",
        help="Profile name to configure (default: current profile).",
    ),
) -> None:
    """Set up a named profile — URL + credentials — like `aws configure`.

    Interactive if any of --url / --user / --password is omitted. Signs in
    to verify the credentials, then persists the token under `profile`
    (defaults to whatever is current, or `default` on a fresh box). Sets
    the new profile as current when it's the first one on this machine.
    """
    target = profile or profile_name()
    if url is None:
        current = get_profile(target) or {}
        default_url = current.get("url", "http://127.0.0.1:8080")
        url = typer.prompt(f"URL for profile {target!r}", default=default_url)
    if user is None:
        current = get_profile(target) or {}
        default_user = current.get("username", "")
        user = typer.prompt(f"Username for profile {target!r}", default=default_user)
    if password is None:
        password = getpass.getpass(f"Password for {user}@{url}: ")
    try:
        sess = do_login(url, user, password, profile=target)
    except ApiError as exc:
        error(exc.message)
        raise typer.Exit(1) from None
    console().print(
        f"profile [bold]{target}[/bold] configured for "
        f"[bold]{sess.username}[/bold] at {sess.url}"
    )


@profiles_app.command("list")
def profiles_list() -> None:
    """Show every configured profile + which one is current."""
    current = profile_name()
    rows = []
    for name in list_profiles():
        p = get_profile(name) or {}
        rows.append(
            {
                "name": name + (" *" if name == current else ""),
                "url": p.get("url", ""),
                "username": p.get("username", ""),
                "expires_at": p.get("expires_at", ""),
            }
        )
    print_table(
        os.environ.get("LABTRIS_OUTPUT", "table"),
        rows,
        [("Name", "name"), ("URL", "url"), ("User", "username"), ("Expires", "expires_at")],
        empty_message="no profiles yet — labtris configure",
    )


@profiles_app.command("use")
def profiles_use(name: str) -> None:
    """Switch which profile is current (the one commands default to)."""
    try:
        set_current(name)
    except KeyError:
        error(f"no such profile {name!r}")
        raise typer.Exit(1) from None
    console().print(f"current profile: [bold]{name}[/bold]")


@profiles_app.command("rm")
def profiles_rm(name: str) -> None:
    """Remove a profile from ~/.config/labtris/profiles.json."""
    delete_profile(name)
    console().print(f"removed profile {name!r}")


def labtrislocal() -> None:
    """Entry point for the `labtrislocal` shim.

    LocalStack ships `awslocal` as a two-line wrapper that forces
    `--endpoint-url=http://localhost:4566` so users don't have to type
    it every time. Here we do the same by forcing `--profile local`:
    the user runs `labtris configure --profile local` once, and then
    every `labtrislocal ...` invocation targets that profile. Falls
    through to the normal `labtris` app so all subcommands work
    unchanged."""
    import sys

    argv = sys.argv[:]
    # If the caller already passed --profile explicitly, don't second-
    # guess them. Otherwise inject `--profile local` at position 1.
    if not any(a in ("--profile", "-p") for a in argv[1:]):
        argv[1:1] = ["--profile", "local"]
    sys.argv = argv
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
