"""Validation compares intent against the dataplane.

The checks themselves are pure given a lab and what netd reports, so they can
be exercised without a host — which matters, because the interesting cases are
the broken ones and breaking a real bridge to test them is a poor trade.
"""

from __future__ import annotations

from labtris_api.validate import Check, _close, _summarise, render


class _Lab:
    id = "01LAB00000000000000000000"
    name = "demo"


def test_a_lab_with_no_failures_reports_ok() -> None:
    result = _summarise(_Lab(), [Check("x", "y", True, "fine")])

    assert result["ok"] is True
    assert result["failed"] == 0


def test_one_failure_is_enough_to_fail_the_lab() -> None:
    result = _summarise(
        _Lab(),
        [Check("a", "b", True, "fine"), Check("c", "d", False, "missing")],
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    assert result["total"] == 2


def test_impairment_comparison_tolerates_kernel_quantisation() -> None:
    """netem stores times in scheduler ticks and probabilities as 32-bit
    fractions, so a round trip rarely returns the exact number that went in.
    Comparing strictly would report every shaped link as broken."""
    assert _close(120, 120)
    assert _close(119.6, 120)
    assert _close(2.499, 2.5)
    assert _close(0, 0)


def test_a_genuinely_different_value_still_fails() -> None:
    """The tolerance must not be wide enough to hide the 8x rate bug that
    shipped before this existed — 5000 kbit applied as 40000."""
    assert not _close(40000, 5000)
    assert not _close(0, 120)
    assert not _close(500, 550, tolerance=0.01)


def test_the_text_form_names_what_failed_and_why() -> None:
    """This is what someone pastes when asking why their lab does not work."""
    text = render(
        _summarise(
            _Lab(),
            [
                Check("link ends share a segment", "va ↔ vb", False,
                      "b1 vs b2 — traffic cannot cross"),
                Check("impairment is applied", "A→B va", True, "matches the lab"),
            ],
        )
    )

    assert "1/2 checks passed" in text
    assert "[FAIL] link ends share a segment" in text
    assert "traffic cannot cross" in text
    assert "[PASS] impairment is applied" in text
