"""Zero-dep HTTP client for a Labtris API instance.

Kept intentionally small: urllib rather than httpx so this package can be
shipped and imported without pulling in the whole server-side dependency
set. Two consumers today:

- `labtris_mcp` — the assistant's MCP server calls Labtris here to run
  each tool the model invokes.
- `labtris_cli` — the `labtris` command-line tool.

Anything reaching for `LABTRIS_API_URL`, `LABTRIS_TOKEN`, or the JSON
envelope shape our routers return should import from this module."""

from labtris_client.api import Api, ApiError

__all__ = ["Api", "ApiError"]
