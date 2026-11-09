"""Hub health check: performance, errors and capacity, reviewed automatically.

Pulls the hub's ``GET_HEALTH_REPORT`` (slow routes, slow spoke commands,
timeouts, host load/memory/disk, event-loop lag) plus the mirrored hub logs,
applies fixed rules and files one deduped ``automated-fix`` issue per problem
(through ``log_scan.file_escalated_issue``), so the normal fix pipeline
investigates and repairs it. ``evaluate`` is pure and unit-tested; the I/O sits
in ``scan_health``.

Each finding carries a stable ``fingerprint`` that is also its issue title, so a
problem that persists folds into the same issue instead of opening duplicates.
"""
import logging
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ab.health_check")

DEFAULTS = {
    "health_check_enabled": True,
    "health_check_interval_s": 1800,
    "hc_slow_route_min_hits": 3,       # slow responses before a route is reported
    "hc_slow_route_max_s": 10.0,       # ...or a single response this slow
    "hc_slow_command_min_hits": 3,
    "hc_timeout_min": 5,               # request timeouts per (spoke, command)
    "hc_load_per_cpu": 1.5,
    "hc_mem_pct": 92.0,
    "hc_disk_pct": 88.0,
    "hc_loop_lag_s": 1.0,
    "hc_repeat_error_min": 25,         # identical (normalised) ERROR lines in the log tail
    "hc_vault_gets_min": 200,          # Azure Key Vault GETs in the log tail
    "hc_max_issues_per_run": 5,
}

# Command prefix -> MODULE_TYPE_REPO key (log_scan) of the repo that owns it.
_PREFIX_MODULE = {
    "NETBOX": "ipam", "NW": "nw", "TRUENAS": "storage", "PXMX": "hypervisor",
    "CPPM": "nac", "OPNSENSE": "firewall", "FW": "firewall", "LDAP": "directory",
    "CS": "simulation", "LE": "certificates", "DNS": "dns", "DHCP": "dhcp",
}

_HINT = {
    "slow_route": ("Find the handler, then cut the latency: run independent upstream calls "
                   "concurrently (asyncio.gather / a thread pool), cache slow-changing lookups "
                   "with a TTL, paginate or narrow queries, and avoid per-item round trips."),
    "slow_command": ("The spoke handler is slow: parallelise independent API calls, reuse "
                     "already-fetched rows instead of one GET per item, cache lookups, and "
                     "raise progress keepalives only for genuinely long work."),
    "timeouts": ("Repeated request timeouts: find what blocks the spoke or hub event loop "
                 "(sync I/O on the loop, serialised API calls, a too-short timeout for a "
                 "legitimately long command) and move it off the loop or batch it."),
    "host": "Find the process or loop driving the resource use and bound it.",
    "loop_lag": ("The hub event loop is saturated: look for blocking calls or CPU-heavy work "
                 "on the loop and move it to a thread or a background task."),
    "repeat_error": "One error repeats many times: fix the cause, or log it once per interval.",
    "vault": "Cache secret reads with a TTL instead of fetching on every cycle.",
    "slow_query": "Add the missing index, narrow the query, or move the work to the background.",
}

_DIGITS = re.compile(r"\d+")
_TS = re.compile(r"^\S+\s+\S+\s+")


def _module_for_command(command: str) -> str:
    return _PREFIX_MODULE.get(str(command or "").split("_", 1)[0].upper(), "hub")


def _finding(kind: str, key: str, title: str, severity: str, module: str,
             evidence: List[str], detail: str) -> Dict[str, Any]:
    return {"kind": kind, "key": key, "severity": severity, "module": module,
            "fingerprint": f"Health check: {title}", "title": f"Health check: {title}",
            "detail": detail, "evidence": evidence, "hint": _HINT.get(kind, "")}


def _norm(line: str) -> str:
    """A log line with its timestamp and numbers removed, for grouping repeats."""
    return _DIGITS.sub("#", _TS.sub("", line))[:200]


def evaluate(report: Optional[Dict[str, Any]], log_lines: List[str],
             cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Findings (most severe first) from a hub health report and recent log lines."""
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    out: List[Dict[str, Any]] = []
    report = report or {}
    host = report.get("host") or {}
    perf = report.get("perf") or {}

    cpus = float(host.get("cpus") or 1)
    load = (host.get("load") or [0])[0]
    if load / cpus >= c["hc_load_per_cpu"]:
        out.append(_finding("host", "load", "hub load average is high", "high", "hub",
                            [f"1-min load {load} on {int(cpus)} CPUs"], "Sustained CPU saturation on the hub."))
    if (host.get("mem_used_pct") or 0) >= c["hc_mem_pct"]:
        out.append(_finding("host", "mem", "hub memory is nearly full", "high", "hub",
                            [f"memory used {host['mem_used_pct']}%"], "Memory pressure on the hub."))
    if (host.get("disk_used_pct") or 0) >= c["hc_disk_pct"]:
        out.append(_finding("host", "disk", "hub disk is nearly full", "high", "hub",
                            [f"disk used {host['disk_used_pct']}%"], "Disk pressure on the hub."))
    if (report.get("loop_lag_s") or 0) >= c["hc_loop_lag_s"]:
        out.append(_finding("loop_lag", "lag", "hub event loop is lagging", "high", "hub",
                            [f"event-loop lag {report['loop_lag_s']}s"], "The hub loop is saturated."))

    for r in perf.get("routes") or []:
        if r["slow"] >= c["hc_slow_route_min_hits"] or r["max_s"] >= c["hc_slow_route_max_s"]:
            out.append(_finding(
                "slow_route", r["key"], f"slow page {r['key']}", "medium", "hub",
                [f"{r['slow']} of {r['count']} responses >= {perf.get('slow_route_s')}s; "
                 f"avg {r['avg_s']}s, max {r['max_s']}s"],
                f"`{r['key']}` is slow for users."))
    for r in perf.get("commands") or []:
        if r["slow"] >= c["hc_slow_command_min_hits"]:
            out.append(_finding(
                "slow_command", f"{r['spoke']}:{r['key']}",
                f"slow command {r['key']} on {r['spoke']}", "medium", _module_for_command(r["key"]),
                [f"{r['slow']} of {r['count']} calls >= {perf.get('slow_command_s')}s; "
                 f"avg {r['avg_s']}s, max {r['max_s']}s"],
                f"`{r['key']}` takes long on `{r['spoke']}`."))

    names = report.get("spoke_names") or {}
    for sid, cmds in (report.get("timeouts_by_spoke") or {}).items():
        for cmd, n in cmds.items():
            if n >= c["hc_timeout_min"]:
                spoke = names.get(sid) or sid
                out.append(_finding("timeouts", f"{spoke}:{cmd}",
                                    f"{cmd} keeps timing out on {spoke}", "high",
                                    _module_for_command(cmd),
                                    [f"{n} request timeouts since the hub started"],
                                    f"`{cmd}` to `{spoke}` times out repeatedly."))

    errs: Counter = Counter()
    sample: Dict[str, str] = {}
    vault = 0
    slow_q: List[str] = []
    for ln in log_lines or []:
        if "vault.azure.net/secrets" in ln:
            vault += 1
        if re.search(r"slow query|long[- ]running quer|statement timeout|duration: \d{4,} ms", ln, re.I):
            slow_q.append(ln[:240])
        if " ERROR " in ln or ln.startswith("ERROR") or "Traceback" in ln:
            k = _norm(ln)
            errs[k] += 1
            sample.setdefault(k, ln[:300])
    for k, n in errs.most_common(5):
        if n >= c["hc_repeat_error_min"]:
            out.append(_finding("repeat_error", k, f"repeated error: {k[:70]}", "medium", "hub",
                                [f"{n} occurrences in the recent log", sample[k]],
                                "The same error keeps recurring."))
    if vault >= c["hc_vault_gets_min"]:
        out.append(_finding("vault", "vault", "excessive Key Vault reads", "medium", "hub",
                            [f"{vault} vault secret GETs in the recent log"],
                            "Secrets are fetched far more often than needed."))
    if slow_q:
        out.append(_finding("slow_query", "q", "long-running database queries", "medium", "hub",
                            slow_q[:5], "Slow queries were logged."))

    # Give the fixer measurements to work from: the host's state when the
    # problem was seen, the same command's timing, and the log lines naming it.
    ctx = (f"host at report time: load {load} on {int(cpus)} CPUs, memory "
           f"{host.get('mem_used_pct', '?')}%, disk {host.get('disk_used_pct', '?')}%, "
           f"event-loop lag {report.get('loop_lag_s') or 0}s")
    for f in out:
        if f["kind"] in ("slow_route", "slow_command", "timeouts"):
            f["evidence"].append(ctx)
        if f["kind"] == "timeouts":
            spoke, _, cmd = f["key"].partition(":")
            r = next((r for r in perf.get("commands") or []
                      if r["key"] == cmd and spoke in (r.get("spoke"), names.get(r.get("spoke")))), None)
            if r:
                f["evidence"].append(f"{cmd} timing on this spoke: {r['count']} calls, avg "
                                     f"{r['avg_s']}s, max {r['max_s']}s, {r['slow']} slow")
        needle = f["key"].split(":")[-1] if f["kind"] in ("slow_route", "slow_command", "timeouts") else ""
        if needle:
            hits = [ln[:240] for ln in log_lines or [] if needle in ln][-5:]
            if hits:
                f["evidence"].append("recent log lines mentioning it:")
                f["evidence"].extend(hits)
    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: rank.get(f["severity"], 3))
    return out


def render_issue(f: Dict[str, Any]) -> Dict[str, str]:
    """analysis + log slice for ``file_escalated_issue``."""
    analysis = (f"{f['title']}\n\n**Severity:** {f['severity']}\n\n{f['detail']}\n\n"
                f"**Suggested direction:** {f['hint']}\n\n"
                "Reproduce and measure before and after; keep the change to the slow path.")
    return {"analysis": analysis, "log_slice": "\n".join(f["evidence"])}


_last: Dict[str, Any] = {"ts": 0.0, "findings": [], "filed": [], "error": "", "report": {}, "running": False, "duration_s": 0.0}
_history: List[Dict[str, Any]] = []
HISTORY_MAX = 96


def _snapshot(report: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of a hub report the dashboard shows: host gauges and the worst offenders."""
    perf = report.get("perf") or {}
    worst = lambda rows: sorted(rows or [], key=lambda r: r.get("max_s", 0), reverse=True)[:10]  # noqa: E731
    return {"host": report.get("host") or {}, "loop_lag_s": report.get("loop_lag_s") or 0,
            "routes": worst(perf.get("routes")), "commands": worst(perf.get("commands")),
            "timeouts": {(report.get("spoke_names") or {}).get(sid) or sid: cmds
                         for sid, cmds in (report.get("timeouts_by_spoke") or {}).items()}}


def status_of(result: Dict[str, Any]) -> str:
    """unavailable (no report) | critical (any high finding) | degraded | healthy | pending."""
    if result.get("error"):
        return "unavailable"
    if not result.get("ts"):
        return "pending"
    sev = {f.get("severity") for f in result.get("findings") or []}
    return "critical" if "high" in sev else "degraded" if sev else "healthy"


def last_result() -> Dict[str, Any]:
    return dict(_last)


def dashboard(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything the Health page renders: last result, status, schedule, history, thresholds."""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if k in DEFAULTS and v is not None})
    res = last_result()
    interval = float(cfg["health_check_interval_s"])
    return {**res, "status": status_of(res), "enabled": bool(cfg["health_check_enabled"]),
            "interval_s": interval,
            "next_run_ts": (res["ts"] + interval) if res.get("ts") and cfg["health_check_enabled"] else None,
            "history": list(_history), "thresholds": {k: v for k, v in cfg.items() if k.startswith("hc_")}}


def scan_health(config: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """One health review: fetch, evaluate, file. Honours the enable flag and
    interval unless ``force``. Never raises."""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if k in DEFAULTS and v is not None})
    now = time.time()
    if not force:
        if not cfg["health_check_enabled"]:
            return last_result()
        if now - _last["ts"] < float(cfg["health_check_interval_s"]):
            return last_result()
    if _last.get("running"):
        return last_result()
    _last.update(ts=now, running=True)
    try:
        from log_scan import _get_hub_agent_client, file_escalated_issue, get_hub_logs
        client = _get_hub_agent_client()
        report = client.request_sync("GET_HEALTH_REPORT", {}, timeout=30) if client else None
        if not (isinstance(report, dict) and "host" in report):
            why = ("not connected/approved" if report is None and client is None else
                   report.get("message") if isinstance(report, dict) and report.get("message")
                   else "no reply within 30s")
            _last.update(error="hub health report unavailable: %s" % why, findings=[], filed=[],
                         running=False, duration_s=round(time.time() - now, 1))
            return last_result()
        lines = [e.get("log", "") for e in get_hub_logs(max_per_module=3000)]
        findings = evaluate(report, lines, cfg)
        filed = []
        for f in findings[: int(cfg["hc_max_issues_per_run"])]:
            r = render_issue(f)
            ok, detail = file_escalated_issue(f["module"], r["log_slice"], r["analysis"],
                                              verdict="health-check", config=config)
            filed.append({"title": f["title"], "module": f["module"], "ok": ok, "detail": detail})
        _last.update(error="", findings=findings, filed=filed, report=_snapshot(report))
        host = report.get("host") or {}
        _history.append({"ts": now, "findings": len(findings),
                         "high": sum(1 for f in findings if f["severity"] == "high"),
                         "load": (host.get("load") or [0])[0], "mem": host.get("mem_used_pct") or 0,
                         "disk": host.get("disk_used_pct") or 0, "lag": report.get("loop_lag_s") or 0})
        del _history[:-HISTORY_MAX]
    except Exception as e:  # noqa: BLE001
        logger.warning("health check failed: %s", e)
        _last["error"] = str(e)
    finally:
        _last.update(running=False, duration_s=round(time.time() - now, 1))
    return last_result()
