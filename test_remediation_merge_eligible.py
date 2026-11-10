"""Merge-eligible PRs must not be remediated.

Regression pins for the "remediate -> stale head -> never merge" livelock.

Observed in production (ab.log, 2026-09-26):

    maybe_auto_remediate: lbockenstedt/nw#134 - panel score 0.91 is below the
        0.95 target (deficit 0.04)
    auto_remediate_pr: remediation succeeded ... Fix pushed
    pr_review: remediation pushed a fix; deferring the auto-merge decision to
        the re-review of the new head
    pr_review: auto-merge held ... no pre-review record for the current head yet

The merge bar (feature_automerge_min_confidence, 0.90) sits BELOW the
remediation target (pr_remediate_target_score, 0.95). A PR landing in that gap
was merge-eligible yet still remediated; remediation pushes a commit, the head
moves, the panel results the merge gate reads go stale, and the cycle repeats.
Such PRs never merged - they burned the attempt budget and sometimes regressed
an Approve into a Reject.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pr_remediate import _clears_merge_bar, remediation_pending, should_remediate

# Auto-merge enabled at the fleet's real bar.
CFG = {"feature_automerge_min_confidence": 0.90}


def _rec(conf1=0.91, conf2=0.905, verdict1="Approve", verdict2="Approve", **kw):
    rec = {
        "panel_verdict": verdict1,
        "panel2_verdict": verdict2,
        "panel_confidence": conf1,
        "panel2_confidence": conf2,
        "panel_status": None,
        "panel2_status": None,
        "errors": 0,
        "warnings": 0,
    }
    rec.update(kw)
    return rec


def test_the_exact_nw134_livelock_no_longer_remediates():
    """The production case: 0.91/0.905 against a 0.90 bar and a 0.95 target."""
    should, reason, deficit = should_remediate(_rec(), CFG)
    assert not should, reason
    assert deficit == 0.0
    assert "auto-merge bar" in reason


def test_merge_eligible_pr_is_not_held_back_from_merging():
    """remediation_pending delegates to should_remediate, so the hold lifts."""
    pending, reason = remediation_pending(_rec(), dict(CFG, pr_auto_remediate_enabled=True))
    assert not pending, reason


def test_score_between_the_bars_still_remediates_when_automerge_is_off():
    """Default threshold is 1.0 ("off"): behaviour is exactly as before."""
    should, reason, deficit = should_remediate(_rec(), {})
    assert should, reason
    assert "0.95 target" in reason
    assert deficit > 0


def test_below_the_merge_bar_still_remediates():
    should, reason, _ = should_remediate(_rec(conf1=0.80), CFG)
    assert should, reason
    assert "target" in reason


def test_a_reject_verdict_still_remediates_even_with_high_confidence():
    """A confident Reject is not merge-eligible; it must still be repaired."""
    should, reason, _ = should_remediate(_rec(verdict2="Reject", conf2=0.99), CFG)
    assert should, reason


def test_a_panel_that_could_not_run_is_not_an_approval():
    clears, _ = _clears_merge_bar(_rec(panel2_status="queue_for_retry"), CFG)
    assert not clears


def test_exactly_at_the_bar_clears_it():
    clears, threshold = _clears_merge_bar(_rec(conf1=0.90, conf2=0.90), CFG)
    assert clears
    assert threshold == 0.90


def test_a_hair_under_the_bar_does_not_clear_it():
    clears, _ = _clears_merge_bar(_rec(conf1=0.8999), CFG)
    assert not clears


def test_missing_confidence_is_not_an_approval():
    clears, _ = _clears_merge_bar(_rec(conf1=None), CFG)
    assert not clears


def test_helper_never_raises_on_junk():
    """Junk confidences and thresholds must degrade to "does not clear", not blow up."""
    for rec in (None, {}, _rec(conf1="not-a-number"), _rec(conf2=object())):
        clears, threshold = _clears_merge_bar(rec, CFG)
        assert clears is False
        assert isinstance(threshold, float)
    for cfg in ({"feature_automerge_min_confidence": "junk"},
                {"feature_automerge_min_confidence": None}):
        clears, threshold = _clears_merge_bar(_rec(), cfg)
        assert threshold == 1.0
        assert clears is False


def test_a_zero_threshold_is_honoured_not_rewritten_to_one():
    """0.0 is falsy; reading it with `or 1.0` would silently mean "auto-merge off"."""
    clears, threshold = _clears_merge_bar(_rec(conf1=0.1, conf2=0.1),
                                          {"feature_automerge_min_confidence": 0.0})
    assert threshold == 0.0
    assert clears


def test_threshold_is_clamped_into_range():
    _, hi = _clears_merge_bar(_rec(), {"feature_automerge_min_confidence": 7})
    _, lo = _clears_merge_bar(_rec(), {"feature_automerge_min_confidence": -3})
    assert hi == 1.0
    assert lo == 0.0
