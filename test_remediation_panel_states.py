"""ab#275 review findings: `should_remediate` must not conflate distinct panel
states, and must not score a missing confidence number as zero.

The state-logic panel denied ab#275 on two concrete defects in this function:

1. The guard ``if panel_status or panel2_status: return False`` collapsed four
   states into one. Panel 1 returning a full DENY while panel 2 merely errored
   was reported as "nothing to act on", silently suppressing remediation for a
   real recommendation -- the exact failure the function exists to eliminate.

2. ``score = min(confs) if confs else 0.0`` gave "no confidence reported" the
   same value as "confidence 0%", making the deficit the full target, forcing
   the largest remediation tier, and making an Approve carrying no number look
   maximally deficient.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pr_remediate import should_remediate

TARGET = {"pr_remediate_target_score": 0.95}


# --------------------------------------------------------------------------
# Defect 1 -- mixed panel states
# --------------------------------------------------------------------------

def test_deny_survives_the_other_panel_erroring():
    """Panel 1 DENIED with a real verdict; panel 2 was unavailable. There IS a
    recommendation to act on, so it must be acted on."""
    rec = {"panel_verdict": "Deny", "panel_confidence": 0.85,
           "panel2_status": "queue_for_retry", "panel2_verdict": None}
    should, reason, deficit = should_remediate(rec, TARGET)
    assert should is True
    assert "below Approve" in reason
    assert deficit == 0.95, "a non-Approve verdict still floors the deficit at the full target"


def test_deny_survives_when_it_is_the_second_panel_that_ran():
    rec = {"panel_status": "provider_error", "panel_verdict": None,
           "panel2_verdict": "Request changes", "panel2_confidence": 0.40}
    should, reason, _ = should_remediate(rec, TARGET)
    assert should is True and "below Approve" in reason


def test_low_score_survives_the_other_panel_erroring():
    """Not just denials -- a surviving Approve below target is still actionable."""
    rec = {"panel_verdict": "Approve", "panel_confidence": 0.60,
           "panel2_status": "timeout", "panel2_verdict": None}
    should, reason, deficit = should_remediate(rec, TARGET)
    assert should is True
    assert "below the 0.95 target" in reason
    assert round(deficit, 4) == 0.35


def test_errored_panel_confidence_is_never_counted():
    """A stale confidence left on a panel that errored must not drag the score
    down (or prop it up): only a usable verdict's confidence counts."""
    rec = {"panel_verdict": "Approve", "panel_confidence": 0.99,
           "panel2_status": "timeout", "panel2_verdict": "Deny", "panel2_confidence": 0.01}
    should, reason, _ = should_remediate(rec, TARGET)
    assert should is False, reason
    assert "already meets" in reason


def test_both_panels_erroring_still_does_nothing():
    """The original behaviour is preserved where it was right: with no usable
    verdict anywhere there is no recommendation, and regenerating a fix against
    an outage would burn tokens and move the head for nothing."""
    rec = {"panel_status": "queue_for_retry", "panel2_status": "queue_for_retry"}
    should, reason, deficit = should_remediate(rec, TARGET)
    assert should is False
    assert "panel could not run" in reason and deficit == 0.0


def test_no_verdict_and_no_status_still_reports_no_verdict():
    rec = {"errors": 0, "warnings": 0}
    should, reason, _ = should_remediate(rec, TARGET)
    assert should is False and reason == "no panel verdict recorded"


# --------------------------------------------------------------------------
# Defect 2 -- unknown confidence is not zero confidence
# --------------------------------------------------------------------------

def test_approve_without_a_confidence_number_is_not_remediated():
    rec = {"panel_verdict": "Approve", "panel_confidence": None,
           "panel2_verdict": "Approve", "panel2_confidence": None,
           "errors": 0, "warnings": 0}
    should, reason, deficit = should_remediate(rec, TARGET)
    assert should is False, reason
    assert "no confidence reported" in reason
    assert deficit == 0.0, "an absent metric must not present as a full-target deficit"


def test_missing_confidence_does_not_inflate_the_deficit_on_a_denial():
    """A denial with no number is still a denial, floored at the full target --
    but via the verdict rule, not by pretending the confidence was 0%."""
    rec = {"panel_verdict": "Deny", "panel_confidence": None}
    should, _, deficit = should_remediate(rec, TARGET)
    assert should is True and deficit == 0.95


def test_unparseable_confidence_is_treated_as_unknown_not_zero():
    rec = {"panel_verdict": "Approve", "panel_confidence": "n/a",
           "errors": 0, "warnings": 0}
    should, reason, deficit = should_remediate(rec, TARGET)
    assert should is False, reason
    assert deficit == 0.0


def test_tier1_findings_still_win_when_confidence_is_unknown():
    """Unknown confidence must not swallow a real Tier-1 finding."""
    rec = {"panel_verdict": "Approve", "panel_confidence": None,
           "errors": 1, "warnings": 0}
    should, reason, _ = should_remediate(rec, TARGET)
    assert should is True and "Tier-1 findings present" in reason


# --------------------------------------------------------------------------
# Unrated seats are an open concern (paired with app_state._panel_dissent_stats)
# --------------------------------------------------------------------------

def test_unrated_seat_is_an_open_concern():
    rec = {"panel_verdict": "Approve", "panel_confidence": 0.99,
           "panel_dissents": 0, "panel_rated": 1, "panel_unrated": 1,
           "errors": 0, "warnings": 0}
    should, reason, _ = should_remediate(rec, TARGET)
    assert should is True
    assert "no usable verdict" in reason


def test_unrated_seat_is_ignored_when_address_all_concerns_is_off():
    rec = {"panel_verdict": "Approve", "panel_confidence": 0.99,
           "panel_unrated": 1, "errors": 0, "warnings": 0}
    cfg = dict(TARGET, pr_remediate_address_all_concerns=False)
    should, reason, _ = should_remediate(rec, cfg)
    assert should is False, reason


def test_fully_rated_unanimous_panel_still_clears():
    rec = {"panel_verdict": "Approve", "panel_confidence": 0.99,
           "panel_dissents": 0, "panel_rated": 2, "panel_unrated": 0,
           "panel_min_reviewer_confidence": 0.98,
           "errors": 0, "warnings": 0}
    should, reason, deficit = should_remediate(rec, TARGET)
    assert should is False, reason
    assert "already meets" in reason and deficit == 0.0
