"""Platform Auto Scale: grow an LM Azure VM, but only to a size the operator allowed.

Each health-check run reads every VM's last 2 hours (free memory, CPU, CPU
credits) and last 7 days from Azure Monitor and decides per VM:

  grow        it is short of memory or CPU and a larger allowed size fixes it;
              AB resizes it (one VM per run, then 24h cooldown per VM)
  needs_work  it is short and no allowed size is larger: AB does not resize,
              it files a finding so the workload gets fixed (as the NetBox
              device-walk work was) or a larger size gets allowed
  shrink      a cheaper allowed size would still leave 2x headroom; shown as
              a recommendation only, never done automatically
  ok          nothing to do

Allowed sizes are picked per VM under Settings -> Monitoring. A VM with none
picked may only stay at its current size. LM-AB is never resized automatically
(az_console.NEVER_RESIZE); AB only recommends a size for itself.
``decide`` is pure and unit-tested; Azure I/O is in ``collect`` and ``run``.
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import az_console

logger = logging.getLogger("ab.auto_scale")

# Sizes AB may choose from: vCPUs, memory, baseline CPU % per vCPU (100 = not
# burstable) and Linux pay-as-you-go $/month in westus3 (Azure retail prices, Nov 2026).
CATALOG = [
    {"size": "Standard_B2ats_v2", "vcpu": 2, "ram_gb": 1, "baseline_pct": 20, "usd_month": 6.86},
    {"size": "Standard_B2als_v2", "vcpu": 2, "ram_gb": 4, "baseline_pct": 30, "usd_month": 27.45},
    {"size": "Standard_B2as_v2", "vcpu": 2, "ram_gb": 8, "baseline_pct": 40, "usd_month": 54.90},
    {"size": "Standard_F2s_v2", "vcpu": 2, "ram_gb": 4, "baseline_pct": 100, "usd_month": 61.76},
    {"size": "Standard_D2as_v5", "vcpu": 2, "ram_gb": 8, "baseline_pct": 100, "usd_month": 62.78},
    {"size": "Standard_B4als_v2", "vcpu": 4, "ram_gb": 8, "baseline_pct": 30, "usd_month": 97.09},
    {"size": "Standard_B4as_v2", "vcpu": 4, "ram_gb": 16, "baseline_pct": 40, "usd_month": 109.50},
    {"size": "Standard_F4s_v2", "vcpu": 4, "ram_gb": 8, "baseline_pct": 100, "usd_month": 123.37},
    {"size": "Standard_D4as_v5", "vcpu": 4, "ram_gb": 16, "baseline_pct": 100, "usd_month": 125.56},
]
_SPEC = {c["size"]: c for c in CATALOG}

DEFAULTS = {
    "autoscale_enabled": True,         # resize for real (grow only); off = show what it would do
    "autoscale_mem_free_pct": 10.0,    # short of memory: free memory below this for the whole 2h
    "autoscale_cpu_pct": 85.0,         # short of CPU (not burstable): CPU above this for the whole 2h
    "autoscale_credits_min": 30.0,     # short of CPU (burstable): credits below this while above baseline
    "autoscale_cooldown_h": 24,        # hours between two resizes of the same VM
}

STATE_FILE = "/etc/ab/autoscale.json"
HISTORY_MAX = 50
_GIB = 2 ** 30


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (config or {}).items() if k in DEFAULTS and v is not None})
    return c


def _capacity(spec: Dict[str, Any]) -> float:
    """CPU the size can use all day without running out of credits, in vCPUs."""
    return spec["vcpu"] * spec["baseline_pct"] / 100


def _cheapest(sizes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return min(sizes, key=lambda s: s["usd_month"]) if sizes else None


def decide(vm: Dict[str, Any], allowed: List[str], cfg: Dict[str, Any],
           last_resize_ts: float = 0.0, now: Optional[float] = None) -> Dict[str, Any]:
    """What to do with one VM row from ``collect``. Pure.
    Returns {"action", "to", "why"}; action is grow, needs_work, cooldown,
    recommend (LM-AB), shrink or ok."""
    c = _cfg(cfg)
    now = time.time() if now is None else now
    cur = _SPEC.get(vm.get("size") or "")
    if not cur:
        return {"action": "ok", "to": None, "why": f"{vm.get('size')} is not in the size catalog"}
    choices = [_SPEC[s] for s in allowed or [] if s in _SPEC and s != cur["size"]]
    ram = cur["ram_gb"] * _GIB

    free, cpu = vm.get("free_2h") or [], vm.get("cpu_2h") or []
    short_mem = bool(free) and max(free) < ram * float(c["autoscale_mem_free_pct"]) / 100
    if cur["baseline_pct"] < 100:
        credits = vm.get("credits")
        short_cpu = (credits is not None and credits < float(c["autoscale_credits_min"])
                     and bool(cpu) and sum(cpu) / len(cpu) >= cur["baseline_pct"])
    else:
        short_cpu = bool(cpu) and min(cpu) >= float(c["autoscale_cpu_pct"])

    if short_mem or short_cpu:
        why = " and ".join(x for x, on in (
            (f"free memory stayed under {c['autoscale_mem_free_pct']:g}% for 2h", short_mem),
            ("CPU stayed above what the size sustains for 2h", short_cpu)) if on)
        bigger = [s for s in choices
                  if s["ram_gb"] >= cur["ram_gb"] and _capacity(s) >= _capacity(cur)
                  and (not short_mem or s["ram_gb"] > cur["ram_gb"])
                  and (not short_cpu or _capacity(s) > _capacity(cur))]
        to = _cheapest(bigger)
        if (vm.get("name") or "").upper() in az_console.NEVER_RESIZE:
            return {"action": "recommend", "to": to and to["size"], "why": why}
        if not to:
            return {"action": "needs_work", "to": None, "why": why}
        if now - last_resize_ts < float(c["autoscale_cooldown_h"]) * 3600:
            return {"action": "cooldown", "to": to["size"], "why": why}
        return {"action": "grow", "to": to["size"], "why": why}

    # Shrink only on a full week of data, keeping 2x the week's peak memory use
    # and 2x its average CPU within the smaller size's baseline.
    free_min, cpu_avg = vm.get("free_7d_min"), vm.get("cpu_7d_avg")
    if free_min is None or cpu_avg is None or (vm.get("days_7d") or 0) < 7:
        return {"action": "ok", "to": None, "why": "not short of memory or CPU"}
    used, cpu_vcpus = max(ram - free_min, 0), cpu_avg / 100 * cur["vcpu"]
    smaller = [s for s in choices if s["usd_month"] < cur["usd_month"]
               and s["ram_gb"] * _GIB >= 2 * used and _capacity(s) >= 2 * cpu_vcpus]
    to = _cheapest(smaller)
    if to:
        return {"action": "shrink", "to": to["size"],
                "why": f"7-day peak memory use {used / _GIB:.1f} GB and average CPU "
                       f"{cpu_vcpus:.2f} vCPU fit with 2x headroom (saves "
                       f"${cur['usd_month'] - to['usd_month']:.2f}/month)"}
    return {"action": "ok", "to": None, "why": "not short of memory or CPU"}


def _json(res: Dict[str, Any]) -> Any:
    if res.get("error") or res.get("exit_code"):
        return None
    try:
        return json.loads(res.get("stdout") or "null")
    except ValueError:
        return None


def _series(rows: Any, name: str, field: str) -> List[float]:
    for m in rows if isinstance(rows, list) else []:
        if m.get("n") == name:
            return [float(x) for x in m.get(field) or [] if x is not None]
    return []


def collect(config: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """One row per VM: name, size, 2h free memory/CPU series, credits, 7-day
    minimum free memory and average CPU. None when Azure access is off."""
    if not az_console.azure_enabled(config):
        return None
    rg = az_console._resource_group(config)
    vms = _json(az_console.run_az(
        ["vm", "list", "-g", rg, "--query", "[].{name:name,id:id,size:hardwareProfile.vmSize}",
         "-o", "json"], config))
    if not isinstance(vms, list):
        return None
    out = []
    for v in vms:
        names = ["Percentage CPU", "Available Memory Bytes"]
        if (v.get("size") or "").startswith("Standard_B"):
            names.append("CPU Credits Remaining")
        recent = _json(az_console.run_az(
            ["monitor", "metrics", "list", "--resource", v["id"], "--metric", *names,
             "--interval", "PT15M", "--offset", "2h", "--aggregation", "Average", "-o", "json",
             "--query", "value[].{n:name.value,avg:timeseries[0].data[].average}"], config))
        week = _json(az_console.run_az(
            ["monitor", "metrics", "list", "--resource", v["id"], "--metric", *names[:2],
             "--interval", "P1D", "--offset", "7d", "--aggregation", "Average", "Minimum", "-o", "json",
             "--query", "value[].{n:name.value,avg:timeseries[0].data[].average,"
                        "min:timeseries[0].data[].minimum}"], config))
        credits = _series(recent, "CPU Credits Remaining", "avg")
        cpu_days, free_days = _series(week, "Percentage CPU", "avg"), _series(week, "Available Memory Bytes", "min")
        out.append({"name": v.get("name"), "size": v.get("size") or "",
                    "cpu_2h": _series(recent, "Percentage CPU", "avg"),
                    "free_2h": _series(recent, "Available Memory Bytes", "avg"),
                    "credits": credits[-1] if credits else None,
                    "cpu_7d_avg": sum(cpu_days) / len(cpu_days) if cpu_days else None,
                    "free_7d_min": min(free_days) if free_days else None,
                    "days_7d": min(len(cpu_days), len(free_days))})
    return out


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: Dict[str, Any]) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        logger.warning("auto scale: could not save state: %s", e)


def known_vms(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """VMs for the settings picker: the last run's VMs plus any with saved choices."""
    seen = {r["name"]: r.get("size") or "" for r in load_state().get("vms") or [] if r.get("name")}
    for name in ((config or {}).get("autoscale_allowed") or {}):
        seen.setdefault(name, "")
    return [{"name": n, "size": seen[n]} for n in sorted(seen)]


def _top_processes(vm: str, config: Dict[str, Any]) -> str:
    res = az_console.run_server_shell(vm, "ps -eo comm,rss,pcpu --sort=-rss | head -6", config)
    return (res.get("output") or res.get("error") or "").strip()


def _finding(vm: Dict[str, Any], d: Dict[str, Any], title: str, detail: str,
             severity: str, extra: Optional[List[str]] = None) -> Dict[str, Any]:
    t = f"Health check: {title}"
    ev = [f"{vm['name']} {vm['size']} free_2h_GB={[round(x / _GIB, 2) for x in vm.get('free_2h') or []]} "
          f"cpu_2h={[round(x) for x in vm.get('cpu_2h') or []]} credits={vm.get('credits')}"] + (extra or [])
    return {"kind": "autoscale_" + d["action"], "key": vm["name"], "severity": severity, "module": "hub",
            "fingerprint": t, "title": t, "detail": detail, "evidence": ev,
            "hint": "Find what uses the memory or CPU on the VM (top processes above) and fix the "
                    "workload, as was done for the NetBox device walks. If the load is genuine, allow "
                    "a larger size under Settings -> Monitoring -> Platform Auto Scale."}


def run(config: Dict[str, Any], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Collect, decide, resize at most one VM, save the state for the Health
    page. Returns health findings. Never raises."""
    c = _cfg(config)
    now = time.time() if now is None else now
    try:
        vms = collect(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto scale: collect failed: %s", e)
        return []
    if vms is None:
        return []
    state = load_state()
    last = state.get("last_resize") or {}
    history = state.get("history") or []
    allowed = (config or {}).get("autoscale_allowed") or {}
    found, rows, resized = [], [], False
    for vm in vms:
        d = decide(vm, allowed.get(vm["name"]) or [], c, float(last.get(vm["name"]) or 0), now)
        if d["action"] == "grow":
            if not c["autoscale_enabled"]:
                d["why"] += " (auto scale is off)"
            elif resized:
                d["why"] += " (one resize per run; next run)"
            else:
                resized = True
                res = az_console.resize_vm(vm["name"], d["to"], config)
                ok = not res.get("error") and not res.get("exit_code")
                detail = (res.get("error") or res.get("stderr") or "").strip()[:300]
                history.append({"ts": now, "vm": vm["name"], "from": vm["size"], "to": d["to"],
                                "ok": ok, "why": d["why"], "detail": detail})
                logger.info("auto scale: resize %s %s -> %s: %s", vm["name"], vm["size"], d["to"],
                            "ok" if ok else detail)
                if ok:
                    last[vm["name"]] = now
                    d["action"] = "grown"
                else:
                    found.append(_finding(vm, d, f"Platform Auto Scale could not resize {vm['name']}",
                                          f"Resizing {vm['name']} from {vm['size']} to {d['to']} failed: "
                                          f"{detail}", "high"))
        elif d["action"] == "needs_work":
            found.append(_finding(vm, d, f"{vm['name']} needs more than its largest allowed size",
                                  f"{vm['name']} ({vm['size']}): {d['why']}, and no larger size is allowed "
                                  "for it, so Platform Auto Scale did not resize it.", "high",
                                  [_top_processes(vm["name"], config)]))
        elif d["action"] == "recommend":
            found.append(_finding(vm, d, f"{vm['name']} is short of resources",
                                  f"{vm['name']} ({vm['size']}): {d['why']}. AppBuilder runs here and never "
                                  f"resizes itself; resize it by hand"
                                  + (f" to {d['to']}." if d["to"] else " or fix the workload."), "medium",
                                  [_top_processes(vm["name"], config)]))
        rows.append({"name": vm["name"], "size": vm["size"], **d})
    _save_state({"ts": now, "vms": rows, "last_resize": last, "history": history[-HISTORY_MAX:]})
    return found


def dashboard(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The Health page's Platform Auto Scale panel: last decisions and resize history."""
    s = load_state()
    return {"enabled": bool(_cfg(config)["autoscale_enabled"]), "ts": s.get("ts"),
            "vms": s.get("vms") or [], "history": list(reversed(s.get("history") or []))[:10],
            "allowed": (config or {}).get("autoscale_allowed") or {}}
