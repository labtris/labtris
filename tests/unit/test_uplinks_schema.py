"""UplinkBridgesIn's validators — the API-layer preflight for the
`configure_uplinks` verb. Netd repeats the checks (defence in depth); we
catch the obvious shapes here so a browser gets a helpful message
without a network round-trip."""

from __future__ import annotations

import pytest

from labtris_api.schemas import UplinkBridgesIn


def test_empty_is_legal() -> None:
    """Zero uplinks is legal — same shape a "remove the last one" apply
    passes."""
    m = UplinkBridgesIn(pairs=[])
    assert m.pairs == []


def test_one_pair_survives() -> None:
    m = UplinkBridgesIn.model_validate({"pairs": [{"nic": "ens192"}]})
    assert len(m.pairs) == 1
    assert m.pairs[0].nic == "ens192"


def test_duplicates_refused() -> None:
    with pytest.raises(ValueError, match="more than once"):
        UplinkBridgesIn.model_validate(
            {"pairs": [{"nic": "ens192"}, {"nic": "ens192"}]}
        )


def test_empty_nic_refused() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        UplinkBridgesIn.model_validate({"pairs": [{"nic": ""}]})


def test_missing_nic_field_refused() -> None:
    """A pair without a `nic` field is a shape error — pydantic surfaces
    the missing-field one for us."""
    with pytest.raises(ValueError):
        UplinkBridgesIn.model_validate({"pairs": [{}]})
