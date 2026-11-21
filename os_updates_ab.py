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
import fcntl
import json
import logging
import math
# os provides file existence checks, atomic renames and path joins.
import os
# time drives the polling loop and deadline while waiting on the watchdog.
import time
import tempfile
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
logger = logging.getLogger(__name__)


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


def _publish_json(path: str, data: Dict[str, Any], *, exclusive: bool = False) -> None:
    """Publish a complete file, optionally refusing to replace a queued request."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".",
                delete=False) as handle:
            tmp = handle.name
            json.dump(data, handle)
        # A hard link publishes atomically without overwriting an unclaimed request.
        if exclusive:
            os.link(tmp, path)
        else:
            os.replace(tmp, path)
            tmp = None
    finally:
        if tmp is not None:
            _remove_private(tmp)


def _remove_private(path: str) -> None:
    """Remove only our private handoff file, logging a cleanup failure."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        logger.error("OS handoff cleanup failed for %s: %s", path, error)


def _read_request(path: str) -> tuple[Dict[str, Any], float]:
    """Read one request and the publication time of that exact opened file."""
    with open(path) as handle:
        request = json.load(handle)
        modified = os.fstat(handle.fileno()).st_mtime
    if not isinstance(request, dict):
        raise ValueError("OS request must be an object")
    return request, modified


def _request_expiry(request: Dict[str, Any], modified: float) -> float:
    """Validate a lease, deriving a bounded deadline for older producers."""
    if (not isinstance(request.get("id"), str) or not request["id"]
            or request.get("action") not in ("apply", "reboot")):
        raise ValueError("invalid OS request identity or action")
    # Legacy producers already used these wait bounds; mtime supplies their start.
    wait = REBOOT_WAIT_S if request["action"] == "reboot" else APPLY_WAIT_S
    expires = request.get("expires_at", modified + wait)
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise ValueError("invalid OS request deadline")
    try:
        expires = float(expires)
    except OverflowError:
        raise ValueError("invalid OS request deadline") from None
    if not math.isfinite(expires):
        raise ValueError("invalid OS request deadline")
    return expires


def _claim_request(kind: str) -> Optional[str]:
    """Atomically win a queued file against the consumer or timeout withdrawal."""
    claimed = f"{REQUEST_FILE}.{kind}.{uuid.uuid4().hex}"
    try:
        os.rename(REQUEST_FILE, claimed)
    except FileNotFoundError:
        return None
    return claimed


def _cancel_queued(req_id: str, kind: str = "cancel") -> bool:
    """Withdraw only the matching request; preserve foreign files for diagnosis."""
    claimed = _claim_request(kind)
    if claimed is None:
        return False
    try:
        request, _ = _read_request(claimed)
    except (OSError, ValueError) as error:
        logger.error("Cannot identify withdrawn OS request %s: %s", claimed, error)
        return False
    if request.get("id") != req_id:
        logger.error("Foreign OS request preserved at %s; withdrawal outcome unknown", claimed)
        return False
    _remove_private(claimed)
    return True


def _delegate(action: str, packages: Optional[List[str]], wait_s: float) -> Dict[str, Any]:
    """Serialize producer lifetimes, including withdrawal, across threads/processes."""
    try:
        with open(REQUEST_FILE + ".lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "ERROR", "message": "an OS update is already queued on LM-AB"}
            return _delegate_locked(action, packages, wait_s)
    except OSError as error:
        logger.error("OS watchdog handoff failed: %s", error)
        return {"status": "ERROR", "message": f"OS watchdog handoff failed; outcome unknown: {error}"}


def _delegate_locked(action: str, packages: Optional[List[str]], wait_s: float) -> Dict[str, Any]:
    """Publish under producer ownership and wait without extending a queued lease."""
    # A dead producer can leave a request, but never an indefinitely valid lease.
    try:
        previous, modified = _read_request(REQUEST_FILE)
        expires = _request_expiry(previous, modified)
    except FileNotFoundError:
        pass
    except ValueError as error:
        logger.error("Queued OS request is invalid: %s", error)
        return {"status": "ERROR", "message": f"queued OS request is invalid: {error}"}
    else:
        if expires > time.time():
            return {"status": "ERROR", "message": "an OS update is already queued on LM-AB"}
        if not _cancel_queued(previous["id"], "expired"):
            return {"status": "ERROR", "message": "queued OS request changed; outcome unknown"}

    req_id = uuid.uuid4().hex
    deadline = time.monotonic() + wait_s
    try:
        _publish_json(REQUEST_FILE, {
            "id": req_id, "action": action, "packages": packages,
            "expires_at": time.time() + wait_s,
        }, exclusive=True)
    except FileExistsError:
        return {"status": "ERROR", "message": "an OS update is already queued on LM-AB"}

    # A status for another request is not an acknowledgement of this action.
    while time.monotonic() < deadline:
        time.sleep(min(POLL_S, max(0.0, deadline - time.monotonic())))
        # Read the latest status written by the watchdog.
        try:
            with open(STATUS_FILE) as f:
                status = json.load(f)
        # The file may not exist yet or may be mid-write; just try again on the next poll.
        except (OSError, ValueError):
            continue
        # Only accept a finished status carrying our own request id.
        if isinstance(status, dict) and status.get("id") == req_id and status.get("state") == "done":
            return status.get("result") or {"status": "ERROR", "message": "no result"}
    withdrawn = _cancel_queued(req_id)
    detail = ("queued request withdrawn; not executed"
              if withdrawn else "action may still be running or completed; outcome unknown")
    return {"status": "ERROR", "message":
            f"the LM-AB watchdog did not finish the {action} within {int(wait_s)}s; {detail}"}


def run_request() -> None:
    """Claim once, reject expired work and publish the result before releasing ownership."""
    claimed = _claim_request("claim")
    if claimed is None:
        return
    req = {}
    try:
        # Recheck the lease after ownership is won, before any privileged action.
        try:
            req, modified = _read_request(claimed)
            expires = _request_expiry(req, modified)
            if time.time() >= expires:
                result = {"status": "ERROR", "message": "OS request expired before start; not executed"}
            elif req["action"] == "reboot":
                result = os_update.reboot_node()
            else:
                result = os_update.apply_updates(req.get("packages"))
        except Exception as error:  # noqa: BLE001 - never retry a claimed privileged action
            logger.error("OS watchdog request failed: %s", error)
            result = {"status": "ERROR", "message": f"watchdog: {error}"}
        # Lost acknowledgement is never repaired by replaying the claimed action.
        _publish_json(STATUS_FILE, {
            "id": req.get("id"), "state": "done", "result": result, "at": time.time(),
        })
    except OSError as error:
        logger.error("OS watchdog status publication failed: %s", error)
        raise
    finally:
        _remove_private(claimed)
