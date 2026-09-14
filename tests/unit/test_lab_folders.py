"""Folders, moves and clones — the organisational half of a lab's life.

The path normaliser gets the most attention here because it is the only thing
standing between a typed string and two folders that look identical in a list
and sort apart. The clone tests care about what must *not* be copied.
"""

from __future__ import annotations

import pytest

from labtris_api.naming import normalise_folder


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CCNA/Week 1", "CCNA/Week 1"),
        # The four ways a hand-typed path arrives wrong, all collapsing to one
        # value — otherwise the same folder appears twice in the picker.
        ("/CCNA/Week 1", "CCNA/Week 1"),
        ("CCNA/Week 1/", "CCNA/Week 1"),
        ("CCNA//Week 1", "CCNA/Week 1"),
        ("  CCNA / Week 1  ", "CCNA/Week 1"),
        ("", ""),
        (None, ""),
        ("///", ""),
    ],
)
def test_paths_normalise_to_one_form(raw: str | None, expected: str) -> None:
    assert normalise_folder(raw) == expected


def test_traversal_segments_are_dropped_not_rejected() -> None:
    """A folder is a string on a row, not a filesystem path, so ".." is never
    dangerous — but it is always a typo, and silently dropping it beats
    refusing the whole move."""
    assert normalise_folder("../../etc/passwd") == "etc/passwd"
    assert normalise_folder("CCNA/./Week 1") == "CCNA/Week 1"


def test_backslashes_are_treated_as_separators() -> None:
    """Someone will paste a Windows path. Two folders differing only by slash
    direction would be indistinguishable on screen."""
    assert normalise_folder(r"CCNA\Week 1") == "CCNA/Week 1"


def test_depth_and_length_are_bounded() -> None:
    """Unbounded nesting is a denial-of-service on the folder tree renderer,
    and an unbounded string is one on the column."""
    assert normalise_folder("a/" * 40).count("/") == 7
    assert len(normalise_folder("x" * 500)) <= 200


def test_normalising_is_idempotent() -> None:
    """A lab moved twice to the same place must not drift."""
    for raw in ("/CCNA//Week 1/", "CCNA/Week 1", "  a / b  "):
        once = normalise_folder(raw)
        assert normalise_folder(once) == once
