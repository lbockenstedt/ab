#!/usr/bin/env python3
"""Self-test for the background-queued Dismiss action with GitHub retry.

Run:  python3 bugfixer/test_dismiss_background_retry.py

routes.py cannot be imported directly (it pulls in the full FastAPI app /
main.py's circular-import chain), so this extracts the SOURCE of the pure
route bodies via ast and execs them with stubbed I/O (_close_issue_on_github,
load_processed/save_processed, logger, time.sleep).

Regression guard: clicking Dismiss used to await _close_issue_on_github
in-line, so the HTTP request (and the browser) blocked for however long the
single best-effort GitHub call took, with no retry at all. delete_issue now
has to (1) return before the GitHub call finishes, (2) run the close in a
background thread retried up to 5x, and (3) record the outcome in
state["dismiss_jobs"] so /dismiss_status can report it for the WebUI's
completion toast.
"""
import ast
import asyncio
import threading
import time as real_time


def _load_ns():
    routes_src = open("routes.py").read()
    routes_tree = ast.parse(routes_src)

    segs = []
    want = {"delete_issue", "dismiss_status"}
    for node in routes_tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in want:
            segs.append(ast.get_source_segment(routes_src, node))
        if isinstance(node, ast.FunctionDef) and node.name == "_close_issue_on_github_with_retry":
            segs.append(ast.get_source_segment(routes_src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_DISMISS_MAX_RETRIES":
                    segs.append(ast.get_source_segment(routes_src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    _store = {}

    def load_processed():
        return dict(_store)

    def save_processed(d):
        _store.clear()
        _store.update(d)

    def recompute_issue_counters(processed):
        pass

    class _FakeTime:
        # Real time.sleep would make a 5-attempt backoff (1+2+4+8s) run for
        # ~15s per test case; a no-op keeps this test fast without touching
        # the retry COUNT logic under test.
        def sleep(self, _):
            pass

    state = {"processed": {}, "dismiss_jobs": {}}

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
        "recompute_issue_counters": recompute_issue_counters,
        "threading": threading, "time": _FakeTime(),
        "asyncio": asyncio, "JSONResponse": _JSONResponse,
        "Request": _FakeRequest,
    }
    exec("\n\n".join(segs), ns)
    ns["_FakeRequest"] = _FakeRequest
    ns["_store"] = _store
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _wait_for(pred, timeout=5.0):
    deadline = real_time.time() + timeout
    while real_time.time() < deadline:
        if pred():
            return True
        real_time.sleep(0.02)
    return pred()


def main():
    ok = True
    ns = _load_ns()
    state = ns["state"]

    # ── delete_issue returns before the (slow) GitHub call finishes ────────
    gate = threading.Event()
    calls = []

    def _slow_close(issue_id):
        calls.append(issue_id)
        gate.wait(timeout=5)
        return True, f"closed {issue_id}"

    ns["_close_issue_on_github"] = _slow_close
    ns["_store"].update({"r/x:1": {"status": "failed"}})
    state["processed"] = {"r/x:1": {"status": "failed"}}

    t0 = real_time.time()
    resp = _run(ns["delete_issue"](ns["_FakeRequest"]({"issue_id": "r/x:1"})))
    elapsed = real_time.time() - t0
    ok &= _check("delete_issue returns promptly, not waiting on the GitHub call",
                 elapsed < 1.0 and resp.get("background") is True)
    ok &= _check("delete_issue removed the issue from local history immediately",
                 "r/x:1" not in ns["_store"])
    ok &= _check("dismiss_status is 'pending' while the background close is in flight",
                 _run(ns["dismiss_status"]("r/x:1"))["status"] == "pending")
    gate.set()  # let the background thread finish
    ok &= _check("background close eventually completes",
                 _wait_for(lambda: state["dismiss_jobs"].get("r/x:1", {}).get("status") == "done"))
    ok &= _check("dismiss_status reports 'done' with the real GitHub message",
                 _run(ns["dismiss_status"]("r/x:1"))["message"] == "closed r/x:1")

    # ── unknown issue_id reports 'unknown', not a crash ─────────────────────
    ok &= _check("dismiss_status on an unseen issue_id returns 'unknown'",
                 _run(ns["dismiss_status"]("r/x:999"))["status"] == "unknown")

    # ── retries up to 5x, then succeeds on the 3rd attempt ──────────────────
    attempts = {"n": 0}

    def _fails_twice_then_ok(issue_id):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient GitHub 502")
        return True, f"closed {issue_id} on attempt {attempts['n']}"

    ns["_close_issue_on_github"] = _fails_twice_then_ok
    ns["_store"].update({"r/x:2": {"status": "failed"}})
    state["processed"] = {"r/x:2": {"status": "failed"}}
    _run(ns["delete_issue"](ns["_FakeRequest"]({"issue_id": "r/x:2"})))
    ok &= _check("recovers within the retry budget: 'done' after 2 failures + 1 success",
                 _wait_for(lambda: state["dismiss_jobs"].get("r/x:2", {}).get("status") == "done"))
    ok &= _check("stopped retrying once it succeeded (3 attempts, not 5)",
                 attempts["n"] == 3)

    # ── exhausts all 5 attempts and reports 'error', never crashes ──────────
    def _always_fails(issue_id):
        raise RuntimeError("permanently rate-limited")

    ns["_close_issue_on_github"] = _always_fails
    ns["_store"].update({"r/x:3": {"status": "failed"}})
    state["processed"] = {"r/x:3": {"status": "failed"}}
    _run(ns["delete_issue"](ns["_FakeRequest"]({"issue_id": "r/x:3"})))
    ok &= _check("gives up as 'error' after exhausting the retry budget",
                 _wait_for(lambda: state["dismiss_jobs"].get("r/x:3", {}).get("status") == "error"))
    ok &= _check("retried exactly _DISMISS_MAX_RETRIES (5) times, not more",
                 ns["_DISMISS_MAX_RETRIES"] == 5)
    ok &= _check("error message names the exhausted attempt count",
                 "5 attempts" in state["dismiss_jobs"]["r/x:3"]["message"])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running background-dismiss retry self-test...")
    import sys
    sys.exit(main())
