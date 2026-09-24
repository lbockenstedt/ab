"""ab#275 reachability finding: the per-head guardrail latch never cleared for
the records that were already stuck.

`maybe_auto_remediate` replaced a permanent `auto_remediate_blocked` latch with
a per-head one, so a PR whose new commit no longer trips a guardrail gets
re-evaluated. The unblock branch read:

    if head_sha and rec.get("auto_remediate_blocked_head") not in (None, head_sha):

A record latched BEFORE `auto_remediate_blocked_head` existed has no stored head,
so `.get()` returned None, `None not in (None, head_sha)` was False, and the
record fell through to `_skip` -- permanently. The one-way door was therefore
only ever removed for latches written after the change, which is precisely the
set of records that did not need rescuing. lm#1018 is a real example: it was
latched by a `psk-hardcode` false positive and would have stayed blocked for
the rest of its life.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pr_remediate


class _Head:
    def __init__(self, sha, ref="fix/thing"):
        self.sha = sha
        self.ref = ref


class _PR:
    def __init__(self, number=1018, sha="newhead0000"):
        self.number = number
        self.head = _Head(sha)
        self.state = "open"
        self.merged = False
        self.draft = False
        self.comments = []

    def get_files(self):
        return []

    def create_issue_comment(self, body):
        self.comments.append(body)


class _Repo:
    full_name = "lbockenstedt/lm"


def _run(rec, sha="newhead0000"):
    key = "lbockenstedt/lm#1018"
    pr_remediate.state["pr_reviews"] = {key: dict(rec)}
    calls = []
    pr_remediate.auto_remediate_pr = lambda *a, **k: calls.append(1) or (True, "remediated")
    ok, msg = pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(sha=sha), {})
    return ok, msg, pr_remediate.state["pr_reviews"].get(key, {})


def setup_module(_module):
    _run.__dict__["_orig"] = pr_remediate.auto_remediate_pr


def teardown_module(_module):
    pr_remediate.auto_remediate_pr = _run.__dict__["_orig"]


def test_legacy_latch_without_a_stored_head_is_cleared():
    """THE REGRESSION: no auto_remediate_blocked_head at all (a pre-change
    record). It must be treated as stale and re-evaluated, not honoured."""
    ok, msg, rec = _run({"auto_remediate_blocked": True,
                         "panel_verdict": "Deny", "panel_confidence": 0.85})
    assert ok is True, msg
    assert rec.get("auto_remediate_blocked") is False


def test_latch_for_a_different_head_is_cleared():
    ok, msg, rec = _run({"auto_remediate_blocked": True,
                         "auto_remediate_blocked_head": "oldhead1111",
                         "panel_verdict": "Deny", "panel_confidence": 0.85})
    assert ok is True, msg
    assert rec.get("auto_remediate_blocked") is False


def test_latch_for_the_current_head_still_holds():
    """The latch must still do its job on the commit that actually tripped it,
    otherwise every scan would re-run the fixer against the same bad diff."""
    ok, msg, _rec = _run({"auto_remediate_blocked": True,
                          "auto_remediate_blocked_head": "newhead0000"})
    assert ok is False
    assert "blocked by guardrail" in msg


def test_latch_holds_when_the_pr_has_no_head_sha():
    """With no head to compare against there is no evidence the diff changed,
    so the latch must fail closed rather than clearing on every scan."""
    key = "lbockenstedt/lm#1018"
    pr_remediate.state["pr_reviews"] = {key: {"auto_remediate_blocked": True}}
    pr = _PR()
    pr.head = None
    ok, msg = pr_remediate.maybe_auto_remediate(None, _Repo(), pr, {})
    assert ok is False and "blocked by guardrail" in msg
