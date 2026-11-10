#!/usr/bin/env python3
"""Self-test for the dismissed-issue rolling dedup window (Gap 1).

Run:  python3 ab/test_dedup_dismissed_rolling_window.py

github_ops.py cannot be imported directly (it pulls in the app's circular
import chain), so this extracts the SOURCE of find_global_duplicate_issue via
ast and execs it with a fake PyGithub-shaped repo/issue set.

Regression guard: a ab-dismissed issue's label promises "will not be
reopened" — an unconditional claim — but the dedup search used to anchor its
60-day recurrence window to the issue's ORIGINAL closed_at, so a dismissal
older than 60 days silently dropped out of the search regardless of how often
it kept recurring. Dismissed issues now anchor to updated_at instead — a
ROLLING window renewed by the "still recurring" comment create_automated_issue
posts on each suppressed match (see test_gap1_dismiss_suppress_comment in
test_gap2... no — see the suppression test in this same session's github_ops
change) — so a weekly recurrence for a year stays suppressed indefinitely,
while a dismissal that goes quiet for the full window ages out and its next
occurrence surfaces normally, exactly like before this change. Non-dismissed
closed issues (the ORIGINAL "reopen a recently-closed fix" mechanism) must
keep using closed_at, unchanged.
"""
import ast
from datetime import datetime, timedelta, timezone


def _load_fn():
    src = open("github_ops.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "find_global_duplicate_issue":
            seg = ast.get_source_segment(src, node)
    assert seg, "find_global_duplicate_issue not found"

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    # Isolate the WINDOW logic under test: make every candidate "match" once it
    # clears the window filter, so pass/fail here reflects only the window,
    # not the title/body similarity heuristic (covered separately by dedup.py).
    def _is_duplicate_match(nt, nb, et, eb):
        return True

    def load_config():
        return {}

    ns = {
        "logger": _NoLog(), "datetime": datetime, "timezone": timezone,
        "_is_duplicate_match": _is_duplicate_match, "load_config": load_config,
        "DEDUP_CLOSED_WINDOW_DAYS": 60, "GLOBAL_FALLBACK_JACCARD": 0.8,
        "_normalize_for_dedup": lambda t: (t or "").lower(),
        "_jaccard": lambda a, b: 1.0 if a and b else 0.0,
        "_body_signal_match": lambda nb, eb: True,
    }
    exec(seg, ns)
    return ns["find_global_duplicate_issue"]


class _Label:
    def __init__(self, name):
        self.name = name


class _Issue:
    def __init__(self, number, state, labels, closed_at, updated_at, title="t", body="x y z w q"):
        self.number = number
        self.state = state
        self.labels = [_Label(n) for n in labels]
        self.closed_at = closed_at
        self.updated_at = updated_at
        self.title = title
        self.body = body


class _Repo:
    def __init__(self, issues):
        self._issues = issues

    def get_issues(self, state='all', sort='updated', direction='desc'):
        return list(self._issues)


class _GhCurrent:
    def __init__(self, repo):
        self._repo = repo

    def get_repo(self, name):
        return self._repo


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True
    find_dup = _load_fn()
    now = datetime.now(timezone.utc)

    # 1. Dismissed issue: closed 200 days ago (WAY past the window on closed_at
    #    alone), but updated 5 days ago (a recent recurrence comment). Must
    #    still be found — rolling window anchored to updated_at.
    old_closed_recent_activity = _Issue(
        101, "closed", ["ab-dismissed"],
        closed_at=now - timedelta(days=200), updated_at=now - timedelta(days=5))
    repo1 = _Repo([old_closed_recent_activity])
    gh1 = _GhCurrent(repo1)
    issue, repo_name, was_closed = find_dup(gh1, ["r/x"], {"title": "t", "body": "x y z w q", "repo": "r/x"})
    ok &= _check("dismissed issue with RECENT activity found despite old closed_at",
                 issue is not None and issue.number == 101)

    # 2. Dismissed issue: closed 200 days ago AND no activity since (updated_at
    #    also 200 days ago) — must age out, same as before this change.
    old_closed_no_activity = _Issue(
        102, "closed", ["ab-dismissed"],
        closed_at=now - timedelta(days=200), updated_at=now - timedelta(days=200))
    repo2 = _Repo([old_closed_no_activity])
    gh2 = _GhCurrent(repo2)
    issue2, _, _ = find_dup(gh2, ["r/x"], {"title": "t", "body": "x y z w q", "repo": "r/x"})
    ok &= _check("dismissed issue with NO recent activity still ages out after the window",
                 issue2 is None)

    # 3. Non-dismissed closed issue (the original reopen-a-fix mechanism) closed
    #    70 days ago must STILL use closed_at, unaffected by this change — even
    #    if something else bumped updated_at recently (e.g. an unrelated label
    #    edit), it should not be resurrected as a match.
    normal_closed_stale = _Issue(
        103, "closed", [],  # no ab-dismissed label
        closed_at=now - timedelta(days=70), updated_at=now - timedelta(days=1))
    repo3 = _Repo([normal_closed_stale])
    gh3 = _GhCurrent(repo3)
    issue3, _, _ = find_dup(gh3, ["r/x"], {"title": "t", "body": "x y z w q", "repo": "r/x"})
    ok &= _check("non-dismissed closed issue still anchors to closed_at (unchanged behavior)",
                 issue3 is None)

    # 4. Non-dismissed closed issue within the window (30 days) is still found —
    #    the pre-existing "reopen a recent fix" path must be untouched.
    normal_closed_within_window = _Issue(
        104, "closed", [],
        closed_at=now - timedelta(days=30), updated_at=now - timedelta(days=30))
    repo4 = _Repo([normal_closed_within_window])
    gh4 = _GhCurrent(repo4)
    issue4, _, was_closed4 = find_dup(gh4, ["r/x"], {"title": "t", "body": "x y z w q", "repo": "r/x"})
    ok &= _check("non-dismissed closed issue WITHIN the window is still found (reopen path intact)",
                 issue4 is not None and issue4.number == 104 and was_closed4 is True)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running dismissed-issue rolling-window self-test...")
    import sys
    sys.exit(main())
