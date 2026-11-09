"""save_settings persists the hub health-check knobs and falls back to defaults on bad input."""
import health_check as hc
from test_feature_settings_roundtrip import _FakeRequest, _base_pairs, _load_ns, _run


def _save(**overrides):
    ns = _load_ns()
    _run(ns["save_settings"](_FakeRequest(_base_pairs(**overrides))))
    return ns["_config_holder"]["config"]


def test_knobs_persist_and_coerce():
    cfg = _save(health_check_enabled="on", health_check_interval_s="600", hc_mem_pct="85.5")
    assert cfg["health_check_enabled"] is True
    assert cfg["health_check_interval_s"] == 600
    assert cfg["hc_mem_pct"] == 85.5


def test_bad_or_blank_falls_back_and_unchecked_disables():
    cfg = _save(hc_disk_pct="abc", hc_loop_lag_s="")
    assert cfg["health_check_enabled"] is False
    assert cfg["hc_disk_pct"] == hc.DEFAULTS["hc_disk_pct"]
    assert cfg["hc_loop_lag_s"] == hc.DEFAULTS["hc_loop_lag_s"]
