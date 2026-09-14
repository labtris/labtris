from labtris_api.impair import PRESETS


def test_presets_cover_the_demo_profiles() -> None:
    assert PRESETS["satellite"]["delay_ms"] == 550
    assert PRESETS["3g"]["rate_kbit"] == 384
    assert PRESETS["lossy-wan"]["loss_pct"] == 5.0
    assert PRESETS["clear"] == {}
