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
import pytest

from test_pr_fix_retry_feedback import APPROVE, Harness, _reject


class BarrenHarness(Harness):
    """Harness that can also set pr_fix_max_barren_retries."""

    def __init__(self, *a, max_barren=2, **kw):
        super().__init__(*a, **kw)
        self.max_barren = max_barren

    def run(self):
        ns = self._namespace()
        return ns["fix_one_pr"]("o/r", 7, config={
            "qa_enabled": self.qa,
            "pr_fix_max_attempts": self.max_attempts,
            "pr_fix_max_barren_retries": self.max_barren,
            "GITHUB_TOKEN": "tok",
        })


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
