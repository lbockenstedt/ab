"""Platform Auto Scale: grows only within allowed sizes, never LM-AB, one resize per run."""
import json

import auto_scale as asc
import az_console

GB = 2 ** 30


def _vm(name="LM-HUB", size="Standard_B2als_v2", free_gb=2.0, cpu=5.0, credits=300.0, **kw):
    row = {"name": name, "size": size, "free_2h": [free_gb * GB] * 8, "cpu_2h": [cpu] * 8,
           "credits": credits, "cpu_7d_avg": None, "free_7d_min": None, "days_7d": 0}
    row.update(kw)
    return row


def test_short_of_memory_grows_to_the_cheapest_larger_allowed_size():
    d = asc.decide(_vm(free_gb=0.2), ["Standard_B4as_v2", "Standard_B2as_v2"], {}, now=1e9)
    assert (d["action"], d["to"]) == ("grow", "Standard_B2as_v2")


def test_short_but_nothing_larger_allowed_needs_work_not_a_resize():
    assert asc.decide(_vm(free_gb=0.2), [], {}, now=1e9)["action"] == "needs_work"
    # A smaller allowed size is no way out either.
    assert asc.decide(_vm(free_gb=0.2), ["Standard_B2ats_v2"], {}, now=1e9)["action"] == "needs_work"


def test_one_low_sample_is_not_sustained():
    vm = _vm(free_gb=0.2)
    vm["free_2h"][3] = 2 * GB
    assert asc.decide(vm, ["Standard_B2as_v2"], {}, now=1e9)["action"] == "ok"


def test_burstable_out_of_credits_above_baseline_needs_more_cpu_capacity():
    vm = _vm(cpu=40.0, credits=5.0)
    # B4als_v2 has more capacity but costs more than B2as_v2, which also has more.
    d = asc.decide(vm, ["Standard_B4als_v2", "Standard_B2as_v2"], {}, now=1e9)
    assert (d["action"], d["to"]) == ("grow", "Standard_B2as_v2")
    # Low credits while idle recover on their own.
    assert asc.decide(_vm(cpu=3.0, credits=5.0), ["Standard_B2as_v2"], {}, now=1e9)["action"] == "ok"


def test_cooldown_and_lm_ab_are_not_resized():
    d = asc.decide(_vm(free_gb=0.2), ["Standard_B2as_v2"], {}, last_resize_ts=1e9 - 3600, now=1e9)
    assert d["action"] == "cooldown"
    d = asc.decide(_vm("LM-AB", free_gb=0.2), ["Standard_B2as_v2"], {}, now=1e9)
    assert (d["action"], d["to"]) == ("recommend", "Standard_B2as_v2")


def test_resize_vm_refuses_lm_ab_before_running_anything(monkeypatch):
    monkeypatch.setattr(az_console, "_ensure_login", lambda c: (_ for _ in ()).throw(AssertionError))
    res = az_console.resize_vm("lm-ab", "Standard_B2as_v2", {"CHAT_AZURE_ENABLED": True})
    assert "never resized" in res["error"]


def test_shrink_is_recommended_only_with_a_full_week_and_2x_headroom():
    week = dict(cpu_7d_avg=2.0, free_7d_min=3.7 * GB)
    assert asc.decide(_vm(days_7d=7, **week), ["Standard_B2ats_v2"], {}, now=1e9)["to"] == "Standard_B2ats_v2"
    assert asc.decide(_vm(days_7d=3, **week), ["Standard_B2ats_v2"], {}, now=1e9)["action"] == "ok"
    busy = dict(cpu_7d_avg=2.0, free_7d_min=3.0 * GB)  # 1 GB used needs 2 GB
    assert asc.decide(_vm(days_7d=7, **busy), ["Standard_B2ats_v2"], {}, now=1e9)["action"] == "ok"


def test_run_resizes_one_vm_per_run_and_files_what_it_cannot_fix(monkeypatch, tmp_path):
    monkeypatch.setattr(asc, "STATE_FILE", str(tmp_path / "autoscale.json"))
    vms = [_vm("LM-HUB", free_gb=0.2), _vm("LM-ZBX", free_gb=0.2), _vm("LM-UI", free_gb=0.2),
           _vm("LM-AB", free_gb=0.2)]
    monkeypatch.setattr(asc, "collect", lambda c: vms)
    monkeypatch.setattr(asc, "_top_processes", lambda vm, c: "postgres 900000")
    resized = []
    monkeypatch.setattr(az_console, "resize_vm", lambda vm, size, c: resized.append((vm, size)) or {"exit_code": 0})
    big = ["Standard_B2as_v2"]
    cfg = {"autoscale_allowed": {"LM-HUB": big, "LM-ZBX": big, "LM-AB": big}}
    found = asc.run(cfg, now=1e9)
    assert resized == [("LM-HUB", "Standard_B2as_v2")]
    assert sorted((f["kind"], f["key"]) for f in found) == [("autoscale_needs_work", "LM-UI"),
                                                            ("autoscale_recommend", "LM-AB")]
    state = json.load(open(asc.STATE_FILE))
    assert {r["name"]: r["action"] for r in state["vms"]} == {
        "LM-HUB": "grown", "LM-ZBX": "grow", "LM-UI": "needs_work", "LM-AB": "recommend"}
    # Next run: LM-HUB is cooling down, so LM-ZBX gets its turn.
    resized.clear()
    asc.run(cfg, now=1e9 + 1800)
    assert resized == [("LM-ZBX", "Standard_B2as_v2")]
    # Turned off: nothing is resized.
    resized.clear()
    monkeypatch.setattr(asc, "STATE_FILE", str(tmp_path / "other.json"))
    asc.run({**cfg, "autoscale_enabled": False}, now=1e9)
    assert resized == []


def test_collect_uses_read_only_az_and_reads_series(monkeypatch):
    def fake(argv, config, timeout=120):
        assert az_console.vet_az_args(argv)[0], argv
        if argv[:2] == ["vm", "list"]:
            return {"exit_code": 0, "stdout": json.dumps([{"name": "A", "id": "/r/A", "size": "Standard_B2als_v2"}])}
        if "PT15M" in argv:
            return {"exit_code": 0, "stdout": json.dumps([
                {"n": "Percentage CPU", "avg": [10, None, 20]}, {"n": "Available Memory Bytes", "avg": [GB]},
                {"n": "CPU Credits Remaining", "avg": [50, 40]}])}
        return {"exit_code": 0, "stdout": json.dumps([
            {"n": "Percentage CPU", "avg": [4, 6], "min": [1, 1]},
            {"n": "Available Memory Bytes", "avg": [3 * GB, 3 * GB], "min": [2 * GB, GB]}])}
    monkeypatch.setattr(az_console, "run_az", fake)
    [row] = asc.collect({"CHAT_AZURE_ENABLED": True})
    assert row == {"name": "A", "size": "Standard_B2als_v2", "cpu_2h": [10.0, 20.0], "free_2h": [float(GB)],
                   "credits": 40.0, "cpu_7d_avg": 5.0, "free_7d_min": float(GB), "days_7d": 2}
    assert asc.collect({}) is None


def test_settings_save_allowed_sizes_per_vm():
    from test_feature_settings_roundtrip import _FakeRequest, _base_pairs, _load_ns, _run
    ns = _load_ns()
    pairs = _base_pairs(autoscale_enabled="on") + [
        ("autoscale_allowed", "LM-HUB|Standard_B2as_v2"), ("autoscale_allowed", "LM-HUB|Standard_B4as_v2"),
        ("autoscale_allowed", "LM-ZBX|Standard_B2as_v2"), ("autoscale_cooldown_h", "12")]
    _run(ns["save_settings"](_FakeRequest(pairs)))
    cfg = ns["_config_holder"]["config"]
    assert cfg["autoscale_enabled"] is True and cfg["autoscale_cooldown_h"] == 12
    assert cfg["autoscale_allowed"] == {"LM-HUB": ["Standard_B2as_v2", "Standard_B4as_v2"],
                                        "LM-ZBX": ["Standard_B2as_v2"]}
