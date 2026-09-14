from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
from dataclasses import dataclass, field

from labtris_api.errors import runtime_error, unprocessable

#: Each session is a whole X server plus a Wireshark process — on the order of
#: 200-300 MB. This is a lab tool, not a tenant service; the cap exists so a
#: stuck browser tab cannot quietly consume the host.
MAX_SESSIONS = 4

#: Displays live in their own range so they cannot collide with the VNC
#: displays QEMU hands out (which start at :1 and are counted from 5900).
DISPLAY_BASE = 60
GEOMETRY = "1440x900x24"


@dataclass
class Session:
    id: str
    ifname: str
    display: int
    vnc_port: int
    procs: list[asyncio.subprocess.Process] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"wireshark on {self.ifname}"


_sessions: dict[str, Session] = {}


def get(session_id: str) -> Session | None:
    return _sessions.get(session_id)


def _free_display() -> tuple[int, int]:
    """A display number whose X socket and VNC port are both unused."""
    for n in range(DISPLAY_BASE, DISPLAY_BASE + 40):
        if os.path.exists(f"/tmp/.X11-unix/X{n}"):
            continue
        port = 5900 + n
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return n, port
    raise runtime_error("no free X display for a Wireshark session")


def _require_tools() -> None:
    missing = [t for t in ("Xvfb", "wireshark", "x11vnc") if shutil.which(t) is None]
    if missing:
        raise unprocessable(
            "Wireshark sessions need " + ", ".join(missing) + " on the lab host: "
            "`apt install wireshark xvfb x11vnc`, then `dpkg-reconfigure wireshark-common` "
            "so dumpcap may capture without root."
        )


async def _spawn(*args: str, env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        env={**os.environ, **(env or {})},
        start_new_session=True,
    )


async def start(session_id: str, ifname: str) -> Session:
    """Run the real Wireshark against a lab interface, on a display of its own.

    Not a reimplementation of Wireshark in the browser and not a pcap
    download: the actual binary, with its dissectors, filter bar and follow-
    stream, driven live. It renders to a headless X server which x11vnc
    exports on loopback, and the existing guacd tunnel carries that to the
    page — the same path the QEMU consoles already use."""
    existing = _sessions.get(session_id)
    if existing is not None:
        return existing
    _require_tools()
    if len(_sessions) >= MAX_SESSIONS:
        raise unprocessable(
            f"{MAX_SESSIONS} Wireshark sessions are already open; each is a whole X "
            "server and Wireshark process. Close one first."
        )

    display, port = _free_display()
    session = Session(id=session_id, ifname=ifname, display=display, vnc_port=port)
    try:
        session.procs.append(
            await _spawn("Xvfb", f":{display}", "-screen", "0", GEOMETRY, "-nolisten", "tcp")
        )
        for _ in range(50):
            if os.path.exists(f"/tmp/.X11-unix/X{display}"):
                break
            await asyncio.sleep(0.1)
        else:
            raise runtime_error("Xvfb did not come up")

        # dumpcap carries cap_net_raw for the `wireshark` group, which this
        # process is not in until a fresh login — sg gives us the group without
        # needing one, and without running any of it as root.
        cmd = (
            f"wireshark -i {ifname} -k "
            f"-o gui.window_title:'labtris {ifname}' "
            "-o capture.no_interface_load:TRUE"
        )
        session.procs.append(
            await _spawn("sg", "wireshark", "-c", cmd, env={"DISPLAY": f":{display}"})
        )
        session.procs.append(
            await _spawn(
                "x11vnc",
                "-display", f":{display}",
                "-rfbport", str(port),
                # Loopback only: guacd reaches it, nothing else can.
                "-localhost",
                "-nopw",
                "-forever",
                "-shared",
                "-noxdamage",
                "-quiet",
            )
        )
        for _ in range(60):
            with contextlib.suppress(OSError), socket.socket() as probe:
                probe.settimeout(0.5)
                probe.connect(("127.0.0.1", port))
                break
            await asyncio.sleep(0.2)
        else:
            raise runtime_error("x11vnc did not start listening")
    except Exception:
        await _kill(session)
        raise

    _sessions[session_id] = session
    return session


async def _kill(session: Session) -> None:
    for proc in reversed(session.procs):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    for proc in session.procs:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
    session.procs.clear()


async def stop(session_id: str) -> bool:
    session = _sessions.pop(session_id, None)
    if session is None:
        return False
    await _kill(session)
    return True


async def stop_all() -> None:
    """Nothing else reaps these — they are grandchildren in their own session,
    which is what keeps them alive across an API reload and what makes them
    leak if we do not clean up on shutdown."""
    for session_id in list(_sessions):
        await stop(session_id)


def listing() -> list[dict[str, object]]:
    return [
        {"id": s.id, "ifname": s.ifname, "display": s.display, "vnc_port": s.vnc_port}
        for s in _sessions.values()
    ]
