"""
feature_drive.py — BugFixer's feature auto-drive: intake + classify stage.

Phase 1 of the plan at ~/.claude/plans/agile-snacking-lark.md. This module
owns the SEPARATE intake query + the deterministic-then-LLM classifier that
decides, for each LM-filed feature request, whether it is:

  - "build"    — a safe bolt-on. Phase 1 stops here (logs readiness only);
                 Phase 2's feature_build.py picks these up and actually builds.
  - "flag"     — crosses an operator-defined boundary (feature_boundary.py).
                 No code touched; labeled + commented for a human, stays queued.
  - "clarify"  — not risky, just under-specified. No code touched; the LLM's
                 concrete questions are posted as a comment so the requester
                 (or a human proxying for them) can fill in the gaps. Re-
                 evaluated on the next cycle like any other open issue — if
                 someone has replied, Stage B sees the new comments and may
                 now return "build". (v1 scope, explicit 2026-08-12 user
                 decision: GitHub-comment only — routing this to the specific
                 original LM submitter inside LM's own UI is a separate,
                 later, cross-repo phase, not this one.)

Why a SEPARATE worker instead of widening scan_repo_issues' query
(workers.py:753-758): GitHub's `labels=` filter is AND semantics. Adding
"enhancement" to monitored_labels would require an issue to carry BOTH
"automated-fix" AND "enhancement" to be fetched at all — silently breaking
the entire bug pipeline. A second query with its own single label sidesteps
that trap entirely.

Intake is gated on <!-- report-type: feature --> by default
(feature_drive_require_marker) — a marker only scan_bugs (log_scan.py) writes,
and only for a request that already cleared LM's admin-approval gate. So the
v1 operator flow is exactly: someone files a feature request in LM -> an
admin approves it -> this pipeline picks it up automatically the next cycle.
"""
import hashlib
import re
from datetime import datetime

from main import (
    logger,
    load_config,
    load_processed,
    save_processed,
    recompute_issue_counters,
    get_monitored_repos,
    call_llm,
    state,
)
from fix_engine import _robust_json_loads, _norm_confidence
from workers import _schedule_check
import feature_boundary
import skills_loader
from github_ops import _ensure_label

# Terminal feature-drive statuses — once an issue lands in one of these,
# feature_drive skips it on future cycles (mirrors scan_repo_issues' terminal
# check at workers.py:785). "feature_needs_info" is intentionally ABSENT: a
# clarify outcome must be re-evaluated on the next cycle in case someone
# replied, whereas flag/built/failed are done until a human acts.
_TERMINAL_STATUSES = {"feature_flagged", "feature_built", "feature_failed"}

_FEATURE_MARKER_RE = re.compile(r"<!--\s*report-type:\s*feature\s*-->")
_BOUNDARY_FLAG_MARKER = "<!-- bugfixer-boundary-flag: v1 -->"

_CLASSIFY_SYSTEM = (
    "You are BugFixer's feature-request classifier. You decide whether an "
    "incoming feature request is a safe, small bolt-on that can be built "
    "automatically using EXISTING project infrastructure, whether it crosses "
    "an operator-defined boundary that requires a human decision, or whether "
    "it is too under-specified to build safely yet (but not risky).\n\n"
    "Default to caution. If you are not confident a request is a small, "
    "additive bolt-on that reuses existing patterns (a new button, a new "
    "route following an existing shape, a new field, a new simple report), "
    "prefer 'clarify' over guessing, and prefer 'flag' over building "
    "something that touches a listed boundary even slightly."
)

_CLASSIFY_PROMPT_TMPL = """## Feature request

Title: {title}

Body:
{body}
{fix_context}

{boundaries}

## Available build recipes (skills) — pick one by name if you choose "build", else null
{skills}

## Your task

Decide exactly one verdict:
- "build": this is a safe bolt-on that reuses existing infrastructure and does
  not require touching anything in the Boundaries list above. Name the skill
  (recipe) that best fits, or null if none of the skills fit (a build with no
  matching skill will be refused downstream, so be honest here).
- "flag": building this would require touching one or more of the Boundaries
  above. List which boundary id(s) apply.
- "clarify": this is NOT risky, but there isn't enough concrete detail to
  build it safely (e.g. no target page/module named, no clear description of
  what the control should do). Write 2-4 short, concrete, answerable
  questions that would unblock a "build" verdict.

Return ONLY a JSON object:
{{"verdict": "build"|"flag"|"clarify", "boundary_ids": [...], "skill": "<name>"|null,
  "questions": [...], "reason": "<one or two sentences>", "confidence": <0.0-1.0>}}
"""

_CLASSIFY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["build", "flag", "clarify"]},
        "boundary_ids": {"type": "array", "items": {"type": "string"}},
        "skill": {"type": ["string", "null"]},
        "questions": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "confidence": {"type": "number", "description": "0.0-1.0"},
    },
    "required": ["verdict", "reason", "confidence"],
}


def _is_feature_request(body):
    return bool(_FEATURE_MARKER_RE.search(body or ""))


def _fix_context_block(body):
    """Best-effort hub screenshot/console context via the same helper
    scan_bugs's fix pipeline already uses — genuinely useful for "add a
    button here" style requests. Never raises; empty string on any failure
    (matches _bug_report_fix_context's own contract)."""
    try:
        from log_scan import _bug_report_fix_context
        return _bug_report_fix_context(body or "")
    except Exception as e:
        logger.debug(f"feature_drive: fix-context lookup skipped: {e}")
        return ""


def classify(issue_title, issue_body, config):
    """Stage A (deterministic) then Stage B (one LLM call). Returns:
        {"verdict": "build"|"flag"|"clarify", "boundary_ids": [...],
         "skill": str|None, "questions": [...], "reason": str, "confidence": float}
    Fails CLOSED to "flag" on any error, unparseable response, or an
    unrecognized verdict string — ambiguity is never treated as permission
    to build."""
    boundaries = config.get("feature_boundaries") or []

    pre = feature_boundary.prefilter(issue_title, issue_body, boundaries)
    if pre["hard"]:
        ids = [h["id"] for h in pre["hits"]]
        return {
            "verdict": "flag", "boundary_ids": ids, "skill": None, "questions": [],
            "reason": "Deterministic match on boundary rule(s): " + ", ".join(ids) + ".",
            "confidence": 1.0,
        }

    try:
        skills = skills_loader.get_loaded()
        skills_block = "\n".join(
            f"- {n}: {(s.get('description') or '')[:200]}" for n, s in sorted(skills.items())
        ) or "(no skills loaded — a \"build\" verdict will have nothing to work from)"
        boundaries_block = feature_boundary.render_boundaries_for_prompt(boundaries) \
            or "(no boundaries configured — nothing to flag on)"
        fix_context = _fix_context_block(issue_body)

        prompt = _CLASSIFY_PROMPT_TMPL.format(
            title=issue_title or "", body=issue_body or "", fix_context=fix_context,
            boundaries=boundaries_block, skills=skills_block,
        )
        raw = call_llm(prompt, system_prompt=_CLASSIFY_SYSTEM, task_kind="review",
                       json_schema=_CLASSIFY_JSON_SCHEMA)
        data = _robust_json_loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"classifier response was not a JSON object: {raw[:200]!r}")

        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in ("build", "flag", "clarify"):
            raise ValueError(f"unrecognized verdict: {verdict!r}")

        confidence = _norm_confidence(data.get("confidence"))
        boundary_ids = [str(x) for x in (data.get("boundary_ids") or [])] if verdict == "flag" else []
        questions = [str(x).strip() for x in (data.get("questions") or []) if str(x).strip()][:6] \
            if verdict == "clarify" else []
        skill = data.get("skill") if verdict == "build" else None
        skill = skill if (skill and skill in skills) else None
        reason = str(data.get("reason") or "").strip()[:1000]

        if verdict == "clarify" and not questions:
            # A clarify verdict with no actual questions is useless to the
            # requester and would loop forever — treat it as a flag instead
            # so a human at least sees it, rather than silently vanishing.
            raise ValueError("verdict='clarify' but no questions were provided")

        return {"verdict": verdict, "boundary_ids": boundary_ids, "skill": skill,
                "questions": questions, "reason": reason, "confidence": confidence}
    except Exception as e:
        logger.warning(f"feature_drive: classifier failed, failing closed to 'flag': {e}")
        return {"verdict": "flag", "boundary_ids": [], "skill": None, "questions": [],
                "reason": f"Classifier error (failed closed): {e}", "confidence": 0.0}


def _already_commented(issue, marker_prefix):
    """True if a comment carrying `marker_prefix` already exists — the
    idempotency check shared by _flag_issue and _clarify_issue so a repeat
    scan of the same still-open issue doesn't re-comment every cycle."""
    try:
        for c in issue.get_comments():
            if marker_prefix in (c.body or ""):
                return True
    except Exception as e:
        logger.debug(f"feature_drive: comment scan failed, proceeding as not-yet-commented: {e}")
    return False


def _flag_issue(gh_repo, issue, result):
    """Labels + comments a boundary-crossing request. No code touched, issue
    stays open. Idempotent via _BOUNDARY_FLAG_MARKER."""
    _ensure_label(gh_repo, "bugfixer-needs-human")
    if _already_commented(issue, _BOUNDARY_FLAG_MARKER):
        return
    boundaries = {b.get("id"): b for b in (load_config().get("feature_boundaries") or [])}
    matched = [boundaries[i] for i in result["boundary_ids"] if i in boundaries]
    if matched:
        rule_lines = "\n".join(f"- **{b.get('label', b.get('id'))}**: {b.get('rule', '')}" for b in matched)
    else:
        rule_lines = f"- {result.get('reason') or 'Crosses a configured boundary.'}"
    body = (
        "🤖 **BugFixer — Feature Auto-Drive**\n\n"
        "This request would require touching something outside what BugFixer is allowed to "
        "build automatically, so **no code has been touched**. It stays open here for a human "
        "to design and implement.\n\n"
        f"**Boundary crossed:**\n{rule_lines}\n\n"
        f"{_BOUNDARY_FLAG_MARKER}"
    )
    try:
        issue.add_to_labels("bugfixer-needs-human")
    except Exception as e:
        logger.warning(f"feature_drive: could not label {issue.number} needs-human: {e}")
    try:
        issue.create_comment(body)
    except Exception as e:
        logger.warning(f"feature_drive: could not comment on {issue.number}: {e}")


def _clarify_marker(questions):
    """Content-hash the questions into the marker so a re-run with the SAME
    questions doesn't re-comment (idempotent), but a re-run with DIFFERENT
    questions (e.g. a partial reply prompted a second round) does comment
    again — a plain fixed marker would suppress that legitimate update."""
    qhash = hashlib.sha1("\n".join(questions).encode("utf-8", "replace")).hexdigest()[:10]
    return f"<!-- bugfixer-clarify: v1:{qhash} -->"


def _clarify_issue(gh_repo, issue, result):
    """Labels + comments an under-specified (but not risky) request with
    concrete follow-up questions. No code touched, issue stays open, and is
    NOT terminal — it's re-classified on the next cycle in case of a reply."""
    _ensure_label(gh_repo, "bugfixer-needs-info")
    marker = _clarify_marker(result["questions"])
    # Exact-marker match: the hash is derived from the question text itself,
    # so this alone gives "same questions -> skip, different questions ->
    # comment again" without any separate prefix check.
    if _already_commented(issue, marker):
        return
    q_lines = "\n".join(f"{i}. {q}" for i, q in enumerate(result["questions"], 1))
    body = (
        "🤖 **BugFixer — Feature Auto-Drive**\n\n"
        "This looks like a reasonable request, but there isn't quite enough detail to build it "
        "safely yet. Could you (or whoever can speak to this) reply on this issue with answers to:\n\n"
        f"{q_lines}\n\n"
        "No code has been touched. This will be re-evaluated automatically once there's a reply.\n\n"
        f"{marker}"
    )
    try:
        issue.add_to_labels("bugfixer-needs-info")
    except Exception as e:
        logger.warning(f"feature_drive: could not label {issue.number} needs-info: {e}")
    try:
        issue.create_comment(body)
    except Exception as e:
        logger.warning(f"feature_drive: could not comment on {issue.number}: {e}")


def scan_feature_requests(gh, config):
    """Feature auto-drive's intake worker — see module docstring. Classifies
    up to feature_drive_max_per_cycle open, marker-gated requests per cycle;
    flag/clarify are fully handled here (Phase 1). A "build" verdict is only
    logged for now — feature_build.py (Phase 2) is what actually picks up and
    builds those."""
    global state
    if not config.get("feature_drive_enabled", False):
        return
    if state.get("paused") or state.get("blackout"):
        logger.debug("feature_drive: skipped — paused/blackout")
        return

    sched = _schedule_check(config)
    if not sched.get("allowed", True):
        logger.info(f"feature_drive: deferring — {sched.get('reason')}")
        return

    label = (config.get("feature_drive_label") or "enhancement").strip()
    if not label:
        return
    require_marker = config.get("feature_drive_require_marker", True)
    max_per_cycle = max(0, int(config.get("feature_drive_max_per_cycle") or 1))
    if max_per_cycle == 0:
        return
    repos = config.get("feature_drive_repos") or get_monitored_repos(config)

    processed = load_processed()
    handled = 0
    for repo_name in repos:
        if handled >= max_per_cycle:
            break
        try:
            repo_obj = gh.get_repo(repo_name)
            for issue in repo_obj.get_issues(labels=[label], state="open"):
                if handled >= max_per_cycle:
                    break
                if issue.pull_request:
                    continue
                if any(lbl.name == "bugfixer-dismissed" for lbl in issue.labels):
                    continue
                body = issue.body or ""
                if require_marker and not _is_feature_request(body):
                    continue
                issue_id = f"{repo_name}:{issue.number}"
                if processed.get(issue_id, {}).get("status") in _TERMINAL_STATUSES:
                    continue

                handled += 1
                try:
                    result = classify(issue.title or "", body, config)
                except Exception as e:
                    logger.error(f"feature_drive: classify raised for {issue_id} (not swallowed by "
                                f"classify() itself — this is a bug): {e}")
                    continue

                if result["verdict"] == "flag":
                    _flag_issue(repo_obj, issue, result)
                    processed[issue_id] = {
                        "status": "feature_flagged",
                        "boundary_ids": result["boundary_ids"],
                        "reason": result["reason"],
                        "timestamp": datetime.now().isoformat(),
                    }
                    save_processed(processed)
                    recompute_issue_counters(processed)
                elif result["verdict"] == "clarify":
                    _clarify_issue(repo_obj, issue, result)
                    processed[issue_id] = {
                        "status": "feature_needs_info",
                        "questions": result["questions"],
                        "timestamp": datetime.now().isoformat(),
                    }
                    save_processed(processed)
                    recompute_issue_counters(processed)
                else:  # "build"
                    logger.info(
                        f"feature_drive: {issue_id} classified BUILD "
                        f"(skill={result['skill']!r}, confidence={result['confidence']:.2f}) — "
                        f"dispatching to feature_build."
                    )
                    try:
                        from feature_build import build_feature
                        build_feature(gh, repo_obj, issue, result, config)
                    except Exception as e:
                        # build_feature is itself best-effort/never-raises by
                        # contract (see its own docstring) — this except is
                        # defense-in-depth so a genuinely unexpected error
                        # (e.g. the import itself failing) doesn't take down
                        # the whole scan cycle.
                        logger.exception(f"feature_drive: build_feature raised unexpectedly for {issue_id}: {e}")
                    # build_feature updates `processed` itself (feature_built /
                    # feature_flagged / feature_failed) — reload so this
                    # worker's own local `processed` dict doesn't clobber it
                    # on a later save_processed call this same cycle.
                    processed = load_processed()
        except Exception as e:
            logger.exception(f"feature_drive: scan failed for {repo_name}: {e}")
