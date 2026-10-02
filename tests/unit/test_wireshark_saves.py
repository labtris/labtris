"""Captures saved inside a Wireshark session, and getting them to the user.

Wireshark runs on the lab host, so File → Save As writes to the server. These
cover the two things that decide whether that is useful or dangerous: only
finished files are offered, and a name from the client cannot walk out of the
session's own directory.
"""

from __future__ import annotations

import os
import time

import pytest

from labtris_api import wireshark
from labtris_api.errors import ApiError


@pytest.fixture(autouse=True)
def _isolated_save_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        wireshark.settings, "wireshark_save_dir", str(tmp_path), raising=False
    )


def test_a_capture_still_being_written_is_not_offered() -> None:
    """A pcap handed over mid-save downloads truncated, and reads as a
    corrupt capture rather than as the partial file it is."""
    d = wireshark.save_dir("iface-01TEST")
    f = d / "fresh.pcap"
    f.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 64)

    assert wireshark.saved_files("iface-01TEST") == [], "offered a file still settling"

    # Backdate it past the settle window and it becomes available.
    old = time.time() - (wireshark._SETTLE_SECONDS + 1)
    os.utime(f, (old, old))
    names = [e["name"] for e in wireshark.saved_files("iface-01TEST")]
    assert names == ["fresh.pcap"]


def test_an_empty_file_is_not_offered() -> None:
    """Wireshark creates the file before it has written anything to it."""
    d = wireshark.save_dir("iface-01TEST")
    f = d / "empty.pcap"
    f.touch()
    old = time.time() - 60
    os.utime(f, (old, old))
    assert wireshark.saved_files("iface-01TEST") == []


def test_a_name_cannot_escape_the_session_directory(tmp_path) -> None:
    """The name comes from the client. Resolving it and checking the parent is
    what stops `../../etc/passwd` being a download link."""
    wireshark.save_dir("iface-01TEST")
    secret = tmp_path / "other.pcap"
    secret.write_bytes(b"not yours")

    for attempt in ("../other.pcap", "../../etc/passwd", "sub/../../other.pcap"):
        with pytest.raises(ApiError):
            wireshark.saved_file("iface-01TEST", attempt)


def test_a_session_id_cannot_escape_either() -> None:
    """save_dir() builds a path from the id, so the same applies one level up."""
    for bad in ("../elsewhere", "a/b", "", ".hidden", "/abs"):
        with pytest.raises(ApiError):
            wireshark.save_dir(bad)


def test_a_real_name_resolves_inside_the_session() -> None:
    d = wireshark.save_dir("iface-01TEST")
    (d / "capture.pcapng").write_bytes(b"\x0a\x0d\x0d\x0a" + b"\x00" * 32)
    got = wireshark.saved_file("iface-01TEST", "capture.pcapng")
    assert got.parent == d.resolve()
    assert got.read_bytes().startswith(b"\x0a\x0d\x0d\x0a")


def test_files_come_back_oldest_first() -> None:
    """A client downloading everything it has not seen gets them in the order
    they were made."""
    d = wireshark.save_dir("iface-01TEST")
    base = time.time() - 600
    for i, name in enumerate(["third.pcap", "first.pcap", "second.pcap"]):
        f = d / name
        f.write_bytes(b"\x00" * 32)
        stamp = base + {"first.pcap": 0, "second.pcap": 10, "third.pcap": 20}[name]
        os.utime(f, (stamp, stamp))
    names = [e["name"] for e in wireshark.saved_files("iface-01TEST")]
    assert names == ["first.pcap", "second.pcap", "third.pcap"]
