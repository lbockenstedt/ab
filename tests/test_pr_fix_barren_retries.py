"""A barren fix attempt must not spend the retry budget.

fix_one_pr gets a small, bounded number of attempts (pr_fix_max_attempts,
default 3). Those attempts are meant to test fix *hypotheses*: generate a fix,
let the skeptical panel judge it, feed the critique back, try again.

But an attempt where the model returns an empty completion, or a reply with no
JSON object in it, or malformed JSON, tests no hypothesis at all. Nothing was
produced that could be applied or reviewed. Charging those to the budget is how
cs#143 and lm#1047 reached attempts=3 / exhausted_human_review without a single
generated fix ever having been evaluated -- the live log reads "retry 3/3 --
feedback: The response was not valid JSON" and then "remediation failed: The
model returned an empty response".

Barren attempts now draw on their own separate, hard-capped allowance. A flaky
generation no longer strands a PR, and the cap still stops an endless retry
loop against a provider that is down.

Reuses the AST-extraction harness from test_pr_fix_retry_feedback (pr_review.py
cannot be imported standalone).
"""
import sys

import pytest

from test_pr_fix_retry_feedback import APPROVE, Harness, _reject


class BarrenHarness(Harness):
    """Harness that can also set pr_fix_max_barren_retries."""

    def __init__(self, *a, max_barren=2, **kw):
        super().__init__(*a, **kw)
        self.max_barren = max_barren

    def run(self):
        """Run fix_one_pr with pr_fix_max_barren_retries set as well. It goes
        through Harness.run, which puts the real modules back afterwards."""
        return super().run({"pr_fix_max_barren_retries": self.max_barren})


EMPTY = (False, "empty", [])
NO_JSON = (False, "no_json", [])
BAD_JSON = (False, "invalid_json", [])
ANCHOR_MISS = (False, "edit_anchor_miss", ["def foo():"])
NO_EDITS = (False, "no_edits", [])
OK = True


# -- barren attempts are not charged ---------------------------------------

@pytest.mark.parametrize("barren_kind", [EMPTY, NO_JSON, BAD_JSON])
def test_barren_attempt_does_not_consume_the_budget(barren_kind):
    """Two barren replies then a good fix must still succeed, even though the
    fix budget alone is only 2 attempts."""
    h = BarrenHarness([barren_kind, barren_kind, OK], [APPROVE],
                      verify=[(True, "")], max_attempts=2, max_barren=2)
    ok, _ = h.run()
    assert ok is True
    assert len(h.error_contexts) == 3


def test_barren_allowance_is_bounded():
    """An endlessly barren model must terminate, not loop forever."""
    h = BarrenHarness([EMPTY], [APPROVE], max_attempts=3, max_barren=2)
    ok, msg = h.run()
    assert ok is False
    # 3 charged attempts + 2 barren = 5 generations, then stop.
    assert len(h.error_contexts) == 5
    assert "empty response" in msg


def test_barren_allowance_can_be_disabled():
    h = BarrenHarness([EMPTY], [APPROVE], max_attempts=2, max_barren=0)
    ok, _ = h.run()
    assert ok is False
    assert len(h.error_contexts) == 2


def test_barren_allowance_is_clamped():
    """A silly config value must not grant unlimited retries."""
    h = BarrenHarness([EMPTY], [APPROVE], max_attempts=1, max_barren=999)
    ok, _ = h.run()
    assert ok is False
    # clamped to 3 -> 1 charged + 3 barren
    assert len(h.error_contexts) == 4


# -- attempts that DID produce something still cost --------------------------

def test_anchor_miss_still_charges_the_budget():
    """The model produced structured edits that simply did not apply. That is
    real signal about the fix, so it spends an attempt."""
    h = BarrenHarness([ANCHOR_MISS], [APPROVE], max_attempts=2, max_barren=2)
    ok, _ = h.run()
    assert ok is False
    assert len(h.error_contexts) == 2


def test_no_edits_still_charges_the_budget():
    h = BarrenHarness([NO_EDITS], [APPROVE], max_attempts=2, max_barren=2)
    ok, _ = h.run()
    assert ok is False
    assert len(h.error_contexts) == 2


def test_panel_rejection_still_charges_the_budget():
    h = BarrenHarness([OK], [_reject()], max_attempts=2, max_barren=2)
    ok, _ = h.run()
    assert ok is False
    assert len(h.error_contexts) == 2


# -- feedback and logging ----------------------------------------------------

def test_barren_retry_still_gets_feedback():
    h = BarrenHarness([EMPTY, OK], [APPROVE], verify=[(True, "")],
                      max_attempts=2, max_barren=2)
    h.run()
    assert h.error_contexts[0] is None
    assert "empty response" in (h.error_contexts[1] or "")


def test_barren_retry_is_logged_as_uncharged():
    h = BarrenHarness([EMPTY, OK], [APPROVE], verify=[(True, "")],
                      max_attempts=2, max_barren=2)
    h.run()
    assert any("not charged to the fix budget" in c for c in h.log.calls), h.log.calls


def test_mixed_barren_and_real_attempts():
    """A barren reply between two real attempts must leave both real attempts
    available."""
    h = BarrenHarness([ANCHOR_MISS, EMPTY, OK], [APPROVE], verify=[(True, "")],
                      max_attempts=2, max_barren=2)
    ok, _ = h.run()
    assert ok is True
    assert len(h.error_contexts) == 3


def test_a_run_puts_the_real_modules_back():
    """BarrenHarness.run used to call _namespace() itself and skip the restore,
    so its fake fix_engine stayed in sys.modules for every later test file."""
    before = {n: sys.modules.get(n) for n in ("fix_engine", "git", "github")}
    BarrenHarness([OK], [APPROVE], verify=[(True, "")]).run()
    assert {n: sys.modules.get(n) for n in before} == before


# -- the fix ledger (fix_outcomes) ------------------------------------------

class _FakeLedger:
    """Stands in for the fix_outcomes module and keeps each record() call."""

    def __init__(self):
        self.records = []

    def record(self, repo, number, attempt, model, result, reasons=(), usd=None,
               from_feedback=False, note=""):
        """Keep one attempt's outcome, using the same signature as fix_outcomes.record."""
        self.records.append({"result": result, "reasons": list(reasons),
                             "from_feedback": from_feedback, "note": note})


@pytest.fixture
def ledger(monkeypatch):
    """Swap the fix_outcomes module for a fake that fix_one_pr's lazy import picks up."""
    fake = _FakeLedger()
    monkeypatch.setitem(sys.modules, "fix_outcomes", fake)
    return fake


class RaiseOnCallHarness(BarrenHarness):
    """Harness whose apply_ai_fix raises on the given call numbers (1-based)."""

    def __init__(self, *a, raise_on=(), **kw):
        super().__init__(*a, **kw)
        self.raise_on = set(raise_on)

    def apply_ai_fix(self, repo_path, issue_body, error_context=None, **kw):
        """Record the call, then raise on the scripted call numbers to mimic a failed generation."""
        self.error_contexts.append(error_context)
        if len(self.error_contexts) in self.raise_on:
            raise RuntimeError("model unavailable")
        return f"<<fix-{len(self.error_contexts)}>>"


@pytest.mark.parametrize("barren_kind", ["generation", "parse"])
def test_a_barren_retry_clears_the_feedback_flag(ledger, barren_kind):
    """After a panel reject, a barren retry replaces the critique with its own message,
    so the attempt that finally passes must not be recorded as a feedback retry."""
    rejects = [_reject(), _reject(), APPROVE]
    if barren_kind == "generation":
        h = RaiseOnCallHarness([OK, OK, OK], rejects, verify=[(True, "")],
                               max_attempts=3, max_barren=2, raise_on=[2])
    else:
        h = BarrenHarness([OK, EMPTY, OK], rejects, verify=[(True, "")],
                          max_attempts=3, max_barren=2)
    ok, _ = h.run()
    assert ok is True
    assert ledger.records[-1]["result"] == "pass"
    assert ledger.records[-1]["from_feedback"] is False


def test_a_panel_that_cannot_run_records_an_unscored_attempt(ledger):
    """A queue_for_retry review was charged to the budget, so it must leave a ledger row."""
    h = BarrenHarness([OK], [{"status": "queue_for_retry", "reason": "panel offline"}],
                      max_attempts=2)
    ok, _ = h.run()
    assert ok is False
    assert [r["result"] for r in ledger.records] == ["unscored"]
    assert "panel offline" in ledger.records[0]["note"]


def test_an_unverifiable_sandbox_records_an_unscored_attempt(ledger):
    """A verification-infrastructure failure was charged to the budget, so it must leave a ledger row."""
    h = BarrenHarness([OK], [APPROVE],
                      verify=[(False, "Verification infrastructure unavailable: no python")],
                      max_attempts=2, qa=True)
    ok, _ = h.run()
    assert ok is False
    assert [r["result"] for r in ledger.records] == ["unscored"]

