from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


class Capability(StrEnum):
    SUSPEND = "suspend"
    SNAPSHOT = "snapshot"
    HOTPLUG_NIC = "hotplug_nic"
    EXEC = "exec"
    SERIAL = "serial"


class StopMode(StrEnum):
    GRACEFUL = "graceful"
    FORCE = "force"


@dataclass(frozen=True)
class RuntimeHandle:
    node_id: str
    ref: str
    pid: int | None = None


@dataclass(frozen=True)
class RuntimeState:
    exists: bool
    running: bool
    pid: int | None
    exit_code: int | None
    detail: str = ""


@dataclass(frozen=True)
class ConsoleEndpoint:
    kind: Literal["exec", "serial", "vnc", "telnet"]
    target: str
    meta: dict[str, str]


@dataclass(frozen=True)
class IfaceSpec:
    iface_id: str
    idx: int
    guest_name: str
    host_ifname: str
    mac: str
    peer_ifname: str | None = None
    bridge: str | None = None


@dataclass(frozen=True)
class NodeSpec:
    """Everything a runtime needs. No ORM objects cross this boundary."""

    node_id: str
    lab_id: str
    name: str
    image: str
    env: dict[str, str]
    cmd: list[str] | None
    cpu_limit: float | None
    ram_mb: int | None
    #: Emulated NIC model for hypervisor runtimes; None takes the image default.
    nic_model: str | None
    #: Disk controller the guest kernel expects — virtio for most modern
    #: Linuxes, sata for NX-OSv 9000 and some vendor appliances, ide for
    #: guests without virtio-blk. None means "use the runtime's fallback"
    #: (built-in catalog for a catalog id, "virtio" otherwise).
    disk_bus: str | None = None
    #: True when the guest's real console is its framebuffer, not a serial
    #: port. Comes from the template for saved images; None falls back to
    #: the catalog. Wrong here means opening a serial console on a desktop
    #: guest returns silence.
    graphical: bool | None = None
    #: Path to a companion BIOS file. If set, qemu is invoked with
    #: `-bios <path>`. NX-OSv 9000 needs EVE's OVMF-sata.fd; other
    #: appliances (vjunosevoefi) also point here.
    bios: str | None = None
    #: Path to a companion CD-ROM ISO attached at every boot as `-cdrom`.
    #: NX-OSv 9000 ships its initial config schema this way (1.3 GB
    #: cdrom.iso); Cisco cat9kv/uccx follow the same pattern.
    cdrom: str | None = None
    #: Extra qemu args appended verbatim. Per-template (admin-authored
    #: at template registration), NOT per-node — the per-node
    #: qemu_opts.extra_args stays gated behind settings.qemu_allow_extra_args.
    #: Trust model: same as picking the disk image itself.
    extra_args: list[str] = field(default_factory=list)
    interfaces: list[IfaceSpec] = field(default_factory=list)
    #: Hypervisor-level options: chipset, acceleration, boot order, whether
    #: this node keeps a data volume. Ignored by runtimes that have no machine
    #: to configure.
    qemu_opts: dict[str, Any] = field(default_factory=dict)


class NodeRuntime(Protocol):
    kind: str
    capabilities: frozenset[Capability]

    async def create(self, spec: NodeSpec) -> RuntimeHandle: ...
    async def start(self, h: RuntimeHandle) -> None: ...
    async def stop(self, h: RuntimeHandle, mode: StopMode) -> None: ...
    async def destroy(self, h: RuntimeHandle) -> None: ...
    async def attach_iface(self, h: RuntimeHandle, i: IfaceSpec, bridge: str) -> None: ...
    async def detach_iface(self, h: RuntimeHandle, i: IfaceSpec) -> None: ...
    async def sync_interfaces(self, h: RuntimeHandle, ifaces: list[IfaceSpec]) -> None: ...
    async def set_guest_link(self, h: RuntimeHandle, i: IfaceSpec, up: bool) -> None: ...
    async def console(self, h: RuntimeHandle) -> ConsoleEndpoint: ...
    async def observe(self, h: RuntimeHandle) -> RuntimeState: ...
    async def suspend(self, h: RuntimeHandle) -> None: ...
    async def resume(self, h: RuntimeHandle) -> None: ...
    async def logs(self, h: RuntimeHandle, lines: int) -> str: ...
    async def write_file(self, h: RuntimeHandle, path: str, content: str) -> None: ...
    async def save_snapshot(self, h: RuntimeHandle, name: str) -> None: ...
    async def load_snapshot(self, h: RuntimeHandle, name: str) -> None: ...
    async def list_snapshots(self, h: RuntimeHandle) -> list[str]: ...
