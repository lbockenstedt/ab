import health_check as hc


def _report(**kw):
    base = {"host": {"load": [0.5], "cpus": 4, "mem_used_pct": 40, "disk_used_pct": 40},
            "perf": {"slow_route_s": 3, "slow_command_s": 5, "routes": [], "commands": []},
            "timeouts_by_spoke": {}, "spoke_names": {}, "loop_lag_s": 0}
    base.update(kw)
    return base


def test_healthy_report_has_no_findings():
    assert hc.evaluate(_report(), []) == []


def test_slow_route_command_and_timeouts_become_findings_with_owning_module():
    rep = _report(
        perf={"slow_route_s": 3, "slow_command_s": 5,
              "routes": [{"key": "GET /api/netbox/racks/{rack_id}/elevation", "count": 10,
                          "slow": 6, "avg_s": 8.1, "max_s": 14.0}],
              "commands": [{"key": "NETBOX_GET_DEVICES", "spoke": "netbox-1", "count": 9,
                            "slow": 4, "avg_s": 7.0, "max_s": 20.0}]},
        timeouts_by_spoke={"s1": {"NW_POLL": 7}}, spoke_names={"s1": "nw-1"})
    got = {f["kind"]: f for f in hc.evaluate(rep, [])}
    assert got["slow_route"]["module"] == "hub"
    assert got["slow_command"]["module"] == "ipam"
    assert got["timeouts"]["module"] == "nw" and "nw-1" in got["timeouts"]["title"]
    assert all(f["fingerprint"] == f["title"] for f in got.values())


def test_host_pressure_and_log_patterns():
    rep = _report(host={"load": [9.0], "cpus": 4, "mem_used_pct": 95, "disk_used_pct": 50})
    lines = ["2026-11-08 10:00:%02d,1 ERROR boom id=%d" % (i % 60, i) for i in range(30)]
    lines += ["GET https://x.vault.azure.net/secrets/a"] * 200
    kinds = [f["kind"] for f in hc.evaluate(rep, lines)]
    assert kinds.count("host") == 2
    assert "repeat_error" in kinds and "vault" in kinds
    assert hc.evaluate(rep, [], {"hc_load_per_cpu": 99, "hc_mem_pct": 99}) == []


def test_scan_health_respects_disable_and_interval():
    hc._last.update(ts=0.0)
    assert hc.scan_health({"health_check_enabled": False}) == hc.last_result()
    assert hc._last["ts"] == 0.0


def test_dashboard_status_and_history():
    hc._last.update(ts=0.0, findings=[], error="")
    hc._history.clear()
    assert hc.dashboard({})["status"] == "pending"
    assert hc.status_of({"ts": 1, "findings": [{"severity": "medium"}]}) == "degraded"
    assert hc.status_of({"ts": 1, "findings": [{"severity": "high"}]}) == "critical"
    assert hc.status_of({"ts": 1, "findings": [], "error": "x"}) == "unavailable"
    snap = hc._snapshot({"host": {"cpus": 2}, "perf": {"routes": [{"max_s": 1}, {"max_s": 9}]},
                         "timeouts_by_spoke": {"s1": {"A": 2}}, "spoke_names": {"s1": "nw"}})
    assert snap["routes"][0]["max_s"] == 9 and snap["timeouts"] == {"nw": {"A": 2}}
