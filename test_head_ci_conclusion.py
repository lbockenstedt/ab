"""head_ci_conclusion — the only ground truth AppBuilder has about whether a fix works.

AppBuilder cannot run a repo's tests: fix_engine.verify_fix executes them through
run_sandboxed_command, which requires Docker, and the host has none — so
qa_enabled is False and every generated fix is pushed unverified. Before this
helper existed nothing read GitHub Actions either (the one Actions call in
pr_actions releases PARKED runs and never looks at a conclusion), so a fix that
broke the suite generated no signal anywhere and the fixer re-spent its budget on
the logic the panel named while the real breakage went unmentioned.

The single most important property here is that "no evidence" is NOT "pass":
returning success for a commit with no check runs would hand out a false
all-clear and merge red code.
"""
import sys
import types

import pytest


def _load():
    """Import pr_actions.head_ci_conclusion without dragging in the service.

    pr_actions imports `main` (the FastAPI app) at module scope, which starts
    workers and cannot be imported twice in a test process.
    """
    import ast
    import os
    src = open(os.path.join(os.path.dirname(__file__), "pr_actions.py")).read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "head_ci_conclusion")
    ns = {"requests": _FakeRequests, "logger": _quiet_logger()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<head_ci>", "exec"), ns)
    return ns["head_ci_conclusion"]


def _quiet_logger():
    log = types.SimpleNamespace()
    log.debug = lambda *a, **k: None
    return log


class _Resp:
    def __init__(self, status_code=200, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class _FakeRequests:
    """Stands in for the `requests` module inside the extracted function."""
    next_response = None
    next_error = None
    last_call = None

    @classmethod
    def get(cls, url, params=None, headers=None, timeout=None):
        cls.last_call = {"url": url, "params": params, "headers": headers,
                         "timeout": timeout}
        if cls.next_error is not None:
            raise cls.next_error
        return cls.next_response


@pytest.fixture(autouse=True)
def _reset():
    _FakeRequests.next_response = None
    _FakeRequests.next_error = None
    _FakeRequests.last_call = None


def _runs(*specs):
    return _Resp(payload={"check_runs": [
        {"name": n, "status": s, "conclusion": c} for n, s, c in specs]})


def test_no_check_runs_is_unknown_not_success():
    """THE critical case: a commit with no checks must never look green.

    An empty list is the natural shape for a repo with no CI, a run that has not
    registered yet, or a token that cannot see checks. Reporting that as
    "success" would let the merge gate wave through code nothing ever built.
    """
    fn = _load()
    _FakeRequests.next_response = _Resp(payload={"check_runs": []})
    assert fn("o/r", "sha", "tok") == ("unknown", "")
    # Same for a payload missing the key entirely, or holding junk.
    _FakeRequests.next_response = _Resp(payload={})
    assert fn("o/r", "sha", "tok") == ("unknown", "")
    _FakeRequests.next_response = _Resp(payload={"check_runs": "nope"})
    assert fn("o/r", "sha", "tok") == ("unknown", "")
    _FakeRequests.next_response = _Resp(payload={"check_runs": [None, 7, "x"]})
    assert fn("o/r", "sha", "tok") == ("unknown", "")


def test_failure_reports_each_failing_check_by_name():
    fn = _load()
    _FakeRequests.next_response = _runs(
        ("test", "completed", "failure"),
        ("lint", "completed", "success"),
        ("build", "completed", "timed_out"))
    state, details = fn("o/r", "sha", "tok")
    assert state == "failure"
    assert "test: failure" in details and "build: timed_out" in details
    # The passing check is not named -- the summary is a list of what to fix.
    assert "lint" not in details


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_all_bad_conclusions_count_as_failure(conclusion):
    fn = _load()
    _FakeRequests.next_response = _runs(("test", "completed", conclusion))
    assert fn("o/r", "sha", "tok")[0] == "failure"


def test_failure_wins_over_pending():
    """A red check is actionable now; waiting for a sibling changes nothing."""
    fn = _load()
    _FakeRequests.next_response = _runs(
        ("slow", "in_progress", None), ("test", "completed", "failure"))
    assert fn("o/r", "sha", "tok")[0] == "failure"


def test_incomplete_run_is_pending():
    fn = _load()
    _FakeRequests.next_response = _runs(
        ("test", "completed", "success"), ("slow", "queued", None))
    assert fn("o/r", "sha", "tok") == ("pending", "")


def test_parked_run_is_pending_not_failure():
    """action_required means GitHub never STARTED the run for a bot-opened PR.

    _release_parked_checks is what clears those; calling it a failure would send
    the fixer chasing a break that does not exist.
    """
    fn = _load()
    _FakeRequests.next_response = _runs(("test", "completed", "action_required"))
    assert fn("o/r", "sha", "tok") == ("pending", "")


def test_benign_conclusions_are_success():
    fn = _load()
    _FakeRequests.next_response = _runs(
        ("test", "completed", "success"),
        ("optional", "completed", "skipped"),
        ("advisory", "completed", "neutral"))
    assert fn("o/r", "sha", "tok") == ("success", "")


@pytest.mark.parametrize("setup", ["no_token", "http_error", "network", "bad_json",
                                   "not_a_dict"])
def test_every_failure_to_observe_is_unknown(setup):
    """No evidence is never a verdict -- in either direction.

    It must not block a merge (that would deadlock every repo the token cannot
    read) and must not clear one (that would be a false all-clear).
    """
    fn = _load()
    token = "tok"
    if setup == "no_token":
        token = ""
    elif setup == "http_error":
        _FakeRequests.next_response = _Resp(status_code=404, payload={})
    elif setup == "network":
        _FakeRequests.next_error = RuntimeError("connection reset")
    elif setup == "bad_json":
        _FakeRequests.next_response = _Resp(raises=True)
    elif setup == "not_a_dict":
        _FakeRequests.next_response = _Resp(payload=["nope"])
    assert fn("o/r", "sha", token) == ("unknown", "")


def test_no_token_makes_no_request_at_all():
    fn = _load()
    assert fn("o/r", "sha", "") == ("unknown", "")
    assert _FakeRequests.last_call is None


def test_queries_the_right_commit_endpoint():
    fn = _load()
    _FakeRequests.next_response = _runs(("test", "completed", "success"))
    fn("owner/repo", "abc123", "tok")
    call = _FakeRequests.last_call
    assert call["url"].endswith("/repos/owner/repo/commits/abc123/check-runs")
    assert call["headers"]["Authorization"] == "token tok"
    assert call["timeout"] == 10.0


def test_never_raises_on_hostile_payloads():
    """This runs inside the merge path; an exception there is an outage."""
    fn = _load()
    for payload in ({"check_runs": [{"name": None, "status": "completed",
                                     "conclusion": "failure"}]},
                    {"check_runs": [{}]},
                    {"check_runs": [{"status": "completed"}]}):
        _FakeRequests.next_response = _Resp(payload=payload)
        state, details = fn("o/r", "sha", "tok")
        assert state in ("success", "failure", "pending", "unknown")
        assert isinstance(details, str)
    # A run with no name still has to be nameable in the summary.
    _FakeRequests.next_response = _Resp(payload={"check_runs": [
        {"name": None, "status": "completed", "conclusion": "failure"}]})
    assert fn("o/r", "sha", "tok") == ("failure", "check: failure")


def main():
    sys.exit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
