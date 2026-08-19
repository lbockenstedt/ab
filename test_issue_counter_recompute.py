#!/usr/bin/env python3
"""Self-test for the left-nav issue-counter badges staying in sync with
delete_issue / delete_all_issues / clear_history.

Run:  python3 ab/test_issue_counter_recompute.py

routes.py cannot be imported directly (it pulls in the full FastAPI app /
main.py's circular-import chain), so this extracts the SOURCE of the pure
route bodies via ast and execs them with stubbed I/O (_close_issue_on_github,
save_processed/load_processed, threading).

Regression guard: recompute_issue_counters() (app_state.py) is the single
source of truth for the badge counters and its own docstring says to
call it after "close, reopen, dismiss, delete" — but delete_issue,
clear_history, and delete_all_issues instead hand-decremented/reset only
success_count/failure_count, so closed_count/pending_verification_count/
non_actionable_count were NEVER updated by those three actions (and
delete_issue's narrow status check missed "resolved" entirely, since
_RESOLVED_STATUSES grew a 4th value after that check was written). The badge
only ever looked right again after some UNRELATED action (resolve/reopen)
happened to call recompute_issue_counters, or a source of truth rebuild
(restart) — not because these three actions ever fixed it themselves.

Extended (feature auto-drive, Phase 1): three more free-form statuses
(feature_flagged/feature_needs_info/feature_built, feature_drive.py) joined
the store, each with its OWN counter — widened from a 5-tuple to an 8-tuple
so a future half-added status (see fix_engine.py's still-uncounted
"awaiting_human" for a live example of exactly that failure mode) gets
caught here too.
"""
import ast
import asyncio
import threading


def _load_ns():
    app_state_src = open("app_state.py").read()
    routes_src = open("routes.py").read()
    app_tree = ast.parse(app_state_src)
    routes_tree = ast.parse(routes_src)

    segs = []
    for node in app_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "recompute_issue_counters":
            segs.append(ast.get_source_segment(app_state_src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_RESOLVED_STATUSES":
                    segs.append(ast.get_source_segment(app_state_src, node))
    for node in routes_tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
                "delete_issue", "clear_history", "delete_all_issues"):
            segs.append(ast.get_source_segment(routes_src, node))
        if isinstance(node, ast.FunctionDef) and node.name == "_close_issue_on_github":
            # Replaced by a stub below — real one hits the GitHub API.
            pass

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    _store = {}  # in-memory stand-in for the processed.json file

    def load_processed():
        return dict(_store)

    def save_processed(d):
        _store.clear()
        _store.update(d)

    def _close_issue_on_github(issue_id):
        return True, f"stub-closed {issue_id}"

    def _close_issue_on_github_with_retry(issue_id):
        # delete_issue backgrounds the real GitHub retry loop (routes.py) in a
        # thread; this test is about counter bookkeeping, not retry behavior,
        # so the stub just mirrors the single-attempt outcome synchronously.
        ok, msg = _close_issue_on_github(issue_id)
        state["dismiss_jobs"][issue_id] = {"status": "done" if ok else "error", "message": msg}

    state = {"processed": {}, "success_count": 0, "failure_count": 0,
              "closed_count": 0, "pending_verification_count": 0,
              "non_actionable_count": 0, "dismiss_jobs": {},
              "feature_flagged_count": 0, "feature_needs_info_count": 0,
              "feature_built_count": 0}

    class _FakeRequest:
        def __init__(self, payload):
            self._payload = payload
        async def json(self):
            return self._payload

    class _JSONResponse:
        def __init__(self, status_code=200, content=None):
            self.status_code = status_code
            self.content = content

    ns = {
        "logger": _NoLog(), "state": state,
        "load_processed": load_processed, "save_processed": save_processed,
        "_close_issue_on_github": _close_issue_on_github,
        "_close_issue_on_github_with_retry": _close_issue_on_github_with_retry,
        "threading": threading, "JSONResponse": _JSONResponse,
        "Request": _FakeRequest,
    }
    exec("\n\n".join(segs), ns)
    ns["_store"] = _store
    ns["_FakeRequest"] = _FakeRequest
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main():
    ok = True
    ns = _load_ns()
    state = ns["state"]
    recompute = ns["recompute_issue_counters"]

    # Seed one issue per status bucket (5 original + 3 feature auto-drive),
    # then recompute once to get the baseline right (mirrors what a real
    # running instance would have).
    seed = {
        "r/x:1": {"status": "resolved"},
        "r/x:2": {"status": "closed"},
        "r/x:3": {"status": "pending_verification"},
        "r/x:4": {"status": "non-actionable"},
        "r/x:5": {"status": "failed"},
        "r/x:6": {"status": "feature_flagged"},
        "r/x:7": {"status": "feature_needs_info"},
        "r/x:8": {"status": "feature_built"},
    }

    def _counters():
        return (state["success_count"], state["closed_count"],
                state["pending_verification_count"], state["non_actionable_count"],
                state["failure_count"], state["feature_flagged_count"],
                state["feature_needs_info_count"], state["feature_built_count"])

    ns["save_processed"](seed)
    state["processed"] = dict(seed)
    recompute(seed)
    ok &= _check("baseline: all eight buckets populated",
                 _counters() == (1, 1, 1, 1, 1, 1, 1, 1))

    # ── delete_issue on a "resolved" issue (missed by the old narrow tuple
    #    check, which only knew about fixed/verified/awaiting_prod_verification)
    _run(ns["delete_issue"](ns["_FakeRequest"]({"issue_id": "r/x:1"})))
    ok &= _check("delete_issue: dismissing a 'resolved' issue decrements success_count",
                 state["success_count"] == 0)

    # ── delete_issue on a "closed" issue (never touched at all before the fix)
    _run(ns["delete_issue"](ns["_FakeRequest"]({"issue_id": "r/x:2"})))
    ok &= _check("delete_issue: dismissing a 'closed' issue decrements closed_count",
                 state["closed_count"] == 0)

    # ── clear_history must reset ALL eight buckets, not just success/failure
    _run(ns["clear_history"]())
    ok &= _check("clear_history: resets every counter to 0",
                 _counters() == (0, 0, 0, 0, 0, 0, 0, 0))

    # ── delete_all_issues: same check, from a fresh non-zero baseline
    ns["save_processed"](seed)
    state["processed"] = dict(seed)
    recompute(seed)
    _run(ns["delete_all_issues"](ns["_FakeRequest"]({})))
    ok &= _check("delete_all_issues: resets every counter to 0",
                 _counters() == (0, 0, 0, 0, 0, 0, 0, 0))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running issue-counter recompute self-test...")
    import sys
    sys.exit(main())
