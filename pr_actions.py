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
# os builds clone paths and reads the GitHub token from the environment.
import os
from datetime import datetime
# time drives polling loops (mergeability, parked-run release).
import time
# tempfile gives a throwaway directory for the conflict-resolution clone.
import tempfile

# requests calls GitHub's REST API directly for check runs and Actions runs.
import requests
# PyGithub's error type, inspected to classify merge failures.
from github import GithubException

# Shared app logger and in-memory state (holds PR review records).
from main import logger, state
# Persists merge/approval facts onto the stored PR review record.
from app_state import update_pr_review
# Creates the approval label in a repo if it does not exist yet.
from github_ops import _ensure_label
# Allowlist deciding which branches may be deleted after a merge.
from branch_policy import may_delete
# Reads config.json (for the branch policy and GitHub token).
from config_store import load_config

# GitHub label that marks a PR as approved through AppBuilder.
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
    # Only a GitHub 405 can be a merge conflict.
    if not isinstance(exc, GithubException) or exc.status != 405:
        return False
    # GitHub puts the reason in the error body's "message"; conflicts say "conflict".
    data = exc.data if isinstance(exc.data, dict) else {}
    return "conflict" in str(data.get("message", exc.data)).lower()


def _merge_refusal(exc):
    """A GitHub *policy* refusal of a merge as ``(status_code, message)``, or
    None if *exc* is not one.

    A refusal is a condition only a human can clear -- branch protection or a
    repository ruleset rejecting the merge, a token without the ``workflow``
    scope, a required check still red. None of those are AppBuilder defects;
    the repository is behaving exactly as configured.

    They used to propagate out of merge_pr as an exception, so the caller
    logged them at ERROR with a stack trace -- and AppBuilder's own log scanner
    then filed each one as a bug report against AppBuilder (ab#107, #170,
    #192). Returning them as a structured result instead lets the caller log at
    WARNING, which is what an operator-actionable condition is, and lets it
    record the reason ONCE rather than re-reporting the identical refusal on
    every poll of an unchanged PR.

    Merge conflicts are deliberately NOT claimed here: they have their own
    auto-resolve recovery path in merge_pr, which must still run."""
    # Not a GitHub error at all: not a policy refusal.
    if not isinstance(exc, GithubException):
        return None
    # Read GitHub's error message (lower-cased copy for matching).
    data = exc.data if isinstance(exc.data, dict) else {}
    msg = str(data.get("message", exc.data) or "")
    low = msg.lower()
    # Merge conflicts are handled by the auto-resolve path, not here.
    if exc.status == 405 and "conflict" in low:
        return None
    # Branch protection or a ruleset rejected the merge.
    if "rule violation" in low:
        return (409, f"branch protection / repository rules refused the merge: {msg}")
    # The token cannot update workflow files without the "workflow" scope.
    if exc.status == 403 and "workflow" in low and "scope" in low:
        return (403, "the configured GitHub token lacks the `workflow` scope needed to "
                     f"update a workflow file: {msg}")
    # Any other 405: GitHub says the PR is not mergeable right now.
    if exc.status == 405:
        return (409, f"GitHub refused the merge (the PR is not in a mergeable state): {msg}")
    # Some other error: let the caller treat it as a real failure.
    return None


def _github_message(exc):
    """GitHub's error message from a GithubException."""
    data = exc.data if isinstance(exc.data, dict) else {}
    return str(data.get("message", exc.data) or "")


# Merge refusals that clear by themselves within seconds (ab#500, ab#547,
# opnsense#115): a required check that has not finished yet, or a merge of
# the same PR already running.
_TRANSIENT_MERGE_REFUSALS = ("is in progress", "is queued",
                             "merge already in progress")


def _merge_is_transient(exc):
    """True when GitHub refused the merge only for now (see above).

    A ruleset refusal lists every blocker on its own line, so EVERY listed
    blocker must be transient: one still-running check next to a rule only a
    human can clear is a refusal."""
    if not isinstance(exc, GithubException):
        return False
    lines = [ln.strip().lower() for ln in _github_message(exc).splitlines()]
    blockers = [ln for ln in lines if ln and ln != "repository rule violations found"]
    return bool(blockers) and all(
        any(t in ln for t in _TRANSIENT_MERGE_REFUSALS) for ln in blockers)


def _expects_missing_check(msg):
    """True when a merge refusal is "a required status check never reported",
    as opposed to one that genuinely failed.

    GitHub phrases it as ``Required status check "test" is expected.`` inside a
    rule-violation message. It does NOT mean the check is red -- it means no
    run ever posted that context, which is what happens when the workflow run
    is parked (see ``_release_parked_checks``). That distinction matters
    because a red check is a human's problem while a parked run is ours."""
    # GitHub's wording for a check that never reported: "Required status check ... is expected".
    low = str(msg or "").lower()
    return "required status check" in low and "is expected" in low


# The last check-run counts seen per (repo, head sha), from lookups AppBuilder
# already makes; the dashboard reads them (mission_control) without calling GitHub.
CI_SEEN = {}
_CI_SEEN_MAX = 200


def _remember_ci(repo_name, head_sha, runs, failing):
    """Cache a commit's check-run counts: finished, passed and failed.

    Only success/neutral/skipped count as passed; a cancelled, parked or
    unrecognised run is finished but never a pass (as in head_ci_conclusion)."""
    done = sum(1 for r in runs if r.get("status") == "completed")
    passed = sum(1 for r in runs if r.get("status") == "completed"
                 and r.get("conclusion") in ("success", "neutral", "skipped"))
    CI_SEEN[(repo_name, head_sha)] = {"done": done, "passed": passed, "total": len(runs), "failed": len(failing),
                                      "at": datetime.now().isoformat(timespec="seconds")}
    while len(CI_SEEN) > _CI_SEEN_MAX:
        CI_SEEN.pop(next(iter(CI_SEEN)))


def head_ci_conclusion(repo_name, head_sha, token, *, timeout=10.0):
    """The CI verdict for *head_sha* as ``(state, details)``, where state is one
    of ``success``/``failure``/``pending``/``unknown``.

    With ``qa_enabled`` on, ``fix_engine.verify_fix`` runs the fleet smoke test
    (the checks and tests CI runs) in the sandbox before a fix is pushed; with
    it off, every generated fix is pushed unverified. Either way GitHub Actions
    is the final ground truth for whether a fix actually works, and nothing was
    reading it: the sole Actions
    call in this module releases PARKED runs and never looks at a conclusion.
    The result was a fixer flying blind — it would push a fix that broke the
    suite, get told nothing, and spend its remediation budget re-fixing the
    logic the panel named while the real breakage went unmentioned.

    ``unknown`` is deliberately distinct from ``success`` and is returned when
    there is no token, the lookup fails, or the commit has NO check runs at all.
    Callers must treat it as "no evidence", never as a pass — reporting a commit
    with no checks as green is exactly the false all-clear this exists to stop.
    A parked (``action_required``) run counts as pending, not failure, because
    ``_release_parked_checks`` is what clears those.
    """
    # Without a token we cannot look anything up: no evidence.
    if not token:
        return "unknown", ""
    # Ask GitHub for every check run on the commit.
    try:
        resp = requests.get(
            "https://api.github.com/repos/%s/commits/%s/check-runs" % (repo_name, head_sha),
            params={"per_page": 100},
            headers={"Authorization": "token " + token,
                     "Accept": "application/vnd.github+json"},
            timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — network: no evidence, not a verdict
        logger.debug("pr_actions: CI lookup for %s @ %s failed: %s", repo_name, head_sha, exc)
        return "unknown", ""
    # Non-200 reply: no evidence.
    if resp.status_code != 200:
        logger.debug("pr_actions: CI lookup for %s @ %s returned HTTP %s",
                     repo_name, head_sha, resp.status_code)
        return "unknown", ""
    # Decode the JSON body; malformed JSON means no evidence.
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("pr_actions: CI lookup for %s @ %s gave bad JSON: %s",
                     repo_name, head_sha, exc)
        return "unknown", ""
    # Keep only well-formed check-run entries.
    if not isinstance(payload, dict):
        return "unknown", ""
    runs = payload.get("check_runs")
    runs = [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
    # No check runs at all is "unknown", never "success" -- unless the runs
    # failed to start (zero jobs): that is a GitHub Actions outage, not a verdict.
    if not runs:
        import gh_actions_health
        stuck = gh_actions_health.infra_failed_runs(repo_name, head_sha, token)
        if stuck:
            gh_actions_health.note_infra_failure(len(stuck))
            return "infra", "%d workflow run(s) never started (GitHub Actions outage)" % len(stuck)
        return "unknown", ""

    # cancelled / stale / startup_failure / action_required never produced a
    # test verdict: absent evidence, so pending (non-blocking, never a pass).
    # A completed run with a missing/unrecognised conclusion is unknown.
    # Failing check names, plus flags for still-running and unrecognised results.
    failing, pending, unrecognised = [], False, False
    # Classify each check run.
    for run in runs:
        conclusion = run.get("conclusion")
        # Still queued or running.
        if run.get("status") != "completed":
            pending = True
        # A real failure.
        elif conclusion in ("failure", "timed_out"):
            failing.append("%s: %s" % (run.get("name") or "check", conclusion))
        # Finished without producing a test verdict: treat as pending.
        elif conclusion in ("action_required", "cancelled", "stale", "startup_failure"):
            pending = True
        # Anything not clearly passing is unrecognised.
        elif conclusion not in ("success", "neutral", "skipped"):
            unrecognised = True
    _remember_ci(repo_name, head_sha, runs, failing)
    # Any failure wins, then pending, then unknown; otherwise everything passed.
    if failing:
        return "failure", ", ".join(failing)
    if pending:
        return "pending", ""
    if unrecognised:
        return "unknown", ""
    return "success", ""


def _release_parked_checks(repo_name, head_sha, token, *, timeout=60.0):
    """Approve every workflow run parked as ``action_required`` on *head_sha*,
    returning how many were released.

    GitHub does not start the checks of a PR opened by a bot token; it parks
    each run as ``action_required``. A parked run never posts its status, so a
    branch protection rule that requires that check can never be satisfied and
    the PR is MERGEABLE/BLOCKED forever -- no failure to remediate, no red
    check to look at, just silence. promote.yml/backmerge.yml release their own
    runs, but this is the safety net for every other route to a PR, and for the
    case where that step raced and missed one.

    Deliberately approves ALL parked runs rather than stopping at the first:
    several workflows are triggered per PR and they park at slightly different
    instants, which is exactly the race that stranded ab#338."""
    class _Outcome(int):
        """The count, plus WHY it is what it is. A bare 0 conflates four
        distinct states (no token, the lookup never succeeded, nothing was
        parked, every approve failed); the caller must tell them apart."""
        outcome = "released"

    def _result(n, outcome):
        """Return the count wrapped in an _Outcome carrying the reason for that count."""
        r = _Outcome(n)
        r.outcome = outcome
        return r

    # Cannot call the API without a token.
    if not token:
        return _result(0, "no-token")
    # Auth headers for every GitHub call below.
    headers = {"Authorization": "token " + token,
               "Accept": "application/vnd.github+json"}
    # Runs already approved, and how many consecutive polls found nothing new.
    approved = set()
    quiet = 0
    lookup_ok = False   # at least one poll actually got a run list back
    saw_runs = False    # at least one parked run was actually found
    # Keep polling until the timeout; runs may park at slightly different times.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Ask GitHub for runs on this commit that are waiting for approval.
        runs = []
        try:
            resp = requests.get(
                "https://api.github.com/repos/%s/actions/runs" % repo_name,
                params={"head_sha": head_sha, "status": "action_required",
                        "per_page": 100},
                headers=headers, timeout=10)
            # A good reply: remember the lookup worked and read the run list.
            if resp.status_code == 200:
                lookup_ok = True
                payload = resp.json() or {}
                found = payload.get("workflow_runs")
                runs = found if isinstance(found, list) else []
        except Exception as exc:  # network/JSON — retry on the next poll
            logger.debug("pr_actions: parked-run lookup for %s @ %s failed: %s",
                         repo_name, head_sha, exc)
        # Approve every newly found parked run.
        new = 0
        for run in runs:
            # Skip malformed entries and runs without an id.
            if not isinstance(run, dict):
                continue
            run_id = run.get("id")
            if not run_id:
                continue
            # A parked run exists; skip it if we already approved it.
            saw_runs = True
            if run_id in approved:
                continue
            # Ask GitHub to approve (start) the run.
            try:
                res = requests.post(
                    "https://api.github.com/repos/%s/actions/runs/%s/approve"
                    % (repo_name, run_id), headers=headers, timeout=10)
            # Network failure: try again on the next poll.
            except Exception as exc:
                logger.debug("pr_actions: approving parked run %s of %s failed: %s",
                             repo_name, run_id, exc)
                continue
            # Approved: record it and log the release.
            if res.status_code < 300:
                approved.add(run_id)
                new += 1
                logger.info("pr_actions: released parked workflow run %s on %s @ %s",
                            run_id, repo_name, head_sha[:8])
            # GitHub refused; log why.
            else:
                logger.warning("pr_actions: could not approve parked run %s on %s "
                               "(HTTP %s): %s", run_id, repo_name,
                               res.status_code, (res.text or "")[:200])
        # After at least one approval, stop once three polls in a row find nothing new.
        if approved and not new:
            quiet += 1
            if quiet >= 3:
                break
        # Something new appeared (or nothing approved yet): reset the quiet counter.
        else:
            quiet = 0
        # Pause between polls.
        time.sleep(5)
    # Report the number released, or which of the zero-states happened.
    if approved:
        return _result(len(approved), "released")
    if not lookup_ok:
        return _result(0, "lookup-failed")
    if saw_runs:
        return _result(0, "approve-failed")
    return _result(0, "none-parked")


def _conflicted_paths(repo_git):
    """Return the paths that are currently in an unresolved merge-conflict state."""
    # List unmerged ("U") paths, one per line, ignoring blank lines.
    out = repo_git.git.diff("--name-only", "--diff-filter=U")
    return [p for p in out.splitlines() if p.strip()]


def _resolve_tree_conflicts(repo_git, base_ref):
    """On the currently-checked-out (head) branch, merge ``base_ref`` and
    auto-resolve ONLY the allowlisted cosmetic conflicts in
    ``_AUTO_RESOLVE_CONFLICTS``. Returns ``(resolved, detail)``. On success a
    completed merge commit is left on the branch; on any non-allowlisted
    conflict the merge is aborted (branch left untouched) and ``resolved`` is
    False so the caller hands the PR back to a human."""
    # Imported lazily; GitPython is only needed for conflict resolution.
    import git
    # Try a plain merge of the base; success means there were no conflicts.
    try:
        repo_git.git.merge(base_ref, "--no-edit")
        return True, "base merged cleanly (no conflicts)"
    # The merge stopped on conflicts; inspect them.
    except git.GitCommandError:
        pass  # conflicts — inspect below
    # Which paths conflict, and which of them we are not allowed to auto-resolve.
    conflicts = _conflicted_paths(repo_git)
    unresolvable = [p for p in conflicts if p not in _AUTO_RESOLVE_CONFLICTS]
    # Any real code conflict (or a failure with no conflicted paths): abort and hand back.
    if unresolvable or not conflicts:
        repo_git.git.merge("--abort")
        return False, ("unresolvable conflict(s) in: %s — a human must resolve"
                       % ", ".join(sorted(unresolvable or conflicts)))
    # Resolve each allowed path by taking the configured side, then stage it.
    for path in conflicts:
        side = _AUTO_RESOLVE_CONFLICTS[path]
        repo_git.git.checkout("--%s" % side, "--", path)
        repo_git.git.add("--", path)
    # Finish the merge commit with the default message.
    repo_git.git.commit("--no-edit")
    return True, ("auto-resolved cosmetic conflict(s) in: %s"
                  % ", ".join(sorted(conflicts)))


def _wait_mergeable(pr, timeout=20.0):
    """GitHub recomputes a PR's mergeability asynchronously after a push;
    poll until it's known (True/False) or we give up. Returns the last known
    ``mergeable`` (True / False / None-on-timeout)."""
    # Give up after the timeout.
    deadline = time.time() + timeout
    # Poll until GitHub has computed mergeability.
    while time.time() < deadline:
        try:
            pr.update()  # refresh from GitHub
        except Exception:  # noqa: BLE001
            pass
        # GitHub knows the answer now.
        if pr.mergeable is not None:
            return pr.mergeable
        time.sleep(2)
    # Timed out: return whatever GitHub last reported (possibly None).
    return pr.mergeable


def _auto_resolve_pr_conflicts(repo, pr, token):
    """Update a stale PR branch by merging its base into it and resolving only
    cosmetic (VERSION) conflicts, then push the branch back so ``pr.merge()``
    can retry. Mirrors the manual "merge main into the branch, keep the
    intended VERSION, push" recovery, but bails to a human on any real code
    conflict. Returns ``(ok, detail)``. Same-repo branches only (a fork head
    can't be pushed to)."""
    # Imported lazily; GitPython is only needed here.
    import git
    # Only branches in this same repo can be pushed to.
    head_repo = getattr(pr.head, "repo", None)
    if head_repo is None or head_repo.full_name != repo.full_name:
        return False, "PR head is on a fork — AppBuilder can't update it"
    # The PR's branch and the branch it merges into.
    head_ref, base_ref = pr.head.ref, pr.base.ref
    # Work in a temporary clone that is deleted afterwards.
    with tempfile.TemporaryDirectory(prefix="ab-merge-") as tmp:
        path = os.path.join(tmp, "repo")
        # Clone with the token, then strip it straight back out of .git/config
        # (mirrors fix_engine's hygiene); re-applied only for the push below.
        url = repo.clone_url.replace("https://", "https://%s@" % token)
        repo_git = git.Repo.clone_from(url, path)
        repo_git.remotes.origin.set_url(repo.clone_url)
        # Set a commit identity for the merge commit.
        with repo_git.config_writer() as cw:
            cw.set_value("user", "name", "AppBuilder")
            cw.set_value("user", "email",
                         "223556219+Copilot@users.noreply.github.com")
        # Check out the PR branch and merge the base into it.
        repo_git.git.checkout(head_ref)
        ok, detail = _resolve_tree_conflicts(repo_git, "origin/%s" % base_ref)
        # A real conflict: give up and report why.
        if not ok:
            return False, detail
        # Push the updated branch back using a temporarily authenticated remote.
        from fix_engine import _authenticated_remote, push_or_raise
        with _authenticated_remote(repo_git.remotes.origin, repo.clone_url, token):
            push_or_raise(repo_git.remotes.origin, "HEAD:%s" % head_ref)
    return True, detail


def _github_token():
    """Return the configured GitHub token (config.json first, then the environment)."""
    # Imported lazily to avoid a circular import at module load.
    from config_store import load_config
    # Read config, then fall back to the GITHUB_TOKEN environment variable.
    cfg = load_config() or {}
    return cfg.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")


def approve_pr(gh, repo_name, number, *, actor="human"):
    """Applies the ab-approved label + posts an approval comment.
    Does NOT itself update state["pr_reviews"] — the caller does that
    (routes.py calls mark_pr_approved; the auto path calls it too, which is
    what makes merge_pr's approval guard genuinely pass rather than being
    bypassed). Returns (repo, pr) PyGithub objects for the caller's own
    follow-up (e.g. applying further labels)."""
    # Load the repo and PR.
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(number)
    # Make sure the approval label exists, then apply it (failure is only logged).
    _ensure_label(repo, _APPROVE_LABEL)
    try:
        pr.add_to_labels(_APPROVE_LABEL)
    except Exception as e:
        logger.warning(f"pr_actions: could not label {repo_name}#{number} approved: {e}")
    # Pick the comment wording for a human approval or an automatic one.
    comment = (
        "✅ **Approved** via AppBuilder (human review). Cleared to merge/pull."
        if actor == "human" else
        "🤖 **Auto-Approved** via AppBuilder Feature Auto-Drive — both review panels "
        "cleared the configured confidence threshold and the diff touched no "
        "configured boundary. Merging automatically."
    )
    # Post the approval comment (failure is only logged).
    try:
        pr.create_issue_comment(comment)
    except Exception as e:
        logger.warning(f"pr_actions: could not comment on {repo_name}#{number}: {e}")
    # Return the objects for the caller's follow-up.
    return repo, pr


def _delete_pr_branch(repo, pr):
    """Delete the merged PR's head branch so AppBuilder doesn't leave stale
    ``bug/*``/``ai-feature/*`` branches piling up after every merge (GitHub's own
    "delete branch on merge" only fires for the web UI, not the API merge
    ``merge_pr`` performs). Only same-repo branches are removed — a fork head
    lives in someone else's repo and isn't ours to delete — and any failure is
    logged, never raised, so branch cleanup can't turn a successful merge into
    an error.

    What may be deleted is decided by ``branch_policy.may_delete``: an
    ALLOWLIST of AppBuilder's own throwaway branches, never a shared one. This
    used to guard only ``repo.default_branch``, which left ``dev`` and ``qa``
    deletable — and a low-confidence fix opens its PR with ``dev`` as the head
    branch, so merging it deleted ``dev`` outright."""
    # Find the PR's head branch and the repo it lives in.
    head = getattr(pr, "head", None)
    head_repo = getattr(head, "repo", None)
    # Missing data or a fork branch: not ours to delete.
    if not head or not head_repo or head_repo.full_name != repo.full_name:
        return
    # Ask the branch policy whether this branch may be deleted.
    ref = head.ref
    ok, why = may_delete(ref, load_config() or {},
                         repo_default_branch=getattr(repo, "default_branch", None))
    # Protected or shared branch: keep it.
    if not ok:
        logger.info(f"pr_actions: {repo.full_name} kept branch {ref} — {why}")
        return
    # Delete the branch; "already gone" or "protected" errors are just logged.
    try:
        repo.get_git_ref("heads/%s" % ref).delete()
        logger.info(f"pr_actions: {repo.full_name} deleted merged branch {ref}")
    except GithubException as e:
        logger.info(f"pr_actions: {repo.full_name} could not delete branch {ref} "
                    f"(already gone or protected): {e}")


def merge_pr(gh, repo_name, number):
    """Returns (status_code, response_dict) — the exact shape routes.py's
    prior _do_merge closure returned, just relocated so both the human route
    and the auto path share one implementation instead of routes.py owning
    the only copy. Guards, in order: already-merged (idempotent) ->
    closed-without-merge (reconcile, don't attempt) -> must be approved
    first (the ONE guard that makes unattended merging safe: see
    pr_review._maybe_auto_merge, which satisfies this by calling approve_pr
    + mark_pr_approved first, not by skipping this check) -> merge."""
    # Load the repo and PR.
    repo = gh.get_repo(repo_name)
    pr = repo.get_pull(number)
    # Already merged: record it and report success (idempotent).
    if pr.merged:
        update_pr_review(repo_name, number, merged=True)
        return 200, {"status": "success", "message": "already merged"}
    # Closed without merging: record it and do not try to merge.
    if (pr.state or "").lower() == "closed":
        update_pr_review(repo_name, number, closed=True)
        return 409, {"status": "error", "closed": True,
            "message": f"PR #{number} is closed on GitHub (not merged) — its changes may have "
                       f"merged under another PR. Marked CLOSED."}
    # Look up AppBuilder's own review record for this PR.
    rec = (state.get("pr_reviews") or {}).get("%s#%s" % (repo_name, number)) or {}
    # Must be approved first; this is the guard that keeps unattended merges safe.
    if not rec.get("approved"):
        return 409, {"status": "error", "needs_approval": True,
            "message": f"PR #{number} must be Approved before it can be merged."}
    # Try the merge.
    try:
        res = pr.merge()  # default merge commit; raises if not mergeable
    # The merge failed.
    except GithubException as e:
        # Another AppBuilder thread, or a retried request, may have merged it
        # in the meantime: that is a success, not a refusal.
        try:
            pr.update()
        except GithubException:
            pass
        if pr.merged:
            update_pr_review(repo_name, number, merged=True, merge_blocked_reason=None)
            logger.info(f"pr_actions: {repo_name} #{number} already merged by a concurrent attempt")
            return 200, {"status": "success", "message": "already merged"}
        # A check still running or a merge already in flight clears by itself:
        # retry on the next poll instead of recording a refusal.
        if _merge_is_transient(e):
            msg = f"PR #{number}: {_github_message(e)} -- retrying the merge on the next poll."
            logger.info("pr_actions: %s #%s — %s", repo_name, number, msg)
            return 409, {"status": "error", "blocked": True, "retryable": True,
                         "deferred": True, "message": msg}
        # Not a conflict: check whether it is a policy refusal; otherwise re-raise.
        if not _is_merge_conflict(e):
            refusal = _merge_refusal(e)
            if refusal is None:
                raise
            # Unpack the refusal's status code and reason.
            status, why = refusal
            # Before recording this as "only a human can clear it", check
            # whether it is the one refusal we CAN clear: a required check that
            # never reported because its workflow run is parked. Releasing the
            # run lets the check post and the next poll merge normally.
            if _expects_missing_check(why):
                # Try to release any parked workflow runs for the PR's head commit.
                released = _release_parked_checks(
                    repo_name, pr.head.sha, _github_token())
                # Something was released: the next poll can retry the merge.
                if released:
                    msg = (f"PR #{number}: released {released} parked workflow "
                           f"run(s); the required check can now report — "
                           f"retrying the merge on the next poll.")
                    logger.info("pr_actions: %s #%s — %s", repo_name, number, msg)
                    update_pr_review(repo_name, number, merge_blocked_reason=None)
                    return 409, {"status": "error", "blocked": True,
                                 "retryable": True, "released_checks": int(released),
                                 "message": msg}
                # Nothing released: say WHICH of the four zero-states this was,
                # so "no run is parked at all" is not recorded as if it were a
                # failed approval or an unreachable API.
                detail = {
                    "no-token": "no GitHub token is configured, so a parked "
                                "workflow run could not be released",
                    "lookup-failed": "the parked-run lookup never succeeded "
                                     "(network error, or a token without "
                                     "`actions` access) -- whether a run is "
                                     "parked is UNKNOWN",
                    "approve-failed": "a parked workflow run was found but "
                                      "GitHub refused to approve it",
                    "none-parked": "no workflow run is parked for this head, so "
                                   "the required check is missing for some "
                                   "other reason (e.g. the workflow never "
                                   "triggered)",
                }.get(getattr(released, "outcome", ""), "")
                # Append the explanation to the refusal reason.
                if detail:
                    why = "%s -- %s" % (why, detail)
            # A required check missing because GitHub Actions never started the
            # run is an outage, not a policy problem: retry later instead of
            # recording a block only a human can clear.
            if _expects_missing_check(why):
                import gh_actions_health
                token = _github_token()
                stuck = gh_actions_health.infra_failed_runs(repo_name, pr.head.sha, token)
                if stuck:
                    gh_actions_health.note_infra_failure(len(stuck))
                    down, _ = gh_actions_health.outage(load_config())
                    rerun = 0 if down else gh_actions_health.rerun_infra_failed(
                        repo_name, pr.head.sha, token)
                    msg = (f"PR #{number}: required checks never started (GitHub Actions "
                           f"outage); " + (f"re-ran {rerun} run(s)" if rerun else "backing off")
                           + " -- retrying the merge later.")
                    logger.info("pr_actions: %s #%s — %s", repo_name, number, msg)
                    update_pr_review(repo_name, number, merge_blocked_reason=None)
                    return 409, {"status": "error", "blocked": True, "retryable": True,
                                 "released_checks": 0, "infra": True, "message": msg}
            # Record the refusal once on the review record and return a non-retryable error.
            logger.warning("pr_actions: %s #%s merge refused by GitHub — %s",
                           repo_name, number, why)
            update_pr_review(repo_name, number, merge_blocked_reason=why)
            return status, {"status": "error", "blocked": True, "retryable": False,
                            "message": f"PR #{number}: {why}"}
        # The base advanced under a stale branch. Try the same recovery a human
        # would: merge the base into the branch, auto-resolve only cosmetic
        # (VERSION) conflicts, push, then retry the merge once. Any real code
        # conflict aborts and is handed back to a human.
        logger.info(f"pr_actions: {repo_name} #{number} has merge conflicts — "
                    f"attempting auto-resolve")
        # Conflict resolution needs a token to clone and push.
        token = _github_token()
        if not token:
            return 409, {"status": "error", "conflict": True,
                "message": f"PR #{number} has merge conflicts and no GitHub token "
                           f"is configured to auto-resolve them."}
        # Try the merge-base-into-branch recovery.
        ok, detail = _auto_resolve_pr_conflicts(repo, pr, token)
        # A real conflict: hand it back to a human.
        if not ok:
            return 409, {"status": "error", "conflict": True,
                "message": f"PR #{number} has merge conflicts AppBuilder could not "
                           f"auto-resolve ({detail}). Resolve them manually, then merge."}
        # Branch updated: reload the PR and wait for GitHub to recompute mergeability.
        logger.info(f"pr_actions: {repo_name} #{number} branch updated ({detail}); "
                    f"re-checking mergeability")
        pr = repo.get_pull(number)
        _wait_mergeable(pr)
        res = pr.merge()  # retry once, now that the branch carries the base
        logger.info(f"pr_actions: {repo_name} #{number} merged after auto-resolving conflicts")
    # Merged: record it, clean up the branch and report success.
    update_pr_review(repo_name, number, merged=True, merge_blocked_reason=None)
    _delete_pr_branch(repo, pr)
    logger.info(f"pr_actions: {repo_name} #{number} MERGED")
    return 200, {"status": "success", "merged": bool(getattr(res, "merged", True)),
                "message": getattr(res, "message", "merged")}
