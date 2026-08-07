"""
check_test_regressions.py — actually RUNS the repo's test suite for a PR's
head branch, and compares the failure set against its base branch, flagging
only NEW failures. Every other Tier-1 check (parity, secrets, undefined-
names, unattended-mutation) and the LLM skeptical panel are pure static
analysis — none of them execute a single line of the PR's code.

Motivating gap: an eagerly-constructed asyncio.Semaphore() in lm#151 silently
hijacked the process-wide default event loop and broke ~18 UNRELATED tests
elsewhere in the same repo's suite. No amount of LLM review — tools or no
tools, diff-only or full-file — catches that: it's pure RUNTIME behavior,
only visible by actually executing the code and diffing the failure set
against baseline. It was found in THIS session only by running pytest twice
(with and without the change) and comparing output.

Pre-existing failures are deliberately filtered out (base-vs-head diff, not
a bare pass/fail) — this repo carries 9-18 baseline-failing tests unrelated
to any single PR; treating those as review-blocking noise for every PR would
make this check worse than useless.

Safety / cost:
  - Runs INSIDE run_sandboxed_command's Docker sandbox (fix_engine.py) — the
    SAME fail-closed mechanism verify_fix/fix_one_pr already use for
    untrusted repo code. No Docker => this check reports "skipped", never
    runs anything unsandboxed.
  - The base branch's failure set is cached per (repo, base_sha) for
    _BASE_CACHE_TTL_S so N open PRs against the same base don't each
    independently re-run the whole base suite.
  - Bounded by run_sandboxed_command's existing per-command timeout (head and
    base each get their own budget) — a hung/slow suite degrades to
    'skipped', never blocks the rest of the review.
  - OFF by default (config key ``pr_test_regression_enabled``) — this is
    real code execution against PR-authored content, a materially different
    risk/cost class from the other (pure static) Tier-1 checks, so it's
    opt-in per install rather than firing on every monitored repo from day
    one. Also respects the existing ``qa_enabled`` flag verify_fix uses.
"""
import logging
import os
import re
import tempfile
import time

logger = logging.getLogger(__name__)

_BASE_CACHE_TTL_S = 3600  # re-run the base suite at most once an hour per (repo, sha)
_FAILED_LINE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)

# {(repo_full_name, base_sha): (fetched_at, failure_set)}
_base_cache = {}


def _detect_test_cmd(path):
    """Same heuristic verify_fix uses (Priority 3 local-detection path) —
    kept as an isolated copy here rather than importing/refactoring
    verify_fix, so this module has no import-time dependency on fix_engine's
    full LLM/GitHub stack (this needs to be safely importable from a
    lightweight scan context)."""
    try:
        files = os.listdir(path)
    except OSError:
        return None
    if "package.json" in files:
        return "npm test"
    if "requirements.txt" in files or "pyproject.toml" in files:
        return "python3 -m pytest -q"
    if "go.mod" in files:
        return "go test ./..."
    if "Makefile" in files:
        return "make test"
    return None


def _clone_and_checkout(clone_url, token, ref, dest):
    import git
    url = clone_url.replace("https://", "https://%s@" % token) if token else clone_url
    repo_git = git.Repo.clone_from(url, dest)
    if token:
        repo_git.remotes.origin.set_url(clone_url)  # strip the token back out immediately
    repo_git.git.checkout(ref)
    return repo_git


def _run_suite(path):
    """Returns (ok, failure_set, note). ok=False + failure_set=None means the
    run itself couldn't be attempted/completed (no test framework detected,
    Docker unavailable, timeout, ...) — the caller must treat that as
    'skip', never as 'zero failures'."""
    from fix_engine import run_sandboxed_command  # local import — see module docstring

    cmd = _detect_test_cmd(path)
    if not cmd:
        return False, None, "no recognized test framework in this repo"
    result = run_sandboxed_command(cmd, path)
    if result.returncode == 127:
        return False, None, "sandbox unavailable (%s)" % (result.stderr or "").strip()[:200]
    failures = set(_FAILED_LINE_RE.findall((result.stdout or "") + "\n" + (result.stderr or "")))
    return True, failures, None


def _base_failures(repo_full_name, clone_url, token, base_sha):
    cached = _base_cache.get((repo_full_name, base_sha))
    if cached and (time.time() - cached[0]) < _BASE_CACHE_TTL_S:
        return cached[1]
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, "base")
        try:
            _clone_and_checkout(clone_url, token, base_sha, dest)
        except Exception as e:  # noqa: BLE001
            logger.info("check_test_regressions: base clone failed for %s@%s: %s",
                       repo_full_name, base_sha, e)
            return None
        ok, failures, note = _run_suite(dest)
    if not ok:
        logger.info("check_test_regressions: base run skipped for %s@%s: %s",
                   repo_full_name, base_sha, note)
        return None
    _base_cache[(repo_full_name, base_sha)] = (time.time(), failures)
    return failures


def check_test_regressions(repo, pr, config, token):
    """repo/pr: PyGithub Repository/PullRequest (same objects _review_one
    already has). token: GitHub token for the authenticated clone URL.
    Returns a list of {level, title, detail} findings — level='error' for a
    genuine new failure (unlike the other, advisory-only Tier-1 checks: a
    reproducible test regression is not a heuristic guess, it's a fact).
    Best-effort throughout: any failure to run either side degrades to no
    finding (never a false positive from an inconclusive run), and this
    never raises out to the caller."""
    if not config.get("qa_enabled", True):
        return []
    if not config.get("pr_test_regression_enabled", False):
        return []
    try:
        base_sha = pr.base.sha
        base_failures = _base_failures(repo.full_name, repo.clone_url, token, base_sha)
        if base_failures is None:
            return []  # couldn't establish a baseline — never flag without one
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "head")
            try:
                _clone_and_checkout(repo.clone_url, token, pr.head.sha, dest)
            except Exception as e:  # noqa: BLE001
                logger.info("check_test_regressions: head clone failed for %s#%s: %s",
                           repo.full_name, pr.number, e)
                return []
            ok, head_failures, note = _run_suite(dest)
        if not ok:
            logger.info("check_test_regressions: head run skipped for %s#%s: %s",
                       repo.full_name, pr.number, note)
            return []
        new_failures = sorted(head_failures - base_failures)
        if not new_failures:
            return []
        shown = new_failures[:15]
        more = len(new_failures) - len(shown)
        return [{
            "level": "error",
            "title": "%d test(s) newly failing on this PR's branch (not failing on base)" % len(new_failures),
            "detail": (
                "Ran this repo's test suite on both this PR's head (`%s`) and its "
                "base (`%s`) inside the sandbox and diffed the failure sets — these "
                "tests fail on head but NOT on base, so this PR is the cause:\n\n"
                + "\n".join("- `%s`" % t for t in shown)
                + ("\n- …and %d more" % more if more > 0 else "")
                + "\n\nPre-existing failures unrelated to this PR are excluded (this "
                  "repo has some independent of any single change)."
            ) % (pr.head.sha[:8], base_sha[:8]),
        }]
    except Exception as e:  # noqa: BLE001 — never break the rest of the review
        logger.warning("check_test_regressions: unexpected error for %s#%s: %s",
                       repo.full_name, pr.number, e)
        return []
