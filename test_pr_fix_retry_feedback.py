"""fix_one_pr must retry with accurate feedback, like the issue-fix loop does.

fix_one_pr used to make ONE blind attempt. A parse failure, a skeptical-panel
rejection or a verification failure each returned immediately, and the reason —
which fix_engine already computes precisely (parse_and_apply.last_reason /
.last_failures, and the panel's critique) — was flattened into a UI string and
discarded. fix_engine's ISSUE loop has always fed that reason back into
apply_ai_fix(error_context=...); the PR path never did, so every "Fix" click
regenerated the same fix from identical inputs and the panel rejected it again
for the same reason.

pr_review.py can't be imported standalone (circular imports + optional deps), so
these use the codebase's AST-extraction pattern, with fix_one_pr's function-local
imports (git / github / fix_engine) satisfied via sys.modules fakes.
"""
import ast
import sys
import types

import pytest


def _extract(path, funcs):
    src = open(path, encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            segs.append(ast.get_source_segment(src, node))
    return "\n\n".join(segs)


class _Logger:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return lambda msg, *a: self.calls.append(msg % a if a else msg)
        raise AttributeError(name)


class _Git:
    """Stands in for repo_git.git — records reset/clean so we can prove each
    retry restarts from the PR head rather than a half-applied fix."""

    def __init__(self, rec):
        self.rec = rec

    def checkout(self, *a, **kw):
        self.rec.append(("checkout",) + a)

    def reset(self, *a):
        self.rec.append(("reset",) + a)

    def clean(self, *a):
        self.rec.append(("clean",) + a)

    def add(self, **kw):
        self.rec.append(("add",))


class _RepoGit:
    def __init__(self, rec):
        self.git = _Git(rec)
        self.rec = rec
        self.remotes = types.SimpleNamespace(
            origin=types.SimpleNamespace(set_url=lambda u: None,
                                         push=lambda spec: rec.append(("push", spec))))
        self.index = types.SimpleNamespace(
            commit=lambda msg: rec.append(("commit", msg)))


class _PR:
    def __init__(self):
        self.number, self.title, self.body = 7, "t", "b"
        self.merged, self.state = False, "open"
        self.head = types.SimpleNamespace(sha="deadbeef", ref="feature/x")
        self.comments = []

    def get_files(self):
        return [types.SimpleNamespace(filename="a.py")]

    def create_issue_comment(self, body):
        self.comments.append(body)


class _Repo:
    def __init__(self, pr):
        self._pr = pr
        self.clone_url = "https://github.com/o/r.git"

    def get_pull(self, n):
        return self._pr


class Harness:
    """Drives fix_one_pr with scripted per-attempt outcomes."""

    def __init__(self, parse_results, reviews, verify=None, qa=False, max_attempts=3):
        self.parse_results = list(parse_results)
        self.reviews = list(reviews)
        self.verify = list(verify or [])
        self.rec = []
        self.pr = _PR()
        self.error_contexts = []   # the feedback handed to each apply_ai_fix call
        self.log = _Logger()
        self.qa = qa
        self.max_attempts = max_attempts

    # -- fix_engine fakes -------------------------------------------------
    def apply_ai_fix(self, repo_path, issue_body, error_context=None, **kw):
        self.error_contexts.append(error_context)
        return "<<fix-%d>>" % len(self.error_contexts)

    def _make_parse_and_apply(self):
        """A plain function, not a bound method: the production code reads
        parse_and_apply.last_reason / .last_failures as FUNCTION attributes, and
        those cannot be set on a bound method."""
        harness = self

        def parse_and_apply(fix_code, path):
            i = len(harness.error_contexts) - 1
            res = harness.parse_results[min(i, len(harness.parse_results) - 1)]
            if isinstance(res, tuple):
                ok, reason, misses = res
            else:
                ok, reason, misses = res, None, []
            parse_and_apply.last_reason = reason
            parse_and_apply.last_failures = misses
            if not ok:
                return False, None, 0.0
            return True, {"a.py": "x"}, 0.9

        parse_and_apply.last_reason = None
        parse_and_apply.last_failures = []
        return parse_and_apply

    def review_fix(self, *a, **kw):
        i = len(self.error_contexts) - 1
        return self.reviews[min(i, len(self.reviews) - 1)]

    def verify_fix(self, *a, **kw):
        i = len(self.error_contexts) - 1
        return self.verify[min(i, len(self.verify) - 1)]

    def run(self):
        ns = self._namespace()
        return ns["fix_one_pr"]("o/r", 7, config={"qa_enabled": self.qa,
                                                  "pr_fix_max_attempts": self.max_attempts,
                                                  "GITHUB_TOKEN": "tok"})

    def _namespace(self):
        import contextlib
        import os as _os

        fix_engine = types.ModuleType("fix_engine")
        fix_engine._claim_issue = lambda i: True
        fix_engine._release_issue = lambda i: None
        fix_engine._authenticated_remote = lambda *a, **kw: contextlib.nullcontext()
        fix_engine.apply_ai_fix = self.apply_ai_fix
        fix_engine.parse_and_apply = self._make_parse_and_apply()
        fix_engine.review_fix = self.review_fix
        fix_engine.verify_fix = self.verify_fix
        fix_engine.prepare_environment = lambda p: None

        git_mod = types.ModuleType("git")
        git_mod.Repo = types.SimpleNamespace(
            clone_from=lambda url, path: _RepoGit(self.rec))

        gh_mod = types.ModuleType("github")
        gh_mod.Github = lambda tok: types.SimpleNamespace(
            get_repo=lambda name: _Repo(self.pr))

        for name, mod in (("fix_engine", fix_engine), ("git", git_mod),
                          ("github", gh_mod)):
            sys.modules[name] = mod

        ns = {
            "os": _os, "logger": self.log, "state": {},
            "load_config": lambda: {},
            "check_parity": lambda r, c: [{"level": "warning", "title": "t", "detail": "d"}],
            "_resolve_cross_repo_twins": lambda gh, f, since=None: f,
            "check_secrets": lambda f: [],
            "find_missing_tooltips_in_files": lambda f: [],
            "check_undefined_names": lambda r, f, s: [],
            "_review_one": lambda *a, **kw: None,
        }
        exec(_extract("pr_review.py", {"fix_one_pr"}), ns)
        return ns


APPROVE = {"verdict": "Approve", "confidence": 0.9, "critique": ""}


def _reject(critique="the fix ignores the null check"):
    return {"verdict": "Reject", "confidence": 0.2, "critique": critique}


# --------------------------------------------------------------------------
# Panel rejection must be fed back, not discarded
# --------------------------------------------------------------------------

def test_panel_rejection_is_fed_into_the_next_attempt():
    h = Harness(parse_results=[True], reviews=[_reject("missing bounds check"), APPROVE])
    ok, msg = h.run()
    assert ok is True
    assert len(h.error_contexts) == 2
    assert h.error_contexts[0] is None          # first attempt has no feedback
    assert "missing bounds check" in h.error_contexts[1]


def test_retry_after_rejection_resets_the_worktree():
    """Attempt 2 must start from the PR head, not from attempt 1's edits."""
    h = Harness(parse_results=[True], reviews=[_reject(), APPROVE])
    h.run()
    assert ("reset", "--hard") in h.rec
    assert ("clean", "-fd") in h.rec


def test_repeated_rejection_stops_at_max_attempts():
    h = Harness(parse_results=[True], reviews=[_reject()], max_attempts=3)
    ok, msg = h.run()
    assert ok is False
    assert len(h.error_contexts) == 3
    assert "rejected" in msg.lower()


def test_only_one_rejection_comment_is_posted():
    """Previously each click posted a comment; a retry loop must not spam the
    PR with one comment per internal attempt."""
    h = Harness(parse_results=[True], reviews=[_reject()], max_attempts=3)
    h.run()
    assert len(h.pr.comments) == 1


def test_nothing_is_pushed_when_every_attempt_is_rejected():
    h = Harness(parse_results=[True], reviews=[_reject()], max_attempts=2)
    h.run()
    assert not [r for r in h.rec if r[0] == "push"]


# --------------------------------------------------------------------------
# Parse failures must be classified, not flattened to "invalid JSON"
# --------------------------------------------------------------------------

def test_anchor_miss_feedback_tells_the_model_to_requote_the_file():
    """The old code said "AI generated invalid JSON format" even when the JSON
    was perfectly valid and only the search anchors missed — sending the model
    to fix the wrong thing."""
    h = Harness(parse_results=[(False, "edit_anchor_miss", ["a.py: 'def foo():'"]), True],
                reviews=[APPROVE])
    ok, _ = h.run()
    assert ok is True
    fb = h.error_contexts[1]
    assert "search" in fb and "not found" in fb
    assert "a.py: 'def foo():'" in fb
    assert "not valid JSON" not in fb


def test_anchor_miss_message_is_not_mislabelled_as_invalid_json():
    h = Harness(parse_results=[(False, "edit_anchor_miss", ["x"])], max_attempts=1,
                reviews=[APPROVE])
    ok, msg = h.run()
    assert ok is False
    assert "invalid JSON" not in msg


@pytest.mark.parametrize("reason,expected", [
    ("no_json", "No JSON object was found"),
    ("no_edits", "contained no edits"),
    ("empty", "empty response"),
    ("invalid_json", "not valid JSON"),
])
def test_each_parse_failure_gets_its_own_feedback(reason, expected):
    h = Harness(parse_results=[(False, reason, []), True], reviews=[APPROVE])
    h.run()
    assert expected in h.error_contexts[1]


def test_parse_failure_then_success_pushes():
    h = Harness(parse_results=[(False, "invalid_json", []), True], reviews=[APPROVE])
    ok, msg = h.run()
    assert ok is True
    assert [r for r in h.rec if r[0] == "push"]


# --------------------------------------------------------------------------
# Verification failures
# --------------------------------------------------------------------------

def test_verification_failure_is_fed_back():
    h = Harness(parse_results=[True], reviews=[APPROVE], qa=True,
                verify=[(False, "test_x failed"), (True, "")])
    ok, _ = h.run()
    assert ok is True
    assert "test_x failed" in h.error_contexts[1]


def test_verification_failure_exhausts_and_reports():
    h = Harness(parse_results=[True], reviews=[APPROVE], qa=True,
                verify=[(False, "boom")], max_attempts=2)
    ok, msg = h.run()
    assert ok is False
    assert "verification" in msg.lower()
    assert len(h.pr.comments) == 1


# --------------------------------------------------------------------------
# Loop bounds — this must not become the token-burning loop we just removed
# --------------------------------------------------------------------------

def test_first_attempt_success_costs_exactly_one_generation():
    h = Harness(parse_results=[True], reviews=[APPROVE])
    ok, _ = h.run()
    assert ok is True
    assert len(h.error_contexts) == 1
    assert ("reset", "--hard") not in h.rec


@pytest.mark.parametrize("configured,expected", [
    (1, 1), (2, 2), (3, 3), (5, 5),
    (99, 5),     # hard ceiling
    (0, 1), (-4, 1), (None, 1), ("junk", 3),
])
def test_attempt_count_is_bounded(configured, expected):
    h = Harness(parse_results=[True], reviews=[_reject()], max_attempts=configured)
    h.run()
    assert len(h.error_contexts) == expected


def test_panel_outage_does_not_burn_retries():
    """A queued/unavailable panel is an outage, not a bad fix — regenerating
    would spend tokens against the same outage."""
    h = Harness(parse_results=[True],
                reviews=[{"status": "queue_for_retry", "reason": "all reviewers down"}],
                max_attempts=3)
    ok, msg = h.run()
    assert ok is False
    assert len(h.error_contexts) == 1
    assert "could not run" in msg
