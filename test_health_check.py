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


def test_self_update_route_is_not_reported_as_slow():
    rep = _report(perf={"slow_route_s": 3, "slow_command_s": 5, "commands": [],
                        "routes": [{"key": "POST /admin/ops/self-update", "count": 1,
                                    "slow": 1, "avg_s": 14.0, "max_s": 14.0}]})
    assert hc.evaluate(rep, []) == []


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


def test_scan_health_rejects_error_reply_and_reports_why(monkeypatch):
    import sys
    import types

    class _Client:
        def request_sync(self, *a, **k):
            return {"status": "error", "message": "Unknown HUB_REQUEST type"}

    fake = types.SimpleNamespace(_get_hub_agent_client=lambda: _Client(),
                                 file_escalated_issue=None, get_hub_logs=lambda **k: [])
    monkeypatch.setitem(sys.modules, "log_scan", fake)
    hc._last.update(ts=0.0, running=False)
    res = hc.scan_health({}, force=True)
    assert "Unknown HUB_REQUEST type" in res["error"] and res["running"] is False
    assert hc.status_of(res) == "unavailable"


def test_findings_carry_measurements_for_the_fixer():
    report = {"host": {"cpus": 2, "load": [0.5], "mem_used_pct": 40, "disk_used_pct": 30},
              "spoke_names": {"s1": "mipbe-netbox"},
              "timeouts_by_spoke": {"s1": {"NETBOX_GET_IPS": 6}},
              "perf": {"commands": [{"key": "NETBOX_GET_IPS", "spoke": "s1", "count": 20,
                                     "slow": 9, "avg_s": 12.5, "max_s": 40.0}]}}
    logs = ["x NETBOX_GET_IPS took 41s", "unrelated"]
    f = [x for x in hc.evaluate(report, logs) if x["kind"] == "timeouts"][0]
    text = "\n".join(f["evidence"])
    assert "avg 12.5s" in text and "load 0.5 on 2 CPUs" in text and "took 41s" in text
    assert "unrelated" not in text


def test_saturated_spoke_queue_is_reported_against_its_module():
    """A spoke refusing commands (queue full) is a high finding owned by its module."""
    from health_check import evaluate
    rep = {"host": {}, "spoke_queues": {
        "netbox-1": {"inflight": 8, "max_inflight": 8, "rejected_full": 40,
                     "by_type": {"NETBOX_SYNC_DEVICE": {"inflight": 7, "rejected": 38,
                                                        "avg_s": 20.0, "max_s": 90.0}}},
        "nw-1": {"inflight": 1, "max_inflight": 8, "rejected_full": 0, "by_type": {}}}}
    f = [x for x in evaluate(rep, []) if x["kind"] == "queue"]
    assert len(f) == 1 and f[0]["module"] == "ipam" and f[0]["severity"] == "high"
    assert "NETBOX_SYNC_DEVICE" in " ".join(f[0]["evidence"])


def test_fleet_data_quality_findings():
    """Auto-rack failures, NetBox duplicates and DHCP declines each file one finding."""
    from health_check import evaluate
    rep = {"host": {}, "fleet": {
        "autorack": {"named": 10, "conflicts": 1, "cannot": 1,
                     "sample": [{"name": "srv-N31-R44", "wanted": "R44 U31 front", "reason": "2U"}]},
        "netbox_duplicates": {"groups": 2, "scanned": 50, "sample": [{"name": "ilo1", "key": "SN", "duplicates": 1}]},
        "dhcp_declines": [{"subnet": "10.21.0.0/24", "declined": 12, "total": 200},
                          {"subnet": "10.22.0.0/24", "declined": 1, "total": 200}]}}
    by_kind = {f["kind"]: f for f in evaluate(rep, [])}
    assert by_kind["autorack"]["module"] == "ipam" and "srv-N31-R44" in " ".join(by_kind["autorack"]["evidence"])
    assert by_kind["netbox_dupes"]["module"] == "ipam"
    assert by_kind["dhcp_declines"]["evidence"] == ["10.21.0.0/24: 12 declined of 200"]
    assert evaluate({"host": {}, "fleet": {"autorack": {"conflicts": 0, "cannot": 0}}}, []) == []


def test_azure_vm_findings_credits_agent_power():
    import azure_health as az
    vms = [{"name": "LM-NETBOX", "size": "Standard_B2als_v2", "power": "VM running", "agent": "Ready",
            "credits": 0.7, "cpu_pct": 30.0},
           {"name": "LM-IDLE", "size": "Standard_B1s", "power": "VM running", "agent": "Ready",
            "credits": 5.0, "cpu_pct": 1.0},
           {"name": "LM-AB", "size": "Standard_B1ms", "power": "VM running", "agent": "Not Ready",
            "credits": 288.0, "cpu_pct": 17.0},
           {"name": "LM-OFF", "size": "Standard_D2s_v5", "power": "VM deallocated", "agent": "",
            "credits": None, "cpu_pct": None}]
    kinds = {(f["kind"], f["key"]) for f in az.evaluate(vms)}
    assert kinds == {("azure_credits", "LM-NETBOX"), ("azure_agent", "LM-AB"), ("azure_power", "LM-OFF")}
    assert az.evaluate(None) == [] and az.findings({}) == []


def test_azure_collect_reads_vms_through_read_only_az(monkeypatch):
    import az_console
    import azure_health as az
    import json as _j
    calls = []

    def fake(argv, config, timeout=120):
        calls.append(argv)
        assert az_console.vet_az_args(argv)[0], argv
        if argv[:2] == ["vm", "list"]:
            return {"exit_code": 0, "stdout": _j.dumps([{"name": "A", "id": "/r/A", "size": "Standard_B2s"}])}
        if argv[:2] == ["vm", "get-instance-view"]:
            return {"exit_code": 0, "stdout": _j.dumps({"power": "VM running", "agent": "Ready"})}
        return {"exit_code": 0, "stdout": _j.dumps([{"n": "CPU Credits Remaining", "v": 4.0},
                                                      {"n": "Percentage CPU", "v": 40.0}])}
    import az_console
    monkeypatch.setattr(az_console, "run_az", fake)
    got = az.collect({"CHAT_AZURE_ENABLED": True})
    assert got == [{"name": "A", "size": "Standard_B2s", "power": "VM running", "agent": "Ready",
                    "credits": 4.0, "cpu_pct": 40.0}]
    assert az.collect({}) is None
