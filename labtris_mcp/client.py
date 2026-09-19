"""Backward-compat re-export.

The client used to live here alone. `labtris_cli` needed the same code, so
it moved to `labtris_client/`. Every existing importer of `labtris_mcp.client`
still works because this module re-exports the same names — nothing had
to change in `labtris_mcp/tools.py`, `labtris_api/agent.py`, or
`labtris_api/routers/ai.py`, all of which had already reached for
`from labtris_mcp.client import Api, ApiError`."""

from labtris_client import Api, ApiError

__all__ = ["Api", "ApiError"]
