"""Parked workflow runs must be released, or the PR is blocked forever.

THE FAILURE THIS LOCKS DOWN
GitHub does not start the checks of a PR opened by a bot token -- it parks each
workflow run as ``action_required``. A parked run never posts a status, so a
branch protection rule requiring that context can never be satisfied:

    Repository rule violations found
    Required status check "test" is expected.

There is no red check and no failure to remediate, so AppBuilder's remediation
path sees nothing wrong and the PR simply stops (ab#338 sat MERGEABLE/BLOCKED
while `CI` was parked and `branch-flow` had been released).

The release step in promote.yml/backmerge.yml existed already, but it returned
as soon as it had approved ANY parked run. Two workflows are triggered per PR
and they park at slightly different instants, so whichever one parked a moment
later stayed parked -- an intermittent, silent PR stall.

pr_actions.py cannot be imported (it pulls in ``main``, which boots a second
AppBuilder), so the functions under test are extracted with ast and exec'd
against stubs -- the same approach test_twin_parity.py uses.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(ROOT, "pr_actions.py")).read()
REVIEW_SRC = open(os.path.join(ROOT, "pr_review.py")).read()


def _extract(name, src=SRC):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError("%s not found" % name)


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeRequests:
    """Serves one GET payload per poll, and records every approve POST."""

    def __init__(self, polls, post_status=201):
        self._polls = list(polls)
        self._post_status = post_status
        self.approved = []
        self.gets = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets += 1
        ids = self._polls.pop(0) if self._polls else []
        return _Resp(200, {"workflow_runs": [{"id": i} for i in ids]})

    def post(self, url, headers=None, timeout=None):
        self.approved.append(int(url.rstrip("/").split("/")[-2]))
        return _Resp(self._post_status, text="nope")


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _release(fake_requests, sleeps=None):
    ns = {"requests": fake_requests, "logger": _NoLog(),
          "time": _FakeTime(sleeps if sleeps is not None else [])}
    exec(_extract("_release_parked_checks"), ns)
    return ns["_release_parked_checks"]


class _FakeTime:
    """monotonic() advances 1s per sleep so the loop terminates deterministically."""

    def __init__(self, log):
        self.now = 0.0
        self.log = log

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.log.append(secs)
        self.now += secs


def _expects():
    ns = {}
    exec(_extract("_expects_missing_check"), ns)
    return ns["_expects_missing_check"]


# --------------------------------------------------------------------------
# Telling "never reported" apart from "failed"
# --------------------------------------------------------------------------
def test_missing_check_refusal_is_recognised():
    expects = _expects()
    assert expects('Repository rule violations found\n\n'
                   'Required status check "test" is expected.\n\n')


@pytest.mark.parametrize("msg", [
    "",
    None,
    "Required status check \"test\" failed.",
    "branch protection / repository rules refused the merge: "
    "At least 1 approving review is required by reviewers with write access.",
    "the configured GitHub token lacks the `workflow` scope",
])
def test_other_refusals_are_not_claimed(msg):
    """A red check, a missing review or a scope problem are NOT ours to clear;
    claiming them would make AppBuilder retry forever instead of recording the
    refusal once."""
    assert not _expects()(msg)


# --------------------------------------------------------------------------
# The race: approve every parked run, not just the first
# --------------------------------------------------------------------------
def test_a_run_that_parks_later_is_still_released():
    """THE REGRESSION. `CI` and `branch-flow` do not park at the same instant.
    The old loop approved whichever it saw first and returned, leaving the
    other parked -- and if that was `CI`, the required `test` check never
    reported."""
    fake = _FakeRequests([[101], [101, 202], [101, 202], [101, 202], [101, 202]])
    released = _release(fake)("lbockenstedt/ab", "d" * 40, "tok")
    assert fake.approved == [101, 202], fake.approved
    assert released == 2


def test_a_run_is_never_approved_twice():
    fake = _FakeRequests([[7], [7], [7], [7], [7], [7]])
    assert _release(fake)("lbockenstedt/ab", "d" * 40, "tok") == 1
    assert fake.approved == [7]


def test_polling_stops_once_the_queue_is_quiet():
    """It must not burn the whole window after the work is done -- this runs
    inline in the merge path."""
    sleeps = []
    fake = _FakeRequests([[5]] + [[5]] * 20)
    _release(fake, sleeps)("lbockenstedt/ab", "d" * 40, "tok")
    assert fake.gets == 4, "expected 1 approving poll + 3 quiet polls"


def test_no_parked_runs_does_not_exit_early():
    """Nothing parked yet is the COMMON case right after a PR opens: the runs
    appear a few seconds later. Bailing out on the first empty poll would
    reintroduce the stall."""
    sleeps = []
    fake = _FakeRequests([[], [], [], [], [], [9], [9], [9], [9]])
    assert _release(fake, sleeps)("lbockenstedt/ab", "d" * 40, "tok") == 1
    assert fake.approved == [9]


def test_no_token_is_a_no_op():
    fake = _FakeRequests([[1]])
    assert _release(fake)("lbockenstedt/ab", "d" * 40, "") == 0
    assert fake.approved == []


def test_a_failed_approval_is_not_counted():
    fake = _FakeRequests([[1], [1], [1], [1], [1], [1]], post_status=403)
    assert _release(fake)("lbockenstedt/ab", "d" * 40, "tok") == 0


def test_a_transport_error_does_not_propagate():
    class _Boom(_FakeRequests):
        def get(self, *a, **k):
            self.gets += 1
            raise RuntimeError("connection reset")

    fake = _Boom([[1]])
    assert _release(fake)("lbockenstedt/ab", "d" * 40, "tok") == 0


# --------------------------------------------------------------------------
# Wiring: the release must be attempted BEFORE the refusal is written off
# --------------------------------------------------------------------------
def test_merge_pr_tries_the_release_before_recording_the_refusal():
    body = _extract("merge_pr")
    assert "_expects_missing_check" in body, "merge_pr never checks for a parked check"
    assert "_release_parked_checks" in body, "merge_pr never releases the parked run"
    release = body.index("_release_parked_checks")
    record = body.index("merge_blocked_reason=why")
    assert release < record, ("the refusal is recorded before the release is "
                              "attempted, so the PR stays blocked")


def test_a_released_pr_is_retryable():
    """Returning retryable=False would make pr_review record it as a permanent
    refusal and stay quiet -- the released check would report and nothing
    would ever merge it."""
    body = _extract("merge_pr")
    seg = body[body.index("_release_parked_checks"):]
    seg = seg[:seg.index("merge_blocked_reason=why")]
    assert '"retryable": True' in seg
    assert '"released_checks"' in seg


def test_pr_review_does_not_treat_a_release_as_a_refusal():
    body = _extract("_maybe_auto_merge", REVIEW_SRC)
    assert 'result.get("released_checks")' in body, \
        "auto-merge cannot tell a released check from a refusal"
    assert body.index('result.get("released_checks")') < \
        body.index('result.get("retryable") is False'), \
        "the refusal branch shadows the released-checks branch"


# --------------------------------------------------------------------------
# The workflows must not reintroduce the race
# --------------------------------------------------------------------------
@pytest.mark.parametrize("wf", ["promote.yml", "backmerge.yml"])
def test_workflow_release_step_approves_every_parked_run(wf):
    path = os.path.join(ROOT, ".github", "workflows", wf)
    text = open(path).read()
    step = text[text.index("action_required"):]
    step = step[:step.index("no parked run found")]
    assert "exit 0" not in step, \
        "the release loop returns after the first approval -- the other run stays parked"
    assert re.search(r'case " \$approved "', step), \
        "the loop does not dedupe, so it would re-approve the same run every poll"
    assert "quiet" in step, "the loop has no quiet-period exit"
