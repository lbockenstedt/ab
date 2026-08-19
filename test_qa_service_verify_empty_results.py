#!/usr/bin/env python3
"""Self-test for _qa_service_verify's handling of a completed-but-empty QA run.

Run:  python3 ab/test_qa_service_verify_empty_results.py

fix_engine.py cannot be imported directly (it pulls in the full main.py ->
routes.py circular-import chain -- see test_dismiss_background_retry.py's
docstring for the same problem with routes.py, and test_review_pool_fix.py
hits the identical ImportError when it tries `from fix_engine import *`), so
this extracts _qa_service_verify's SOURCE via ast and execs it with a
stubbed `requests` module and `time.sleep`, exactly like that file does for
delete_issue.

Regression guard: ab issue #815's auto-fix (PR #817) introduced an
actual IndentationError into llm_client.py, yet AppBuilder's own PR comment
claimed "Verification: Tests passed successfully". The QA-service path
(verify_fix's priority-1 check when QA_API_URL is configured) computed
`passed == total` off `results = data.get("results", [])` without ever
checking that any tests actually ran -- when the service reports status
COMPLETED with zero results (module name mismatch, no suite registered,
etc.), `passed == total` is vacuously True for 0 == 0, so a COMPLETED run
that tested NOTHING was reported as a full pass. This pins that a
COMPLETED-with-zero-results response is now treated as inconclusive (None),
not a pass, so verify_fix's existing None-handling falls back to local
tests instead of rubber-stamping an unverified fix.
"""
import ast


def _load_ns():
    src_path = "fix_engine.py"
    import os
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_engine.py")
    src = open(src_path).read()
    tree = ast.parse(src)

    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_qa_service_verify":
            segs.append(ast.get_source_segment(src, node))

    module_src = "\n\n".join(segs)

    class _FakeResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json_data = json_data

        def json(self):
            return self._json_data

    class _FakeRequests:
        """Scripted stand-in for the `requests` module: POST /api/run always
        succeeds (202); GET /api/status returns whatever `status_sequence`
        (a list, consumed one entry per poll) provides, repeating the last
        entry once exhausted."""

        def __init__(self, status_sequence):
            self._seq = list(status_sequence)
            self._calls = 0

        def post(self, url, json=None, timeout=None):
            return _FakeResponse(202, {})

        def get(self, url, timeout=None):
            idx = min(self._calls, len(self._seq) - 1)
            self._calls += 1
            return _FakeResponse(200, self._seq[idx])

    class _FakeTime:
        def __init__(self):
            self._t = 0.0

        def time(self):
            return self._t

        def sleep(self, s):
            self._t += s

    def _make_ns(status_sequence):
        ns = {
            "requests": _FakeRequests(status_sequence),
            "time": _FakeTime(),
        }
        exec(compile(module_src, "fix_engine_extract", "exec"), ns)
        return ns

    return _make_ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running _qa_service_verify empty-results self-test...")
    ok = True
    make_ns = _load_ns()

    # --- COMPLETED with zero results must NOT be treated as a pass ---------
    ns = make_ns([{"status": "COMPLETED", "results": []}])
    passed, summary = ns["_qa_service_verify"]("owner/ab", {"QA_API_URL": "http://qa"})
    ok &= _check("COMPLETED with 0 results is NOT reported as passed (was vacuously True)",
                 passed is not True)
    ok &= _check("COMPLETED with 0 results is treated as inconclusive (None), not a hard failure",
                 passed is None)
    ok &= _check("summary mentions 0 results so the caller/log has context",
                 "0" in summary)

    # --- COMPLETED with real results: a genuine full pass still passes -----
    ns = make_ns([{"status": "COMPLETED",
                    "results": [{"name": "t1", "status": "PASS"},
                                {"name": "t2", "status": "PASS"}]}])
    passed, summary = ns["_qa_service_verify"]("owner/ab", {"QA_API_URL": "http://qa"})
    ok &= _check("COMPLETED with 2/2 PASS results is reported as passed",
                 passed is True)

    # --- COMPLETED with a real failure still reports failure ---------------
    ns = make_ns([{"status": "COMPLETED",
                    "results": [{"name": "t1", "status": "PASS"},
                                {"name": "t2", "status": "FAIL"}]}])
    passed, summary = ns["_qa_service_verify"]("owner/ab", {"QA_API_URL": "http://qa"})
    ok &= _check("COMPLETED with a real failing test is reported as failed",
                 passed is False)
    ok &= _check("failed test name surfaces in the summary", "t2" in summary)

    # --- FAILED status is never a pass, even with misleading counts --------
    ns = make_ns([{"status": "FAILED", "results": []}])
    passed, summary = ns["_qa_service_verify"]("owner/ab", {"QA_API_URL": "http://qa"})
    ok &= _check("FAILED status with 0 results is reported as failed, not passed",
                 passed is False)

    # --- IDLE status (service never actually ran anything) isn't a pass ----
    ns = make_ns([{"status": "IDLE", "results": []}])
    passed, summary = ns["_qa_service_verify"]("owner/ab", {"QA_API_URL": "http://qa"})
    ok &= _check("IDLE status is reported as failed, not passed",
                 passed is False)

    if ok:
        print("\nALL CASES PASSED")
        return 0
    print("\nSOME CASES FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
