from __future__ import annotations

from labtris_api.errors import unprocessable
from labtris_api.runtime.base import NodeRuntime
from labtris_api.runtime.docker import DockerRuntime
from labtris_api.runtime.qemu import QemuRuntime

_docker = DockerRuntime()
_qemu = QemuRuntime()
_RUNTIMES: dict[str, NodeRuntime] = {
    _docker.kind: _docker,
    _qemu.kind: _qemu,
}


def get_runtime(kind: str) -> NodeRuntime:
    runtime = _RUNTIMES.get(kind)
    if runtime is None:
        raise unprocessable(f"runtime {kind!r} is not available in this phase")
    return runtime
