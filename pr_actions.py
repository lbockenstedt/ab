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
import os
import time
import tempfile

from github import GithubException

from main import logger, state
from app_state import update_pr_review
from github_ops import _ensure_label

_APPROVE_LABEL = "ab-approved"

# Paths whose merge conflicts AppBuilder is allowed to auto-resolve when
# updating a stale PR branch with its base, and which side to keep. Kept
# deliberately tiny and cosmetic-only: a code/logic conflict must NEVER be
# machine-resolved — it aborts and goes back to a human. ``VERSION`` is a
# display-only string (change detection keys off the commit hash, not this
# file), and the recurring cause of PR conflicts is the base advancing its
# ``VERSION`` while a fix branch sat, so we keep the base's value ("theirs")
# to avoid regressing the base's displayed version.
_AUTO_RESOLVE_CONFLICTS = {"VERSION": "theirs"}


def _is_merge_conflict(exc):
    """True only for the GitHub 405 that specifically means *merge conflicts*
    (the base and head diverged on the same lines) — NOT the other 405s
    ``pr.merge()`` raises for "not mergeable" reasons like required status
    checks still pending/failing, which must not trigger a branch rewrite."""
    if not isinstance(exc, GithubException) or exc.status != 405:
        return False
    data = exc.data if isinstance(exc.data, dict) else {}
    return "conflict" in str(data.get("message", exc.data)).lower()


def _conflicted_paths(repo_git):
    out = repo_git.git.diff("--name-only", "--diff-filter=U")
    return [p for p in out.splitlines() if p.strip()]


def _resolve_tree_conflicts(repo_git, base_ref):
    """On the currently-checked-out (head) branch, merge ``base_ref`` and
    auto-resolve ONLY the allowlisted cosmetic conflicts in
    ``_AUTO_RESOLVE_CONFLICTS``. Returns ``(resolved, detail)``. On success a
    completed merge commit is left on the branch; on any non-allowlisted
    conflict the merge is aborted (branch left untouched) and ``resolved`` is
    False so the caller hands the PR back to a human."""
    import git
    try:
        repo_git.git.merge(base_ref, "--no-edit")
        return True, "base merged cleanly (no conflicts)"
    except git.GitCommandError:
        pass  # conflicts — inspect below
    conflicts = _conflicted_paths(repo_git)
    unresolvable = [p for p in conflicts if p not in _AUTO_RESOLVE_CONFLICTS]
    if unresolvable or not conflicts:
        repo_git.git.merge("--abort")
        return False, ("unresolvable conflict(s) in: %s — a human must resolve"
                       % ", ".join(sorted(unresolvable or conflicts)))
    for path in conflicts:
        side = _AUTO_RESOLVE_CONFLICTS[path]
        repo_git.git.checkout("--%s" % side, "--", path)
        repo_git.git.add("--", path)
    repo_git.git.commit("--no-edit")
    return True, ("auto-resolved cosmetic conflict(s) in: %s"
                  % ", ".join(sorted(conflicts)))


def _wait_mergeable(pr, timeout=20.0):
    """GitHub recomputes a PR's mergeability asynchronously after a push;
    poll until it's known (True/False) or we give up. Returns the last known
    ``mergeable`` (True / False / None-on-timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pr.update()  # refresh from GitHub
        except Exception:  # noqa: BLE001
            pass
        if pr.mergeable is not None:
            return pr.mergeable
        time.sleep(2)
    return pr.mergeable


def _auto_resolve_pr_conflicts(repo, pr, token):
    """Update a stale PR branch by merging its base into it and resolving only
    cosmetic (VERSION) conflicts, then push the branch back so ``pr.merge()``
    can retry. Mirrors the manual "merge main into the branch, keep the
    intended VERSION, push" recovery, but bails to a human on any real code
    conflict. Returns ``(ok, detail)``. Same-repo branches only (a fork head
    can't be pushed to)."""
    import git
    head_repo = getattr(pr.head, "repo", None)
    if head_repo is None or head_repo.full_name != repo.full_name:
        return False, "PR head is on a fork — AppBuilder can't update it"
    head_ref, base_ref = pr.head.ref, pr.base.ref
    with tempfile.TemporaryDirectory(prefix="ab-merge-") as tmp:
        path = os.path.join(tmp, "repo")
        # Clone with the token, then strip it straight back out of .git/config
        # (mirrors fix_engine's hygiene); re-applied only for the push below.
        url = repo.clone_url.replace("https://", "https://%s@" % token)
        repo_git = git.Repo.clone_from(url, path)
        repo_git.remotes.origin.set_url(repo.clone_url)
        with repo_git.config_writer() as cw:
            cw.set_value("user", "name", "AppBuilder")
            cw.set_value("user", "email",
                         "223556219+Copilot@users.noreply.github.com")
        repo_git.git.checkout(head_ref)
        ok, detail = _resolve_tree_conflicts(repo_git, "origin/%s" % base_ref)
        if not ok:
            return False, detail
        from fix_engine import _authenticated_remote
        with _authenticated_remote(repo_git.remotes.origin, repo.clone_url, token):
            repo_git.remotes.origin.push("HEAD:%s" % head_ref)
    return True, detail


def _github_token():
    from config_store import load_config
    cfg = load_config() or {}
    return cfg.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")


def approve_pr(gh, repo_name, number, *, actor="human"):
    """Applies the ab-approved label + posts an approval comment.
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
        "✅ **Approved** via AppBuilder (human review). Cleared to merge/pull."
        if actor == "human" else
        "🤖 **Auto-Approved** via AppBuilder Feature Auto-Drive — both review panels "
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
    try:
        res = pr.merge()  # default merge commit; raises if not mergeable
    except GithubException as e:
        if not _is_merge_conflict(e):
            raise
        # The base advanced under a stale branch. Try the same recovery a human
        # would: merge the base into the branch, auto-resolve only cosmetic
        # (VERSION) conflicts, push, then retry the merge once. Any real code
        # conflict aborts and is handed back to a human.
        logger.info(f"pr_actions: {repo_name} #{number} has merge conflicts — "
                    f"attempting auto-resolve")
        token = _github_token()
        if not token:
            return 409, {"status": "error", "conflict": True,
                "message": f"PR #{number} has merge conflicts and no GitHub token "
                           f"is configured to auto-resolve them."}
        ok, detail = _auto_resolve_pr_conflicts(repo, pr, token)
        if not ok:
            return 409, {"status": "error", "conflict": True,
                "message": f"PR #{number} has merge conflicts AppBuilder could not "
                           f"auto-resolve ({detail}). Resolve them manually, then merge."}
        logger.info(f"pr_actions: {repo_name} #{number} branch updated ({detail}); "
                    f"re-checking mergeability")
        pr = repo.get_pull(number)
        _wait_mergeable(pr)
        res = pr.merge()  # retry once, now that the branch carries the base
        logger.info(f"pr_actions: {repo_name} #{number} merged after auto-resolving conflicts")
    update_pr_review(repo_name, number, merged=True)
    logger.info(f"pr_actions: {repo_name} #{number} MERGED")
    return 200, {"status": "success", "merged": bool(getattr(res, "merged", True)),
                "message": getattr(res, "message", "merged")}
