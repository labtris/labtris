"""In-memory framebuffer cache backed by a persistent RFB client per VM.

Screenshot latency without this: ~250-300 ms per call — HMP round trip,
`screendump` synchronous render to disk, PNG read, unlink. Every call
pays it, even if the frame did not change since the last one.

With this: one background task per VM speaks RFB to QEMU's own VNC port
(`-vnc 127.0.0.1:N`), keeps the latest framebuffer in memory, and every
screenshot serves from that memory in ~1 ms. QEMU only pushes updates
for rectangles that actually changed, so a static console costs
virtually nothing over the wire.

Kept intentionally minimal: Raw encoding only (QEMU supports it
natively, every VNC server does), no auth (Labtris's own -vnc is
bound to 127.0.0.1 and share=force-shared, no password), no cursor
pseudo-encoding, no clipboard. If any part misbehaves, `screenshot()`
raises and the caller falls back to `screendump_png`.

Protocol reference: RFC 6143.
"""

from __future__ import annotations

import asyncio
import struct
from io import BytesIO
from pathlib import Path
from typing import Any

from labtris_api.errors import runtime_error


# RFB message types (server → client)
_MSG_FRAMEBUFFER_UPDATE = 0
_MSG_SET_COLOUR_MAP_ENTRIES = 1
_MSG_BELL = 2
_MSG_SERVER_CUT_TEXT = 3


class VncCache:
    """One persistent RFB client per VM. Screenshots read from memory."""

    def __init__(self, host: str, port: int, node_id: str) -> None:
        self.host = host
        self.port = port
        self.node_id = node_id
        self.width = 0
        self.height = 0
        self._fb: bytearray = bytearray()  # BGRA 32bpp, row-major
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        # Set when the first FramebufferUpdate has been fully applied.
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Connect, handshake, request the initial full frame, spawn the read
        task. Blocks until the first frame lands (or fails)."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=3.0,
            )
            await self._handshake()
        except Exception as exc:
            self._error = exc
            self._close_writer()
            raise
        self._task = asyncio.create_task(self._read_loop(), name=f"vnc-cache-{self.node_id}")
        # First frame — waited on before returning so screenshot() has data.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise runtime_error("RFB client did not receive an initial frame in 5s") from exc

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._close_writer()

    def _close_writer(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

    async def _handshake(self) -> None:
        assert self._reader is not None and self._writer is not None
        # ProtocolVersion — server sends "RFB xxx.yyy\n"; we reply with our
        # supported version. 003.008 is the modern one QEMU speaks.
        version = await self._reader.readexactly(12)
        if not version.startswith(b"RFB "):
            raise runtime_error(f"unexpected RFB greeting: {version!r}")
        self._writer.write(b"RFB 003.008\n")
        await self._writer.drain()

        # SecurityTypes — server sends N followed by N bytes, one per type.
        # We accept None (1). QEMU with no password offers exactly [None].
        n_types = struct.unpack("!B", await self._reader.readexactly(1))[0]
        if n_types == 0:
            # Server refuses connection; a length-prefixed reason follows.
            rlen = struct.unpack("!I", await self._reader.readexactly(4))[0]
            reason = (await self._reader.readexactly(rlen)).decode(errors="replace")
            raise runtime_error(f"RFB server refused connection: {reason}")
        types = await self._reader.readexactly(n_types)
        if 1 not in types:
            raise runtime_error(
                f"RFB server does not offer None security (offered {list(types)}); "
                "Labtris expects the -vnc socket to be passwordless."
            )
        self._writer.write(b"\x01")  # None
        await self._writer.drain()

        # SecurityResult — u32, 0 = OK.
        result = struct.unpack("!I", await self._reader.readexactly(4))[0]
        if result != 0:
            raise runtime_error(f"RFB SecurityResult = {result}")

        # ClientInit — one byte: shared. share=force-shared on our side,
        # but this must still be 1 for QEMU to let us in alongside a
        # Guacamole viewer.
        self._writer.write(b"\x01")
        await self._writer.drain()

        # ServerInit — width, height, pixel format (16 bytes), name length,
        # name. We ignore name; we override pixel format below.
        header = await self._reader.readexactly(24)
        self.width, self.height = struct.unpack("!HH", header[:4])
        # pixel_format = header[4:20]  # we're about to overwrite it
        name_len = struct.unpack("!I", header[20:24])[0]
        await self._reader.readexactly(name_len)  # discard name

        # SetPixelFormat — 32bpp BGRA, big-endian off, true-colour, all
        # masks byte-aligned. Pillow understands BGRA out of the box.
        self._writer.write(b"\x00\x00\x00\x00")  # msg type + 3 padding
        self._writer.write(
            struct.pack(
                "!BBBBHHHBBB",
                32,     # bits-per-pixel
                24,     # depth
                0,      # big-endian-flag
                1,      # true-colour-flag
                255,    # red-max
                255,    # green-max
                255,    # blue-max
                16,     # red-shift  (matches BGRA in little-endian layout)
                8,      # green-shift
                0,      # blue-shift
            )
        )
        self._writer.write(b"\x00\x00\x00")  # padding to 16 bytes
        await self._writer.drain()

        # SetEncodings — Raw only (0). QEMU always supports it.
        self._writer.write(struct.pack("!BBHi", 2, 0, 1, 0))
        await self._writer.drain()

        self._fb = bytearray(self.width * self.height * 4)
        await self._request_update(incremental=False)

    async def _request_update(self, *, incremental: bool) -> None:
        assert self._writer is not None
        # FramebufferUpdateRequest: type=3, incremental, x, y, w, h
        self._writer.write(
            struct.pack("!BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height)
        )
        await self._writer.drain()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                msg_type = (await self._reader.readexactly(1))[0]
                if msg_type == _MSG_FRAMEBUFFER_UPDATE:
                    await self._handle_update()
                elif msg_type == _MSG_BELL:
                    pass
                elif msg_type == _MSG_SERVER_CUT_TEXT:
                    # 3 bytes padding + u32 length + text — discard.
                    await self._reader.readexactly(3)
                    tlen = struct.unpack("!I", await self._reader.readexactly(4))[0]
                    await self._reader.readexactly(tlen)
                elif msg_type == _MSG_SET_COLOUR_MAP_ENTRIES:
                    # Only sent for indexed pixel formats; we asked for
                    # true-colour, so treat as protocol error.
                    raise runtime_error(
                        "RFB server sent SetColourMapEntries — pixel format "
                        "was not accepted"
                    )
                else:
                    raise runtime_error(f"unknown RFB message type {msg_type}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any I/O or protocol error kills the client
            self._error = exc
            self._close_writer()

    async def _handle_update(self) -> None:
        assert self._reader is not None
        # 1 byte padding + u16 n_rects
        pad_n = await self._reader.readexactly(3)
        n_rects = struct.unpack("!H", pad_n[1:3])[0]
        for _ in range(n_rects):
            x, y, w, h = struct.unpack("!HHHH", await self._reader.readexactly(8))
            encoding = struct.unpack("!i", await self._reader.readexactly(4))[0]
            if encoding == 0:  # Raw
                await self._paste_rect(x, y, w, h)
            elif encoding == -223:  # DesktopSize pseudo-encoding
                # Resize can arrive if the guest changes resolution. Grow
                # the framebuffer and request a full refresh next round.
                self.width, self.height = w, h
                self._fb = bytearray(w * h * 4)
                await self._request_update(incremental=False)
            else:
                raise runtime_error(
                    f"RFB rect at ({x},{y}) uses encoding {encoding}; only "
                    "Raw (0) is supported"
                )
        # Ready to render.
        self._ready.set()
        # Ask for the next set of changed rectangles.
        await self._request_update(incremental=True)

    async def _paste_rect(self, x: int, y: int, w: int, h: int) -> None:
        assert self._reader is not None
        data = await self._reader.readexactly(w * h * 4)
        stride = self.width * 4
        for row in range(h):
            src = row * w * 4
            dst = (y + row) * stride + x * 4
            self._fb[dst : dst + w * 4] = data[src : src + w * 4]

    async def screenshot_png(self) -> bytes:
        """Return the current framebuffer as PNG bytes."""
        if self._error is not None:
            raise self._error
        if not self._ready.is_set():
            raise runtime_error("RFB client has no framebuffer yet")
        async with self._lock:
            from PIL import Image

            # 32bpp on the wire, byte order B G R X where X is the fourth
            # byte at the alpha position. RFB does not guarantee anything
            # about that byte when depth<bpp (here depth=24, bpp=32), so
            # we ignore it entirely — reading it as alpha gave a fully
            # transparent framebuffer that Pillow saved as an all-A=0 PNG
            # (renders as white on any viewer background).
            img = Image.frombytes(
                "RGB", (self.width, self.height), bytes(self._fb), "raw", "BGRX"
            )
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()


# One client per VM, keyed by the VM's directory (matches how other
# per-VM state is keyed in qemu.py). Nothing here needs to survive a
# restart of the API — first screenshot after a restart just reconnects.
_clients: dict[Path, VncCache] = {}
_locks: dict[Path, asyncio.Lock] = {}


def _lock_for(vm_dir: Path) -> asyncio.Lock:
    lock = _locks.get(vm_dir)
    if lock is None:
        lock = asyncio.Lock()
        _locks[vm_dir] = lock
    return lock


async def get_cached_client(vm_dir: Path, port: int, node_id: str) -> VncCache:
    """Return a live cache for this VM, opening one on first use.

    Idempotent: a second caller for the same VM gets the same client.
    On error the client is torn down and the next call reopens.
    """
    lock = _lock_for(vm_dir)
    async with lock:
        client = _clients.get(vm_dir)
        if client is not None and client._error is None:
            return client
        # Fell out from under us or first call — build fresh.
        if client is not None:
            await client.stop()
        client = VncCache("127.0.0.1", port, node_id)
        await client.start()
        _clients[vm_dir] = client
        return client


async def drop_cached_client(vm_dir: Path) -> None:
    """Stop and forget the cache for this VM (call on node stop)."""
    client = _clients.pop(vm_dir, None)
    if client is not None:
        await client.stop()
