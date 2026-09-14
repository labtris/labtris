from __future__ import annotations

from collections.abc import Callable
from typing import Any

from labtris_mcp.client import Api

#: name -> (description, json schema, handler). Kept as data so tools/list and
#: tools/call cannot drift apart.
TOOLS: dict[str, tuple[str, dict[str, Any], Handler]] = {}


Handler = Callable[[Api, dict[str, Any]], Any]


def tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        TOOLS[name] = (
            description,
            {"type": "object", "properties": properties, "required": required or []},
            fn,
        )
        return fn

    return register


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}


@tool(
    "list_catalog",
    "Images that can be placed in a lab: Docker images, and QEMU images with their "
    "RAM/vCPU floor, login, and whether they are already downloaded. A QEMU image that "
    "is not cached costs a multi-GB download on first start.",
    {},
)
def _catalog(api: Api, _: dict[str, Any]) -> Any:
    return api.get("/api/v1/catalog")


@tool("list_labs", "Every lab on this instance.", {})
def _labs(api: Api, _: dict[str, Any]) -> Any:
    return api.get("/api/v1/labs")


@tool(
    "get_lab",
    "One lab in full: its nodes with their interfaces and state, its networks, and its links.",
    {"lab_id": STR},
    ["lab_id"],
)
def _lab(api: Api, a: dict[str, Any]) -> Any:
    return api.get(f"/api/v1/labs/{a['lab_id']}")


@tool("create_lab", "Create an empty lab.", {"name": STR, "description": STR}, ["name"])
def _create_lab(api: Api, a: dict[str, Any]) -> Any:
    return api.post("/api/v1/labs", {"name": a["name"], "description": a.get("description", "")})


@tool(
    "delete_lab",
    "Delete a lab and everything in it: containers, VMs, bridges, and any stretched "
    "network's endpoints on every host it spans.",
    {"lab_id": STR},
    ["lab_id"],
)
def _delete_lab(api: Api, a: dict[str, Any]) -> Any:
    api.delete(f"/api/v1/labs/{a['lab_id']}")
    return {"deleted": a["lab_id"]}


@tool(
    "add_node",
    "Add a node. runtime is 'docker' or 'qemu'. For qemu, image is a catalog id such as "
    "'cirros' or 'ubuntu-24.04'; for docker it is an image reference such as 'alpine:3.20'. "
    "Leave ram_mb/cpu_limit unset to take the catalog's defaults.",
    {
        "lab_id": STR,
        "name": STR,
        "runtime": {"type": "string", "enum": ["docker", "qemu"]},
        "image": STR,
        "ram_mb": INT,
        "cpu_limit": NUM,
        "interfaces": {"type": "integer", "description": "how many interfaces to create"},
    },
    ["lab_id", "name", "runtime", "image"],
)
def _add_node(api: Api, a: dict[str, Any]) -> Any:
    count = int(a.get("interfaces") or 1)
    body = {
        "name": a["name"],
        "runtime": a["runtime"],
        "image": a["image"],
        "interfaces": [{} for _ in range(max(1, count))],
    }
    for key in ("ram_mb", "cpu_limit"):
        if a.get(key) is not None:
            body[key] = a[key]
    return api.post(f"/api/v1/labs/{a['lab_id']}/nodes", body)


@tool(
    "start_node",
    "Start a node. A QEMU image being used for the first time is downloaded and converted "
    "before boot, which can take many minutes; poll get_lab or image_status for progress "
    "rather than calling this again.",
    {"node_id": STR},
    ["node_id"],
)
def _start(api: Api, a: dict[str, Any]) -> Any:
    return api.post(f"/api/v1/nodes/{a['node_id']}/start")


@tool(
    "stop_node",
    "Stop a node. mode is 'graceful' (default) or 'force'.",
    {"node_id": STR, "mode": {"type": "string", "enum": ["graceful", "force"]}},
    ["node_id"],
)
def _stop(api: Api, a: dict[str, Any]) -> Any:
    return api.post(f"/api/v1/nodes/{a['node_id']}/stop", {"mode": a.get("mode", "graceful")})


@tool(
    "connect_nodes",
    "Wire two nodes together with a point-to-point link, using the first free interface on "
    "each (adding one if needed).",
    {"lab_id": STR, "a_node_id": STR, "b_node_id": STR},
    ["lab_id", "a_node_id", "b_node_id"],
)
def _connect(api: Api, a: dict[str, Any]) -> Any:
    lab = api.get(f"/api/v1/labs/{a['lab_id']}")
    used = {i for link in lab["links"] for i in (link["a_iface_id"], link["b_iface_id"])}

    def pick(node_id: str) -> str:
        node = next(n for n in lab["nodes"] if n["id"] == node_id)
        free = [i for i in node["interfaces"] if i["id"] not in used and not i["network_id"]]
        if free:
            used.add(free[0]["id"])
            return str(free[0]["id"])
        return str(api.post(f"/api/v1/nodes/{node_id}/interfaces", {})["id"])

    return api.post(
        f"/api/v1/labs/{a['lab_id']}/links",
        {"a_iface_id": pick(a["a_node_id"]), "b_iface_id": pick(a["b_node_id"])},
    )


@tool(
    "create_network",
    "Create a shared segment. kind 'bridge' is lab-internal; 'cloud' enslaves one of the "
    "host's own NICs named by cloud_ref so the lab can reach outside — see host_interfaces. "
    "One NIC can back only one cloud, and the NIC carrying the host's default route is "
    "refused unless allow_default_route is set, because binding it takes the host off the "
    "network.",
    {
        "lab_id": STR,
        "name": STR,
        "kind": {"type": "string", "enum": ["bridge", "cloud"]},
        "cloud_ref": STR,
        "allow_default_route": {"type": "boolean"},
    },
    ["lab_id", "name", "kind"],
)
def _create_net(api: Api, a: dict[str, Any]) -> Any:
    return api.post(
        f"/api/v1/labs/{a['lab_id']}/networks",
        {
            "name": a["name"],
            "kind": a["kind"],
            "cloud_ref": a.get("cloud_ref"),
            "allow_default_route": bool(a.get("allow_default_route")),
        },
    )


@tool(
    "join_network",
    "Put a node onto a shared segment, using its first free interface (adding one if needed). "
    "Takes effect immediately on a running node.",
    {"node_id": STR, "network_id": STR},
    ["node_id", "network_id"],
)
def _join(api: Api, a: dict[str, Any]) -> Any:
    node = api.get(f"/api/v1/nodes/{a['node_id']}")
    free = [i for i in node["interfaces"] if not i["network_id"]]
    if free:
        iface = free[0]["id"]
    else:
        iface = api.post(f"/api/v1/nodes/{a['node_id']}/interfaces", {})["id"]
    return api.patch(f"/api/v1/interfaces/{iface}", {"network_id": a["network_id"]})


@tool("host_interfaces", "The host NICs a cloud network can bind to.", {})
def _host_ifaces(api: Api, _: dict[str, Any]) -> Any:
    return api.get("/api/v1/system/host-interfaces")


@tool(
    "impair_link",
    "Apply netem to a link. Each field is optional and applies to the a->b direction unless "
    "reverse is true. Omitting everything clears the qdisc.",
    {
        "link_id": STR,
        "delay_ms": INT,
        "jitter_ms": INT,
        "loss_pct": NUM,
        "rate_kbit": INT,
        "duplicate_pct": NUM,
        "reorder_pct": NUM,
        "corrupt_pct": NUM,
        "reverse": {"type": "boolean", "description": "apply to b->a instead"},
        "both": {"type": "boolean", "description": "apply the same spec to both directions"},
    },
    ["link_id"],
)
def _impair(api: Api, a: dict[str, Any]) -> Any:
    keys = (
        "delay_ms",
        "jitter_ms",
        "loss_pct",
        "rate_kbit",
        "duplicate_pct",
        "reorder_pct",
        "corrupt_pct",
    )
    spec = {k: a[k] for k in keys if a.get(k)}
    body: dict[str, Any] = {}
    if a.get("both"):
        body = {"impair_ab": spec, "impair_ba": spec}
    elif a.get("reverse"):
        body = {"impair_ba": spec}
    else:
        body = {"impair_ab": spec}
    return api.patch(f"/api/v1/links/{a['link_id']}", body)


@tool(
    "capture_start",
    "Start a tcpdump on one interface or link. Read it with capture_read.",
    {"interface_id": STR, "link_id": STR, "bpf": STR},
)
def _cap_start(api: Api, a: dict[str, Any]) -> Any:
    body = {"bpf": a.get("bpf", "")}
    if a.get("link_id"):
        return api.post(f"/api/v1/links/{a['link_id']}/capture/start", body)
    return api.post(f"/api/v1/interfaces/{a['interface_id']}/capture/start", body)


@tool(
    "capture_read",
    "Read decoded packets buffered since the last read.",
    {"interface_id": STR, "link_id": STR},
)
def _cap_read(api: Api, a: dict[str, Any]) -> Any:
    if a.get("link_id"):
        return api.get(f"/api/v1/links/{a['link_id']}/capture")
    return api.get(f"/api/v1/interfaces/{a['interface_id']}/capture")


@tool("capture_stop", "Stop a capture.", {"interface_id": STR, "link_id": STR})
def _cap_stop(api: Api, a: dict[str, Any]) -> Any:
    if a.get("link_id"):
        return api.post(f"/api/v1/links/{a['link_id']}/capture/stop")
    return api.post(f"/api/v1/interfaces/{a['interface_id']}/capture/stop")


@tool(
    "node_logs",
    "Recent output from a node — container logs, or the QEMU process log.",
    {"node_id": STR, "lines": INT},
    ["node_id"],
)
def _logs(api: Api, a: dict[str, Any]) -> Any:
    return api.get(f"/api/v1/nodes/{a['node_id']}/logs?lines={int(a.get('lines') or 200)}")


@tool(
    "set_node_resources",
    "Change a node's RAM and vCPU count. Neither can change on a live guest, so this takes "
    "effect on the node's next start.",
    {"node_id": STR, "ram_mb": INT, "cpu_limit": NUM},
    ["node_id"],
)
def _resources(api: Api, a: dict[str, Any]) -> Any:
    body = {k: a[k] for k in ("ram_mb", "cpu_limit") if a.get(k) is not None}
    return api.patch(f"/api/v1/nodes/{a['node_id']}/resources", body)


@tool(
    "console_exec",
    "Run one shell command inside a running node and return its output. Docker nodes get "
    "real stdout/stderr/exit_code via `docker exec sh -c`. QEMU nodes get whatever the serial "
    "console produced within the timeout — no shell-prompt detection, output may be partial "
    "or include unrelated console text (the response's `warnings` will say so). Use this for "
    "questions like \"what address did DHCP give this VM?\" — cheaper and more direct than "
    "running a capture on the veth.",
    {"node_id": STR, "command": STR, "timeout_s": INT},
    ["node_id", "command"],
)
def _console_exec(api: Api, a: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"command": a["command"]}
    if a.get("timeout_s") is not None:
        body["timeout_s"] = int(a["timeout_s"])
    return api.post(f"/api/v1/nodes/{a['node_id']}/console/exec", body)


@tool("list_hosts", "Hosts in the multi-host control plane, and whether each is reachable.", {})
def _hosts(api: Api, _: dict[str, Any]) -> Any:
    return api.get("/api/v1/hosts")


@tool(
    "host_capabilities",
    "What a host's kernel can actually build — probed, not assumed. links.vxlan.supported "
    "decides whether a stretched network can exist there.",
    {"host_id": STR},
    ["host_id"],
)
def _host_caps(api: Api, a: dict[str, Any]) -> Any:
    return api.get(f"/api/v1/hosts/{a['host_id']}/capabilities")


@tool("health", "Whether the API, database, netd and Docker are up.", {})
def _health(api: Api, _: dict[str, Any]) -> Any:
    return api.get("/api/v1/health")
