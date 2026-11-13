"""Dashboard "Mission Control": what needs a person, what AppBuilder is still
working on, and what finished recently.

Built from the same stores the Status table lists (processed issues and PR
reviews), so every item here is also a row there. The dashboard renders it and
GET /api/mission-control returns it as JSON.
"""
from datetime import datetime, timezone

from pr_review_retry import MAX_AUTO_QUEUE_RETRIES

WINDOW_DAYS = 7
OUTCOMES_MAX = 8

# Merge holds that AppBuilder clears by itself (pr_review's auto-merge step):
# it is still improving the PR, GitHub Actions has not run the checks yet, or a
# release lock / blackout window is in force (it merges once that ends).
_SELF_CLEARING_HOLDS = ("holding the merge while AppBuilder improves", "waiting on GitHub Actions",
                        "under a release lock", "in a blackout window")

# Auto-merge "holds" that only say auto-merge does not apply to this PR (a kill
# switch is off, or the repo or branch is not opted in): a person reviews it as usual.
_NOT_APPLICABLE_HOLDS = ("feature_drive_enabled is off", "feature_automerge_enabled is off",
                         "is not in feature_automerge_repos", "never eligible")

# Issue status -> (group, label) for issues that wait on a person.
_ISSUE_NEEDS = {
    "failed": ("failed", "Fix failed"),
    "pending_verification": ("verify", "Pending verification"),
    "feature_flagged": ("feature", "Feature flagged"),
    "non-actionable": ("other", "Not actionable"),
    "ops-alert": ("other", "Ops alert"),
    "awaiting_human": ("other", "Needs a person"),
    "triaged": ("other", "Fix deferred (triage only)"),
    "feature_failed": ("feature", "Feature build failed"),
}
# Issue status -> label for issues AppBuilder (or the requester) still owns.
_ISSUE_WORKING = {
    "processing": "Fixing",
    "awaiting_review": "Waiting for reviewers",
    "feature_classify_retry": "Re-classifying",
    # feature_drive re-classifies it every cycle; the requester answers on GitHub.
    "feature_needs_info": "Waiting on requester",
    "reopened": "Reopened, fixing again",
    "feature_built": "Feature built, PR open",
}
_ISSUE_DONE = {
    "fixed": "fixed",
    "verified": "verified in prod",
    "awaiting_prod_verification": "fixed, awaiting prod check",
    "resolved": "resolved",
    "closed": "closed",
}
# Order of the attention list: held PRs first, then by how much a person is needed.
_GROUP_ORDER = ("pr", "failed", "verify", "feature", "other")


def _when(value):
    """Parse an ISO or "YYYY-MM-DD HH:MM:SS" timestamp; None when missing or bad."""
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _age(then, now):
    """Short age like "45m", "3h" or "2d"; "" when the time is unknown."""
    if then is None:
        return ""
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 60:
        return "%dm" % minutes
    if minutes < 48 * 60:
        return "%dh" % (minutes // 60)
    return "%dd" % (minutes // 1440)


def _short(text, limit=220):
    """One line, cut to `limit` characters."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _repo_and_number(key, sep):
    """("lm", "123") from "owner/lm:123" (issues) or "owner/lm#123" (PRs)."""
    repo, _, number = str(key).rpartition(sep)
    return repo.rsplit("/", 1)[-1] or str(key), number


def _issue_why(info):
    """Why an issue is waiting on a person, in one line."""
    status = info.get("status")
    if status == "pending_verification":
        return "Fix is committed. Confirm the problem is gone, then Resolve, or Reopen to retry."
    if status == "failed":
        why = info.get("error") or "AppBuilder could not find a fix it could verify."
        if info.get("failure_kind") == "qa_failed":
            # The error carries the whole test log; its headline is the useful part.
            why = why.split(" — ")[0] + ". The test output is under All activity."
        if info.get("failure_confidence") is not None and info.get("failure_threshold") is not None:
            why = "confidence %.2f, needs %.2f. %s" % (info["failure_confidence"], info["failure_threshold"], why)
        return why
    if status == "feature_flagged":
        if info.get("boundary_ids"):
            return "Crosses a boundary (%s). A person needs to build or approve it." % ", ".join(info["boundary_ids"])
        return info.get("last_error") or info.get("reason") or "Flagged for a person."
    if status == "ops-alert":
        return "Handed to ops: needs a person or a device fixed. " + (info.get("reason") or info.get("error") or "")
    return info.get("reason") or info.get("error") or ""


def panel_chips(pr):
    """Panel verdict chips: [{"label", "ok"}]; a panel that did not run is left out."""
    chips = []
    for name, prefix in (("Panel 1", "panel"), ("Panel 2", "panel2")):
        verdict = pr.get(prefix + "_verdict")
        if not verdict or pr.get(prefix + "_status"):
            continue
        conf = pr.get(prefix + "_confidence")
        label = "%s · %s" % (name, verdict) + (" %.2f" % conf if conf is not None else "")
        chips.append({"label": label, "ok": verdict == "Approve"})
    if pr.get("findings"):
        chips.append({"label": "%d finding%s" % (pr["findings"], "" if pr["findings"] == 1 else "s"), "ok": False})
    return chips


def iso_utc(value):
    """A task start time as ISO 8601 in UTC with its offset.

    start_time is a naive server-local datetime. Sent without an offset, the
    browser reads it as ITS local time, so a server on UTC and a browser on US
    Central showed hours of extra "total" on the Working on now cards."""
    if not isinstance(value, datetime):
        return value
    return value.astimezone(timezone.utc).isoformat()


def age_of(value, now=None):
    """Short age ("45m", "3h", "2d") of an ISO or "YYYY-MM-DD HH:MM:SS" timestamp."""
    return _age(_when(value), now or datetime.now())


# Tone of each attention group; the issue status chips in All activity use the same colours.
GROUP_TONE = {"pr": "amber", "failed": "red", "verify": "sky", "feature": "purple", "other": "slate"}


def issue_chip(status):
    """(label, tone) of the one status chip on an All activity issue row."""
    if status in _ISSUE_NEEDS:
        group, label = _ISSUE_NEEDS[status]
        return label, GROUP_TONE[group]
    if status in _ISSUE_WORKING:
        return _ISSUE_WORKING[status], "blue"
    if status in _ISSUE_DONE:
        return _ISSUE_DONE[status].capitalize(), "slate" if status == "closed" else "green"
    return str(status or "unknown"), "slate"


def pr_chip(pr):
    """(label, tone, why) of the one status chip on an All activity PR row.

    Same precedence as the row's data-pr-state: auto_merged is checked before
    merged because an auto-merged PR is also merged=True. An open PR reads as
    Mission Control files it (_pr_state): "Held" only when the hold waits on a
    person, so a self-clearing hold (GitHub Actions, release lock, blackout)
    stays "Pending" here just as it stays in flight up there."""
    if pr.get("auto_merged"):
        return "Auto-merged", "teal", ""
    if pr.get("merged"):
        return "Merged", "purple", ""
    if pr.get("denied"):
        return "Denied", "red", ""
    if pr.get("closed"):
        return "Closed", "slate", ""
    kind, label, why = _pr_state(pr)
    if pr.get("approved"):
        return "Approved", "green", why
    if kind == "needs" and label == "Auto-merge held":
        return "Held", "amber", why
    return "Pending", "indigo", why


def _pr_state(pr):
    """Where an open PR stands: ("needs", label, why) or ("working", label, why)."""
    hold = pr.get("auto_merge_blocked_reason") or ""
    not_applicable = any(m in hold for m in _NOT_APPLICABLE_HOLDS)
    self_clearing = any(m in hold for m in _SELF_CLEARING_HOLDS)
    if pr.get("approved"):
        refused = pr.get("auto_merge_refusal") or pr.get("merge_blocked_reason")
        if refused:
            return "needs", "Merge refused", "GitHub refused the merge: %s. Fix that, then Merge." % refused
        if self_clearing:
            return "working", "Improving", hold
        # A later hold beats an older auto-merge approval (auto_merge_reason
        # survives re-scans and head moves; the hold is the latest decision).
        if hold and not not_applicable:
            return "needs", "Auto-merge held", hold
        if pr.get("auto_merge_reason") and not hold:
            # AppBuilder approved it to auto-merge; a deferred merge retries on the next poll.
            return "working", "Auto-merging", pr["auto_merge_reason"]
        return "needs", "Approved", "You approved it. Merge when ready."
    if pr.get("auto_remediate_status") == "exhausted_human_review":
        return "needs", "Fixes stopped", pr.get("auto_remediate_reason") or hold or "AppBuilder ran out of fix attempts."
    # A queued review keeps the last head's hold; the retry decides what happens next.
    # Once the automatic retries are used up, nothing retries until a person acts.
    if "queue_for_retry" in (pr.get("panel_status"), pr.get("panel2_status")):
        if int(pr.get("queue_retry_count") or 0) < MAX_AUTO_QUEUE_RETRIES:
            return "working", "Review queued", "A review panel was unavailable; it retries on the next poll."
        return "needs", "Panel unavailable", "A review panel was unavailable and the automatic retries are used up. Approve or Re-review."
    if self_clearing:
        return "working", "Improving", hold
    if not_applicable:
        return "needs", "Awaiting review", "Reviewed; auto-merge does not apply (%s). Approve, Fix or Deny it." % hold
    if hold:
        return "needs", "Auto-merge held", hold
    return "needs", "Awaiting review", "Reviewed. Approve, Fix or Deny it."


def build(processed, pr_reviews, active_tasks=None, now=None):
    """The dashboard summary: {"kpis", "working", "attention", "in_flight", "outcomes"}."""
    now = now or datetime.now()
    attention, in_flight, outcomes = [], [], []
    fixed_recent = failed_recent = auto_merged_recent = 0

    def recent(then):
        """True when `then` is inside the last WINDOW_DAYS days."""
        return then is not None and (now - then).days < WINDOW_DAYS

    for key, info in (processed or {}).items():
        status = info.get("status")
        repo, number = _repo_and_number(key, ":")
        then = _when(info.get("closed_at") or info.get("timestamp"))
        base = {"id": key, "repo": repo, "number": number, "title": info.get("title") or "",
                "url": info.get("issue_url") or "https://github.com/%s/issues/%s" % (key.rpartition(":")[0], number),
                "age": _age(then, now), "_t": then}
        if status in _ISSUE_NEEDS:
            group, label = _ISSUE_NEEDS[status]
            attention.append({**base, "kind": "issue", "status": status, "group": group,
                              "label": label, "why": _short(_issue_why(info))})
        elif status in _ISSUE_WORKING:
            in_flight.append({**base, "kind": "issue", "label": _ISSUE_WORKING[status]})
        if status == "failed" and recent(then):
            failed_recent += 1
        if status in _ISSUE_DONE and recent(then):
            # "Fixed" counts what the Resolved filter shows; a plain close is not a fix.
            fixed_recent += status != "closed"
            outcomes.append({**base, "kind": "issue", "text": _ISSUE_DONE[status],
                             "tone": "muted" if status == "closed" else "ok"})

    for key, pr in (pr_reviews or {}).items():
        repo, number = _repo_and_number(key, "#")
        then = _when(pr.get("outcome_at") or pr.get("reviewed_at"))
        base = {"id": key, "repo": repo, "number": number, "title": pr.get("title") or "",
                "url": pr.get("url") or "", "base_ref": pr.get("base_ref") or "",
                "age": _age(then, now), "_t": then}
        if pr.get("auto_merged") or pr.get("merged") or pr.get("denied") or pr.get("closed"):
            if pr.get("auto_merged") and recent(then):
                auto_merged_recent += 1
            if not recent(then):
                continue
            if pr.get("auto_merged"):
                text, tone = "auto-merged", "ok"
            elif pr.get("merged"):
                text, tone = "merged", "ok"
            elif pr.get("denied"):
                text, tone = "denied", "bad"
            else:
                text, tone = "closed without merging", "muted"
            outcomes.append({**base, "kind": "pr", "text": text, "tone": tone, "panels": panel_chips(pr)})
            continue
        where, label, why = _pr_state(pr)
        item = {**base, "kind": "pr", "label": label, "why": _short(why), "panels": panel_chips(pr),
                "approved": bool(pr.get("approved")), "findings": pr.get("findings") or 0}
        if where == "needs":
            attention.append({**item, "group": "pr"})
        else:
            in_flight.append(item)

    oldest_first = datetime.min
    attention.sort(key=lambda i: (_GROUP_ORDER.index(i["group"]), i["_t"] or oldest_first))
    in_flight.sort(key=lambda i: i["_t"] or oldest_first)
    outcomes.sort(key=lambda i: i["_t"] or oldest_first, reverse=True)
    for item in attention + in_flight + outcomes:
        item.pop("_t", None)

    # The tasks running right now (the dashboard's "Working on now"), oldest first.
    working = []
    for tid, task in (active_tasks or {}).items():
        started = task.get("start_time")
        working.append({"id": tid, "name": task.get("name") or "", "kind": task.get("kind") or "",
                        "started": iso_utc(started)})
    working.sort(key=lambda t: str(t["started"] or ""))

    groups = {g: sum(1 for i in attention if i["group"] == g) for g in _GROUP_ORDER}
    return {
        "kpis": {
            "needs_you": len(attention),
            "running": len(working),
            "in_flight": len(in_flight),
            "fixed_7d": fixed_recent,
            "auto_merged_7d": auto_merged_recent,
            "failed_7d": failed_recent,
        },
        "groups": groups,
        "working": working,
        "attention": attention,
        "in_flight": in_flight,
        "outcomes": outcomes[:OUTCOMES_MAX],
    }
