Labtris 0.6.0 — the release that borrows the shape of the parts of
LocalStack, vrnetlab, and containerlab worth borrowing. A first-class
`labtris` CLI, YAML-defined "is the lab ready?" checks per lab, and
`.tar.gz` snapshots of an entire running lab (topology, disks, live
memory, container filesystems) that any Labtris host can load as a new
lab. Plus everything that landed in 0.5.0's window — VNC assistant
tools, prefetch, container-console fixes, FRR container fix, the
shutdown that stopped leaking into 502s.

## Install

```
curl -fsSL https://labtris.com/install | sudo bash
```

Or from the ISO — `labtris-0.6.0-amd64.iso.part-*` on the release
page. Reassemble and verify:

```
cat labtris-0.6.0-amd64.iso.part-* > labtris-0.6.0-amd64.iso
sha256sum -c labtris-0.6.0-amd64.sha256
```

Upgrading from 0.5.0 or 0.4.0: `cd /opt/labtris && git pull && sudo
alembic upgrade head && sudo systemctl restart labtris-api`. The
migration adds three JSONB columns to `labs` (`hooks_source`, `hooks`,
`hooks_state`) — nullable, no data touched, safe to run against an
existing instance.

## The `labtris` CLI

One binary, nested subcommands, rich tables for humans, `--format
json` for scripts. Consolidates `labtris-doctor` and `labtris-image`;
both stay as deprecation shims so nothing scripted breaks.

```
labtris login --url http://<host>:8081 --user admin
labtris lab list
labtris lab show <lab-id>
labtris node exec <node-id> -- ip -br addr
labtris system status -o json | jq .
```

Auth is the same 7-day JWT the browser uses — the login endpoint
returns it in the response body now, alongside the httponly cookie,
so a non-browser caller does not have to parse `Set-Cookie`. Stored
in `~/.config/labtris/auth.json` at mode 0600. `LABTRIS_TOKEN` +
`LABTRIS_API_URL` in the environment override the file for CI.

Full page: [The labtris CLI](https://docs.labtris.com/cli).

## Ready hooks

A lab **started** is not the same as a lab **ready**. Every node
moves to `running` in a few seconds, but the router hasn't converged,
the app hasn't opened its port, the guest is still on its first-boot
dialogue. Ready hooks bridge that gap: YAML on the lab names what
"ready" means for this lab, and Labtris fires the checks when every
node is up.

```yaml
ready_when: all_nodes_running
hooks:
  - name: "wait for BGP up on R1"
    kind: serial_wait
    node: R1
    wait_for: "% BGP session established"
    timeout_s: 60
  - name: "ping between DC1 and DC2"
    kind: ping
    from_node: dc1-host
    to: 10.20.0.1
```

Four kinds: `serial_wait` (regex on a QEMU serial console), `command`
(exec inside a Docker container), `ping` and `http` (from inside a
Docker node's netns via `nsenter`). Sequential; first failure stops
the sequence. Auto-fire is one-shot per apply — subsequent runs are
manual (`labtris lab hooks run` or the Run-now button).

Modelled on LocalStack's `/etc/localstack/init/ready.d/*.sh` shape,
adapted to networks: the check is the point, and the answer to "is
my lab actually up?" should be a plain sentence about the network,
not a stack trace. The teaching pattern this enables: an instructor
ships a lab plus a `hooks.yml`; students break something; they click
Run now; the state table names which check failed and what its
output was.

New surfaces: `PUT/GET/POST/DELETE /api/v1/labs/{id}/hooks`, MCP
tools `lab_hooks_{show,apply,run,clear}`, `labtris lab hooks *`
verbs, a **Hooks** dock tab in the UI.

Full page: [Ready hooks](https://docs.labtris.com/hooks).

## Portable snapshots (pods)

Snapshots package an entire lab into a `.tar.gz` that any Labtris
host can load as a new lab. Modelled on LocalStack's `pod save/load`;
the archive format is `labtris-pod-v1`.

```
labtris lab snapshot <lab-id> --mode cold      # small, boots fresh
labtris lab snapshot <lab-id> --mode hot       # big, resumes exactly
scp pod.tar.gz labtris@<other-host>:/var/lib/labtris/.../pods/
labtris lab load /var/lib/labtris/.../pod.tar.gz --name restored-1
```

**Cold** freezes at power-off: lab must be stopped, small archive
(kilobytes for topology-only, megabytes for QEMU disks), boots fresh
on load — perfect for "here is a lab ready to run".

**Hot** freezes running state: `savevm` embeds live QEMU memory into
the qcow2, `docker commit` + `docker save` embed running container
filesystems. Load places the disk directly in the new node's vm_dir
and marks it for `loadvm` on first start; the guest wakes at exactly
the moment of capture. Docker containers restore via `docker load`
and a rewritten `image` field.

Restore always creates a NEW lab with fresh ids — never overwrites
in place. Templates that don't exist locally are added under a
`-restored` suffix; templates that do exist are reused.

New surfaces: `POST /api/v1/labs/{id}/snapshot`, `GET /pods`,
`POST /pods/load`, `POST /pods/upload`, `DELETE /pods/{id}`. MCP
tools `lab_snapshot`, `pods_list`, `pod_load`, `pod_delete`.
`labtris {lab snapshot, lab load, pod list, pod inspect, pod rm}`.

Full page: [Portable snapshots](https://docs.labtris.com/snapshots).

## VNC assistant tools (from the 0.5.0 window)

Three MCP tools and three REST endpoints make a graphical guest
first-class for the assistant loop:

- `vnc_screenshot` returns a PNG of the framebuffer. Optional
  `region=x,y,w,h` crop and `grid=N` overlay so the model can pick
  coordinates without guessing. Falls back to an in-memory RFB
  framebuffer cache when the QMP screendump is expensive.
- `vnc_type` types text or sends raw `sendkey` combos, and returns
  the after-screenshot in the same response — one turn per action
  instead of two.
- `vnc_mouse` moves and clicks in framebuffer pixel coordinates via
  QMP `input-send-event`.

Plus `vnc_read` (tesseract OCR on the current framebuffer — a login
prompt in 15 tokens instead of 1500) and `vnc_wait_for_change`
(polls the framebuffer hash until it changes, so a wait is one API
call instead of five screenshots). QMP connection cache reuses one
handshake per node. Default `scale=0.5` and half-size framebuffer
shave the vision-token cost roughly 4×.

Together they close the "there is a login prompt but no serial
getty" gap — Ubuntu Desktop, vendor appliance wizards, PC BIOS/GRUB.

## Prefetch a QEMU image without spawning a node

`POST /api/v1/images/pull` kicks off download+extract+convert in the
background and returns immediately with a status snapshot;
`/images/status` is the polling target. Also `labtris-image pull
<id>` in the CLI (progress bar), the `image_pull` / `image_status`
MCP tools, and a **Pull now** button in the palette next to any
uncached image.

## Bootstrap: type first-boot config into a vendor VM

Vendor VMs (Cisco IOSv, XRv, Junos vMX, etc.) boot to a "Would you
like to enter initial config?" prompt and wait. `packaging/recipes/
bootstrap/*.json` names a small state machine per template:

```json
{
  "step_timeout_s": 60,
  "steps": [
    {"wait_for": "Username:", "type": "cisco\n"},
    {"wait_for": "Password:", "type": "cisco\n"},
    {"wait_for": "Router#",   "type": "conf t\nhostname {name}\nend\nwrite\n"}
  ]
}
```

Runs once per node on first boot; state at
`GET /api/v1/nodes/{id}/bootstrap`. Retry from the current console
state with `POST /nodes/{id}/bootstrap/retry`. Editable per-template
in the UI. Same shape vrnetlab has, but declarative — the state
machine is data, not per-vendor Python.

## Fixes

**FRR containers survive `frrinit.sh restart`.** The default
`frrouting/frr:v8.4.0` CMD exec's watchfrr as tini's child; anything
that stops watchfrr — including `frrinit.sh restart`'s first step —
takes tini with it, and the container dies. CMD is now
`/bin/bash -c '/usr/lib/frr/frrinit.sh start && exec tail -f
/dev/null'`, and the extra caps (SYS_ADMIN, NET_BIND_SERVICE,
SYS_NICE) that zebra needs to start at all are declared alongside
the base NET_ADMIN + NET_RAW.

**API restart no longer leaks into 90-second 502s.** Uvicorn shutdown
waits for asyncio background tasks; the long-lived ones (image
pulls, bootstrap runners, RFB clients, QMP connections, serial
sessions) were not being cancelled in `lifespan.__aexit__`. Every
`systemctl restart labtris-api` then blocked until systemd's default
`TimeoutStopSec=90s` fired SIGKILL, during which nginx returned 502
to every request. Fixed by explicit cancellation in the lifespan
context manager; `TimeoutStopSec=15s` in the systemd unit as
backstop.

**QEMU 8.2 bootindex.** The `-drive if=<bus>` shortcut form rejects
`bootindex`. virtio and ide now build the split form —
`-drive if=none` + `-device …,bootindex=1` — which matches how sata
already worked. Fixes VMs that refused to boot on Ubuntu 24.04.

**Container consoles draw their prompt.** Bash writes its prompt to
*stderr*, and the wrapper had `exec bash -i 2>/dev/null` to hide
bash-not-found errors on sh-only images. That redirect was silencing
every prompt too. Uses `command -v bash` to pick the shell, never
redirects stderr.

**`console_exec` accepts empty commands.** A bare `"\n"` wakes a
getty stuck at `login:`. The old validator refused it and the
assistant gave up configuring Ubuntu VMs.

**Docker console menu label.** Unified to "Console" — was "Shell" on
Docker and "Console" on QEMU, and two names for the same feature
made people ask for a Docker console.

**Dock: stop snapping back to Inspector.** A Svelte 5 `$effect` was
re-firing whenever `dock` changed and re-selecting Inspector on any
click. `untrack()` around the dock read fixes it — clicking Logs
now stays on Logs.

**Canvas: click a wired port, highlight the peer.** The old
behaviour was to arm a wire (which does not make sense on a port
that already has one) and light up every other node as "wire-ok" or
"wire-no". Now selects the link and highlights only the peer node.

**Assistant recovers from tool-call JSON parse errors.** Vertex/Anthropic
gateway occasionally returns malformed JSON in a tool call payload;
the browser now retries once with a nudge appended to the user turn,
and surfaces a cleaner error message on the second failure instead of
the litellm envelope.

**`console_exec` state drift on missing containers.** `docker exec`
against a container that was deleted outside Labtris used to raise
the raw `DockerError(409, ...)`. Now returns a readable "container
is not running — its state has drifted" and re-observes to reconcile
`node.state` in the DB.

## Verified end-to-end

On a real instance:
- `labtris lab hooks apply <id> hooks.yml` — parsed, persisted,
  watcher started; `hooks run` executed a ping from a Docker node's
  netns via nsenter, phase `passed`.
- `labtris lab snapshot <id> --mode cold` — 3 KB archive for a
  9-node lab (all Docker, no per-node bytes); `labtris lab load`
  reproduced the topology in a new lab.
- `labtris lab snapshot <id> --mode hot` — 69 MB archive with a
  running FRR container's committed filesystem embedded; `labtris
  lab load` + `labtris node start <new-id>` produced a running
  container that still had `/tmp/hot-pod-marker.txt` from before
  the snapshot.
- FRR container survives `frrinit.sh restart` (tini stays; zebra
  respawns).
- `systemctl restart labtris-api` returns in ~6 s cleanly (was 90 s
  + SIGKILL).

## Bring your own images

Unchanged from 0.5.0. Labtris redistributes no vendor software; the
GNS3 registry's 228 appliance definitions import natively.

Apache-2.0. Built on Ubuntu; not affiliated with Canonical.
