"""A promotion PR is repaired only when the panel named a defect.

pr_auto_remediate_skip_promotion=False is the supported unattended-chain
configuration: it exists so a panel that DENIES a promote/* PR, or files a
Tier-1 finding against it, gets a repair instead of stalling the chain.
backmerge.yml then carries that repair back to the source branch.

lm#1063 showed the flag being read far more broadly than that. The PR was
reviewed "0 findings", every seat Approve, and merely scored 0.69 against the
0.95 remediation target. should_remediate's score branch fired, and the fix
engine — handed a qa->main promotion diff and no finding to address — invented
edits across dhcp/src/*, content that existed on neither dev nor qa.

That cannot converge either: each attempt moves the head, which re-runs the
panel, which scores about the same, which remediates again, drifting the branch
further from its source every pass until the attempt ceiling stops it.

These tests pin the narrowing: a bare confidence deficit is not actionable
evidence, while a denial or an in-repo Tier-1 finding still is.
"""
import re

import pr_remediate

ACTIONABLE = pr_remediate._promotion_evidence_is_actionable


def _rec(**kw):
    base = {"panel_verdict": "Approve", "panel2_verdict": "Approve",
            "errors": 0, "warnings": 0, "tier1_external": 0}
    base.update(kw)
    return base


# --- the lm#1063 shape: nothing to act on -----------------------------------

def test_approve_with_no_findings_is_not_actionable():
    """The exact lm#1063 record: 0 findings, all Approve, low confidence."""
    assert ACTIONABLE(_rec(panel_confidence=0.69, panel2_confidence=0.84)) is False


def test_confidence_is_never_consulted():
    """Confidence describes the reviewers, not the diff, at any value."""
    for conf in (0.0, 0.1, 0.5, 0.69, 0.94):
        assert ACTIONABLE(_rec(panel_confidence=conf)) is False, conf


def test_empty_and_missing_records_are_not_actionable():
    assert ACTIONABLE(None) is False
    assert ACTIONABLE({}) is False


def test_external_only_findings_are_not_actionable():
    """Cross-repo twin findings cannot be cleared by editing this checkout.

    should_remediate already refuses to spend the attempt budget on them; the
    promotion guard must agree, or the two disagree about the same record.
    """
    assert ACTIONABLE(_rec(warnings=5, tier1_external=5)) is False


def test_unparseable_counts_are_not_actionable():
    """Garbage in the counters must not be read as 'there is a defect'."""
    assert ACTIONABLE(_rec(errors="lots")) is False


# --- still actionable: a stated objection -----------------------------------

def test_denial_is_actionable():
    assert ACTIONABLE(_rec(panel_verdict="Deny")) is True
    assert ACTIONABLE(_rec(panel2_verdict="Request changes")) is True


def test_denial_is_actionable_despite_high_confidence():
    """A reviewer 95% sure the PR is wrong is a 0% approval, not a 95% one."""
    assert ACTIONABLE(_rec(panel_verdict="Deny", panel_confidence=0.99)) is True


def test_verdict_match_is_case_and_space_insensitive():
    for ok in ("approve", "  Approve ", "APPROVE"):
        assert ACTIONABLE(_rec(panel_verdict=ok, panel2_verdict=ok)) is False, ok


def test_in_repo_tier1_findings_are_actionable():
    assert ACTIONABLE(_rec(errors=1)) is True
    assert ACTIONABLE(_rec(warnings=1)) is True


def test_mixed_internal_and_external_findings_are_actionable():
    """Only an ALL-external record is inert; one in-repo finding is repairable."""
    assert ACTIONABLE(_rec(warnings=5, tier1_external=4)) is True


# --- the guards actually consult it -----------------------------------------

def _src(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_auto_remediate_guard_consults_the_helper():
    """The flag alone must no longer be the whole condition in pr_remediate."""
    src = _src("pr_remediate.py")
    guard = src[src.index("# Never rewrite a promotion/backmerge PR"):]
    guard = guard[:guard.index("# Guardrails check")]
    assert "_promotion_evidence_is_actionable(rec)" in guard
    # The old shape short-circuited the whole guard on the flag, so with it off
    # the promotion check never ran at all.
    assert not re.search(
        r'config\.get\("pr_auto_remediate_skip_promotion", True\)\s*and\s*_PROMOTION_HEAD_RE',
        guard)


def test_fix_one_pr_guard_consults_the_helper():
    """pr_review carries a twin guard; both read the same flag, so both narrow."""
    src = _src("pr_review.py")
    i = src.index("_PROMOTION_HEAD_RE.match(branch")
    guard = src[i - 400:i + 400]
    assert "_promotion_evidence_is_actionable(_rec)" in guard


def test_promotion_head_match_still_gates_both_guards():
    """Narrowing must not let the guard fire on ordinary feature branches."""
    assert pr_remediate._PROMOTION_HEAD_RE.match("promote/qa-to-main")
    assert pr_remediate._PROMOTION_HEAD_RE.match("backmerge/main-to-dev")
    assert not pr_remediate._PROMOTION_HEAD_RE.match("feature/add-thing")
