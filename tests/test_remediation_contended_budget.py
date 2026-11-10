"""Self-test: losing the per-PR fix lock must not spend the remediation budget.

`fix_one_pr` refuses immediately when another task already holds
`pr-fix:<repo>#<n>`, returning "A fix is already in progress for this PR." That
refusal happens BEFORE a model is selected, before a checkout is made, before a
single token is generated -- nothing was attempted.

`auto_remediate_pr` charged it anyway. `new_attempts = attempts + 1` ran
unconditionally on the failure path, so a scan that did no work still spent a
remediation attempt, appended the answering model to `excluded_models`, and --
when the escalation turn had been granted -- burned the premium budget too.

That is the same defect the barren allowance exists to prevent, one level
further out: a failure that says nothing about the fix must not be paid for
like one that does. lm#1071 reached `exhausted_human_review` with
`auto_remediate_failure: "A fix is already in progress for this PR."` recorded
as its final state -- the budget was spent on contention, not on repair.

Contention deliberately gets NO capped allowance of its own (unlike barren
replies, which need one because a persistently mute model would otherwise loop
forever). The fix lock is an in-process set released in a `finally`, and it
does not survive a restart, so it is always released and the contended scan
simply comes back on the next cycle.
"""
import pytest

import fix_failures
import pr_remediate
from pr_remediate import auto_remediate_pr


class MockFile:
    def __init__(self, filename, patch=""):
        self.filename = filename
        self.patch = patch


class MockPR:
    def __init__(self, title="Test PR", body="", number=99, files=None):
        self.title = title
        self.body = body
        self.number = number
        self._files = files or []
        self.comments = []
        self.state = "open"
        self.merged = False
        self.draft = False

    def get_files(self):
        return self._files

    def create_issue_comment(self, body):
        self.comments.append(body)


class MockRepo:
    def __init__(self, full_name="lbockenstedt/nw"):
        self.full_name = full_name


def _run(rec, failure_msg, config=None, model="gemini-3.8-flash"):
    """Drive auto_remediate_pr once with a fix_fn that fails with `failure_msg`."""
    repo = MockRepo("lbockenstedt/nw")
    files = [MockFile("src/clean.py", "+ def clean(): pass")]
    pr = MockPR(title="Remediation PR", body="Body", number=99, files=files)
    pr_remediate.state["pr_reviews"] = {"lbockenstedt/nw#99": dict(rec)}

    def dummy_fix(*args, **kwargs):
        out = kwargs.get("used_model_out")
        if out is not None:
            out["model"] = model
        return False, failure_msg

    cfg = {"pr_auto_remediate_max_attempts": 3}
    cfg.update(config or {})
    success, msg = auto_remediate_pr(None, repo, pr, cfg, fix_fn=dummy_fix)
    return success, msg, pr_remediate.state["pr_reviews"]["lbockenstedt/nw#99"]


# -- the classifier --------------------------------------------------------

def test_the_contended_string_is_recognised():
    assert fix_failures.is_contended_failure(fix_failures.CONTENDED_LOCK) is True


def test_a_wrapped_contended_failure_is_still_contended():
    assert fix_failures.is_contended_failure(
        "Fix invocation failed: " + fix_failures.CONTENDED_LOCK) is True


@pytest.mark.parametrize("junk", [None, "", "   ", 42, [], object()])
def test_non_messages_are_not_contended(junk):
    assert fix_failures.is_contended_failure(junk) is False


def test_contention_and_barrenness_are_disjoint():
    """They take different paths and must never be confused for one another."""
    assert fix_failures.is_barren_failure(fix_failures.CONTENDED_LOCK) is False
    for barren in (fix_failures.BARREN_EMPTY, fix_failures.BARREN_NO_JSON,
                   fix_failures.BARREN_INVALID_JSON):
        assert fix_failures.is_contended_failure(barren) is False


def test_the_description_fix_lock_is_a_different_message():
    """pr_review has a SECOND lock message for the description-rewrite path:
    "A description fix is already in progress for this PR." That one does not
    propagate to remediation (the caller logs it and continues to the code
    fix), and matching it here would silently make description contention look
    like code contention."""
    assert fix_failures.is_contended_failure(
        "A description fix is already in progress for this PR.") is False


def test_producer_and_consumer_share_one_source_of_truth():
    """pr_review must build the string FROM fix_failures, not respell it --
    a contract spelled out twice is the defect class this module exists for."""
    src = open("pr_review.py", encoding="utf-8").read()
    assert "fix_failures.CONTENDED_LOCK" in src, (
        "pr_review must reference fix_failures.CONTENDED_LOCK")


# -- the budget ------------------------------------------------------------

def test_contention_does_not_charge_the_remediation_budget():
    rec = {"remediation_attempts": 1, "panel_critique": "Issue persists"}
    success, msg, out = _run(rec, fix_failures.CONTENDED_LOCK)
    assert success is False
    assert out["remediation_attempts"] == 1, (
        "a contended scan spent a real remediation attempt -- this is the bug")


def test_contention_does_not_exclude_the_model():
    """The model never answered; blaming it permanently shrinks the pool."""
    rec = {"remediation_attempts": 1, "panel_critique": "Issue persists"}
    _s, _m, out = _run(rec, fix_failures.CONTENDED_LOCK, model="claude-opus-5.5")
    assert not out.get("excluded_models"), (
        "contention excluded a model that was never consulted")


def test_contention_does_not_consume_the_premium_budget():
    """The premium turn is the scarcest resource AppBuilder has (default 1 per
    PR). Spending it on a scan that never reached a model is the worst case."""
    rec = {"remediation_attempts": 3, "panel_critique": "Issue persists",
           "premium_attempts": 0}
    _s, _m, out = _run(rec, fix_failures.CONTENDED_LOCK,
                       config={"pr_auto_remediate_max_attempts": 3,
                               "pr_remediate_premium_attempts": 1})
    assert int(out.get("premium_attempts") or 0) == 0, (
        "contention burned the one premium escalation turn without calling a model")
    assert out["remediation_attempts"] == 3


def test_contention_is_marked_so_it_is_visible():
    rec = {"remediation_attempts": 1, "panel_critique": "Issue persists"}
    _s, _m, out = _run(rec, fix_failures.CONTENDED_LOCK)
    assert out.get("auto_remediate_status") == "contended"


def test_contention_does_not_reach_a_terminal_state():
    """It must stay retryable: the lock frees when the in-flight fix ends."""
    rec = {"remediation_attempts": 1, "panel_critique": "Issue persists"}
    _s, _m, out = _run(rec, fix_failures.CONTENDED_LOCK)
    assert out.get("auto_remediate_status") != "exhausted_human_review"


def test_repeated_contention_never_exhausts_the_budget():
    """The lock is always released, so contention is transient by construction.
    Ten contended scans in a row must leave the budget untouched."""
    rec = {"remediation_attempts": 0, "panel_critique": "Issue persists"}
    out = rec
    for _ in range(10):
        _s, _m, out = _run(out, fix_failures.CONTENDED_LOCK)
    assert out["remediation_attempts"] == 0


# -- the control: real failures must STILL be charged ----------------------

def test_a_genuine_fix_failure_still_charges_the_budget():
    """The whole point is that this change is narrow. An anchor miss is real
    signal about the fix and must still cost an attempt and exclude the model."""
    rec = {"remediation_attempts": 1, "panel_critique": "Issue persists"}
    _s, _m, out = _run(rec, "Anchors that did not match: def foo():",
                       model="gemini-3.8-flash")
    assert out["remediation_attempts"] == 2, (
        "a real failure stopped being charged -- the fix is too broad")
    assert "gemini-3.8-flash" in (out.get("excluded_models") or [])


def test_a_barren_reply_still_takes_the_barren_path():
    """Contention must not have swallowed the pre-existing barren allowance."""
    rec = {"remediation_attempts": 1, "panel_critique": "Issue persists"}
    _s, _m, out = _run(rec, fix_failures.BARREN_NO_JSON)
    assert out["remediation_attempts"] == 1
    assert out.get("remediation_barren") == 1
    assert out.get("auto_remediate_status") == "barren"
