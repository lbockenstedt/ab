"""Dashboard "Mission Control": what needs a person, what AppBuilder is still
working on, and what finished recently.

Built from the same stores the Status table lists (processed issues and PR
reviews), so every item here is also a row there. The dashboard renders it and
GET /api/mission-control returns it as JSON.
"""
import time
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
    """(filter_key, label, tone, why) of an All activity PR row.

    One classification feeds the row's data-pr-state (filter_key), its status
    chip, its Why line and the filter chip counts (pr_filter_counts), so none of
    them can drift from Mission Control. auto_merged is checked before merged
    because an auto-merged PR is also merged=True. An open PR follows _pr_state:
    it files under 'approved' only when it is plainly approved and waits for a
    Merge; every other open PR (held or refused approvals included) is pending
    ('reviewed')."""
    if pr.get("auto_merged"):
        return "auto_merged", "Auto-merged", "teal", ""
    if pr.get("merged"):
        return "merged", "Merged", "purple", ""
    if pr.get("denied"):
        return "denied", "Denied", "red", ""
    if pr.get("closed"):
        return "closed", "Closed", "slate", ""
    kind, label, why = _pr_state(pr)
    if kind == "working":
        return "reviewed", label, "blue", why
    if label == "Approved":
        return "approved", "Approved", "green", why
    if label == "Merge refused":
        return "reviewed", label, "red", why
    if label == "Auto-merge held":
        return "reviewed", "Held", "amber", why
    return "reviewed", label, "indigo", why


def pr_filter_counts(pr_reviews):
    """How many PR rows each Pull requests filter chip shows, keyed by data-pr-state."""
    counts = dict.fromkeys(("reviewed", "approved", "merged", "auto_merged", "denied", "closed"), 0)
    for pr in (pr_reviews or {}).values():
        counts[pr_chip(pr)[0]] += 1
    return counts


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


# The Working on now stepper: every bug fix, PR and promotion walks these steps.
STEPS = (("triage", "Triage"), ("fix", "Fix"), ("tests", "Tests"),
         ("review", "Review panels"), ("pr", "PR"), ("merge", "Merge"))
_STEP_KEYS = tuple(k for k, _ in STEPS)
# Fix-pipeline phase names (fix_engine's update_task_state) -> step, for tasks
# that started before set_task_step recorded a stage.
_PHASE_STAGE = (("Triaging", "triage"), ("Fix Attempt", "fix"), ("Reviewing", "review"), ("Verifying", "tests"))
# Branch pairs a promotion PR moves code along.
_PROMOTION_ROUTES = {("dev", "qa"), ("qa", "main"), ("dev", "main")}


def stepper(current, done=()):
    """The six steps as [{"key", "label", "state"}]; state is done, current or todo.

    ``done`` holds the steps really completed (a fix that was reviewed before it
    was tested shows Review panels done while Tests is current)."""
    return [{"key": k, "label": label,
             "state": "current" if k == current else "done" if k in done else "todo"}
            for k, label in STEPS]


def _duration(seconds):
    """"45s", "12m", "2h 10m" or "3d 4h" for a time spent in a state."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    if minutes < 48 * 60:
        return "%dh %dm" % (minutes // 60, minutes % 60)
    return "%dd %dh" % (minutes // 1440, (minutes % 1440) // 60)


def _until(next_at, now_ts):
    """"in 12m" until a unix time, or "" when it is unknown."""
    if not next_at:
        return ""
    return "in %s" % _duration(max(0, next_at - now_ts))


def _last_line(stream):
    """Last non-blank line of a task's streamed LLM reply, cut to 160 characters."""
    for line in reversed(str(stream or "").strip().splitlines()):
        if line.strip():
            return line.strip()[:160]
    return ""


def next_poll_label(next_poll_at, now_ts=None):
    """Header pill text: "Next poll 3m", "Next poll now" or "" when no poll is scheduled."""
    if not next_poll_at:
        return ""
    left = next_poll_at - (now_ts if now_ts is not None else time.time())
    return "Next poll now" if left < 60 else "Next poll %s" % _duration(left)


def is_promotion(pr):
    """True for a PR that promotes one environment branch to the next (dev -> qa -> main)."""
    return (pr.get("head_ref"), pr.get("base_ref")) in _PROMOTION_ROUTES


def _pr_kind(pr):
    """Kind chip of a PR card: Promotion, Feature (an ai-feature/ branch) or PR review."""
    if is_promotion(pr):
        return "Promotion"
    if str(pr.get("head_ref") or "").startswith("ai-feature/"):
        return "Feature"
    return "PR review"


def task_card(tid, task, processed=None, pr_reviews=None, now=None):
    """One running task as a Working on now card.

    Everything comes from the task entry the workers keep current
    (app_state.update_task_state / set_task_step): the stage reached, the step
    text, the item worked on (subject) and the models that worked on it."""
    now = now or datetime.now()
    processed, pr_reviews = processed or {}, pr_reviews or {}
    name = task.get("name") or str(tid)
    stage = task.get("stage") or next((st for prefix, st in _PHASE_STAGE if name.startswith(prefix)), "")
    subject = task.get("subject") or ""
    card = {"id": tid, "live": True, "title": name, "desc": "", "kind": "Task", "url": "",
            "started": iso_utc(task.get("start_time"))}
    if subject and subject in pr_reviews:
        pr = pr_reviews[subject]
        repo, number = _repo_and_number(subject, "#")
        card.update(kind=_pr_kind(pr), title="%s #%s" % (repo, number), desc=pr.get("title") or "",
                    url=pr.get("url") or "")
    elif subject:
        repo, number = _repo_and_number(subject, "#")
        card.update(kind="PR review", title="%s #%s" % (repo, number))
    elif ":" in str(tid) and str(tid).rpartition(":")[2].isdigit():
        info = processed.get(tid) or {}
        repo, number = _repo_and_number(tid, ":")
        card.update(kind="Feature" if str(info.get("status", "")).startswith("feature") else "Bug fix",
                    title="%s #%s" % (repo, number), desc=info.get("title") or name,
                    url=info.get("issue_url") or "")
    elif task.get("kind") == "pr":
        card.update(kind="PR review", desc=name)
    if not card["desc"]:
        card["desc"] = name if name != card["title"] else ""
    step = task.get("step") or _last_line(task.get("stream"))
    scope = task.get("scope") or ""
    card["now"] = "%s — %s" % (scope, step) if scope and step else step or scope or "starting"
    card["models"] = " → ".join(task.get("models") or [])
    done = [s for s in (task.get("stages") or []) if s != stage]
    if subject and stage != "pr":
        done.append("pr")  # a PR being reviewed or improved already exists
    if stage:
        # Steps before the first one recorded were passed before tracking began.
        first = min((_STEP_KEYS.index(s) for s in [stage] + done if s in _STEP_KEYS), default=0)
        done += list(_STEP_KEYS[:first])
    card["steps"] = stepper(stage, done) if stage or card["kind"] != "Task" else None
    return card


def _ci_passed(ci):
    """Checks that passed; older cache entries without "passed" only knew done and failed."""
    return ci["passed"] if "passed" in ci else ci["done"] - ci["failed"]


def _ci_wait(ci):
    """The Now text for CI that has not passed yet and has not failed."""
    if ci["done"] < ci["total"]:
        return "waiting for CI (%d/%d checks done)" % (ci["done"], ci["total"])
    return "CI finished without a verdict (%d/%d passed) — Re-run CI" % (_ci_passed(ci), ci["total"])


def _ci_chip(ci):
    """(text, tone) of the attention CI chip; tone is ok, bad or wait."""
    if not ci or not ci["total"]:
        return "", ""
    if ci["failed"]:
        return "CI ✕ %d/%d" % (ci["failed"], ci["total"]), "bad"
    if _ci_passed(ci) == ci["total"]:
        return "CI ✓ %d/%d" % (ci["total"], ci["total"]), "ok"
    return "CI … %d/%d" % (_ci_passed(ci), ci["total"]), "wait"


def _pr_flight(key, pr, ci_seen, next_poll_at, max_attempts, now):
    """(stage, done, now_text) of an open PR, from its cached review record."""
    where, label, why = _pr_state(pr)
    ci = (ci_seen or {}).get((pr.get("repo") or key.rpartition("#")[0], pr.get("head")))
    # Steps already behind the PR: it exists, so triage and PR are done.
    done = {"triage", "pr"}
    if int(pr.get("remediation_attempts") or 0):
        done.add("fix")
    if ci and ci["total"] and _ci_passed(ci) == ci["total"]:
        done.add("tests")
    if pr.get("panel_verdict") or pr.get("panel2_verdict"):
        done.add("review")
    # Facts the Now line quotes: next poll, attempts used, CI still running.
    poll = _until(next_poll_at, now.timestamp())
    attempts = int(pr.get("remediation_attempts") or 0)
    pending_ci = ci and not ci["failed"] and _ci_passed(ci) < ci["total"]
    # Being fixed or waiting on CI.
    if label == "Improving" and "improves" in why:
        return "fix", done, "improving: attempt %d/%d" % (min(attempts + 1, max_attempts), max_attempts)
    if label in ("Improving", "Auto-merging") and pending_ci:
        return "tests", done, _ci_wait(ci)
    if label == "Improving" and "waiting on GitHub Actions" in why:
        return "tests", done, "waiting for CI (checks not started yet)"
    if label == "Auto-merging":
        return "merge", done, "auto-merge approved — merges on the next poll" + (" (%s)" % poll if poll else "")
    if label == "Improving":
        return "merge", done, "held: " + _short(why, 120)
    # Waiting on a review, or stopped and needing a person.
    if label == "Review queued":
        return "review", done, "queued: next PR scan %s" % poll if poll else "queued for the next PR scan"
    if label == "Fixes stopped":
        return "fix", done, "held: remediation limit %d/%d, needs human" % (attempts, max_attempts)
    if label == "Panel unavailable":
        return "review", done, "held: review panel unavailable, retries used up — needs human"
    # Held: say what it is held on.
    if label in ("Auto-merge held", "Merge refused"):
        if ci and ci["failed"]:
            return "tests", done, "held: CI failing (%d of %d checks failed)" % (ci["failed"], ci["total"])
        if pending_ci:
            return "tests", done, "held: " + _ci_wait(ci)
        return "merge", done, "held: " + _short(why, 120)
    # Otherwise it waits on a human (approval, review or promotion).
    if label == "Approved":
        return "merge", done, "approved — waiting for a Merge click"
    if is_promotion(pr):
        return "review", done, "awaiting review before promotion to %s" % pr.get("base_ref")
    verdicts = " · ".join(c["label"] for c in panel_chips(pr))
    return "review", done, "awaiting your review" + (" (%s)" % verdicts if verdicts else "")


def flight_cards(processed, pr_reviews, active_tasks=None, ci_seen=None, next_poll_at=None,
                 max_attempts=3, now=None):
    """Items AppBuilder owns that are not running this second, with their true state.

    Open PRs (being improved, awaiting review, CI or merge, promotions), issues
    still in the pipeline, built features and fixes pending verification. Each
    card's step, Now line and time-in-state come from cached records only."""
    now = now or datetime.now()
    running = {t.get("subject") for t in (active_tasks or {}).values()} | set(active_tasks or {})
    poll = _until(next_poll_at, now.timestamp())
    cards = []
    pr_urls = set()
    for key, pr in (pr_reviews or {}).items():
        if pr.get("auto_merged") or pr.get("merged") or pr.get("denied") or pr.get("closed") or key in running:
            continue
        pr_urls.add(pr.get("url"))
        stage, done, text = _pr_flight(key, pr, ci_seen, next_poll_at, max_attempts, now)
        repo, number = _repo_and_number(key, "#")
        since = _when(pr.get("state_since") or pr.get("reviewed_at"))
        cards.append({"id": key, "live": False, "kind": _pr_kind(pr), "title": "%s #%s" % (repo, number),
                      "desc": pr.get("title") or "", "url": pr.get("url") or "", "base_ref": pr.get("base_ref") or "",
                      "steps": stepper(stage, done), "now": text,
                      "for": _duration((now - since).total_seconds()) if since else "", "_t": since})
    for key, info in (processed or {}).items():
        status = info.get("status")
        if key in running or (status == "feature_built" and info.get("pr_url") in pr_urls):
            continue
        attempts = "attempt %s/%s" % (info["attempts"], info.get("max_attempts") or "?") if info.get("attempts") else ""
        flight = {
            "pending_verification": ("", set(_STEP_KEYS), "merged, awaiting verification"),
            "processing": ("fix", {"triage"}, "fixing — continues on the next scan" + (" (%s)" % poll if poll else "")),
            "awaiting_review": ("review", {"triage", "fix"}, "queued: waiting for reviewers"),
            "reopened": ("triage", set(), "reopened — fixing again on the next scan" + (" (%s)" % poll if poll else "")),
            "feature_classify_retry": ("triage", set(), "re-classifying" + (": " + attempts if attempts else "")),
            "feature_needs_info": ("triage", set(), "waiting on requester: %d question(s) posted" % len(info.get("questions") or [])),
            "feature_built": ("review", {"triage", "fix", "tests", "pr"}, "feature built — PR open, awaiting review"),
        }.get(status)
        if not flight:
            continue
        stage, done, text = flight
        repo, number = _repo_and_number(key, ":")
        since = _when(info.get("timestamp"))
        cards.append({"id": key, "live": False, "title": "%s #%s" % (repo, number),
                      "kind": "Feature" if str(status).startswith("feature") else "Bug fix",
                      "desc": info.get("title") or "", "url": info.get("pr_url") or info.get("issue_url") or "",
                      "steps": stepper(stage, done), "now": text,
                      "for": _duration((now - since).total_seconds()) if since else "", "_t": since})
    cards.sort(key=lambda c: c["_t"] or datetime.min)
    for c in cards:
        c.pop("_t", None)
    return cards


def stamp_state_since(rec, prev=None, now=None):
    """Set rec["state_since"] to when the PR entered its current dashboard state.

    The state is _pr_state's label and reason (or the outcome once closed); it is
    kept from ``prev`` while unchanged, so "for 2h 10m" is time in THIS state."""
    if rec.get("auto_merged") or rec.get("merged") or rec.get("denied") or rec.get("closed"):
        label = pr_chip(rec)[1]
    else:
        # Label plus reason: "Improving" while fixing and while release-locked differ.
        label = "%s: %s" % _pr_state(rec)[1:]
    prev = prev or {}
    if prev.get("state_label") == label and prev.get("state_since"):
        rec["state_since"] = prev["state_since"]
    else:
        rec["state_since"] = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    rec["state_label"] = label


def _median_minutes(values):
    """Median of a list of minutes, or None when it is empty."""
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def fmt_minutes(minutes):
    """"38m" or "2h 5m" for the Median fix time card; "—" when unknown."""
    if minutes is None:
        return "—"
    return _duration(minutes * 60)


def _split_verdict(pr):
    """True when both review panels ran and one approved while the other did not."""
    v1, v2 = pr.get("panel_verdict"), pr.get("panel2_verdict")
    if not v1 or not v2 or pr.get("panel_status") or pr.get("panel2_status"):
        return False
    return (v1 == "Approve") != (v2 == "Approve")


def _outcome_detail(pr):
    """Second line of a PR outcome: the panel verdicts with their confidences."""
    return " · ".join(c["label"] for c in panel_chips(pr) if c["label"].startswith("Panel"))


def promotion_lanes(repos, pr_reviews, now=None):
    """One row per monitored repo (or repo with promotion PRs): its dev -> qa -> main lanes from cached PR reviews.

    A lane shows the open promotion PR on it (number and state). The note says
    what is known: a promotion waiting, or when the repo was last promoted.
    Ahead/behind commit counts are not cached, so they are not shown."""
    now = now or datetime.now()
    rows = []
    repos = list(repos or [])
    # A repo with promotion PRs on record gets a row even when it is not monitored.
    repos += sorted({k.rpartition("#")[0] for k, pr in (pr_reviews or {}).items() if is_promotion(pr)} - set(repos))
    for full in repos:
        lanes = {("dev", "qa"): None, ("qa", "main"): None}
        last = None
        for key, pr in (pr_reviews or {}).items():
            if key.rpartition("#")[0] != full or not is_promotion(pr):
                continue
            route = (pr.get("head_ref"), pr.get("base_ref"))
            if pr.get("merged") or pr.get("auto_merged"):
                then = _when(pr.get("outcome_at") or pr.get("reviewed_at"))
                if then and (last is None or then > last[0]):
                    last = (then, route[1])
            elif not (pr.get("denied") or pr.get("closed")) and route in lanes:
                lanes[route] = {"number": key.rpartition("#")[2], "url": pr.get("url") or "",
                                "label": _pr_state(pr)[1], "held": _pr_state(pr)[0] == "needs"}
        waiting = [(r, l) for r, l in lanes.items() if l]
        if waiting:
            (src, dst), lane = waiting[-1]
            note = "#%s %s → %s: %s" % (lane["number"], src, dst, lane["label"].lower())
        elif last:
            note = "last promoted to %s %s ago" % (last[1], _age(last[0], now))
        else:
            note = "no promotion PRs seen"
        rows.append({"repo": full.rsplit("/", 1)[-1], "full": full,
                     "qa": lanes[("dev", "qa")], "main": lanes[("qa", "main")], "note": note})
    return rows


def build(processed, pr_reviews, active_tasks=None, now=None, ci_seen=None, next_poll_at=None,
          max_attempts=3, repos=None):
    """The dashboard summary: {"kpis", "working", "flight", "attention", "in_flight", "outcomes", "lanes"}.

    "working" are the running tasks' cards and "flight" the waiting in-flight
    cards (Working on now); "in_flight" is the plain list the JSON API has always returned."""
    now = now or datetime.now()
    attention, in_flight, outcomes = [], [], []
    fixed_recent = fixed_prev = failed_recent = auto_merged_recent = 0
    fix_minutes = []

    def recent(then, weeks=1):
        """True when `then` is inside the last `weeks` * WINDOW_DAYS days."""
        return then is not None and (now - then).days < WINDOW_DAYS * weeks

    for key, info in (processed or {}).items():
        status = info.get("status")
        repo, number = _repo_and_number(key, ":")
        then = _when(info.get("closed_at") or info.get("timestamp"))
        base = {"id": key, "repo": repo, "number": number, "title": info.get("title") or "",
                "url": info.get("issue_url") or "https://github.com/%s/issues/%s" % (key.rpartition(":")[0], number),
                "age": _age(then, now), "_t": then}
        if status in _ISSUE_NEEDS:
            group, label = _ISSUE_NEEDS[status]
            item = {**base, "kind": "issue", "status": status, "group": group,
                    "label": label, "why": _short(_issue_why(info))}
            if status == "failed":
                if info.get("attempts"):
                    item["label"] = "Fix failed · attempt %s/%s" % (info["attempts"], info.get("max_attempts") or info["attempts"])
                item["reply"] = _short(info.get("failure_raw_excerpt") or "", 2000)
                item["reply_model"] = info.get("failure_model") or ""
            elif status == "feature_flagged" and info.get("flag_kind") == "boundary":
                item["label"] = "Feature flagged · crosses boundary"
                item["boundary"] = ", ".join(info.get("boundary_ids") or [])
            attention.append(item)
        elif status in _ISSUE_WORKING:
            in_flight.append({**base, "kind": "issue", "label": _ISSUE_WORKING[status]})
        if status == "failed" and recent(then):
            failed_recent += 1
        if status in _ISSUE_DONE and status != "closed" and recent(then, 2) and not recent(then):
            fixed_prev += 1
        if status in _ISSUE_DONE and recent(then):
            # "Fixed" counts what the Resolved filter shows; a plain close is not a fix.
            fixed_recent += status != "closed"
            detail = {"verified": "verified in prod"}.get(status) or (
                "%d file(s) changed" % len(info["files"]) if info.get("files") else "")
            outcomes.append({**base, "kind": "issue", "text": _ISSUE_DONE[status], "detail": detail,
                             "tone": "muted" if status == "closed" else "ok"})
        # Median fix time: issue opened -> fix committed, for fixes in the window.
        opened, fixed_at = _when(info.get("opened_at")), _when(info.get("timestamp"))
        if status in ("pending_verification", "fixed", "verified", "resolved") and opened and recent(fixed_at):
            fix_minutes.append(max(0, (fixed_at - opened).total_seconds() / 60))

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
            outcomes.append({**base, "kind": "pr", "text": text, "tone": tone, "panels": panel_chips(pr),
                             "detail": _outcome_detail(pr)})
            continue
        where, label, why = _pr_state(pr)
        ci = (ci_seen or {}).get((key.rpartition("#")[0], pr.get("head")))
        since = _when(pr.get("state_since") or pr.get("reviewed_at"))
        item = {**base, "kind": "pr", "label": label, "why": _short(why), "panels": panel_chips(pr),
                "approved": bool(pr.get("approved")), "findings": pr.get("findings") or 0,
                "ci": _ci_chip(ci)[0], "ci_tone": _ci_chip(ci)[1],
                "held_for": _duration((now - since).total_seconds()) if since and label == "Auto-merge held" else ""}
        panel_hold = label == "Auto-merge held" and "panel" in why.lower()
        if (label == "Awaiting review" or panel_hold) and _split_verdict(pr):
            item["label"], item["split"] = "Split verdict", True
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

    # The tasks running right now (Working on now), oldest first, then everything else in flight.
    working = [task_card(tid, task, processed, pr_reviews, now) for tid, task in (active_tasks or {}).items()]
    working.sort(key=lambda t: str(t["started"] or ""))
    flight = flight_cards(processed, pr_reviews, active_tasks, ci_seen, next_poll_at, max_attempts, now)

    done_7d = fixed_recent + failed_recent
    groups = {g: sum(1 for i in attention if i["group"] == g) for g in _GROUP_ORDER}
    return {
        "kpis": {
            "needs_you": len(attention),
            "running": len(working),
            "in_flight": len(in_flight),
            "in_progress": len(working) + len(flight),
            "fixed_7d": fixed_recent,
            "fixed_delta": fixed_recent - fixed_prev,
            "auto_merged_7d": auto_merged_recent,
            "failed_7d": failed_recent,
            "success_pct": round(100 * fixed_recent / done_7d) if done_7d else None,
            "median_fix_min": _median_minutes(fix_minutes),
        },
        "groups": groups,
        "working": working,
        "flight": flight,
        "attention": attention,
        "in_flight": in_flight,
        "outcomes": outcomes[:OUTCOMES_MAX],
        "lanes": promotion_lanes(repos, pr_reviews, now),
    }
