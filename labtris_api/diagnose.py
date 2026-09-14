"""Everything you would otherwise ask someone to paste into a bug report.

Deliberately one flat document rather than a dozen endpoints: the point is
that a person having trouble can run one command, or open one tab, and have
the answer to "what is your setup" without a conversation. Nothing here is
allowed to fail the whole report — a section that cannot be read says so and
the rest still renders.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from labtris_api.config import settings


def _run(*cmd: str, timeout: float = 5.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        return (out.stdout or out.stderr).decode(errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text.strip() else ""


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("labtris")
    except Exception:  # noqa: BLE001 - running from a source tree is normal
        return "dev (not installed)"


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mem_mb() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                out[key] = int(rest.strip().split()[0]) // 1024
    except (OSError, ValueError):
        pass
    return out


def _loadavg() -> dict[str, float]:
    """The three load averages from /proc/loadavg.

    Load average is an approximation of "how busy the CPU has been" that
    is one syscall to read, unlike a real utilisation percentage which
    needs two samples of /proc/stat separated by time. The top-bar host
    meter divides `load1` by CPU cores to get a percent-shaped number —
    same convention `top` and `htop` use for that column."""
    out: dict[str, float] = {}
    try:
        parts = Path("/proc/loadavg").read_text().split()
        out["load1"] = float(parts[0])
        out["load5"] = float(parts[1])
        out["load15"] = float(parts[2])
    except (OSError, ValueError, IndexError):
        pass
    return out


def _disk(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
    except OSError:
        return {"path": str(path), "error": "unreadable"}
    return {
        "path": str(path),
        "total_gb": round(usage.total / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "used_pct": round(100 * usage.used / usage.total) if usage.total else 0,
    }


def _dir_size_gb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return round(total / 1e9, 2)


def _ksm() -> dict[str, Any]:
    """Kernel Samepage Merging, and how much it is actually saving.

    The ratio is the number worth looking at: it answers "can this box take
    another class?" far better than free memory does, because most of what
    twenty identical guests occupy is the same pages over and over.

    Off is reported as a warning rather than a fact — a stock Ubuntu ships it
    disabled, and a lab host running it that way is quietly wasting most of
    its RAM."""
    base = Path("/sys/kernel/mm/ksm")
    if not base.is_dir():
        return {"available": False, "note": "this kernel has no KSM support"}

    def read(name: str) -> int | None:
        try:
            return int((base / name).read_text().strip())
        except (OSError, ValueError):
            return None

    run = read("run")
    sharing, shared = read("pages_sharing"), read("pages_shared")
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4

    facts: dict[str, Any] = {
        "available": True,
        "enabled": run == 1,
        "pages_to_scan": read("pages_to_scan"),
        "sleep_millisecs": read("sleep_millisecs"),
        "smart_scan": read("smart_scan"),
        "saved_gb": round((sharing or 0) * page_kb / 1024 / 1024, 1),
    }
    if sharing and shared:
        facts["ratio"] = round(sharing / shared, 1)
    if run != 1:
        facts["note"] = (
            "KSM is OFF — a lab host wastes most of its memory this way. "
            "systemctl start labtris-ksm"
        )
    return facts


def host_facts() -> dict[str, Any]:
    """Things that need no network call and cannot fail."""
    vm_dir = Path(settings.qemu_vm_dir).expanduser()
    cache = Path(settings.qemu_image_cache_dir).expanduser()
    kvm = Path("/dev/kvm")
    return {
        "labtris_version": _version(),
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu": {"model": _cpu_model(), "cores": os.cpu_count()},
        "memory_mb": _mem_mb(),
        "loadavg": _loadavg(),
        "kvm": {
            "device": kvm.exists(),
            "writable": os.access(kvm, os.W_OK) if kvm.exists() else False,
            "note": (
                "hardware acceleration available"
                if kvm.exists() and os.access(kvm, os.W_OK)
                else "no /dev/kvm — QEMU nodes run under TCG emulation and boot slowly"
            ),
        },
        "qemu": _first_line(_run("qemu-system-x86_64", "--version")),
        "accel_setting": settings.qemu_accel,
        "disks": [_disk(vm_dir), _disk(cache)],
        "image_cache_gb": _dir_size_gb(cache),
        "vm_dir_gb": _dir_size_gb(vm_dir),
        "ksm": _ksm(),
        "wireshark": bool(shutil.which("wireshark")),
        "xvfb": bool(shutil.which("Xvfb")),
        "guacd": _first_line(_run("guacd", "-v")) or "not found on PATH",
    }


async def service_facts() -> dict[str, Any]:
    """Everything that needs to talk to something else."""
    out: dict[str, Any] = {}

    try:
        from labtris_api.netd_client import netd

        caps = await asyncio.wait_for(netd.call("host.capabilities", {}), timeout=8)
        out["netd"] = {"reachable": True, "socket": settings.netd_socket, **caps}
    except Exception as exc:  # noqa: BLE001 - "why is it down" is the answer wanted
        out["netd"] = {"reachable": False, "socket": settings.netd_socket, "error": str(exc)}

    try:
        import aiodocker

        docker = aiodocker.Docker()
        try:
            info = await asyncio.wait_for(docker.version(), timeout=8)
            out["docker"] = {"reachable": True, "version": info.get("Version")}
        finally:
            await docker.close()
    except Exception as exc:  # noqa: BLE001
        out["docker"] = {"reachable": False, "error": str(exc)}

    try:
        from sqlalchemy import func, select, text

        from labtris_api.db import SessionLocal
        from labtris_api.models import Lab, Network, Node

        async with SessionLocal() as session:
            revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            counts = {}
            for name, model in (("labs", Lab), ("nodes", Node), ("networks", Network)):
                counts[name] = (
                    await session.execute(select(func.count()).select_from(model))
                ).scalar_one()
            running = (
                await session.execute(
                    select(func.count()).select_from(Node).where(Node.state == "running")
                )
            ).scalar_one()
            failed = (
                await session.execute(
                    select(func.count()).select_from(Node).where(Node.state == "failed")
                )
            ).scalar_one()
        out["database"] = {
            "reachable": True,
            "migration": revision,
            **counts,
            "nodes_running": running,
            "nodes_failed": failed,
        }
    except Exception as exc:  # noqa: BLE001
        out["database"] = {"reachable": False, "error": str(exc)}

    return out


def _live_qemu() -> int:
    """QEMU processes that are actually running, whatever the database thinks."""
    vm_root = Path(settings.qemu_vm_dir).expanduser()
    if not vm_root.exists():
        return 0
    live = 0
    for pid_file in vm_root.glob("*/qemu.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            live += 1
        except (OSError, ValueError):
            continue
    return live


async def collect() -> dict[str, Any]:
    facts = host_facts()
    facts.update(await service_facts())
    facts["qemu_processes"] = _live_qemu()
    facts["logs"] = {
        "qemu": f"{settings.qemu_vm_dir}/<node-id>/qemu.log",
        "api": "wherever this process's stdout goes",
    }
    return facts


def render(facts: dict[str, Any]) -> str:
    """Plain text, because that is what gets pasted into an issue."""
    lines: list[str] = []

    def section(title: str) -> None:
        lines.append("")
        lines.append(title)
        lines.append("-" * len(title))

    def kv(key: str, value: Any) -> None:
        lines.append(f"  {key:<18} {value}")

    lines.append(f"labtris {facts.get('labtris_version')}")
    section("host")
    kv("platform", facts.get("platform"))
    kv("python", facts.get("python"))
    cpu = facts.get("cpu") or {}
    kv("cpu", f"{cpu.get('cores')} x {cpu.get('model')}")
    mem = facts.get("memory_mb") or {}
    if mem:
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        kv("memory", f"{total} MB total, {avail} MB available")
    kvm = facts.get("kvm") or {}
    kv("kvm", f"{kvm.get('note')}")
    kv("qemu", facts.get("qemu") or "not found")
    kv("accel setting", facts.get("accel_setting"))
    ksm = facts.get("ksm") or {}
    if not ksm.get("available"):
        kv("ksm", ksm.get("note", "unavailable"))
    elif ksm.get("enabled"):
        ratio = f"{ksm['ratio']}:1 dedup, {ksm['saved_gb']} GB saved" if ksm.get("ratio") else "on"
        kv("ksm", f"{ratio} (scan {ksm.get('pages_to_scan')}/{ksm.get('sleep_millisecs')}ms)")
    else:
        kv("ksm", ksm.get("note", "off"))
    kv("wireshark", "yes" if facts.get("wireshark") else "no")
    kv("guacd", facts.get("guacd"))

    section("storage")
    for d in facts.get("disks") or []:
        if "error" in d:
            kv(d["path"], d["error"])
        else:
            kv(d["path"], f"{d['free_gb']} GB free of {d['total_gb']} GB ({d['used_pct']}% used)")
    kv("image cache", f"{facts.get('image_cache_gb')} GB")
    kv("vm overlays", f"{facts.get('vm_dir_gb')} GB")

    section("services")
    netd = facts.get("netd") or {}
    kv("netd", "reachable" if netd.get("reachable") else f"DOWN — {netd.get('error')}")
    if netd.get("reachable"):
        links = (netd.get("links") or {}) if isinstance(netd.get("links"), dict) else {}
        if links:
            missing = [k for k, v in links.items() if not v]
            kv(
                "  link types",
                "all supported" if not missing else f"unsupported: {', '.join(missing)}",
            )
    docker = facts.get("docker") or {}
    kv(
        "docker",
        docker.get("version") if docker.get("reachable") else f"DOWN — {docker.get('error')}",
    )
    db = facts.get("database") or {}
    if db.get("reachable"):
        kv("database", f"migration {db.get('migration')}")
        kv(
            "  contents",
            f"{db.get('labs')} labs, {db.get('nodes')} nodes, {db.get('networks')} networks",
        )
        kv("  node states", f"{db.get('nodes_running')} running, {db.get('nodes_failed')} failed")
        # A database that disagrees with the process table is the first thing
        # worth knowing: it means a start or a stop did not finish, and every
        # console and capture built on that state will behave oddly.
        live = facts.get("qemu_processes")
        qemu_nodes = db.get("nodes_running")
        if live is not None and qemu_nodes is not None and live != qemu_nodes:
            kv(
                "  DRIFT",
                f"{live} qemu process(es) alive but {qemu_nodes} node(s) marked running"
                " — some include non-qemu nodes, but a large gap means stale state",
            )
    else:
        kv("database", f"DOWN — {db.get('error')}")

    section("logs")
    for k, v in (facts.get("logs") or {}).items():
        kv(k, v)
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """`labtris-doctor` — the one command to run before asking for help."""
    print(render(asyncio.run(collect())))
