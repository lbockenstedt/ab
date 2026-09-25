"""Shared mutable application state for AppBuilder.

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
    load_pr_reviews,
    load_llm_tps,
    save_pr_reviews,
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
                # Total elapsed must span the WHOLE job, not the current sub-step:
                # the same task_id is re-started for each phase (Triaging → Fix
                # Attempt → Reviewing → Verifying), and resetting start_time here
                # is why the UI's timer read ~"0h 0m 0s" forever.
                started = prev.get("start_time") or datetime.now()
                # The step describes work INSIDE a phase, so it only survives a
                # re-start of the same phase; a new phase name starts blank
                # rather than showing the previous phase's last step.
                carried_step = prev.get("step") or "" if prev.get("name") == task_name else ""
                state["active_tasks"][task_id] = {
                    "name": task_name,
                    "start_time": started,
                    # When the CURRENT phase began, so the UI can show both.
                    "phase_start": datetime.now(),
                    "stream": carried,
                    "kind": kind,
                    "step": carried_step,
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


def set_task_step(task_id, step):
    """Record a human-readable "what the system is doing right now" line on an
    active task, for the UI's Active Tasks card.

    update_task_state's name covers the PHASE; this covers the step inside it.
    Needed because the card otherwise derives its one-liner from the tail of the
    task's streamed LLM reasoning, which leaves every non-LLM stage (static
    parity/secrets checks, GitHub API calls, cloning, test runs) showing nothing
    at all. No-op for an unknown/finished task; never raises.
    """
    if not task_id:
        return
    try:
        with _task_state_lock:
            task = state["active_tasks"].get(task_id)
            if task is not None:
                task["step"] = str(step)[:200]
    except Exception as e:  # noqa: BLE001
        logger.debug(f"set_task_step failed for task_id={task_id!r}: {e}")


def _panel_dissent_stats(review):
    """Per-reviewer outcomes inside ONE panel: (dissents, rated, min_conf, unrated).

    A panel reports a single aggregate verdict, so a panel can come back
    "Approve" while an individual reviewer still dissented or rated the change
    low. Only the aggregate was ever persisted, which made those residual
    concerns invisible to everything downstream — AppBuilder would merge a PR
    that a reviewer had actually objected to, because the panel as a whole said
    yes. These counts are what let it keep improving a PR that technically
    passed but still carries unresolved reviewer concerns.

    A seat we could not score is counted as `unrated` rather than being dropped
    on the floor. Previously a reviewer item with no usable verdict — one that
    errored, timed out, or returned nothing parseable — incremented neither
    `dissents` nor `rated`, so a two-seat panel where one seat failed and one
    approved was byte-identical to unanimous approval. "We never heard from
    this reviewer" must not look like "this reviewer approved".

    "Nothing parseable" includes a whitespace-only verdict. `""` was already
    counted as unrated, but `"   "` is truthy and so used to be counted as a
    rated DISSENT — the same non-answer landing in two different buckets
    depending on whether the provider happened to emit a space. Both now mean
    the same thing: we did not hear from this seat.
    """
    if not review or review.get("status"):
        return (0, 0, None, 0)

    reviews = review.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        return (0, 0, None, 0)

    dissents = 0
    rated = 0
    min_conf = None
    unrated = 0

    for item in reviews:
        if not isinstance(item, dict):
            # A seat we cannot read at all is still a seat we did not hear from.
            unrated += 1
            continue

        verdict = item.get("verdict")
        if verdict is not None and str(verdict).strip():
            rated += 1
            if str(verdict).strip().lower() != "approve":
                dissents += 1
        else:
            unrated += 1

        # Confidence is read even for an unrated seat: a reviewer that reported
        # a number but no parseable verdict still told us how sure it was, and
        # dropping that would hide a low rating behind an unusable verdict.
        confidence = item.get("confidence")
        if confidence is not None:
            try:
                conf_float = float(confidence)
            except (TypeError, ValueError):
                continue
            if conf_float > 1.0:
                conf_float /= 100.0
            conf_float = max(0.0, min(1.0, conf_float))
            if min_conf is None or conf_float < min_conf:
                min_conf = conf_float

    return (dissents, rated, min_conf, unrated)


def record_pr_review(repo, number, title, url, findings, head_sha, summary="", review=None, review2=None,
                     base_ref="", head_ref=""):
    """Persist a PR pre-review result so the UI can list/filter 'PRs Reviewed'.
    Bounded to the most recent _PR_REVIEWS_MAX. findings = list of {level,...} dicts.

    ``review`` is the skeptical panel's ``{confidence, verdict, critique}`` (or a
    ``{status, reason}`` when the panel was unavailable). It used to be rendered
    into the GitHub PR comment and then thrown away, so the advisory verdict was
    visible on github.com but nowhere in AppBuilder's own UI. Stored flat as
    panel_verdict / panel_confidence / panel_status so the PR list can show it
    without re-reading the comment."""
    global state
    levels = {"error": 0, "warning": 0, "advisory": 0}
    items = []
    for f in (findings or []):
        f = f or {}
        lvl = f.get("level")
        if lvl in levels:
            levels[lvl] += 1
        items.append({"level": lvl or "advisory",
                      "title": (f.get("title") or "")[:140],
                      "detail": (f.get("detail") or "")[:300]})

    # Panel result → flat fields for the UI. Confidence is clamped to 0.0-1.0 here
    # too: review_fix already normalizes each vote, but this value is persisted and
    # then rendered, and a 0-100 answer leaking through would show as "9500%".
    _r = review or {}
    panel_status = _r.get("status") or ""          # set only when the panel could not run
    panel_verdict = "" if panel_status else str(_r.get("verdict") or "")
    # The panel's REASONING, not just its verdict. The GitHub comment renders this
    # (pr_review._render_panel appends `critique`), but it was never stored -- so
    # the UI could say "ADVISORY APPROVE · 95%" and nothing about WHY, forcing a
    # click through to GitHub for the substance of a review the bot had already
    # done. Bounded because it is persisted to pr_reviews.json and rendered in a
    # table row.
    panel_critique = ("" if panel_status else str(_r.get("critique") or "").strip())[:4000]
    panel_confidence = None
    if not panel_status and _r.get("confidence") is not None:
        try:
            _c = float(_r["confidence"])
            panel_confidence = max(0.0, min(1.0, _c / 100.0 if _c > 1.0 else _c))
        except (TypeError, ValueError):
            panel_confidence = None

    # Second panel (pr_review._state_logic_review) — same flattening, own flat
    # fields. Previously computed every scan and rendered into the GitHub
    # comment but never persisted here, so its verdict was invisible in
    # AppBuilder's own UI even for ordinary human PRs. Added when feature
    # auto-drive's auto-merge gate needed to require BOTH panels to clear
    # (see pr_review._automerge_decision) — fixes that pre-existing gap for
    # every PR, not just feature-built ones.
    _r2 = review2 or {}
    panel2_status = _r2.get("status") or ""
    panel2_verdict = "" if panel2_status else str(_r2.get("verdict") or "")
    panel2_critique = ("" if panel2_status else str(_r2.get("critique") or "").strip())[:4000]
    panel2_confidence = None
    if not panel2_status and _r2.get("confidence") is not None:
        try:
            _c2 = float(_r2["confidence"])
            panel2_confidence = max(0.0, min(1.0, _c2 / 100.0 if _c2 > 1.0 else _c2))
        except (TypeError, ValueError):
            panel2_confidence = None

    # Per-reviewer dissent inside each panel — see _panel_dissent_stats. An
    # aggregate "Approve" can still hide a reviewer who objected.
    panel_dissents, panel_rated, panel_min_conf, panel_unrated = _panel_dissent_stats(_r)
    panel2_dissents, panel2_rated, panel2_min_conf, panel2_unrated = _panel_dissent_stats(_r2)

    # Composite consensus & average confidence computation
    valid_confs = [c for c in (panel_confidence, panel2_confidence) if c is not None]
    panel_avg_confidence = (sum(valid_confs) / len(valid_confs)) if valid_confs else None

    p1_active = bool(panel_verdict and not panel_status)
    p2_active = bool(panel2_verdict and not panel2_status)
    if p1_active and p2_active:
        v1_app = panel_verdict.strip().lower() == "approve"
        v2_app = panel2_verdict.strip().lower() == "approve"
        if v1_app and v2_app:
            composite_verdict = "Approve"
        elif v1_app or v2_app:
            composite_verdict = "Split"
        else:
            composite_verdict = "Deny"
    elif p1_active:
        composite_verdict = panel_verdict
    elif p2_active:
        composite_verdict = panel2_verdict
    else:
        composite_verdict = ""

    key = "%s#%s" % (repo, number)
    try:
        with _task_state_lock:
            prev = state["pr_reviews"].get(key) or {}
            state["pr_reviews"][key] = {
                "repo": repo,
                "number": number,
                "title": (title or "")[:120],
                "url": url,
                "head": head_sha,
                # Which branch this PR would land on (and from where). In a
                # dev -> qa -> main chain the target is the most important fact
                # about a PR, and the list previously showed only "repo #N" —
                # indistinguishable between a routine dev PR and one aimed at
                # main. Falls back to "" for records written before this field
                # existed, which the template treats as "unknown" rather than
                # rendering an empty badge.
                "base_ref": str(base_ref or "")[:120] or prev.get("base_ref", ""),
                "head_ref": str(head_ref or "")[:120] or prev.get("head_ref", ""),
                "summary": (summary or "")[:600],
                "findings": len(findings or []),
                "errors": levels["error"],
                "warnings": levels["warning"],
                "advisories": levels["advisory"],
                "items": items[:20],
                # Advisory skeptical-panel result (see the docstring). Rebuilt on
                # every re-scan alongside findings, so it always describes the head
                # this record was reviewed at.
                "panel_verdict": panel_verdict,
                "panel_confidence": panel_confidence,
                "panel_status": panel_status,
                "panel_critique": panel_critique,
                "panel2_verdict": panel2_verdict,
                "panel2_confidence": panel2_confidence,
                "panel2_status": panel2_status,
                "panel2_critique": panel2_critique,
                "panel_dissents": panel_dissents,
                "panel_rated": panel_rated,
                "panel_unrated": panel_unrated,
                "panel_min_reviewer_confidence": panel_min_conf,
                "panel2_dissents": panel2_dissents,
                "panel2_rated": panel2_rated,
                "panel2_unrated": panel2_unrated,
                "panel2_min_reviewer_confidence": panel2_min_conf,
                "panel_avg_confidence": panel_avg_confidence,
                "composite_verdict": composite_verdict,
                # Cap on automatic "panel unavailable" retries (pr_review_retry.
                # is_queued_for_retry_stale reads this). Increments only while the
                # panel keeps returning queue_for_retry AT THE SAME head; resets to
                # 0 the moment a real verdict is reached or the head moves, so a
                # fresh commit or a human Reprocess always gets its own full budget.
                "queue_retry_count": (
                    (int(prev.get("queue_retry_count") or 0) + 1)
                    if (panel_status == "queue_for_retry" or panel2_status == "queue_for_retry")
                    and prev.get("head") == head_sha
                    and (prev.get("panel_status") == "queue_for_retry"
                         or prev.get("panel2_status") == "queue_for_retry")
                    else (1 if (panel_status == "queue_for_retry" or panel2_status == "queue_for_retry") else 0)
                ),
                # Preserve a human's Approve across re-scans; reset if the head moved.
                "approved": bool(prev.get("approved")) and prev.get("head") == head_sha,
                # Merged is terminal — keep it so the PR stays listed with its badge.
                "merged": bool(prev.get("merged")),
                # Auto-merged (feature auto-drive only — see pr_review._automerge_decision)
                # follows the MERGED sticky-preservation pattern, not the approved one:
                # once true, it must survive every later re-scan (a later scan recomputing
                # panel_confidence must never make the badge disappear from an already-
                # auto-merged PR — that's the exact bug class this comment exists to avoid).
                "auto_merged": bool(prev.get("auto_merged")),
                "auto_merge_score": prev.get("auto_merge_score"),
                "auto_merge_reason": prev.get("auto_merge_reason"),
                # Why auto-merge was REFUSED (last decision) — survives re-scans so the
                # poll-to-poll "did the reason change?" check has something to compare to.
                # Reset when the head moved: the pre-review comment is re-rendered then
                # (dropping the note), so the next decision must write it again.
                "auto_merge_blocked_reason": (
                    prev.get("auto_merge_blocked_reason") if prev.get("head") == head_sha else None),
                # Remediation bookkeeping. pr_remediate writes these through
                # update_pr_review, but this function REBUILDS the record on every
                # scan -- so leaving them out silently reset the attempt counter,
                # the terminal "exhausted_human_review" state and the failed-model
                # exclusion list once per poll. remediation_pending then never saw
                # the ceiling bind, held the auto-merge forever, and re-remediated
                # the same PR with the same model on every cycle.
                #
                # Deliberately preserved ACROSS a head change, unlike `approved`
                # above: a remediation attempt PUSHES A COMMIT, so the head always
                # moves between attempts. Resetting on head movement would make the
                # budget un-spendable and reinstate the loop. The budget is per-PR,
                # which is what makes remediation_pending's "can never deadlock a
                # PR" guarantee true. Head-scoped guardrail blocks (`auto_remediate_blocked`)
                # carry their latching SHA in `auto_remediate_blocked_head`, which
                # `pr_remediate.maybe_auto_remediate` compares against the new head to
                # re-evaluate guardrails when a commit changes.
                "remediation_attempts": int(prev.get("remediation_attempts") or 0),
                "auto_remediate_status": prev.get("auto_remediate_status"),
                "auto_remediate_failure": prev.get("auto_remediate_failure"),
                "auto_remediate_blocked": prev.get("auto_remediate_blocked"),
                "auto_remediate_blocked_head": prev.get("auto_remediate_blocked_head"),
                "auto_remediate_reason": prev.get("auto_remediate_reason"),
                "last_remediation_complexity": prev.get("last_remediation_complexity"),
                "last_remediation_model": prev.get("last_remediation_model"),
                "excluded_models": list(prev.get("excluded_models") or []),
                # Denied does NOT persist here: record_pr_review only runs for OPEN
                # PRs, and Deny CLOSES the PR — so a still-denied PR is never re-scanned
                # (its badge persists via the stored record). Reaching this line means
                # the PR is OPEN again (reopened), which CLEARS the deny so it's
                # reviewable/approvable again — "reopen clears the deny".
                "denied": False,
                "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            # Bound: drop oldest (dicts preserve insertion order) beyond the cap.
            extra = len(state["pr_reviews"]) - _PR_REVIEWS_MAX
            if extra > 0:
                for k in list(state["pr_reviews"].keys())[:extra]:
                    state["pr_reviews"].pop(k, None)
            save_pr_reviews(state["pr_reviews"])
    except Exception as e:
        logger.error(f"record_pr_review failed for {repo}#{number}: {e}")


def mark_pr_approved(repo, number, approved=True):
    """Flag a reviewed PR as human-approved (set by the Approve button). Returns
    True if the record existed and was updated."""
    return update_pr_review(repo, number, approved=bool(approved))


def update_pr_review(repo, number, **fields):
    """Merge fields (e.g. merged=True, denied=True) into a reviewed-PR record,
    KEEPING it in the list (so merged/denied PRs stay visible with a badge instead
    of vanishing). Returns True if the record existed."""
    global state
    key = "%s#%s" % (repo, number)
    try:
        with _task_state_lock:
            rec = state["pr_reviews"].get(key)
            if not rec:
                return False
            rec.update(fields)
            save_pr_reviews(state["pr_reviews"])
            return True
    except Exception as e:
        logger.error(f"update_pr_review failed for {repo}#{number}: {e}")
        return False


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
# Feature auto-drive (feature_drive.py) — three statuses, none of them "resolved":
# a flagged request needs a human decision; a needs-info request needs a reply;
# a built one has an open PR still needing review/merge.
feature_flagged_count = sum(1 for i in processed_init.values() if i.get("status") == "feature_flagged")
feature_needs_info_count = sum(1 for i in processed_init.values() if i.get("status") == "feature_needs_info")
feature_built_count = sum(1 for i in processed_init.values() if i.get("status") == "feature_built")

state = {
    "status": "Idle", "active_llm": "Unknown",
    "provider_1_online": False, "provider_2_online": False, "provider_3_online": False, "provider_4_online": False,
    # Entry-aware endpoint reachability (the routable llm_entries set) + an
    # any-online rollup. Header pills prefer this over the legacy per-slot flags.
    "llm_endpoints_online": [], "any_llm_online": False,
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
    # Rolling generation throughput, "when busy": each entry is one completed
    # Ollama generation's own tok/s, computed from the eval_count / eval_duration
    # it already returns. Because those cover GENERATION time only, idle periods
    # are excluded by construction -- no need to track busy/idle separately.
    # Bounded deque: this is a live gauge, not history, and an unbounded list on
    # a long-running service is a slow leak. Keyed by model, since a 31b and a 7b
    # differ by an order of magnitude and one average over both is meaningless.
    # Warm-loaded from disk so the Model Performance panel is populated
    # immediately after a restart instead of blank until every model
    # happens to run again.
    "llm_tps": load_llm_tps(),
    "active_tasks": {}, "pr_reviews": load_pr_reviews(), "skills": [], "qa_enabled": config_on_start.get("qa_enabled", True),
    "feature_flagged_count": feature_flagged_count,
    "feature_needs_info_count": feature_needs_info_count,
    "feature_built_count": feature_built_count,
    # Background-dismiss job status, keyed by issue_id, polled by the WebUI so the
    # Dismiss button can return instantly and toast once the GitHub retry loop
    # (delete_issue in routes.py) actually finishes. {"status": "pending"|"done"|
    # "error", "message": str}. Entries are transient — overwritten per dismiss,
    # never persisted to disk.
    "dismiss_jobs": {},
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
    # Hub agent (WebSocket) status — AppBuilder authenticates to the LM Hub as an
    # agent like any other system, instead of the removed static admin token.
    "hub_agent_status": "not_registered",  # not_registered | pending | approved | error
    "hub_agent_message": "",
    "hub_agent_last_seen": "",
    "hub_agent_connected": False,          # LIVE socket state (≠ approval status)
    "hub_agent_last_disconnect": "",
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
    state["feature_flagged_count"] = sum(1 for i in vals if i.get("status") == "feature_flagged")
    state["feature_needs_info_count"] = sum(1 for i in vals if i.get("status") == "feature_needs_info")
    state["feature_built_count"] = sum(1 for i in vals if i.get("status") == "feature_built")
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
    "feature_flagged_count",
    "feature_needs_info_count",
    "feature_built_count",
    "recompute_issue_counters",
    "update_task_state",
    "_task_state_lock",
    "_chat_lock",
]
