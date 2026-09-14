"""MCP server for labtris.

Lets an external coding agent — Claude Code, Cursor, anything that speaks the
Model Context Protocol — build and inspect labs through the same REST API the
browser uses.

Deliberately dependency-free. The official SDK wants pydantic >= 2.12 and a
starlette this project does not pin to, and dragging the API's own dependency
set forward to gain a JSON-RPC loop is a bad trade: MCP's stdio transport is
newline-delimited JSON-RPC 2.0, which the standard library already covers.
"""
