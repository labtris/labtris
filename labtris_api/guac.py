from __future__ import annotations

import asyncio
import codecs

from labtris_api.errors import runtime_error

GUACD_HOST = "127.0.0.1"
GUACD_PORT = 4822

# Highest protocol version we know how to speak. guacd announces its own as the
# first element of `args`; we negotiate down to whichever is lower.
CLIENT_VERSION = "VERSION_1_5_0"
_VERSIONS = [
    "VERSION_1_0_0",
    "VERSION_1_1_0",
    "VERSION_1_3_0",
    "VERSION_1_5_0",
]

# Image formats guacd may encode frames as. Sending an `image` instruction is
# optional, but without it guacd assumes PNG-only and skips JPEG/WebP entirely,
# which makes a full-desktop VNC session crawl.
IMAGE_MIMETYPES = ["image/png", "image/jpeg", "image/webp"]


def encode_instruction(*parts: str) -> str:
    """One Guacamole protocol instruction: comma-separated
    `<length>.<value>` elements, terminated with `;`.

    `<length>` counts *characters*, not bytes — libguac writes it with
    `guac_utf8_strlen()` and guacamole-common-js reads it with JavaScript
    string indexing. Sending a UTF-8 byte count desynchronises the parser on
    the first non-ASCII character (a clipboard paste, a window title)."""
    return ",".join(f"{len(part)}.{part}" for part in parts) + ";"


class GuacSocket:
    """Framed Guacamole protocol socket over an asyncio stream.

    guacd's TCP writes don't align with instruction boundaries, so reads are
    buffered and decoded incrementally: a chunk may end in the middle of a
    multi-byte character *and* in the middle of an instruction."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buf = ""
        self._pos = 0

    async def _fill(self) -> None:
        while True:
            data = await self._reader.read(65536)
            if not data:
                raise asyncio.IncompleteReadError(b"", None)
            text = self._decoder.decode(data)
            if text:
                self._buf = self._buf[self._pos :] + text
                self._pos = 0
                return

    async def _take(self, count: int) -> str:
        out: list[str] = []
        while count:
            if self._pos >= len(self._buf):
                await self._fill()
            chunk = min(count, len(self._buf) - self._pos)
            out.append(self._buf[self._pos : self._pos + chunk])
            self._pos += chunk
            count -= chunk
        return "".join(out)

    async def read(self) -> list[str]:
        """Read exactly one instruction and return its elements."""
        elements: list[str] = []
        while True:
            digits = ""
            while True:
                char = await self._take(1)
                if char == ".":
                    break
                if not char.isdigit():
                    raise runtime_error(
                        f"malformed guacamole instruction length near {digits + char!r}"
                    )
                digits += char
            elements.append(await self._take(int(digits)))
            sep = await self._take(1)
            if sep == ";":
                return elements
            if sep != ",":
                raise runtime_error(f"malformed guacamole instruction separator {sep!r}")

    async def send(self, *parts: str) -> None:
        await self.send_raw(encode_instruction(*parts))

    async def send_raw(self, text: str) -> None:
        self._writer.write(text.encode("utf-8"))
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()


def negotiate_version(guacd_version: str) -> str:
    """Both ends speak the lower of the two versions. A version string we
    don't know is a *newer* guacd (the older ones sent no version at all), so
    offer our own and let guacd cap it — libguac already treats anything it
    doesn't recognise as 1.0.0."""
    if guacd_version not in _VERSIONS:
        return CLIENT_VERSION
    return min(guacd_version, CLIENT_VERSION, key=_VERSIONS.index)


async def connect_guacd(
    protocol: str,
    settings: dict[str, str],
    *,
    width: int = 1024,
    height: int = 768,
    dpi: int = 96,
    timezone: str | None = None,
) -> tuple[GuacSocket, str]:
    """Do the full client half of the Guacamole handshake with guacd
    (`select` -> `args` -> `size`/`audio`/`video`/`image`/`timezone` ->
    `connect` -> `ready`) and hand back the still-open socket for passthrough.

    The version element matters: `args` is `args,VERSION_x_y_z,<param>,...`,
    and libguac's `guac_user_handle_connection()` unconditionally drops the
    *first* element of `connect` as the client's version before handing the
    rest to the protocol's argument parser. Get that wrong and the argument
    count is off by one, the join handler bails — and because guacd sends
    `ready` *before* it attempts the join, the browser is left holding a
    connection that will never produce a frame."""
    try:
        reader, writer = await asyncio.open_connection(GUACD_HOST, GUACD_PORT)
    except OSError as exc:
        raise runtime_error(f"guacd unavailable at {GUACD_HOST}:{GUACD_PORT}: {exc}") from exc

    sock = GuacSocket(reader, writer)
    await sock.send("select", protocol)
    args_insn = await sock.read()
    if not args_insn or args_insn[0] != "args":
        raise runtime_error(f"guacd handshake failed: expected args, got {args_insn}")

    rest = args_insn[1:]
    if rest and rest[0].startswith("VERSION_"):
        version = negotiate_version(rest[0])
        param_names = rest[1:]
    else:  # pre-1.1 guacd: no version element, and no version in `connect`
        version = ""
        param_names = rest

    await sock.send("size", str(width), str(height), str(dpi))
    await sock.send("audio")
    await sock.send("video")
    await sock.send("image", *IMAGE_MIMETYPES)
    # `timezone` only exists from 1.1.0 on; an older guacd logs it as an
    # unexpected handshake instruction and ignores it.
    if timezone and version and _VERSIONS.index(version) >= _VERSIONS.index("VERSION_1_1_0"):
        await sock.send("timezone", timezone)

    values = [settings.get(name, "") for name in param_names]
    await sock.send("connect", *([version, *values] if version else values))

    ready_insn = await sock.read()
    if not ready_insn or ready_insn[0] != "ready":
        raise runtime_error(f"guacd handshake failed: expected ready, got {ready_insn}")
    return sock, ready_insn[1] if len(ready_insn) > 1 else ""
