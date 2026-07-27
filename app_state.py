"""Shared mutable application state for BugFixer.

Extracted verbatim from main.py so the modules split out of main (llm_client,
workers, ollama_setup, ...) can share the single module-level ``state`` dict and
its locks without a circular import back through main. main.py re-exports these
names via ``from app_state import *`` (placed after config_store + llm_client are
re-exported), so the existing ``from main import state`` / ``update_task_state``
surface used by routes.py and the sibling modules is preserved unchanged.

Everything app_state needs to build ``state`` (load_config, load_processed,
get_version, _llm_cb_snapshot, _provider_credit_cb_snapshot, logger) is imported
from main. main re-exports config_store and llm_client names into its namespace
*before* it imports app_state, so these ``from main import`` lookups resolve.
"""
import os
import threading
from datetime import datetime

from main import (
    logger,
    load_config,
    load_processed,
    get_version,
    _llm_cb_snapshot,
    _provider_credit_cb_snapshot,
)

_task_state_lock = threading.Lock()
_chat_lock = threading.RLock()


def update_task_state(task_id, task_name="Unknown Task", action="start", kind="scan"):
    """Manages active tasks and their start times. action can be 'start' or 'end'.
    kind tags the work type for the UI ('scan'/'fix' vs 'pr' for PR pre-review)."""
    global state
    if not task_id:
        logger.debug("update_task_state called with no task_id; ignoring.")
        return
    try:
        if action == "start":
            with _task_state_lock:
                # Same task_id restarting for a new sub-step (e.g. the next CPU-ensemble
                # model) — carry the previous reasoning forward instead of blanking the
                # "AI Thought Process" panel to empty between the many short model calls.
                prev = state["active_tasks"].get(task_id) or {}
                carried = prev.get("stream") or ""
                if carried:
                    carried = carried.rstrip() + f"\n\n── {task_name} ──\n"
                state["active_tasks"][task_id] = {
                    "name": task_name,
                    "start_time": datetime.now(),
                    "stream": carried,
                    "kind": kind,
                }
            logger.info(f"Task started: {task_id} - {task_name}")
        elif action == "end":
            with _task_state_lock:
                if task_id in state["active_tasks"]:
                    del state["active_tasks"][task_id]
            logger.info(f"Task completed: {task_id}")
    except Exception as e:
        logger.error(f"update_task_state failed for task_id={task_id!r} action={action!r}: {e}")


_PR_REVIEWS_MAX = 100


def record_pr_review(repo, number, title, url, findings, head_sha):
    """Persist a PR pre-review result so the UI can list/filter 'PRs Reviewed'.
    Bounded to the most recent _PR_REVIEWS_MAX. findings = list of {level,...} dicts."""
    global state
    levels = {"error": 0, "warning": 0, "advisory": 0}
    for f in (findings or []):
        lvl = (f or {}).get("level")
        if lvl in levels:
            levels[lvl] += 1
    try:
        with _task_state_lock:
            state["pr_reviews"]["%s#%s" % (repo, number)] = {
                "repo": repo,
                "number": number,
                "title": (title or "")[:120],
                "url": url,
                "head": head_sha,
                "findings": len(findings or []),
                "errors": levels["error"],
                "warnings": levels["warning"],
                "advisories": levels["advisory"],
                "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            # Bound: drop oldest (dicts preserve insertion order) beyond the cap.
            extra = len(state["pr_reviews"]) - _PR_REVIEWS_MAX
            if extra > 0:
                for k in list(state["pr_reviews"].keys())[:extra]:
                    state["pr_reviews"].pop(k, None)
    except Exception as e:
        logger.error(f"record_pr_review failed for {repo}#{number}: {e}")


config_on_start = load_config()
processed_init = load_processed()

# Statuses that count as "resolved". "resolved" = a human clicked Resolved
# (human-confirmed sign-off); the others are fix-verified/awaiting states.
_RESOLVED_STATUSES = ("fixed", "verified", "awaiting_prod_verification", "resolved")


# Counters are DERIVED from the processed store (the single source of truth) so a
# reopen → re-close cycle can never double-count (the old running-increment model
# drifted: an issue closed, reopened, then re-closed was tallied twice). Reopened
# issues fold back into the base buckets — recurrence is now tracked through the
# pending-verification workflow, not separate ReOpened counters.
success_count = sum(1 for i in processed_init.values() if i.get("status") in _RESOLVED_STATUSES)
failure_count = sum(1 for i in processed_init.values() if i.get("status") == "failed")
# Issues closed on GitHub and recorded locally as `closed` (terminal resolved state).
closed_count = sum(1 for i in processed_init.values() if i.get("status") == "closed")
# Auto-committed + GitHub-closed, but awaiting a HUMAN to verify the issue is gone
# (then they click Resolved → resolved, or Re-open → reprocess).
pending_verification_count = sum(1 for i in processed_init.values() if i.get("status") == "pending_verification")
# Issues the triage step judged not actionable (not a real bug / can't fix from logs).
non_actionable_count = sum(1 for i in processed_init.values() if i.get("status") == "non-actionable")

state = {
    "status": "Idle", "active_llm": "Unknown",
    "provider_1_online": False, "provider_2_online": False, "provider_3_online": False, "provider_4_online": False,
    "provider_1_configured": False, "provider_2_configured": False, "provider_3_configured": False, "provider_4_configured": False,
    # Per-slot last failover outcome (status sentinel + reason + iso8601), surfaced in the
    # Diagnostics panel so silent skips (e.g. "not_configured") are visible without CLI logs.
    "provider_last_result": {1: None, 2: None, 3: None, 4: None},
    # Bounded recent log of self-update / restart events for the Diagnostics panel.
    "restart_log": [],
    "local_online": False, "cloud_online": False,
    "last_run": "Never", "api_status": "Not Triggered",
    "processed": processed_init,
    "version": get_version(), "llm_stream": "",
    "active_tasks": {}, "pr_reviews": {}, "qa_enabled": config_on_start.get("qa_enabled", True),
    "success_count": success_count, "failure_count": failure_count, "closed_count": closed_count,
    "pending_verification_count": pending_verification_count,
    "non_actionable_count": non_actionable_count,
    "llm_circuit_breaker": _llm_cb_snapshot(),
    "provider_credit_cb": _provider_credit_cb_snapshot(),
    "paused": False,
    "blackout": False,
    "chat_streams": {}, "chat_fix_proposals": {},
    "daily_fixes_count": 0,
    "daily_budget_date": "",
    "scheduler_mode": "full",
    "claude_auth_proc": None,    # background subprocess running `claude auth login`
    "claude_auth_url": "",       # OAuth URL captured from that process
    "claude_auth_done": False,   # True once the process exits 0
    "restart_pending": False,    # True when an update was pulled; restart deferred until cycle end
    "refresh_status_seconds": config_on_start.get("refresh_status_seconds", 30),
    "refresh_logs_seconds": config_on_start.get("refresh_logs_seconds", 10),
    "cpu_count": os.cpu_count() or 4,  # detected core count, surfaced in the Local LLM setup UI
    "local_llm_setup": {},             # last-run summary for the one-click Local LLM setup
    # Hub agent (WebSocket) status — BugFixer authenticates to the LM Hub as an
    # agent like any other system, instead of the removed static admin token.
    "hub_agent_status": "not_registered",  # not_registered | pending | approved | error
    "hub_agent_message": "",
    "hub_agent_last_seen": "",
}


def recompute_issue_counters(processed=None):
    """Re-derive the issue counters from the processed store and write them into
    `state`. Call after ANY change to a processed entry's status/reopened flag
    (close, reopen, dismiss, delete) so the dashboard totals always match reality
    instead of drifting from running +=/-= increments. Reopened issues fold into the
    base buckets — recurrence is tracked via the pending-verification workflow."""
    if processed is None:
        processed = load_processed()
    vals = list(processed.values())
    state["success_count"] = sum(1 for i in vals if i.get("status") in _RESOLVED_STATUSES)
    state["closed_count"] = sum(1 for i in vals if i.get("status") == "closed")
    state["pending_verification_count"] = sum(1 for i in vals if i.get("status") == "pending_verification")
    state["non_actionable_count"] = sum(1 for i in vals if i.get("status") == "non-actionable")
    state["failure_count"] = sum(1 for i in vals if i.get("status") == "failed")
    return state

# Explicit __all__ so ``from app_state import *`` in main re-exports the
# underscore-prefixed locks too (routes.py/chat.py import `_task_state_lock` and
# `_chat_lock` from main); a bare `import *` would otherwise skip them.
__all__ = [
    "state",
    "config_on_start",
    "processed_init",
    "success_count",
    "failure_count",
    "closed_count",
    "pending_verification_count",
    "non_actionable_count",
    "recompute_issue_counters",
    "update_task_state",
    "_task_state_lock",
    "_chat_lock",
]
