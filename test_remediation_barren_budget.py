"""Self-test: a barren model reply must not spend the remediation budget.

`auto_remediate_pr` charged `remediation_attempts += 1` on EVERY failure,
including the ones that mean "the model said nothing we could parse" -- an empty
reply, no JSON, or unparseable JSON. It also appended the model to
`excluded_models` on those same failures.

So three reasoning-prose replies in a row exhausted the 3-attempt ceiling having
made ZERO actual fix attempts, and permanently excluded three working models on
the way. That is exactly how kvm#27 and pxmx#130 reached
`remediation_attempts: 3` with real, un-attempted defects still in the diff, and
why AppBuilder could not repair PRs that a human could.

`fix_one_pr` already grants these replies a separate "barren" allowance. This
path never got the same lesson. The strings that classify a failure as barren
now live in `fix_failures`, imported by both the producer and the consumer, so
the two cannot drift apart the way `_full_file_context`'s diff-header regex did.
"""
import ast
import threading
from datetime import datetime

import pytest

import fix_failures
import pr_remediate
from pr_remediate import auto_remediate_pr


class MockFile:
    def __init__(self, filename, patch=""):
        self.filename = filename
        self.patch = patch


class MockPR:
    def __init__(self, title="Test PR", body="", number=42, files=None):
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

@pytest.mark.parametrize("msg", [
    fix_failures.BARREN_EMPTY,
    fix_failures.BARREN_NO_JSON,
    fix_failures.BARREN_INVALID_JSON,
])
def test_the_three_barren_strings_are_recognised(msg):
    assert fix_failures.is_barren_failure(msg) is True


def test_a_wrapped_barren_failure_is_still_barren():
    """Callers prefix these, e.g. "Fix invocation failed: ..."."""
    assert fix_failures.is_barren_failure(
        "Fix invocation failed: " + fix_failures.BARREN_EMPTY) is True


@pytest.mark.parametrize("junk", [None, "", "   ", 42, [], object()])
def test_non_messages_are_not_barren(junk):
    assert fix_failures.is_barren_failure(junk) is False


def test_a_real_fix_failure_is_not_barren():
    """An anchor miss IS signal about the fix and must spend the budget."""
    assert fix_failures.is_barren_failure(
        "Anchors that did not match: def foo():") is False
    assert fix_failures.is_barren_failure(
        "The response contained no edits.") is False


def test_producer_and_consumer_share_one_source_of_truth():
    """pr_review must build these strings FROM fix_failures, not respell them.

    The whole defect class this guards against is a contract spelled out twice.
    """
    src = open("pr_review.py", encoding="utf-8").read()
    for const in ("BARREN_EMPTY", "BARREN_NO_JSON", "BARREN_INVALID_JSON"):
        assert "fix_failures.%s" % const in src, (
            "pr_review must reference fix_failures.%s" % const)
    assert "The model returned an empty response." not in src, (
        "pr_review still spells a barren string inline -- it can drift from "
        "the copy pr_remediate matches on")


# -- the budget ------------------------------------------------------------

def test_barren_reply_does_not_charge_the_remediation_budget():
    rec = {"remediation_attempts": 1, "panel_critique": "Issue persists"}
    success, msg, out = _run(rec, fix_failures.BARREN_INVALID_JSON)
    assert success is False
    assert out["remediation_attempts"] == 1, (
        "a barren reply spent a real remediation attempt -- this is the bug")
    assert out["remediation_barren"] == 1
    assert out["auto_remediate_status"] == "barren"


def test_barren_reply_does_not_exclude_the_model():
    """The model was never shown to be incompetent -- only silent."""
    rec = {"remediation_attempts": 0, "panel_critique": "x"}
    _, _, out = _run(rec, fix_failures.BARREN_EMPTY)
    assert not out.get("excluded_models"), (
        "a silent model was permanently excluded, shrinking the pool")


def test_the_barren_allowance_binds():
    """Once spent, a barren reply falls through and charges the budget.

    Without a ceiling this trades a budget livelock for an unbounded one.
    """
    rec = {"remediation_attempts": 1, "remediation_barren": 2,
           "panel_critique": "x"}
    _, _, out = _run(rec, fix_failures.BARREN_EMPTY)
    assert out["remediation_attempts"] == 2, "the barren allowance never bound"
    assert out["auto_remediate_status"] == "failed"
    assert "gemini-3.8-flash" in (out.get("excluded_models") or [])


def test_a_substantive_failure_still_charges_the_budget():
    """Regression guard: only barren replies get the allowance."""
    rec = {"remediation_attempts": 1, "panel_critique": "x"}
    _, _, out = _run(rec, "Anchors that did not match: def foo():")
    assert out["remediation_attempts"] == 2
    assert out["auto_remediate_status"] == "failed"
    assert out.get("remediation_barren", 0) == 0


def test_allowance_is_configurable_and_clamped():
    rec = {"remediation_attempts": 0, "remediation_barren": 0,
           "panel_critique": "x"}
    _, _, out = _run(rec, fix_failures.BARREN_EMPTY,
                     config={"pr_remediate_max_barren_retries": 0})
    assert out["remediation_attempts"] == 1, (
        "an allowance of 0 must disable the barren path entirely")


# -- the rebuild hazard ----------------------------------------------------

def _extract(path, funcs):
    src = open(path, encoding="utf-8").read()
    segs = [ast.get_source_segment(src, n) for n in ast.parse(src).body
            if isinstance(n, ast.FunctionDef) and n.name in funcs]
    return "\n\n".join(segs)


class _DummyLogger:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


@pytest.fixture
def pr_env():
    src = _extract("app_state.py",
                   {"record_pr_review", "_panel_dissent_stats", "update_pr_review"})
    state = {"pr_reviews": {}}
    ns = {
        "state": state,
        "_task_state_lock": threading.Lock(),
        "datetime": datetime,
        "save_pr_reviews": lambda x: None,
        "_record_merge_milestone": lambda *a, **k: None,
        "_PR_REVIEWS_MAX": 100,
        "logger": _DummyLogger(),
    }
    exec(src, ns)
    return ns["record_pr_review"], ns["update_pr_review"], state


def test_barren_count_survives_the_per_scan_rebuild(pr_env):
    """record_pr_review REBUILDS the record every poll from a fixed field list.

    Leave the counter out and it resets to 0 on the next scan, the ceiling never
    binds, and the barren path loops forever -- the desc_fixes livelock again.
    """
    record, update, state = pr_env
    record("o/r", 1, "t", "u", [], "sha1")
    update("o/r", 1, remediation_barren=2)
    record("o/r", 1, "t", "u", [], "sha1")
    assert state["pr_reviews"]["o/r#1"]["remediation_barren"] == 2, (
        "barren allowance reset on rescan -- this is the livelock")


def test_barren_count_resets_on_a_new_head(pr_env):
    """A barren reply pushes no commit, so head movement means genuinely new
    code, which earns a fresh allowance."""
    record, update, state = pr_env
    record("o/r", 1, "t", "u", [], "sha1")
    update("o/r", 1, remediation_barren=2)
    record("o/r", 1, "t", "u", [], "sha2")
    assert state["pr_reviews"]["o/r#1"]["remediation_barren"] == 0
