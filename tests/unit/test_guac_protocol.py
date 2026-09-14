from __future__ import annotations

import asyncio

import pytest

from labtris_api.errors import ApiError
from labtris_api.guac import GuacSocket, encode_instruction, negotiate_version


def _socket(payload: bytes, chunk: int = 4096) -> GuacSocket:
    reader = asyncio.StreamReader()
    for i in range(0, len(payload), chunk):
        reader.feed_data(payload[i : i + chunk])
    reader.feed_eof()
    return GuacSocket(reader, None)  # type: ignore[arg-type]


def test_lengths_count_characters_not_bytes() -> None:
    """libguac writes the length with guac_utf8_strlen() and
    guacamole-common-js reads it with JavaScript string indexing — both count
    characters. A UTF-8 byte count desyncs the parser on the first accented
    character in a clipboard paste."""
    assert encode_instruction("clipboard", "héllo") == "9.clipboard,5.héllo;"


async def test_reads_an_instruction_split_across_chunks() -> None:
    payload = encode_instruction("size", "1024", "768").encode()
    assert await _socket(payload, chunk=1).read() == ["size", "1024", "768"]


async def test_reads_multibyte_values_split_mid_character() -> None:
    """A one-byte-at-a-time feed puts a chunk boundary inside the two bytes of
    'é' — the incremental decoder has to hold it rather than fail."""
    payload = encode_instruction("name", "café ☕").encode()
    assert await _socket(payload, chunk=1).read() == ["name", "café ☕"]


async def test_reads_back_to_back_instructions_from_one_chunk() -> None:
    payload = (encode_instruction("ready", "$abc") + encode_instruction("sync", "42")).encode()
    sock = _socket(payload)
    assert await sock.read() == ["ready", "$abc"]
    assert await sock.read() == ["sync", "42"]


async def test_rejects_a_malformed_length() -> None:
    with pytest.raises(ApiError):
        await _socket(b"4x.args;").read()


async def test_signals_eof_rather_than_hanging() -> None:
    with pytest.raises(asyncio.IncompleteReadError):
        await _socket(b"").read()


def test_version_negotiation_takes_the_lower_of_the_two() -> None:
    assert negotiate_version("VERSION_1_1_0") == "VERSION_1_1_0"
    assert negotiate_version("VERSION_1_5_0") == "VERSION_1_5_0"
    # Newer than anything we know: offer ours and let guacd cap it.
    assert negotiate_version("VERSION_9_9_9") == "VERSION_1_5_0"
