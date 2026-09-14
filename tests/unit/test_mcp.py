from __future__ import annotations

import io
import json
from typing import Any

from labtris_mcp.client import ApiError
from labtris_mcp.server import handle, serve
from labtris_mcp.tools import TOOLS


class FakeApi:
    """Records calls instead of making them."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.replies = replies or {}

    def call(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        return self.replies.get(path, {"ok": path})

    def get(self, p: str) -> Any:
        return self.call("GET", p)

    def post(self, p: str, b: Any = None) -> Any:
        return self.call("POST", p, b if b is not None else {})

    def patch(self, p: str, b: Any) -> Any:
        return self.call("PATCH", p, b)

    def delete(self, p: str) -> Any:
        return self.call("DELETE", p)


def _call(name: str, args: dict[str, Any], api: Any) -> dict[str, Any]:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    return handle(msg, api)  # type: ignore[arg-type,return-value]


def test_initialize_advertises_tools() -> None:
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, FakeApi())
    assert reply["result"]["protocolVersion"]
    assert "tools" in reply["result"]["capabilities"]
    assert reply["result"]["serverInfo"]["name"] == "labtris"


def test_every_tool_is_listed_with_a_schema() -> None:
    """tools/list is generated from the same table tools/call dispatches on, so
    a tool cannot be advertised without a handler or vice versa."""
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, FakeApi())
    tools = listed["result"]["tools"]
    assert {t["name"] for t in tools} == set(TOOLS)
    for t in tools:
        assert t["description"], f"{t['name']} has no description for the model to read"
        assert t["inputSchema"]["type"] == "object"
        for req in t["inputSchema"]["required"]:
            assert req in t["inputSchema"]["properties"], f"{t['name']} requires an unlisted arg"


def test_notifications_are_never_answered() -> None:
    """A JSON-RPC notification has no id; replying to one corrupts the stream."""
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, FakeApi()) is None


def test_unknown_method_is_a_protocol_error() -> None:
    reply = handle({"jsonrpc": "2.0", "id": 7, "method": "nope"}, FakeApi())
    assert reply["error"]["code"] == -32601


def test_api_failures_come_back_as_tool_errors_not_dead_sessions() -> None:
    """A refusal — a NIC already bound, a node already starting — is something
    the model should read and work around, not an aborted conversation."""

    class Boom(FakeApi):
        def call(self, *a: Any, **k: Any) -> Any:
            raise ApiError(409, "host interface 'eth0' is already bound to network 'up1'")

    reply = _call("list_labs", {}, Boom())
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "already bound" in reply["result"]["content"][0]["text"]


def test_unknown_tool_is_reported_to_the_model() -> None:
    reply = _call("teleport", {}, FakeApi())
    assert reply["result"]["isError"] is True


def test_connect_nodes_reuses_free_interfaces_before_making_new_ones() -> None:
    lab = {
        "nodes": [
            {"id": "A", "interfaces": [{"id": "a0", "network_id": None}]},
            {"id": "B", "interfaces": [{"id": "b0", "network_id": None}]},
        ],
        "links": [],
    }
    api = FakeApi({"/api/v1/labs/L": lab})
    _call("connect_nodes", {"lab_id": "L", "a_node_id": "A", "b_node_id": "B"}, api)
    posted = [c for c in api.calls if c[0] == "POST"]
    assert not any("interfaces" in c[1] for c in posted), "should not add ports it did not need"
    assert posted[-1][2] == {"a_iface_id": "a0", "b_iface_id": "b0"}


def test_connect_nodes_adds_a_port_when_all_are_taken() -> None:
    lab = {
        "nodes": [
            {"id": "A", "interfaces": [{"id": "a0", "network_id": None}]},
            {"id": "B", "interfaces": [{"id": "b0", "network_id": None}]},
        ],
        "links": [{"a_iface_id": "a0", "b_iface_id": "b0"}],
    }
    api = FakeApi({"/api/v1/labs/L": lab, "/api/v1/nodes/A/interfaces": {"id": "a1"},
                   "/api/v1/nodes/B/interfaces": {"id": "b1"}})
    _call("connect_nodes", {"lab_id": "L", "a_node_id": "A", "b_node_id": "B"}, api)
    assert api.calls[-1][2] == {"a_iface_id": "a1", "b_iface_id": "b1"}


def test_impair_link_targets_one_direction_by_default() -> None:
    api = FakeApi()
    _call("impair_link", {"link_id": "L1", "delay_ms": 50}, api)
    assert api.calls[-1] == ("PATCH", "/api/v1/links/L1", {"impair_ab": {"delay_ms": 50}})

    api = FakeApi()
    _call("impair_link", {"link_id": "L1", "loss_pct": 2, "reverse": True}, api)
    assert api.calls[-1][2] == {"impair_ba": {"loss_pct": 2}}

    api = FakeApi()
    _call("impair_link", {"link_id": "L1", "delay_ms": 5, "both": True}, api)
    assert api.calls[-1][2] == {"impair_ab": {"delay_ms": 5}, "impair_ba": {"delay_ms": 5}}


def test_impair_link_with_nothing_set_clears_the_qdisc() -> None:
    api = FakeApi()
    _call("impair_link", {"link_id": "L1"}, api)
    assert api.calls[-1][2] == {"impair_ab": {}}


def test_console_exec_posts_to_the_right_endpoint() -> None:
    """The MCP tool is a thin wrapper — the API endpoint does the actual
    work — so the test is just "did we hit the URL and body the endpoint
    expects."""
    api = FakeApi({"/api/v1/nodes/N1/console/exec": {"stdout": "eth0", "runtime": "docker"}})
    _call("console_exec", {"node_id": "N1", "command": "ip -br addr"}, api)
    assert api.calls[-1] == ("POST", "/api/v1/nodes/N1/console/exec", {"command": "ip -br addr"})


def test_console_exec_passes_the_timeout_through_when_set() -> None:
    api = FakeApi()
    _call("console_exec", {"node_id": "N1", "command": "sleep 5", "timeout_s": 20}, api)
    assert api.calls[-1][2] == {"command": "sleep 5", "timeout_s": 20}


def test_stdio_loop_speaks_line_delimited_jsonrpc() -> None:
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    stdout = io.StringIO()
    serve(stdin, stdout, FakeApi())  # type: ignore[arg-type]
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2], "the notification must not have been answered"


def test_malformed_input_does_not_kill_the_server() -> None:
    stdout = io.StringIO()
    serve(io.StringIO("{not json\n"), stdout, FakeApi())  # type: ignore[arg-type]
    assert json.loads(stdout.getvalue())["error"]["code"] == -32700
