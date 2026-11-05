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
# json reads/writes the request and status hand-off files shared with the watchdog.
import json
# os provides file existence checks, atomic renames and path joins.
import os
# time drives the polling loop and deadline while waiting on the watchdog.
import time
# uuid gives each request a unique id so a stale status file is never mistaken for ours.
import uuid
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, List, Optional

# Local copy of lm's OS update logic (apt checks, apply, reboot via the sudo helper).
import os_update

# Hub command names this module answers; anything else is not routed here.
COMMANDS = ("OS_UPDATE_CHECK", "OS_UPDATE_APPLY", "OS_UPDATE_APPLY_PACKAGES", "OS_REBOOT")

# ab's config directory, where the request/status hand-off files live.
CONFIG_DIR = "/etc/ab"
# File ab writes to ask the watchdog to run an apply/reboot.
REQUEST_FILE = os.path.join(CONFIG_DIR, "os_update_request.json")
# File the watchdog writes with the outcome of that request.
STATUS_FILE = os.path.join(CONFIG_DIR, "os_update_status.json")
# Optional OS-update capabilities ab advertises to the hub after a check.
FEATURES = ["packages", "reboot"]
# The hub waits up to ~1h for an apply; stop a little before it gives up.
APPLY_WAIT_S = 3500
# A reboot request only needs to be picked up and started, so wait less than a minute.
REBOOT_WAIT_S = 50
# How often (seconds) to re-read the status file while waiting.
POLL_S = 2.0

# Message shown when the root-owned sudo helper is missing, so patching is impossible.
NO_HELPER = ("the lm-os-update helper is not installed on LM-AB — "
             "re-run ab's install.sh to enable patching")


def handle_command(cmd_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Handle one hub OS-update command and return the hub-facing result dict."""
    # A check is read-only, so ab can answer it directly without the watchdog.
    if cmd_type == "OS_UPDATE_CHECK":
        res = os_update.check_updates(refresh=False)  # ab.service can't run apt-get update
        # When updates are available, advertise the extra features and whether apply can run.
        if res.get("eligible"):
            res["features"] = FEATURES
            res["apply_blocker"] = "" if os.path.exists(os_update.HELPER) else NO_HELPER
        # Default to SUCCESS if the check did not set a status itself.
        res.setdefault("status", "SUCCESS")
        return res
    # Every remaining command needs the sudo helper; fail early with a clear message if absent.
    if not os.path.exists(os_update.HELPER):
        return {"status": "ERROR", "message": NO_HELPER}
    # Reboot the host via the watchdog.
    if cmd_type == "OS_REBOOT":
        return _delegate("reboot", None, REBOOT_WAIT_S)
    # Apply only the specific packages the hub selected.
    if cmd_type == "OS_UPDATE_APPLY_PACKAGES":
        return _delegate("apply", list(data.get("packages") or []), APPLY_WAIT_S)
    # Anything else is a full apply of all pending updates.
    return _delegate("apply", None, APPLY_WAIT_S)


def _delegate(action: str, packages: Optional[List[str]], wait_s: float) -> Dict[str, Any]:
    """Ask the watchdog to run ``action`` and wait for its result."""
    # Refuse to queue a second request while one is still waiting to be picked up.
    if os.path.exists(REQUEST_FILE):
        return {"status": "ERROR", "message": "an OS update is already queued on LM-AB"}
    # Tag this request with a unique id so we can match the watchdog's reply to it.
    req_id = uuid.uuid4().hex
    # Write to a temp file first, then rename, so the watchdog never reads a half-written file.
    tmp = REQUEST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"id": req_id, "action": action, "packages": packages}, f)
    os.replace(tmp, REQUEST_FILE)
    # Stop waiting after wait_s seconds so the hub gets an answer before it gives up.
    deadline = time.time() + wait_s
    # Poll the status file until the watchdog reports this request done or we time out.
    while time.time() < deadline:
        time.sleep(POLL_S)
        # Read the latest status written by the watchdog.
        try:
            with open(STATUS_FILE) as f:
                status = json.load(f)
        # The file may not exist yet or may be mid-write; just try again on the next poll.
        except (OSError, ValueError):
            continue
        # Only accept a finished status carrying our own request id.
        if status.get("id") == req_id and status.get("state") == "done":
            return status.get("result") or {"status": "ERROR", "message": "no result"}
    # The deadline passed without a matching result.
    return {"status": "ERROR",
            "message": f"the LM-AB watchdog did not finish the {action} within {int(wait_s)}s"}


def run_request() -> None:
    """Watchdog side: run a queued request (if any) and record its result."""
    # Load the pending request, if any.
    try:
        with open(REQUEST_FILE) as f:
            req = json.load(f)
    # No request queued (or the file is unreadable): nothing to do this cycle.
    except (OSError, ValueError):
        return
    # Claimed before running, so the request can never run twice.
    os.remove(REQUEST_FILE)
    # Run the requested action; a reboot goes to reboot_node, everything else is an apply.
    try:
        if req.get("action") == "reboot":
            result = os_update.reboot_node()
        else:
            result = os_update.apply_updates(req.get("packages"))
    # Never let a failure crash the watchdog; report it back to ab as an error result.
    except Exception as exc:  # noqa: BLE001
        result = {"status": "ERROR", "message": f"watchdog: {exc}"}
    # Write the result atomically (temp file + rename) so ab never sees a partial file.
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"id": req.get("id"), "state": "done", "result": result,
                   "at": time.time()}, f)
    os.replace(tmp, STATUS_FILE)
