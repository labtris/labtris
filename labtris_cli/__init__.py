"""The `labtris` command-line tool.

A Typer app that talks to a Labtris instance over HTTP through
`labtris_client.Api`. Everything the CLI shows is what the same instance
would render in the web UI — this is a second surface on the same API,
not a parallel implementation.

Session credentials live in `~/.config/labtris/auth.json` and are minted
by the `/auth/login` endpoint (which now returns the token in its body
for non-browser callers). See `labtris_cli.auth` for the persisted shape.

Entry point: `labtris = labtris_cli.__main__:app` in pyproject.toml."""

__all__: list[str] = []
