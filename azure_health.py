"""Azure fleet health: the LM resource group's VMs, reviewed by the hub health check.

Reads the VMs through the secretless service-principal login (``az_console``,
read-only ``az`` shapes only) and reports what only Azure can see: a burstable
(B-series) VM that has run out of CPU credits and is being throttled, a VM
whose guest agent has stopped reporting, a VM that is not running. Findings use
the same shape as ``health_check`` so they are filed, deduped and shown the same
way. ``evaluate`` is pure and unit-tested; the I/O sits in ``collect``.
"""
import json
import logging
from typing import Any, Dict, List, Optional

import az_console

logger = logging.getLogger("ab.azure_health")

DEFAULTS = {
    "hc_azure_enabled": True,          # only acts when CHAT_AZURE_ENABLED is on
    "hc_azure_credits_min": 30.0,      # burstable VM below this many CPU credits is throttled
    "hc_azure_credits_cpu_pct": 15.0,  # ...and only when it is actually busy
}

_HINT = ("The VM is burstable and has used its CPU credits, so Azure caps it at its baseline. "
         "First remove the work that burns CPU (a loop that rewrites data it did not change, "
         "a chatty sync, an unbounded housekeeping job). If load is genuine, resize to a "
         "non-burstable size (`az vm resize`, a few minutes of downtime) — that needs "
         "CHAT_AZURE_ALLOW_MUTATION and a human decision on cost.")


def _json(res: Dict[str, Any]) -> Any:
    """Parsed stdout of an az result, or None when it failed or is not JSON."""
    if res.get("error") or res.get("exit_code"):
        return None
    try:
        return json.loads(res.get("stdout") or "null")
    except ValueError:
        return None


def collect(config: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """One dict per VM: name, size, power, agent, credits, cpu_pct (None when unknown).
    Returns None when Azure access is off or the VM list is unavailable."""
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
        row = {"name": v.get("name"), "size": v.get("size") or "", "power": "", "agent": "",
               "credits": None, "cpu_pct": None}
        view = _json(az_console.run_az(
            ["vm", "get-instance-view", "-g", rg, "-n", v["name"], "--query",
             "{power:instanceView.statuses[1].displayStatus,"
             "agent:instanceView.vmAgent.statuses[0].displayStatus}", "-o", "json"], config))
        if isinstance(view, dict):
            row.update(power=view.get("power") or "", agent=view.get("agent") or "")
        if row["size"].startswith("Standard_B"):
            mets = _json(az_console.run_az(
                ["monitor", "metrics", "list", "--resource", v["id"], "--metric",
                 "CPU Credits Remaining", "Percentage CPU", "--interval", "PT15M", "--offset", "1h",
                 "--aggregation", "Average", "-o", "json", "--query",
                 "value[].{n:name.value,v:timeseries[0].data[-1].average}"], config))
            for m in mets if isinstance(mets, list) else []:
                if m.get("n") == "CPU Credits Remaining":
                    row["credits"] = m.get("v")
                elif m.get("n") == "Percentage CPU":
                    row["cpu_pct"] = m.get("v")
        out.append(row)
    return out


def _finding(kind: str, name: str, title: str, severity: str, evidence: List[str], detail: str) -> Dict[str, Any]:
    t = f"Health check: {title}"
    return {"kind": kind, "key": name, "severity": severity, "module": "hub", "fingerprint": t,
            "title": t, "detail": detail, "evidence": evidence, "hint": _HINT if kind == "azure_credits" else
            "Check the VM in Azure (boot diagnostics, serial console); a restart through "
            "`az vm restart` clears a hung guest agent."}


def evaluate(vms: Optional[List[Dict[str, Any]]], cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Findings for a list of VM rows from ``collect``. Pure."""
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS and v is not None})
    found = []
    for v in vms or []:
        name = v.get("name") or "?"
        ev = [f"{name} {v.get('size')} power={v.get('power')} agent={v.get('agent')} "
              f"credits={v.get('credits')} cpu={v.get('cpu_pct')}"]
        if v.get("power") and "running" not in v["power"].lower():
            found.append(_finding("azure_power", name, f"Azure VM {name} is not running", "high", ev,
                                  f"{name} reports power state {v['power']}."))
            continue
        if v.get("agent") and v["agent"].lower() != "ready":
            found.append(_finding("azure_agent", name, f"Azure VM {name} guest agent is {v['agent']}", "high", ev,
                                  f"{name} is running but its Azure guest agent is not ready, so run-command and "
                                  "extensions cannot reach it."))
        credits, cpu = v.get("credits"), v.get("cpu_pct")
        if (credits is not None and credits < float(c["hc_azure_credits_min"])
                and (cpu or 0) >= float(c["hc_azure_credits_cpu_pct"])):
            found.append(_finding("azure_credits", name, f"Azure VM {name} is out of CPU credits", "high", ev,
                                  f"{name} ({v.get('size')}) has {credits:.1f} CPU credits left at {cpu:.0f}% CPU, "
                                  "so Azure is throttling it to its baseline."))
    return found


def findings(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect and evaluate; empty when Azure access is off. Never raises."""
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if k in DEFAULTS and v is not None})
    if not cfg["hc_azure_enabled"]:
        return []
    try:
        return evaluate(collect(config), cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("azure health check failed: %s", e)
        return []
