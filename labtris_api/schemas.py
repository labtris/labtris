from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LabCreate(BaseModel):
    name: str
    description: str = ""
    #: Slash-separated, "" for the root. Normalised on the way in.
    folder: str = ""


class LabOut(ORMModel):
    id: str
    name: str
    description: str
    locked: bool
    folder: str = ""
    #: The configset last applied, or None if the lab has drifted from any.
    active_configset: str | None = None


class InterfaceIn(BaseModel):
    name: str | None = None
    network_id: str | None = None


class InterfaceOut(ORMModel):
    id: str
    node_id: str
    idx: int
    name: str
    mac: str
    host_ifname: str | None
    network_id: str | None
    vlan_mode: str | None = None
    vlan_id: int | None = None
    trunk_vids: str | None = None
    reserved_ip: str | None = None


class NodeCreate(BaseModel):
    name: str
    runtime: Literal["docker", "qemu", "containerlab", "iol", "dynamips"]
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    cmd: list[str] | None = None
    cpu_limit: float | None = None
    ram_mb: int | None = None
    nic_model: str | None = None
    console: dict[str, Any] = Field(default_factory=dict)
    interfaces: list[InterfaceIn] = Field(default_factory=list)


class NodePatch(BaseModel):
    name: str | None = None
    env: dict[str, str] | None = None
    cmd: list[str] | None = None
    cpu_limit: float | None = None
    ram_mb: int | None = None
    console: dict[str, Any] | None = None
    qemu_opts: dict[str, Any] | None = None


class NodeOut(ORMModel):
    id: str
    lab_id: str
    name: str
    runtime: str
    image: str
    state: str
    cpu_limit: float | None
    ram_mb: int | None
    env: dict[str, Any]
    cmd: list[str] | None
    runtime_ref: str | None
    last_error: str | None
    style: dict[str, Any] = Field(default_factory=dict)
    console: dict[str, Any] = Field(default_factory=dict)
    nic_model: str | None = None
    paused: bool = False
    qemu_opts: dict[str, Any] = Field(default_factory=dict)
    #: Which convention the guest uses for its port names, so the UI can label
    #: "ens3" as an Ubuntu-on-QEMU fact rather than leaving it looking wrong.
    iface_scheme: str = "eth"
    interfaces: list[InterfaceOut] = Field(default_factory=list)


class NodeDetail(NodeOut):
    capabilities: list[str]


class NetworkCreate(BaseModel):
    name: str
    kind: Literal["bridge", "cloud", "vxlan", "nat"] = "bridge"
    #: For `cloud`, the host NIC to enslave (see GET /system/host-interfaces).
    cloud_ref: str | None = None
    #: Required to bind a cloud to the NIC carrying the host's default route,
    #: which will take the host off the network.
    allow_default_route: bool = False
    host_ids: list[str] = Field(default_factory=list)
    vni: int | None = None

    #: NAT only. Left unset, a free /24 is picked from the private range and
    #: the first address becomes the gateway — a NAT network is something you
    #: should be able to drop on the canvas without doing IPAM first.
    subnet: str | None = None
    #: Off by default only in the sense that an unset pool means "no DHCP".
    #: Creating a NAT network through the API with dhcp unset gets a pool,
    #: because a NAT network nobody can get an address on is a trap.
    dhcp: bool = True
    dns_server: str | None = None

    #: Smart bridge. Applies to `bridge` and `nat`; a cloud's tagging is the
    #: physical network's business, not ours.
    vlan_aware: bool = False
    vlan_proto: Literal["802.1Q", "802.1ad"] = "802.1Q"


class NetworkPatch(BaseModel):
    """Edit a network in place. Every field optional; unset means unchanged."""

    name: str | None = None
    subnet: str | None = None
    dhcp: bool | None = None
    dns_server: str | None = None
    vlan_aware: bool | None = None
    vlan_proto: Literal["802.1Q", "802.1ad"] | None = None


class NetworkOut(ORMModel):
    id: str
    lab_id: str
    name: str
    kind: str
    host_ifname: str | None
    cloud_ref: str | None
    vni: int | None = None
    subnet: str | None = None
    gateway: str | None = None
    dhcp_first: str | None = None
    dhcp_last: str | None = None
    dns_server: str | None = None
    vlan_aware: bool = False
    vlan_proto: str = "802.1Q"


class PortReservation(BaseModel):
    """Pin this port to an address, or clear the pin with an explicit null."""

    reserved_ip: str | None = None


class PortVlan(BaseModel):
    """How one port sits on a VLAN-filtering bridge.

    `access` carries one VLAN untagged; `trunk` carries a list tagged. Setting
    mode to null puts the port back to the bridge's default, which is what a
    port on a non-filtering bridge has always had.
    """

    vlan_mode: Literal["access", "trunk"] | None = None
    vlan_id: int | None = None
    trunk_vids: list[int] = Field(default_factory=list)


class InterfacePatch(BaseModel):
    """Move an interface onto a network, or off it with an explicit null."""

    network_id: str | None = None


class NetworkHostOut(BaseModel):
    """One host's endpoint of a stretched (`vxlan`) network."""

    host_id: str
    host_name: str
    underlay_ip: str | None
    vxlan_ifname: str | None


class LinkCreate(BaseModel):
    a_iface_id: str
    b_iface_id: str


class LinkOut(ORMModel):
    id: str
    lab_id: str
    a_iface_id: str
    b_iface_id: str
    network_id: str
    impair_ab: dict[str, Any] | None
    impair_ba: dict[str, Any] | None
    admin_up: bool


class LabListItem(BaseModel):
    """A lab as it appears in the picker.

    Carries who owns it, because on a shared instance the first question about
    any lab in a list is whose it is."""

    id: str
    name: str
    description: str = ""
    locked: bool = False
    owner_id: str | None = None
    owner: str | None = None
    owner_name: str | None = None
    mine: bool = False
    folder: str = ""
    #: What is in the lab, so the switcher can label it without fetching each
    #: one. These are on the model and not merely in the handler's dict for a
    #: reason worth keeping: the response_model filters the response, so a key
    #: the handler adds but the schema does not declare is silently dropped.
    nodes: int = 0
    running: int = 0


class LabDetail(LabOut):
    nodes: list[NodeOut]
    networks: list[NetworkOut]
    links: list[LinkOut]


class LabPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    #: Moving a lab is a folder change; "" moves it back to the root.
    folder: str | None = None


class LabClone(BaseModel):
    """A copy of a lab, ready to run but not running.

    Nothing live is copied: the clone gets fresh MACs and no runtime refs,
    because two labs holding the same MAC would collide the moment both
    started."""

    name: str | None = None
    folder: str | None = None


class FolderRename(BaseModel):
    frm: str = Field(alias="from")
    to: str

    model_config = ConfigDict(populate_by_name=True)


class GeometryBody(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)


class StopBody(BaseModel):
    mode: Literal["graceful", "force"] = "graceful"


class ConsoleOut(BaseModel):
    kind: str
    target: str
    meta: dict[str, str] = Field(default_factory=dict)


class HealthOut(BaseModel):
    status: str
    netd: bool
    db: bool
    docker: bool


class ImpairSpec(BaseModel):
    delay_ms: int = 0
    jitter_ms: int = 0
    loss_pct: float = 0
    rate_kbit: int | None = None
    reorder_pct: float = 0
    duplicate_pct: float = 0
    corrupt_pct: float = 0
    #: ECN CE marking under queue pressure. When true, a RED qdisc sits
    #: below netem+tbf and marks the ECN bit on packets whose queue-avg
    #: has crossed `ecn_min_bytes`, dropping only above `ecn_max_bytes`.
    #: Enables DCTCP / DCQCN / Ultra Ethernet CC testing on plain
    #: shaped Linux links — combined with the `ecn.p4` bmv2 built-in
    #: this covers 'ECN marking somewhere in the fabric' end to end.
    ecn: bool = False
    ecn_min_bytes: int = 50_000
    ecn_max_bytes: int = 150_000
    #: Lossless-Ethernet DCB, the queueing half of 802.1Qbb. This IS
    #: implemented: netd installs a `prio` root with 8 bands
    #: (priomap 0..7) and hangs a RED qdisc with ECN on each band named
    #: in `pfc_priorities`; unnamed bands get the default pfifo. Under a
    #: rate cap or netem the whole tree hangs below them, so the shape is
    #: `tbf → prio → (red|pfifo)`. See labtris_netd/net.py
    #: _pfc_apply_via_cli and docs/design/pfc-implementation.mdx.
    #:
    #: What is NOT implemented is the other half: emitting real 802.1Qbb
    #: PAUSE frames, which needs OVS or an eBPF per-priority pause path.
    #: netd tries `ethtool -A` and logs the EOPNOTSUPP veth returns. So
    #: classes are isolated and ECN-marked — the part that makes RoCEv2
    #: usable — but a sender is never explicitly told to stop.
    #:
    #: (This comment previously said the whole feature was unimplemented
    #: and that netd logged 'pfc requested but not implemented'. That was
    #: true before Phase H and has been wrong since; it is the sort of
    #: stale note that makes someone re-build a thing that already works.)
    pfc: bool = False
    pfc_priorities: list[int] = []


class LinkPatch(BaseModel):
    impair_ab: dict[str, Any] | None = None
    impair_ba: dict[str, Any] | None = None
    admin_up: bool | None = None
    preset: str | None = None


class CaptureIn(BaseModel):
    bpf: str = ""


class GDriveCredentials(BaseModel):
    """A Desktop-app OAuth client from Google Cloud Console."""

    client_id: str
    client_secret: str
    folder_id: str | None = None


class GDriveComplete(BaseModel):
    device_code: str


class FeedbackIn(BaseModel):
    """A report from the annotate tool. `context` is free-form on purpose: the
    browser knows things the server cannot guess, and constraining its shape
    now would only mean losing them."""

    note: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)


class FeedbackOut(ORMModel):
    id: str
    note: str
    status: str
    context: dict[str, Any]
    created_at: datetime


class FeedbackPatch(BaseModel):
    status: Literal["open", "done", "wontfix"]


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class WiresharkIn(BaseModel):
    """What to point Wireshark at. Exactly one of these."""

    interface_id: str | None = None
    link_id: str | None = None
    network_id: str | None = None


class AiChatIn(BaseModel):
    message: str


class NodeStyleIn(BaseModel):
    icon: str | None = None
    color: str | None = None


class NodeResourcesIn(BaseModel):
    """Hardware that takes effect on the node's next start."""

    ram_mb: int | None = Field(default=None, ge=16, le=262144)
    cpu_limit: float | None = Field(default=None, gt=0, le=64)
    nic_model: str | None = None


class NodeConsoleIn(BaseModel):
    """Where a remote-console tunnel should dial for this node.

    Settings are stored per protocol. They have to be: VNC and RDP disagree
    about almost every value — a QEMU node's VNC display is on loopback at
    5900+N while its guest's RDP listener is on a lab network at 3389 — so a
    single shared `port` means configuring one console silently breaks the
    other.

    Anything beyond the fields below is passed to guacd verbatim as a
    connection parameter, so each protocol's full option set (`security`,
    `ignore-cert`, `domain`, `initial-program`, ...) stays reachable without a
    schema change."""

    model_config = ConfigDict(extra="allow")

    protocol: Literal["vnc", "rdp"] = "rdp"
    hostname: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None


class ConfigIn(BaseModel):
    content: str


class ConfigOut(BaseModel):
    content: str | None


class ConfigSetIn(BaseModel):
    configs: dict[str, str] = Field(default_factory=dict)


class TemplateOut(ORMModel):
    id: str
    name: str
    runtime: str
    image: str
    cmd: list[str] | None
    env: dict[str, Any]
    icon: str | None
    #: Declared, not just set on the model: response_model drops any field it
    #: does not know about, so a template would come back over the wire without
    #: the sizing that decides whether it boots.
    spec: dict[str, Any] = {}
    description: str | None = None


class TemplateIn(BaseModel):
    name: str
    icon: str | None = None
    description: str | None = None


class TemplatePatch(BaseModel):
    """Editable fields on an existing template.

    Runtime and image are not editable — they identify the template. Changing
    either would move the row to a different type or point at different bytes,
    which is a delete-and-re-add operation. The per-spec knobs (RAM, CPUs,
    nic-model, disk-bus, iface-scheme, graphical) merge into spec, so unset
    fields aren't clobbered. `qemu_opts` sets the per-template hypervisor
    knobs a spawned node inherits — this is where "cpu: host" lands so a
    guest whose glibc needs sse4.2 can boot.
    """

    name: str | None = None
    icon: str | None = None
    description: str | None = None
    ram_mb: int | None = None
    cpus: int | None = None
    nic_model: str | None = None
    disk_bus: str | None = None
    iface_scheme: str | None = None
    graphical: bool | None = None
    qemu_opts: dict[str, Any] | None = None
    #: Path to a companion BIOS file the runtime passes as `-bios`.
    #: Populated via POST /images/companion which content-hashes it into
    #: the qemu-bios cache. NX-OSv 9000 needs EVE's OVMF-sata.fd; other
    #: appliances (vjunosevoefi) also point here.
    bios: str | None = None
    #: Companion CD-ROM the runtime attaches at every boot as `-cdrom`.
    #: NX-OSv's 1.3GB cdrom.iso ships its initial config schema this way;
    #: Cisco cat9kv/uccx use the same pattern for their config seeds.
    cdrom: str | None = None
    #: Extra qemu args appended verbatim per node. Distinct from
    #: qemu_opts.extra_args which is per-node and settings-gated —
    #: this list is authored per template, applied automatically, and
    #: editing it requires template-admin (same trust as picking the
    #: image itself). Common uses: -smbios manufacturer strings for
    #: Cisco appliances, specific -cpu features for vjunos images.
    qemu_extra_args: list[str] | None = None
    #: First-boot config sequence typed into the serial console. Shape:
    #: `{step_timeout_s?: int, steps: [{wait_for: regex, type: str,
    #: timeout_s?: int}, ...]}`. Runs at most once per node from this
    #: template. `null` clears the block; an object sets it. See
    #: packaging/recipes/bootstrap/ for example recipes.
    bootstrap: dict[str, Any] | None = None


class TaskOut(ORMModel):
    id: str
    lab_id: str | None
    kind: str
    status: str
    progress: int
    total: int
    message: str


class TaskIn(BaseModel):
    kind: Literal["pull_images", "start_all", "stop_all"]


class LabImportIn(BaseModel):
    format: str = "labtris-lab-v1"
    lab: dict[str, Any]
    geometry: dict[str, Any] = Field(default_factory=dict)


class TopologyImportIn(BaseModel):
    """A containerlab or EVE-NG topology, as text."""

    content: str
    filename: str | None = None
    name: str | None = None
    #: Force a parser instead of detecting one: "clab" or "unl".
    format: str | None = None
    #: clab only. The topology has already been deployed by containerlab
    #: inside this instance's namespace, so bind the imported nodes to the
    #: containers it started instead of starting new ones. The value is the
    #: `name:` from the .clab.yml, which is what clab prefixes its container
    #: names with.
    adopt_clab: str | None = None


class UnlImportIn(BaseModel):
    xml: str
    name: str | None = None


class SnapshotIn(BaseModel):
    name: str


class HostCreate(BaseModel):
    name: str
    endpoint: str
    token: str | None = None
    underlay_ip: str | None = None


class HostPatch(BaseModel):
    underlay_ip: str | None = None
    token: str | None = None


class HostOut(ORMModel):
    id: str
    name: str
    endpoint: str
    underlay_ip: str | None
    is_local: bool
    reachable: bool


class UplinkNic(BaseModel):
    """One host NIC to be enslaved into a labtris-managed uplink bridge."""

    nic: str


class UplinkBridgesIn(BaseModel):
    """The set of NICs the host should expose as uplink bridges.

    Full replacement, not an add: the list passed here is what the host
    ends up with. An empty list removes every uplink bridge. Bridge names
    are allocated server-side (br1, br2, br3, …) so two clients can't
    race on naming."""

    pairs: list[UplinkNic] = Field(default_factory=list)

    def model_post_init(self, _ctx: object) -> None:
        seen: set[str] = set()
        for entry in self.pairs:
            if not entry.nic:
                raise ValueError("nic name must not be empty")
            if entry.nic in seen:
                raise ValueError(f"nic {entry.nic!r} appears more than once")
            seen.add(entry.nic)


class ConsoleExecIn(BaseModel):
    """Run one command inside a running lab node and collect the output.

    Docker nodes run the command via `docker exec sh -c '...'` — normal
    exit code, stdout and stderr separated. QEMU nodes get a best-effort
    write to the serial console with a timeout wait for output — no exit
    code, no stderr, and no shell-prompt detection, which is called out in
    the response's `warnings` list."""

    command: str
    #: Wall-clock budget. Clamped [1, 30] so a stuck exec cannot pin the
    #: agent loop indefinitely — the agent has its own per-step timeout
    #: too, but layered defence is cheap and helps interactive callers.
    timeout_s: int = 10

    def model_post_init(self, _ctx: object) -> None:
        # An empty / whitespace-only command is legitimate on a QEMU
        # serial: `send("\n")` wakes a getty stuck at `login:`. Docker
        # gets `sh -c ""`, which is a fast no-op the model can retry
        # from. Refusing here used to block Ubuntu console logins for
        # the assistant.
        if self.timeout_s < 1:
            self.timeout_s = 1
        elif self.timeout_s > 30:
            self.timeout_s = 30


class ConsoleExecOut(BaseModel):
    stdout: str
    stderr: str = ""
    exit_code: int | None = None
    runtime: str
    warnings: list[str] = Field(default_factory=list)


class VncTypeIn(BaseModel):
    """Type text or send raw HMP `sendkey` combos into a QEMU node's display.

    Exactly one of `text` or `keys` is required. `text` treats input as
    ASCII characters and translates to per-key HMP sendkey combos.
    `keys` is a list of raw combos passed verbatim (`ctrl-alt-f2`,
    `esc`, `f1`, `ret`). Non-ASCII characters in `text` are silently
    skipped — the sendkey vocabulary is US-ASCII only."""

    text: str | None = None
    keys: list[str] | None = None
    #: 40 ms matches HMP's default hold. Bumped up if a guest misses
    #: keystrokes; the practical range is [10, 200].
    hold_ms: int = 40
    #: Grab a screenshot after the keystrokes land and return it in the
    #: response. On by default — the whole point of driving VNC is to
    #: watch what happened. Skip only when chaining a burst of keys where
    #: only the final frame matters.
    screenshot: bool = True
    #: Wait this many ms between the last keystroke and the screenshot.
    #: A typed password lands instantly; a menu selection may take a
    #: frame or two to redraw. Clamped [0, 5000].
    settle_ms: int = 250
    #: Scale the after-screenshot (default 0.5x). Same semantics as the
    #: `scale` query on `/vnc/screenshot`.
    scale: float = 0.5

    def model_post_init(self, _ctx: object) -> None:
        if (self.text is None) == (self.keys is None):
            raise ValueError("exactly one of `text` or `keys` must be set")
        if self.hold_ms < 10:
            self.hold_ms = 10
        elif self.hold_ms > 500:
            self.hold_ms = 500
        if self.settle_ms < 0:
            self.settle_ms = 0
        elif self.settle_ms > 5000:
            self.settle_ms = 5000
        if self.scale <= 0 or self.scale > 4:
            raise ValueError("scale must be in (0, 4]")


class VncTypeOut(BaseModel):
    #: Number of keystrokes / combos QEMU accepted. For `text` this may
    #: be less than `len(text)` if characters were skipped.
    keys_sent: int
    #: Base64-encoded PNG of the display after the keystrokes landed and
    #: `settle_ms` elapsed. `null` when the caller asked for
    #: `screenshot=false` (chained-key bursts).
    screenshot: str | None = None
    mime_type: str | None = None


class VncMouseIn(BaseModel):
    """Move the pointer, or move and click, on a QEMU node's display.

    Coordinates are in framebuffer pixels — 0 <= x < fb_width,
    0 <= y < fb_height. The dimensions are returned by the previous
    screenshot; a `vnc_mouse` call without an anchoring screenshot is
    guessing. `button` is `left` / `right` / `middle`. `action` is
    `move` (no click), `click`, or `double_click`."""

    x: int
    y: int
    action: str = "click"
    button: str = "left"
    #: Same shape as VncTypeIn — return a screenshot after so the caller
    #: can see what the click produced.
    screenshot: bool = True
    settle_ms: int = 400
    #: Scale the after-screenshot. See VncTypeIn.scale.
    scale: float = 0.5

    def model_post_init(self, _ctx: object) -> None:
        if self.action not in ("move", "click", "double_click"):
            raise ValueError("action must be 'move', 'click', or 'double_click'")
        if self.button not in ("left", "right", "middle"):
            raise ValueError("button must be 'left', 'right', or 'middle'")
        if self.x < 0 or self.y < 0:
            raise ValueError("coordinates must be non-negative")
        if self.settle_ms < 0:
            self.settle_ms = 0
        elif self.settle_ms > 5000:
            self.settle_ms = 5000
        if self.scale <= 0 or self.scale > 4:
            raise ValueError("scale must be in (0, 4]")


class VncWaitIn(BaseModel):
    """Poll the framebuffer until it stops changing, then return the final PNG.

    Replaces the 'screenshot in a loop' pattern the model falls into
    after a click: one call, one LLM turn. `timeout_ms` bounds how long
    the backend blocks; if the screen never settles the response
    carries `settled: false` and whatever the latest frame is.

    `stable_ms` — how long the frame must be identical before it counts
    as settled. 400 ms handles a menu redraw or a cursor blink cycle."""

    poll_ms: int = 200
    stable_ms: int = 400
    timeout_ms: int = 5000
    scale: float = 0.5

    def model_post_init(self, _ctx: object) -> None:
        if self.poll_ms < 50:
            self.poll_ms = 50
        elif self.poll_ms > 2000:
            self.poll_ms = 2000
        if self.stable_ms < self.poll_ms:
            self.stable_ms = self.poll_ms
        elif self.stable_ms > 10000:
            self.stable_ms = 10000
        if self.timeout_ms < 200:
            self.timeout_ms = 200
        elif self.timeout_ms > 60000:
            self.timeout_ms = 60000
        if self.scale <= 0 or self.scale > 4:
            raise ValueError("scale must be in (0, 4]")


class VncWaitOut(BaseModel):
    #: True if the frame stopped changing before timeout, false if not.
    settled: bool
    #: Base64-encoded PNG of the final frame, at the caller's `scale`.
    screenshot: str
    mime_type: str = "image/png"


class VncReadIn(BaseModel):
    """OCR the QEMU display and return only the text — no image.

    Cheap in vision tokens: a full-screen login prompt costs ~1500
    vision tokens as a PNG and ~15 tokens as text. Use this when you
    only need to READ what is on screen (which prompt, which error,
    which menu item is highlighted); use `vnc_screenshot` when you
    need to SEE it (a graph, a diagram, a colour, pixel positions).

    `region=x,y,w,h` crops before OCR (framebuffer pixels). Runs at
    full resolution so tesseract has enough pixels to be accurate."""

    region: str | None = None

    def model_post_init(self, _ctx: object) -> None:
        if self.region:
            try:
                parts = [int(p) for p in self.region.split(",")]
            except ValueError as exc:
                raise ValueError("region must be four comma-separated integers x,y,w,h") from exc
            if len(parts) != 4 or any(p < 0 for p in parts) or parts[2] <= 0 or parts[3] <= 0:
                raise ValueError("region must be x,y,w,h with w>0 and h>0")


class VncReadOut(BaseModel):
    #: OCR'd text, with the whitespace tesseract chose. Empty string if
    #: the screen is blank or the resolution is too low for OCR.
    text: str


class VncMouseOut(BaseModel):
    fb_width: int
    fb_height: int
    screenshot: str | None = None
    mime_type: str | None = None


class ImagePullIn(BaseModel):
    """Kick off a QEMU catalog image download.

    `image` is a catalog id (`ubuntu-24.04`), an https URL, or a
    `custom-<sha>` saved-image reference. The pull runs in the
    background; the response returns immediately with a status
    snapshot. Poll `/images/status` for progress."""

    image: str


class ImageStatusOut(BaseModel):
    """Snapshot of an image's download / prep state.

    `cached` is the only field that is always present — it says whether
    the qcow2 is on disk right now. The rest are populated while the
    download is running:

    - `phase`: "downloading" | "extracting" | "converting" | absent
      when idle
    - `done`: bytes downloaded so far
    - `total`: total bytes expected (0 when the server didn't return a
      Content-Length; extracting/converting stages have no total)
    - `percent`: 0..100 for downloading; null for other phases."""

    image: str
    cached: bool
    phase: str | None = None
    done: int | None = None
    total: int | None = None
    percent: int | None = None


class ManagementNetworkIn(BaseModel):
    """One request to reconfigure the host's own management NIC.

    Validated here so a malformed address never reaches netd; netd repeats
    the checks anyway (defence in depth), but returning a helpful pydantic
    error from the API layer is a better experience than netd's terse
    `EINVAL` bubbling up through a generic HTTP 400."""

    #: 'dhcp' is the installer default; 'manual' is what an admin picks
    #: when they want the address to stop moving.
    mode: str
    #: The interface that owns the management address. Either a NIC name
    #: (ens160) or a host bridge (br0 — for pre-bridged hosts). The `kind`
    #: field says which so we render into `ethernets:` or `bridges:`.
    interface: str = "ens160"
    #: 'nic' → the yaml lands under `ethernets:`; 'bridge' → under
    #: `bridges:`. A bridge is presumed already defined by another netplan
    #: file — we only set its address, not its members.
    kind: str = "nic"
    #: CIDR (address + prefix). Required and validated when mode='manual'.
    address: str | None = None
    #: Next-hop for the default route. Must be inside `address`'s subnet.
    gateway: str | None = None
    #: Upstream nameservers. Empty allowed on manual — the guest will still
    #: have a working default route.
    dns: list[str] = Field(default_factory=list)

    def model_post_init(self, _ctx: object) -> None:
        import ipaddress

        if self.mode not in ("dhcp", "manual"):
            raise ValueError("mode must be 'dhcp' or 'manual'")
        if self.mode == "manual":
            if not self.address or not self.gateway:
                raise ValueError("manual mode requires address and gateway")
            try:
                iface = ipaddress.ip_interface(self.address)
            except ValueError as exc:
                raise ValueError(f"malformed address {self.address!r}") from exc
            try:
                gw = ipaddress.ip_address(self.gateway)
            except ValueError as exc:
                raise ValueError(f"malformed gateway {self.gateway!r}") from exc
            if gw not in iface.network:
                raise ValueError(
                    f"gateway {self.gateway} is not inside {self.address}'s subnet"
                )
            for ns in self.dns:
                try:
                    ipaddress.ip_address(ns)
                except ValueError as exc:
                    raise ValueError(f"malformed DNS server {ns!r}") from exc
