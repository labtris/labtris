from __future__ import annotations

import base64
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
ARR_STR = {"type": "array", "items": {"type": "string"}}


@tool(
    "list_catalog",
    "Images that can be placed in a lab: Docker images, and QEMU images with their "
    "RAM/vCPU floor, login, and whether they are already downloaded. A QEMU image that "
    "is not cached costs a multi-GB download on first start.",
    {},
)
def _catalog(api: Api, _: dict[str, Any]) -> Any:
    return api.get("/api/v1/catalog")


@tool(
    "image_pull",
    "Prefetch a QEMU catalog image now, without spawning a node first. Kicks off the "
    "download+extract+convert pipeline in the background and returns immediately with a "
    "status snapshot; poll `image_status` for progress. Accepts a catalog id "
    "(`ubuntu-24.04`, `cirros-0.6.2`), an https URL, or a `custom-<sha>` reference. "
    "Idempotent — a call for an already-cached image reports `cached: true` and does "
    "no work. Use this before starting a class or scripted lab so the first node's "
    "start doesn't stall for eight minutes on the download.",
    {"image": STR},
    ["image"],
)
def _image_pull(api: Api, a: dict[str, Any]) -> Any:
    return api.post("/api/v1/images/pull", {"image": a["image"]})


@tool(
    "image_status",
    "Snapshot of a QEMU image's download / prep state. Returns `cached: true|false` "
    "always, plus `phase` (downloading|extracting|converting), `done`/`total` bytes, "
    "and `percent` (0-100) while a pull is in flight. Poll every 2-5 seconds after "
    "`image_pull`; the phase moves through downloading → extracting → converting → "
    "gone (with cached: true). Cheap — one stat call, safe to poll aggressively.",
    {"image": STR},
    ["image"],
)
def _image_status(api: Api, a: dict[str, Any]) -> Any:
    from urllib.parse import quote
    return api.get(f"/api/v1/images/status?image={quote(a['image'])}")


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
    "Run one shell command inside a running node and return its output. Works on Docker "
    "(real stdout/stderr/exit_code via `docker exec sh -c`) and on QEMU nodes that have a "
    "serial getty (kernel/prompt speaks on ttyS0). "
    "IMPORTANT: on QEMU, if this returns an empty `stdout` for a command that should "
    "produce output, the guest almost certainly has no serial getty — switch to "
    "`vnc_screenshot` + `vnc_type` / `vnc_mouse` for that node. Vendor appliances "
    "(PAN-OS, NX-OS wizard, VMware installers, PC BIOS/GRUB), Ubuntu Desktop, and any "
    "OS booted graphically without `systemctl enable serial-getty@ttyS0` all fall into "
    "the VNC-only camp. Use this tool freely for containers and known-serial VMs; "
    "avoid retrying it on the same node after one silent QEMU result.",
    {"node_id": STR, "command": STR, "timeout_s": INT},
    ["node_id", "command"],
)
def _console_exec(api: Api, a: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"command": a["command"]}
    if a.get("timeout_s") is not None:
        body["timeout_s"] = int(a["timeout_s"])
    return api.post(f"/api/v1/nodes/{a['node_id']}/console/exec", body)


@tool(
    "vnc_screenshot",
    "Capture the current framebuffer of a QEMU node's display and return it as a PNG. "
    "Use on nodes with no serial console — vendor appliances, graphical installers, "
    "Ubuntu VMs where `serial-getty` was never enabled. Pair with `vnc_type` and "
    "`vnc_mouse` to drive the guest. QEMU only; containers have no framebuffer.\n\n"
    "Defaults to `scale=0.5` — a 1280x800 framebuffer becomes 640x400, roughly a "
    "quarter of the vision tokens per screenshot and still readable. Pass `scale=1.0` "
    "when you need full resolution (tiny text, pixel-accurate coordinate picking).\n\n"
    "Optional: `region=\"x,y,w,h\"` crops to a rectangle in framebuffer pixels (applied "
    "before scale). `grid=100` overlays a 100-pixel grid with axis labels — useful for "
    "picking coordinates for a follow-up `vnc_mouse` click. Note grid coordinates are "
    "drawn in the post-scale pixel space; use the same `scale` for the follow-up mouse "
    "call so the numbers align.",
    {"node_id": STR, "region": STR, "grid": INT, "scale": NUM},
    ["node_id"],
)
def _vnc_screenshot(api: Api, a: dict[str, Any]) -> Any:
    path = f"/api/v1/nodes/{a['node_id']}/vnc/screenshot"
    q: list[str] = []
    if a.get("region"):
        q.append(f"region={a['region']}")
    if a.get("grid"):
        q.append(f"grid={int(a['grid'])}")
    if a.get("scale") is not None:
        q.append(f"scale={float(a['scale'])}")
    if q:
        path += "?" + "&".join(q)
    png, ctype = api.post_bytes(path)
    return {
        "content": [
            {
                "type": "image",
                "data": base64.b64encode(png).decode(),
                "mimeType": ctype or "image/png",
            }
        ]
    }


@tool(
    "vnc_type",
    "Type text or send raw key combos into a QEMU node's display, as if a human were "
    "at the keyboard, and return the resulting screenshot so you can verify. Two shapes: "
    "`text=\"root\\nubuntu\\n\"` types each character (ASCII only, non-ASCII skipped); "
    "`keys=[\"ctrl-alt-f2\"]` sends one raw HMP sendkey combo per element (function keys, "
    "tty switches, ctrl/alt sequences). The response includes a PNG of the display after "
    "the keystrokes land — you do not need to call vnc_screenshot separately. Set "
    "`screenshot=false` only when firing a burst of typing where only the final frame "
    "matters. QEMU only.",
    {"node_id": STR, "text": STR, "keys": ARR_STR, "hold_ms": INT,
     "screenshot": {"type": "boolean"}, "settle_ms": INT, "scale": NUM},
    ["node_id"],
)
def _vnc_type(api: Api, a: dict[str, Any]) -> Any:
    body: dict[str, Any] = {}
    if a.get("text") is not None:
        body["text"] = a["text"]
    if a.get("keys") is not None:
        body["keys"] = a["keys"]
    if a.get("hold_ms") is not None:
        body["hold_ms"] = int(a["hold_ms"])
    if a.get("screenshot") is not None:
        body["screenshot"] = bool(a["screenshot"])
    if a.get("settle_ms") is not None:
        body["settle_ms"] = int(a["settle_ms"])
    if a.get("scale") is not None:
        body["scale"] = float(a["scale"])
    out = api.post(f"/api/v1/nodes/{a['node_id']}/vnc/type", body)
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"keys_sent: {out.get('keys_sent')}"}
    ]
    if out.get("screenshot"):
        content.append(
            {
                "type": "image",
                "data": out["screenshot"],
                "mimeType": out.get("mime_type") or "image/png",
            }
        )
    return {"content": content}


@tool(
    "vnc_mouse",
    "Move or click on a QEMU node's display at pixel coordinates (x, y). `action` is "
    "'move' (no click), 'click' (default), or 'double_click'. `button` is 'left' "
    "(default), 'right', or 'middle'. Response includes the framebuffer size and a "
    "screenshot after the action so you can verify. Coordinates are the same pixel "
    "space as vnc_screenshot's PNG — take a screenshot first, decide where to click, "
    "then call this. Uses QMP absolute coordinates under the hood, so clicks land "
    "where you asked pixel-accurately.",
    {"node_id": STR, "x": INT, "y": INT, "action": STR, "button": STR,
     "screenshot": {"type": "boolean"}, "settle_ms": INT, "scale": NUM},
    ["node_id", "x", "y"],
)
def _vnc_mouse(api: Api, a: dict[str, Any]) -> Any:
    body: dict[str, Any] = {"x": int(a["x"]), "y": int(a["y"])}
    for k in ("action", "button", "settle_ms"):
        if a.get(k) is not None:
            body[k] = a[k] if k != "settle_ms" else int(a[k])
    if a.get("screenshot") is not None:
        body["screenshot"] = bool(a["screenshot"])
    if a.get("scale") is not None:
        body["scale"] = float(a["scale"])
    out = api.post(f"/api/v1/nodes/{a['node_id']}/vnc/mouse", body)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"fb: {out.get('fb_width')}x{out.get('fb_height')}",
        }
    ]
    if out.get("screenshot"):
        content.append(
            {
                "type": "image",
                "data": out["screenshot"],
                "mimeType": out.get("mime_type") or "image/png",
            }
        )
    return {"content": content}


@tool(
    "vnc_wait_for_change",
    "Block until the QEMU display stops changing, then return the final screenshot. "
    "Use after a click, keystroke, or command that triggers an animation, a menu "
    "redraw, or a boot progress screen — one call replaces the 'screenshot every "
    "500ms' loop the model naturally reaches for. Default: poll every 200ms, "
    "consider settled after 400ms of no change, give up after 5s and return whatever "
    "is on screen with `settled: false`.",
    {"node_id": STR, "poll_ms": INT, "stable_ms": INT, "timeout_ms": INT, "scale": NUM},
    ["node_id"],
)
def _vnc_wait(api: Api, a: dict[str, Any]) -> Any:
    body: dict[str, Any] = {}
    for k in ("poll_ms", "stable_ms", "timeout_ms"):
        if a.get(k) is not None:
            body[k] = int(a[k])
    if a.get("scale") is not None:
        body["scale"] = float(a["scale"])
    out = api.post(f"/api/v1/nodes/{a['node_id']}/vnc/wait_for_change", body)
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"settled: {out.get('settled')}"}
    ]
    if out.get("screenshot"):
        content.append(
            {
                "type": "image",
                "data": out["screenshot"],
                "mimeType": out.get("mime_type") or "image/png",
            }
        )
    return {"content": content}


@tool(
    "vnc_read",
    "Read the text on a QEMU node's display via OCR — no image returned. Two orders "
    "of magnitude cheaper in vision tokens than `vnc_screenshot` when you only need "
    "to READ (which prompt, which error, which menu item, what does that dialog say). "
    "Use `vnc_screenshot` when you need to SEE (a graph, a colour, pixel positions "
    "for a mouse click). Optional `region=\"x,y,w,h\"` crops before OCR. Requires "
    "tesseract-ocr installed on the host; a clear error names the fix if it isn't.",
    {"node_id": STR, "region": STR},
    ["node_id"],
)
def _vnc_read(api: Api, a: dict[str, Any]) -> Any:
    body: dict[str, Any] = {}
    if a.get("region"):
        body["region"] = a["region"]
    return api.post(f"/api/v1/nodes/{a['node_id']}/vnc/read", body)


@tool(
    "node_bootstrap_status",
    "Where a QEMU node is in its first-boot config sequence. Phase is one of "
    "`idle` (never ran / template has no bootstrap block), `running` (typing "
    "into the serial console right now), `done`, `failed`. On `failed`, the "
    "response includes the step and the error. Poll after start_node if you "
    "want to wait for a vendor image to become SSH-ready.",
    {"node_id": STR},
    ["node_id"],
)
def _node_bootstrap_status(api: Api, a: dict[str, Any]) -> Any:
    return api.get(f"/api/v1/nodes/{a['node_id']}/bootstrap")


@tool(
    "node_bootstrap_retry",
    "Wipe the bootstrap markers and re-run against the current console state. "
    "Use after editing the template's bootstrap steps to fix a wrong regex or "
    "a race. The node stays running throughout. Fails if there is no "
    "`bootstrap` block on the template or if the node is not running.",
    {"node_id": STR},
    ["node_id"],
)
def _node_bootstrap_retry(api: Api, a: dict[str, Any]) -> Any:
    return api.post(f"/api/v1/nodes/{a['node_id']}/bootstrap/retry")


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


@tool(
    "lab_hooks_show",
    "Return the lab's ready-hooks block: raw YAML source, parsed form, and "
    "the last-run state (which hook passed, which failed, per-hook output).",
    {"lab_id": STR},
    ["lab_id"],
)
def _lab_hooks_show(api: Api, a: dict[str, Any]) -> Any:
    return api.get(f"/api/v1/labs/{a['lab_id']}/hooks")


@tool(
    "lab_hooks_apply",
    "Replace a lab's ready-hooks block with YAML text. Fails on parse errors. "
    "The auto-fire watcher is restarted so the new hooks fire the next time "
    "the lab's ready condition trips.",
    {"lab_id": STR, "source": STR},
    ["lab_id", "source"],
)
def _lab_hooks_apply(api: Api, a: dict[str, Any]) -> Any:
    return api.call(
        "PUT", f"/api/v1/labs/{a['lab_id']}/hooks", {"source": a["source"]}
    )


@tool(
    "lab_hooks_run",
    "Force a hooks run now, ignoring the ready_when trigger. Blocks until "
    "every hook has run (or the first failure) and returns the outcome.",
    {"lab_id": STR},
    ["lab_id"],
)
def _lab_hooks_run(api: Api, a: dict[str, Any]) -> Any:
    return api.post(f"/api/v1/labs/{a['lab_id']}/hooks/run")


@tool(
    "lab_hooks_clear",
    "Drop the lab's hooks block and per-run state history.",
    {"lab_id": STR},
    ["lab_id"],
)
def _lab_hooks_clear(api: Api, a: dict[str, Any]) -> Any:
    return api.delete(f"/api/v1/labs/{a['lab_id']}/hooks")


@tool(
    "lab_snapshot",
    "Snapshot a lab into a portable pod archive (labtris-pod-v1.tar.gz) on "
    "the server's pod_dir. mode=cold requires the lab stopped; mode=hot "
    "captures live QEMU RAM (savevm) + Docker filesystem (commit+save) so "
    "the restored lab resumes at the same point.",
    {"lab_id": STR, "mode": {"type": "string", "enum": ["cold", "hot"]}},
    ["lab_id"],
)
def _lab_snapshot(api: Api, a: dict[str, Any]) -> Any:
    return api.post(
        f"/api/v1/labs/{a['lab_id']}/snapshot", {"mode": a.get("mode", "cold")}
    )


@tool(
    "pods_list",
    "Every pod archive present in the server's pod_dir, with lab name, "
    "size, node count, mode and creation time.",
    {},
)
def _pods_list(api: Api, _a: dict[str, Any]) -> Any:
    return api.get("/api/v1/pods")


@tool(
    "pod_load",
    "Restore a pod archive from a path on the SERVER host as a new lab. "
    "Never overwrites in place; the new lab gets a fresh name (append "
    "`name` to override) and fresh MACs. QEMU disks are installed into "
    "the custom-image cache so the first start after load boots from the "
    "snapshot state.",
    {"path": STR, "name": STR},
    ["path"],
)
def _pod_load(api: Api, a: dict[str, Any]) -> Any:
    body = {"path": a["path"]}
    if a.get("name"):
        body["name"] = a["name"]
    return api.call("POST", "/api/v1/pods/load", body)


@tool(
    "pod_delete",
    "Remove a pod archive from the server's pod_dir.",
    {"pod_id": STR},
    ["pod_id"],
)
def _pod_delete(api: Api, a: dict[str, Any]) -> Any:
    return api.delete(f"/api/v1/pods/{a['pod_id']}")


@tool(
    "p4_builtins",
    "The curated P4 programs that ship with Labtris for use on a bmv2 "
    "switch node. Each row names the program, a one-line description, "
    "and its byte size.",
    {},
)
def _p4_builtins(api: Api, _a: dict[str, Any]) -> Any:
    return api.get("/api/v1/p4/builtins")


@tool(
    "node_p4_show",
    "Return the P4 program currently mounted on a bmv2 switch node — "
    "source (builtin|uploaded|none), program name, on-disk path, and "
    "the source text if small enough to include.",
    {"node_id": STR},
    ["node_id"],
)
def _node_p4_show(api: Api, a: dict[str, Any]) -> Any:
    return api.get(f"/api/v1/nodes/{a['node_id']}/p4")


@tool(
    "node_p4_set_builtin",
    "Point a bmv2 node at one of the curated built-in P4 programs "
    "(basic_switch, ecmp, ecn, trim). Restart the node for the change "
    "to take effect. Refused if the node is not a bmv2 P4 switch.",
    {"node_id": STR, "builtin": STR},
    ["node_id", "builtin"],
)
def _node_p4_set(api: Api, a: dict[str, Any]) -> Any:
    return api.call(
        "PUT",
        f"/api/v1/nodes/{a['node_id']}/p4",
        {"builtin": a["builtin"]},
    )


@tool(
    "node_p4_clear",
    "Drop the P4 program mounted on a bmv2 node. On next start, the "
    "runtime seeds `basic_switch` as the default again.",
    {"node_id": STR},
    ["node_id"],
)
def _node_p4_clear(api: Api, a: dict[str, Any]) -> Any:
    return api.delete(f"/api/v1/nodes/{a['node_id']}/p4")
