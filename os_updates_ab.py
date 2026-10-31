"""Answer the hub's OS Updates commands for the AppBuilder host.

ab has its own hub client (hub_agent.py) rather than lm's shared control plane,
so these commands are handled here.

ab.service runs as svc_bg with ``CapabilityBoundingSet=CAP_NET_BIND_SERVICE``,
so it cannot sudo. A check only reads apt's package lists (the host's apt-daily
timer refreshes them). Apply and reboot are handed to the watchdog, which runs
as the same user WITHOUT that lock: ab writes ``REQUEST_FILE``, the watchdog
runs ``os_update`` (which uses the root-owned ``lm-os-update`` sudo helper
installed by install.sh) and writes the result to ``STATUS_FILE``, which ab
waits for and returns to the hub. Same delegation as ollama-setup and restart.

``os_update.py`` and ``lm-os-update`` are verbatim copies of lm's
core/src/os_update.py and scripts/lm-os-update — keep them in sync rather than
editing them here.
"""
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import os_update

COMMANDS = ("OS_UPDATE_CHECK", "OS_UPDATE_APPLY", "OS_UPDATE_APPLY_PACKAGES", "OS_REBOOT")

CONFIG_DIR = "/etc/ab"
REQUEST_FILE = os.path.join(CONFIG_DIR, "os_update_request.json")
STATUS_FILE = os.path.join(CONFIG_DIR, "os_update_status.json")
FEATURES = ["packages", "reboot"]
# The hub waits up to ~1h for an apply; stop a little before it gives up.
APPLY_WAIT_S = 3500
REBOOT_WAIT_S = 50
POLL_S = 2.0

NO_HELPER = ("the lm-os-update helper is not installed on LM-AB — "
             "re-run ab's install.sh to enable patching")


def handle_command(cmd_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if cmd_type == "OS_UPDATE_CHECK":
        res = os_update.check_updates(refresh=False)  # ab.service can't run apt-get update
        if res.get("eligible"):
            res["features"] = FEATURES
            res["apply_blocker"] = "" if os.path.exists(os_update.HELPER) else NO_HELPER
        res.setdefault("status", "SUCCESS")
        return res
    if not os.path.exists(os_update.HELPER):
        return {"status": "ERROR", "message": NO_HELPER}
    if cmd_type == "OS_REBOOT":
        return _delegate("reboot", None, REBOOT_WAIT_S)
    if cmd_type == "OS_UPDATE_APPLY_PACKAGES":
        return _delegate("apply", list(data.get("packages") or []), APPLY_WAIT_S)
    return _delegate("apply", None, APPLY_WAIT_S)


def _delegate(action: str, packages: Optional[List[str]], wait_s: float) -> Dict[str, Any]:
    """Ask the watchdog to run ``action`` and wait for its result."""
    if os.path.exists(REQUEST_FILE):
        return {"status": "ERROR", "message": "an OS update is already queued on LM-AB"}
    req_id = uuid.uuid4().hex
    tmp = REQUEST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"id": req_id, "action": action, "packages": packages}, f)
    os.replace(tmp, REQUEST_FILE)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(POLL_S)
        try:
            with open(STATUS_FILE) as f:
                status = json.load(f)
        except (OSError, ValueError):
            continue
        if status.get("id") == req_id and status.get("state") == "done":
            return status.get("result") or {"status": "ERROR", "message": "no result"}
    return {"status": "ERROR",
            "message": f"the LM-AB watchdog did not finish the {action} within {int(wait_s)}s"}


def run_request() -> None:
    """Watchdog side: run a queued request (if any) and record its result."""
    try:
        with open(REQUEST_FILE) as f:
            req = json.load(f)
    except (OSError, ValueError):
        return
    # Claimed before running, so the request can never run twice.
    os.remove(REQUEST_FILE)
    try:
        if req.get("action") == "reboot":
            result = os_update.reboot_node()
        else:
            result = os_update.apply_updates(req.get("packages"))
    except Exception as exc:  # noqa: BLE001
        result = {"status": "ERROR", "message": f"watchdog: {exc}"}
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"id": req.get("id"), "state": "done", "result": result,
                   "at": time.time()}, f)
    os.replace(tmp, STATUS_FILE)
