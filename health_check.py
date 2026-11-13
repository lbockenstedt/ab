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
import json
import logging
import os
import re
from datetime import datetime, timedelta
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
    "hc_slow_command_pct": 20.0,       # ...and at least this share of its calls were slow
    "hc_log_window_s": 3600,           # log rules (repeats, vault reads) look at this much recent log
    "hc_timeout_min": 5,               # request timeouts per (spoke, command)
    "hc_load_per_cpu": 1.5,
    "hc_mem_pct": 92.0,
    "hc_disk_pct": 88.0,
    "hc_loop_lag_s": 1.0,
    "hc_repeat_error_min": 25,         # identical (normalised) ERROR lines in the log tail
    "hc_vault_gets_min": 200,          # Azure Key Vault GETs in the log window
    "hc_queue_rejected_min": 5,        # commands a spoke refused because its queue was full
    "hc_autorack_problems_min": 1,     # devices whose rack-from-name placement fails
    "hc_netbox_dupe_groups_min": 1,    # duplicate NetBox device groups (serial/MAC)
    "hc_command_rate_per_h": 300,      # a read command (GET/LIST) called this often per hour
    "hc_command_rate_min_uptime_s": 900,  # ...once the hub has been up this long
    "hc_netbox_writes_per_h": 4000,    # NetBox changelog writes per hour
    "hc_netbox_flap_objects_min": 10,  # devices whose field flips back to an earlier value
    "hc_dhcp_declined_min": 5,         # DECLINEd addresses held in one DHCP scope
    "hc_azure_enabled": True,          # review the Azure VMs too (needs CHAT_AZURE_ENABLED)
    "hc_azure_credits_min": 30.0,      # burstable VM below this many CPU credits is throttled
    "hc_azure_credits_cpu_pct": 15.0,  # ...and busy at least this much
    "hc_max_issues_per_run": 5,
}

# Routes that are slow by design, so a slow response is not a problem to file.
# /admin/ops/* is the loopback-only operator API: each call runs a job or waits
# on a spoke command inside the request (self-update, autorack, exec,
# dhcp-diagnostics), and no user page uses it (lm#1747, lm#1866, lm#1889, lm#1891).
_SLOW_ROUTE_EXEMPT_PREFIX = "/admin/ops/"

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
    "queue": ("A spoke's command queue is saturated: find which command fills it (by_type "
              "inflight/avg_s), make that handler faster or move its long work to a background "
              "task, and stop the hub re-sending it while it is still in flight. Diagnose live "
              "with POST /admin/ops/spoke-queue."),
    "autorack": ("Rack-from-name placement failed. The name (e.g. N31-R44) must win: check "
                 "netbox_autorack.plan() for device height detection (2U/half-width from the "
                 "device type), slot freeing and controller/host pairing before blaming data. "
                 "Do not ask the user to rack it by hand."),
    "netbox_dupes": ("Duplicate NetBox devices share a serial or MAC: find which sync path "
                     "created the second record (it should have matched by serial/MAC first) "
                     "and fix the match; the dedupe merge only cleans up after it."),
    "call_rate": ("A read command is hitting a spoke far more often than data changes. Serve it "
                  "from the hub cache (refreshed in the background on a floor of minutes) or the "
                  "spoke's TTL cache; find the caller by grepping the command name and make it "
                  "read the cache (fleet-rules 4.8)."),
    "netbox_churn": ("A feeder is rewriting NetBox constantly or flip-flopping a field. Use the "
                     "top fields/sample to find the writer (nw discovery, cppm endpoint sync, "
                     "sync_nw_device), make it compare before writing, and pick one canonical "
                     "value deterministically instead of 'first answer wins'."),
    "dhcp_declines": ("Clients are DECLINEing offered addresses (ARP conflict). Check for the "
                      "decline probation/auto-release working, a stale reservation or a static IP "
                      "in the pool; if it is a network-side cause (proxy-ARP, loop) say so in the issue."),
    "stale_version": ("Spokes stay out of date long after an update. Check the hub's spoke_versions "
                      "cache (only a real .NN / MAJOR.MINOR reply may be stored), that the hub asks "
                      "GET_VERSION after a spoke reconnects, and the spoke's SPOKE_UPDATE log for a "
                      "failed pull or restart. Fix the code path; do not just re-push updates."),
    "repeat_error": "One error repeats many times: fix the cause, or log it once per interval.",
    "vault": "Cache secret reads with a TTL instead of fetching on every cycle.",
    "slow_query": "Add the missing index, narrow the query, or move the work to the background.",
}

_DIGITS = re.compile(r"\d+")
_LINE_TS = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)")
_TS = re.compile(r"^\S+\s+\S+\s+")
_EXC_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception): ")


def _module_for_command(command: str) -> str:
    return _PREFIX_MODULE.get(str(command or "").split("_", 1)[0].upper(), "hub")


def _finding(kind: str, key: str, title: str, severity: str, module: str,
             evidence: List[str], detail: str) -> Dict[str, Any]:
    return {"kind": kind, "key": key, "severity": severity, "module": module,
            "fingerprint": f"Health check: {title}", "title": f"Health check: {title}",
            "detail": detail, "evidence": evidence, "hint": _HINT.get(kind, "")}


def recent_lines(lines: List[str], window_s: float) -> List[str]:
    """Lines no older than ``window_s`` before the newest timestamped line.
    The log mirror keeps days of history; an error that stopped hours ago is
    not a current problem. A line without a timestamp (a traceback body) goes
    with the timestamped line above it."""
    stamps = [m.group(1).replace("T", " ") for m in map(_LINE_TS.match, lines or []) if m]
    if not stamps:
        return list(lines or [])
    cutoff = (datetime.strptime(max(stamps), "%Y-%m-%d %H:%M:%S")
              - timedelta(seconds=window_s)).strftime("%Y-%m-%d %H:%M:%S")
    keep = []
    recent = True
    for ln in lines:
        m = _LINE_TS.match(ln)
        if m:
            recent = m.group(1).replace("T", " ") >= cutoff
        if recent:
            keep.append(ln)
    return keep


def counters(report: Dict[str, Any]) -> Dict[str, Any]:
    """The hub's since-start counters from a report, saved between runs."""
    perf = report.get("perf") or {}
    rows = lambda key: {f"{r.get('spoke', '')}|{r['key']}": [r["count"], r["slow"],  # noqa: E731
                                                            r["avg_s"] * r["count"]]
                        for r in perf.get(key) or []}
    return {"started": float(report.get("generated") or 0) - float(perf.get("uptime_s") or 0),
            "routes": rows("routes"), "commands": rows("commands"),
            "timeouts": report.get("timeouts_by_spoke") or {}}


def since_last(report: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The report with its slow/timeout counters cut to what happened since the
    previous run (``prev`` from ``counters``). The hub counts since it started,
    so without this a problem that is fixed keeps being reported until the hub
    restarts. With no previous run, or after a hub restart, the report is used as is."""
    perf = report.get("perf") or {}
    if not prev or not perf.get("uptime_s") or abs(counters(report)["started"] - prev.get("started", 0)) > 120:
        return report

    def cut(rows, before):
        out = []
        for r in rows or []:
            c0, s0, t0 = before.get(f"{r.get('spoke', '')}|{r['key']}", [0, 0, 0.0])
            n = r["count"] - c0
            if n <= 0:
                continue
            slow = max(r["slow"] - s0, 0)
            avg = max(round((r["avg_s"] * r["count"] - t0) / n, 2), 0)
            # max_s is since hub start; only keep it when this run saw slow calls.
            out.append(dict(r, count=n, slow=slow, avg_s=avg, max_s=r["max_s"] if slow else avg))
        return out

    timeouts = {}
    for sid, cmds in (report.get("timeouts_by_spoke") or {}).items():
        old = prev.get("timeouts", {}).get(sid, {})
        new = {cmd: n - old.get(cmd, 0) for cmd, n in cmds.items() if n - old.get(cmd, 0) > 0}
        if new:
            timeouts[sid] = new
    return dict(report, timeouts_by_spoke=timeouts,
                perf=dict(perf, routes=cut(perf.get("routes"), prev.get("routes", {})),
                          commands=cut(perf.get("commands"), prev.get("commands", {}))))


def _norm(line: str) -> str:
    """A log line with its timestamp and numbers removed, for grouping repeats."""
    return _DIGITS.sub("#", _TS.sub("", line) if _LINE_TS.match(line) else line)[:200]


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
        if r["key"].split(" ", 1)[-1].startswith(_SLOW_ROUTE_EXEMPT_PREFIX):
            continue
        if r["slow"] >= c["hc_slow_route_min_hits"] or r["max_s"] >= c["hc_slow_route_max_s"]:
            out.append(_finding(
                "slow_route", r["key"], f"slow page {r['key']}", "medium", "hub",
                [f"{r['slow']} of {r['count']} responses >= {perf.get('slow_route_s')}s; "
                 f"avg {r['avg_s']}s, max {r['max_s']}s"],
                f"`{r['key']}` is slow for users."))
    for r in perf.get("commands") or []:
        if (r["slow"] >= c["hc_slow_command_min_hits"]
                and 100.0 * r["slow"] / max(r["count"], 1) >= c["hc_slow_command_pct"]):
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
                                    [f"{n} request timeouts since the last check"],
                                    f"`{cmd}` to `{spoke}` times out repeatedly."))

    # Spoke command queues: refusals because the queue was full, or pinned at the cap.
    for spoke, q in (report.get("spoke_queues") or {}).items():
        rejected = int(q.get("rejected_full") or 0)
        cap = int(q.get("max_inflight") or 0)
        full = cap > 0 and int(q.get("inflight") or 0) >= cap
        if rejected < c["hc_queue_rejected_min"] and not full:
            continue
        # The busiest command names the owning module and leads the evidence.
        by_type = q.get("by_type") or {}
        busiest = sorted(by_type.items(), key=lambda kv: (-(kv[1].get("inflight") or 0),
                                                          -(kv[1].get("rejected") or 0)))
        top = busiest[0][0] if busiest else ""
        out.append(_finding(
            "queue", f"{spoke}:queue", f"command queue saturated on {spoke}", "high",
            _module_for_command(top),
            [f"in flight {q.get('inflight')} of {cap}, refused (queue full) {rejected}, "
             f"dropped stale {q.get('dropped_stale', 0)}, backpressure {q.get('backpressure_level', 0)}"]
            + [f"{name}: inflight {v.get('inflight')}, rejected {v.get('rejected')}, "
               f"avg {v.get('avg_s')}s, max {v.get('max_s')}s" for name, v in busiest[:5]],
            f"`{spoke}` is refusing or backing up hub commands."))

    # Data quality: auto-rack failures, NetBox duplicates, DHCP declines.
    fleet = report.get("fleet") or {}
    ar = fleet.get("autorack") or {}
    problems = int(ar.get("conflicts") or 0) + int(ar.get("cannot") or 0)
    if ar and problems >= c["hc_autorack_problems_min"]:
        out.append(_finding(
            "autorack", "autorack", "devices cannot be auto-racked from their names", "medium", "ipam",
            [f"{ar.get('conflicts', 0)} conflict(s), {ar.get('cannot', 0)} cannot place, of "
             f"{ar.get('named', '?')} rack-named devices"]
            + [f"{x.get('name')}: wanted {x.get('wanted') or '?'}, now {x.get('current') or '?'} "
               f"- {x.get('reason') or ''}" for x in ar.get("sample") or []],
            "The auto-rack sweep could not place devices where their names ask."))
    dd = fleet.get("netbox_duplicates") or {}
    if dd and int(dd.get("groups") or 0) >= c["hc_netbox_dupe_groups_min"]:
        out.append(_finding(
            "netbox_dupes", "dupes", "duplicate devices in NetBox", "medium", "ipam",
            [f"{dd['groups']} duplicate group(s) among {dd.get('scanned', '?')} devices"]
            + [f"{x.get('name')} (key {x.get('key')}): {x.get('duplicates')} extra record(s)"
               for x in dd.get("sample") or []],
            "The same device exists more than once in NetBox."))
    if (perf.get("uptime_s") or 0) >= c["hc_command_rate_min_uptime_s"]:
        for r in perf.get("command_rates") or []:
            if r.get("per_hour", 0) >= c["hc_command_rate_per_h"]:
                spoke = names.get(r.get("spoke"), r.get("spoke"))
                out.append(_finding(
                    "call_rate", f"{spoke}:{r['key']}",
                    f"{r['key']} is called very often on {spoke}", "medium",
                    _module_for_command(r["key"]),
                    [f"{r['count']} calls, about {r['per_hour']:.0f} per hour "
                     f"(limit {c['hc_command_rate_per_h']}), hub up {int(perf['uptime_s']) // 60} min"],
                    "A read command is being re-sent far more often than a cache would need."))
    ch = fleet.get("netbox_churn") or {}
    fl = ch.get("flapping") or {}
    wph = int(ch.get("writes_per_hour") or 0)
    nflap = int(fl.get("objects") or 0)
    if ch and (nflap >= c["hc_netbox_flap_objects_min"] or wph >= c["hc_netbox_writes_per_h"]):
        out.append(_finding(
            "netbox_churn", "churn",
            "NetBox is flapping fields" if nflap >= c["hc_netbox_flap_objects_min"]
            else "NetBox write rate is very high", "high", "ipam",
            [f"~{wph} changelog writes/hour (last {int(ch.get('window_s') or 0) // 60} min"
             f"{', sample capped' if ch.get('capped') else ''}); {nflap} device(s) flipped a field "
             f"back to an earlier value; {ch.get('stub_creates', 0)} DEVICE-<mac> stub creates",
             "changes by kind: " + ", ".join(f"{k} {v}" for k, v in (ch.get("by_kind") or {}).items()),
             "most-changed device fields: " + ", ".join(f"{k} {v}" for k, v in ch.get("top_fields") or [])]
            + [f"{x.get('name')}.{x.get('field')}: " + " -> ".join(x.get("values") or [])
               for x in fl.get("sample") or []],
            "Feeders are writing NetBox far more than the data changes."))
    bad = [d for d in fleet.get("dhcp_declines") or []
           if int(d.get("declined") or 0) >= c["hc_dhcp_declined_min"]]
    if bad:
        out.append(_finding(
            "dhcp_declines", "declines", "DHCP addresses stuck declined", "medium", "dhcp",
            [f"{d.get('subnet')}: {d.get('declined')} declined of {d.get('total')}" for d in bad[:10]],
            "Addresses are being DECLINEd and held out of the pool."))

    stale = [s for s in fleet.get("stale_versions") or [] if isinstance(s, dict)]
    if stale:
        out.append(_finding(
            "stale_version", "versions", "spokes stuck on an old version", "medium", "hub",
            [f"{s.get('spoke')}: running {s.get('version')}, latest {s.get('latest')}, "
             f"out of date for {int(s.get('stale_s') or 0) // 60} min" for s in stale[:10]],
            "Spokes are not reaching the latest version after updates."))

    errs: Counter = Counter()
    sample: Dict[str, str] = {}
    vault = 0
    slow_q: List[str] = []
    for ln in recent_lines(log_lines, c["hc_log_window_s"]):
        if "vault.azure.net/secrets" in ln:
            vault += 1
        if re.search(r"slow query|long[- ]running quer|statement timeout|duration: \d{4,} ms", ln, re.I):
            slow_q.append(ln[:240])
        # A traceback is counted by its last line (the exception), not its header.
        if " ERROR " in ln or ln.startswith("ERROR") or _EXC_LINE.match(ln):
            k = _norm(ln)
            errs[k] += 1
            sample.setdefault(k, ln[:300])
    for k, n in errs.most_common(5):
        if n >= c["hc_repeat_error_min"]:
            out.append(_finding("repeat_error", k, f"repeated error: {k[:70]}", "medium", "hub",
                                [f"{n} occurrences in the last {int(c['hc_log_window_s']) // 60} min", sample[k]],
                                "The same error keeps recurring."))
    if vault >= c["hc_vault_gets_min"]:
        out.append(_finding("vault", "vault", "excessive Key Vault reads", "medium", "hub",
                            [f"{vault} vault secret GETs in the last {int(c['hc_log_window_s']) // 60} min"],
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
    import auto_scale
    return {**res, "status": status_of(res), "enabled": bool(cfg["health_check_enabled"]),
            "interval_s": interval,
            "next_run_ts": (res["ts"] + interval) if res.get("ts") and cfg["health_check_enabled"] else None,
            "history": list(_history), "thresholds": {k: v for k, v in cfg.items() if k.startswith("hc_")},
            "autoscale": auto_scale.dashboard(config)}


# The previous run's hub counters, kept on disk so an AB restart does not
# fall back to reporting everything since the hub started.
BASELINE_FILE = "/etc/ab/health_baseline.json"


def _load_baseline() -> Optional[Dict[str, Any]]:
    try:
        with open(BASELINE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_baseline(data: Dict[str, Any]) -> None:
    try:
        tmp = BASELINE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, BASELINE_FILE)
    except OSError as e:
        logger.warning("health check: could not save baseline: %s", e)


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
        import azure_health
        # Full config: cfg only holds health_check keys and would drop CHAT_AZURE_ENABLED.
        azure_findings = azure_health.findings({**(config or {}), **cfg})
        if cfg["hc_azure_enabled"]:
            import auto_scale
            azure_findings += auto_scale.run({**(config or {}), **cfg})
        if not (isinstance(report, dict) and "host" in report):
            why = ("not connected/approved" if report is None and client is None else
                   report.get("message") if isinstance(report, dict) and report.get("message")
                   else "no reply within 30s")
            filed = []
            for f in azure_findings[: int(cfg["hc_max_issues_per_run"])]:
                r = render_issue(f)
                ok, detail = file_escalated_issue(f["module"], r["log_slice"], r["analysis"],
                                                  verdict="health-check", config=config)
                filed.append({"title": f["title"], "module": f["module"], "ok": ok, "detail": detail})
            _last.update(error="hub health report unavailable: %s" % why, findings=azure_findings, filed=filed,
                         running=False, duration_s=round(time.time() - now, 1))
            return last_result()
        prev = _load_baseline()
        _save_baseline(counters(report))
        report = since_last(report, prev)
        lines = [e.get("log", "") for e in get_hub_logs(max_per_module=3000)]
        findings = evaluate(report, lines, cfg) + azure_findings
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
