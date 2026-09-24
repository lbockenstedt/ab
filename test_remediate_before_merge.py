#!/usr/bin/env python3
"""Self-test: AppBuilder improves a PR BEFORE it merges it.

AppBuilder's job on a PR is to drive the skeptical panel's approval score as
close to 100% as it can get it, and only then merge. Two separate defects
defeated that:

1. ``_review_one`` called ``_maybe_auto_merge`` BEFORE ``_maybe_auto_remediate``.
   The merge threshold (``feature_automerge_min_confidence``, 0.90 in prod) is
   LOWER than the remediation target (``pr_remediate_target_score``, 0.95), so
   any PR good enough to merge was merged on the spot and the remediation pass
   that would have carried it closer to 100% never ran.
2. ``_maybe_auto_merge`` asked only whether the PR was good ENOUGH. Even reached
   on its own (the Reprocess/Fix paths call it directly), it would merge over
   the top of a panel critique AppBuilder still intended to act on.

pr_review imports the live app, so the functions are extracted by source via
ast and exec'd against fakes (same technique as test_automerge_refusal_reason).
"""
import ast
import re
import sys
import threading  # noqa: F401 — parity with the sibling harnesses
import types

import pytest

_SRC = open("pr_review.py", encoding="utf-8").read()
_TREE = ast.parse(_SRC)

_WANT_ASSIGNS = {"PR_REVIEW_MARKER", "_SUMMARY_HEADER", "_FEATURE_DRIVE_MARKER_RE",
                 "_AUTOMERGE_NOTE_MARKER", "_IDEMPOTENT_MERGED_REASON"}
_WANT_FUNCS = {"_automerge_note", "_update_automerge_comment", "_find_marker_comment",
               "_maybe_auto_merge", "_extract_summary"}


def _extract(funcs, assigns):
    segs = []
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            segs.append(ast.get_source_segment(_SRC, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in assigns for t in node.targets):
            segs.append(ast.get_source_segment(_SRC, node))
    return "\n\n".join(segs)


def _func(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("pr_review.%s not found" % name)


def _call_lines(node, name):
    """Line numbers of every call to `name` inside `node`."""
    return [n.lineno for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


# --------------------------------------------------------------------------
# 1. Ordering inside _review_one (static — the call order IS the contract)
# --------------------------------------------------------------------------

def test_review_one_remediates_before_it_merges():
    fn = _func("_review_one")
    remediate = _call_lines(fn, "_maybe_auto_remediate")
    merge = _call_lines(fn, "_maybe_auto_merge")
    assert remediate, "_review_one no longer attempts remediation at all"
    assert merge, "_review_one no longer attempts an auto-merge at all"
    assert max(remediate) < min(merge), (
        "_review_one calls _maybe_auto_merge (line %d) before _maybe_auto_remediate "
        "(line %d) — a PR that clears the LOWER merge threshold would be merged "
        "before remediation could raise it toward the approval target"
        % (min(merge), max(remediate)))


def test_review_one_skips_the_merge_when_a_fix_was_pushed():
    """A pushed fix moves the head: every finding/verdict/mergeability answer
    computed earlier in _review_one describes the PREVIOUS commit, so merging on
    that stale record would merge code no panel has seen."""
    fn = _func("_review_one")
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if not _call_lines(node.test, "_maybe_auto_remediate"):
            continue
        if any(isinstance(n, ast.Return) for n in node.body):
            guarded = True
    assert guarded, (
        "the _maybe_auto_remediate(...) call in _review_one is not used as a branch "
        "that returns early — the auto-merge below it would run against a stale head")


def test_maybe_auto_remediate_reports_whether_it_pushed():
    fn = _func("_maybe_auto_remediate")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    assert returns, "_maybe_auto_remediate returns nothing — _review_one cannot tell if the head moved"


# --------------------------------------------------------------------------
# 2. _maybe_auto_merge consults remediation_pending (behavioural)
# --------------------------------------------------------------------------

class _Logger:
    def __init__(self):
        self.calls = []

    def _rec(self, level):
        return lambda msg, *a: self.calls.append((level, msg % a if a else msg))

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return self._rec(name)
        raise AttributeError(name)

    def at(self, level):
        return [m for lv, m in self.calls if lv == level]


class _Comment:
    def __init__(self, body):
        self.body, self.edits = body, []

    def edit(self, body):
        self.edits.append(body)
        self.body = body


class _PR:
    number = 7
    body = ""
    state = "open"
    draft = False
    mergeable = True

    class base:
        ref = "dev"

    def __init__(self, comment):
        self.comment = comment

    def get_files(self):
        return []

    def get_issue_comments(self):
        return [self.comment]


class _Repo:
    full_name = "owner/repo"


class _Env:
    """One exec'd _maybe_auto_merge namespace with a stubbed pr_remediate."""

    def __init__(self, pending=(False, ""), raises=False):
        self.logger = _Logger()
        self.state = {"pr_reviews": {"owner/repo#7": {"panel_verdict": "Approve",
                                                      "panel_confidence": 0.91}}}
        self.updates = []
        self.merges = []
        self.seen = []
        self.comment = _Comment("<!-- ab-pr-review -->\n## 🤖 AppBuilder PR pre-review")
        self.pr = _PR(self.comment)

        def _pending(rec, config):
            self.seen.append((rec, config))
            if raises:
                raise RuntimeError("boom")
            return pending

        self._stub = types.ModuleType("pr_remediate")
        self._stub.remediation_pending = _pending

        def update_pr_review(repo, number, **fields):
            self.updates.append(fields)
            self.state["pr_reviews"]["%s#%s" % (repo, number)].update(fields)
            return True

        class _FA:
            @staticmethod
            def files_from_pr_files(files):
                return []

        def merge_pr(*a, **k):
            self.merges.append((a, k))
            return (200, {"status": "success"})

        self.ns = {"re": re, "logger": self.logger, "state": self.state,
                   "update_pr_review": update_pr_review, "feature_allowlist": _FA,
                   "_automerge_decision": lambda *a, **k: (True, "panel approved"),
                   "approve_pr": lambda *a, **k: None,
                   "mark_pr_approved": lambda *a, **k: None,
                   "merge_pr": merge_pr}
        exec(_extract(_WANT_FUNCS, _WANT_ASSIGNS), self.ns)

    def run(self):
        saved = sys.modules.get("pr_remediate")
        sys.modules["pr_remediate"] = self._stub
        try:
            self.ns["_maybe_auto_merge"](None, _Repo, self.pr, {})
        finally:
            if saved is None:
                sys.modules.pop("pr_remediate", None)
            else:
                sys.modules["pr_remediate"] = saved

    @property
    def rec(self):
        return self.state["pr_reviews"]["owner/repo#7"]


def test_merge_is_held_while_remediation_is_still_pending():
    e = _Env(pending=(True, "panel score 0.91 is below the 0.95 target"))
    e.run()
    assert e.merges == [], "merged a PR that AppBuilder was still going to improve"
    assert e.seen, "_maybe_auto_merge never consulted pr_remediate.remediation_pending"


def test_held_merge_records_a_human_readable_reason():
    e = _Env(pending=(True, "panel score 0.91 is below the 0.95 target"))
    e.run()
    reason = e.rec.get("auto_merge_blocked_reason") or ""
    assert "0.95 target" in reason, reason
    assert reason.strip(), "the operator is given no reason the merge was held"


def test_merge_proceeds_once_nothing_is_pending():
    e = _Env(pending=(False, "panel approved at or above the target"))
    e.run()
    assert e.merges, "a fully approved PR was not merged"


def test_a_broken_remediation_check_never_blocks_a_merge():
    """Fail OPEN: remediation_pending is an optimisation, not a safety gate.
    The real safety gates live in _automerge_decision."""
    e = _Env(raises=True)
    e.run()
    assert e.merges, "an exception inside remediation_pending wedged the merge gate"
    assert e.logger.at("warning"), "the skipped check was not surfaced to the operator"


# --------------------------------------------------------------------------
# 3. The gate can only ever DELAY a merge (bounded by the attempt ceiling)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("attempts,expected", [(0, True), (2, True), (3, False), (7, False)])
def test_pending_gate_is_bounded_by_the_attempt_ceiling(attempts, expected):
    from pr_remediate import remediation_pending
    rec = {"panel_verdict": "Deny", "panel_confidence": 0.4,
           "remediation_attempts": attempts}
    pending, reason = remediation_pending(rec, {"pr_auto_remediate_max_attempts": 3})
    assert pending is expected, reason
