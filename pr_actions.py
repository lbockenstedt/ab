"""
pr_actions.py — the ONE approve implementation and the ONE merge implementation,
shared by the human Settings-UI routes (routes.py: /api/pr-review/approve,
/api/pr-review/merge) and feature auto-drive's auto-merge path
(pr_review.py, gated by _automerge_decision).

Why this exists: before feature auto-drive, "approve" and "merge" only ever
happened via a human clicking a button — routes.py's _do_approve/_do_merge
closures were the only implementations. Auto-merge needed a second caller
that does the SAME actions unattended, and the safest way to do that is to
have exactly one approve function and one merge function that BOTH callers
use — not a bypass of merge_pr's existing "must be approved first" guard,
but a caller (pr_review._maybe_auto_merge) that genuinely calls approve_pr
first, so the guard is satisfied honestly.
"""
from main import logger, state
from app_state import update_pr_review
from github_ops import _ensure_label

_APPROVE_LABEL = "bugfixer-approved"


def approve_pr(gh, repo_name, number, *, actor="human"):
    """Applies the bugfixer-approved label + posts an approval comment.
    Does NOT itself update state["pr_reviews"] — the caller does that
    (routes.py calls mark_pr_approved; the auto path calls it too, which is
    what makes merge_pr's approval guard genuinely pass rather than being
    bypassed). Returns (repo, pr) PyGithub objects for the caller's own
    follow-up (e.g. applying further labels)."""
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(number)
    _ensure_label(repo, _APPROVE_LABEL)
    try:
        pr.add_to_labels(_APPROVE_LABEL)
    except Exception as e:
        logger.warning(f"pr_actions: could not label {repo_name}#{number} approved: {e}")
    comment = (
        "✅ **Approved** via BugFixer (human review). Cleared to merge/pull."
        if actor == "human" else
        "🤖 **Auto-Approved** via BugFixer Feature Auto-Drive — both review panels "
        "cleared the configured confidence threshold and the diff touched no "
        "configured boundary. Merging automatically."
    )
    try:
        pr.create_issue_comment(comment)
    except Exception as e:
        logger.warning(f"pr_actions: could not comment on {repo_name}#{number}: {e}")
    return repo, pr


def merge_pr(gh, repo_name, number):
    """Returns (status_code, response_dict) — the exact shape routes.py's
    prior _do_merge closure returned, just relocated so both the human route
    and the auto path share one implementation instead of routes.py owning
    the only copy. Guards, in order: already-merged (idempotent) ->
    closed-without-merge (reconcile, don't attempt) -> must be approved
    first (the ONE guard that makes unattended merging safe: see
    pr_review._maybe_auto_merge, which satisfies this by calling approve_pr
    + mark_pr_approved first, not by skipping this check) -> merge."""
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(number)
    if pr.merged:
        update_pr_review(repo_name, number, merged=True)
        return 200, {"status": "success", "message": "already merged"}
    if (pr.state or "").lower() == "closed":
        update_pr_review(repo_name, number, closed=True)
        return 409, {"status": "error", "closed": True,
            "message": f"PR #{number} is closed on GitHub (not merged) — its changes may have "
                       f"merged under another PR. Marked CLOSED."}
    rec = (state.get("pr_reviews") or {}).get("%s#%s" % (repo_name, number)) or {}
    if not rec.get("approved"):
        return 409, {"status": "error", "needs_approval": True,
            "message": f"PR #{number} must be Approved before it can be merged."}
    res = pr.merge()  # default merge commit; raises if not mergeable
    update_pr_review(repo_name, number, merged=True)
    logger.info(f"pr_actions: {repo_name} #{number} MERGED")
    return 200, {"status": "success", "merged": bool(getattr(res, "merged", True)),
                "message": getattr(res, "message", "merged")}
