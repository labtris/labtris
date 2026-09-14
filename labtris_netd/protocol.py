from __future__ import annotations

import json
import re
from typing import Any

# Both shapes the API produces: the original kind-letter-plus-eight, and the
# readable form whose mandatory four-character suffix is what marks it ours.
# This is the gate that keeps netd away from docker0 and the host's own NICs,
# so it stays tight enough that no ordinary interface name can satisfy it.
IFNAME_RE = re.compile(r"^(t|b|v|x)([0-9a-z]{8}|[0-9a-z]{1,9}-[0-9a-z]{4})$")


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def decode_line(line: bytes) -> dict[str, Any]:
    text = line.decode("utf-8").strip()
    if not text:
        raise ValueError("empty frame")
    msg = json.loads(text)
    if not isinstance(msg, dict):
        raise ValueError("frame must be a JSON object")
    return msg


class FrameDecoder:
    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buf += data
        frames: list[dict[str, Any]] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw, self._buf = self._buf[:idx], self._buf[idx + 1 :]
            if not raw.strip():
                continue
            frames.append(decode_line(raw + b"\n"))
        return frames
