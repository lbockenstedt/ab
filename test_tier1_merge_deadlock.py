"""A PR that both panels approved, but which carries Tier-1 findings, must
still be remediated.

Observed live on lbockenstedt/lm#1058, which sat open across three consecutive
scans (23:22, 02:33, 07:29) without ever being repaired or merged:

    pr_review    - auto-merge held: Tier-1 findings present (errors or warnings)
    should_remediate - False: "panel score already clears the 0.90 auto-merge
                       bar -- merging beats chasing the 0.95 target"

Two gates, each individually reasonable, that together form a hard deadlock.
The merge gate refuses to merge while findings are open; the remediation gate
refuses to remediate because it believes the PR is already merge-eligible. It
is not -- the merge gate is refusing it. Nothing moves, and the PR is stranded
permanently, which `remediation_pending`'s docstring explicitly promises can
never happen ("can only ever delay a merge by a bounded number of remediation
attempts and can never deadlock a PR").

The fix orders the Tier-1 check ahead of the merge-bar early return. These
tests fail against the old ordering.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pr_remediate import should_remediate

# Target above the merge bar, mirroring production: remediate toward 0.95,
# auto-merge at 0.90. The deadlock lives in the gap between the two.
CFG = {"pr_remediate_target_score": 0.95, "feature_automerge_min_confidence": 0.90,
       "feature_automerge": True}


def _approved_with_findings(**extra):
    rec = {"panel_verdict": "Approve", "panel_confidence": 0.93,
           "panel2_verdict": "Approve", "panel2_confidence": 0.93,
           "panel_dissents": 0, "panel2_dissents": 0,
           "panel_unrated": 0, "panel2_unrated": 0,
           "errors": 1, "warnings": 0}
    rec.update(extra)
    return rec


def test_approved_pr_with_tier1_errors_is_still_remediated():
    """The exact lm#1058 state: unanimous Approve, confidence over the merge
    bar, one Tier-1 error. The merge gate will not take it, so remediation
    must."""
    should, reason, _ = should_remediate(_approved_with_findings(), CFG)
    assert should is True, "deadlock: %s" % reason
    assert "Tier-1 findings present" in reason


def test_approved_pr_with_only_tier1_warnings_is_still_remediated():
    """pr_review holds on errors OR warnings, so warnings alone deadlock too."""
    rec = _approved_with_findings(errors=0, warnings=2)
    should, reason, _ = should_remediate(rec, CFG)
    assert should is True, "deadlock: %s" % reason
    assert "Tier-1 findings present" in reason


def test_tier1_reason_is_reported_not_merely_a_true_verdict():
    """Returning True for the wrong reason picks the wrong remediation tier and
    misreports the cause in the log, so the reason itself is part of the
    contract."""
    _, reason, _ = should_remediate(_approved_with_findings(), CFG)
    assert "clears the" not in reason
    assert "Tier-1" in reason


def test_clean_approved_pr_above_the_merge_bar_is_still_left_alone():
    """The guard must not become "always remediate". With no findings, a
    merge-eligible PR is done -- remediating it would push a commit and
    invalidate the review, the livelock the early return exists to prevent."""
    rec = _approved_with_findings(errors=0, warnings=0)
    should, reason, deficit = should_remediate(rec, CFG)
    assert should is False, reason
    assert deficit == 0.0


def test_findings_clearing_releases_the_pr_to_merge():
    """Termination: remediation must stop once the findings are gone, or the
    fix trades a deadlock for a livelock."""
    assert should_remediate(_approved_with_findings(), CFG)[0] is True
    repaired = _approved_with_findings(errors=0, warnings=0)
    assert should_remediate(repaired, CFG)[0] is False


def test_findings_with_non_approve_verdict_keeps_the_full_target_floor():
    """A DENY that also carries findings is the most severe state there is; the
    Tier-1 branch must not report it as a cheaper job than the verdict branch
    would have."""
    rec = _approved_with_findings(panel_verdict="Reject", panel_confidence=None,
                                  panel2_confidence=None)
    should, reason, deficit = should_remediate(rec, CFG)
    assert should is True, reason
    assert deficit == CFG["pr_remediate_target_score"]


def test_findings_with_dissent_keeps_the_half_target_floor():
    rec = _approved_with_findings(panel_dissents=1)
    should, reason, deficit = should_remediate(rec, CFG)
    assert should is True, reason
    assert deficit >= round(CFG["pr_remediate_target_score"] / 2.0, 4)


def test_no_state_is_both_unmergeable_and_unremediable():
    """The invariant the deadlock violated, checked across the whole grid:
    whenever pr_review would hold the merge for Tier-1 findings, remediation
    must be willing to act."""
    for errors in (0, 1, 3):
        for warnings in (0, 1, 3):
            for conf in (0.91, 0.95, 0.99):
                rec = _approved_with_findings(errors=errors, warnings=warnings,
                                              panel_confidence=conf,
                                              panel2_confidence=conf)
                merge_held = errors > 0 or warnings > 0
                should = should_remediate(rec, CFG)[0]
                if merge_held:
                    assert should is True, (
                        "deadlock at errors=%d warnings=%d conf=%s: merge held "
                        "but remediation declined" % (errors, warnings, conf))
