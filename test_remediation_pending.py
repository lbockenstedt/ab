#!/usr/bin/env python3
"""Self-test: the "improve before merging" gate (pr_remediate.remediation_pending).

AppBuilder's objective is to get a PR as close to 100% panel approval as it can
BEFORE merging it, so merging a PR it was about to improve defeats the purpose —
and that is exactly what happened, because the merge threshold
(feature_automerge_min_confidence, 0.90) sits BELOW the remediation target
(pr_remediate_target_score, 0.95).

Every "stop trying" condition below (kill switch, guardrail block, exhausted
attempts, target already met) must return not-pending, so this gate can only
ever DELAY a merge by a bounded number of attempts and can never deadlock a PR.

Generated on the local Ollama GPU (qwen3-coder), reviewed and pinned by hand.
"""
import pytest
from pr_remediate import remediation_pending

def _rec(**over):
    r = {"panel_verdict": "Approve", "panel2_verdict": "Approve",
         "panel_confidence": 0.99, "panel2_confidence": 0.99,
         "panel_dissents": 0, "panel2_dissents": 0,
         "errors": 0, "warnings": 0}
    r.update(over)
    return r

CONFIG = {"pr_auto_remediate_enabled": True, "pr_auto_remediate_max_attempts": 3, "pr_remediate_target_score": 0.95}

def test_pending_when_panel_denies():
    rec = _rec(panel_verdict="Deny")
    result = remediation_pending(rec, CONFIG)
    assert result[0] == True
    assert isinstance(result[1], str) and len(result[1]) > 0

def test_pending_when_score_below_target():
    rec = _rec(panel_confidence=0.80)
    result = remediation_pending(rec, CONFIG)
    assert result[0] == True
    assert isinstance(result[1], str) and len(result[1]) > 0

def test_pending_when_individual_reviewer_dissents():
    rec = _rec(panel_dissents=1)
    result = remediation_pending(rec, CONFIG)
    assert result[0] == True
    assert isinstance(result[1], str) and len(result[1]) > 0

def test_not_pending_when_fully_approved():
    rec = _rec()
    result = remediation_pending(rec, CONFIG)
    assert result[0] == False
    assert isinstance(result[1], str) and len(result[1]) > 0

def test_not_pending_when_no_record():
    result1 = remediation_pending(None, CONFIG)
    result2 = remediation_pending({}, CONFIG)
    assert result1[0] == False
    assert isinstance(result1[1], str) and len(result1[1]) > 0
    assert result2[0] == False
    assert isinstance(result2[1], str) and len(result2[1]) > 0

def test_not_pending_when_remediation_disabled():
    config = dict(CONFIG)
    config["pr_auto_remediate_enabled"] = False
    rec = _rec(panel_verdict="Deny")
    result = remediation_pending(rec, config)
    assert result[0] == False
    assert isinstance(result[1], str) and len(result[1]) > 0

def test_not_pending_when_guardrail_blocked():
    rec = _rec(panel_verdict="Deny", auto_remediate_blocked=True)
    result = remediation_pending(rec, CONFIG)
    assert result[0] == False
    assert isinstance(result[1], str) and len(result[1]) > 0

def test_not_pending_when_status_exhausted():
    rec = _rec(panel_verdict="Deny", auto_remediate_status="exhausted_human_review")
    result = remediation_pending(rec, CONFIG)
    assert result[0] == False
    assert isinstance(result[1], str) and len(result[1]) > 0

def test_not_pending_when_attempts_reached_max():
    rec = _rec(panel_verdict="Deny", remediation_attempts=3)
    result = remediation_pending(rec, CONFIG)
    assert result[0] == False
    assert isinstance(result[1], str) and len(result[1]) > 0
    assert "exhausted" in result[1]

def test_attempt_ceiling_bounds_the_delay():
    for attempts in [0, 1, 2]:
        rec = _rec(panel_verdict="Deny", remediation_attempts=attempts)
        result = remediation_pending(rec, CONFIG)
        assert result[0] == True
        assert isinstance(result[1], str) and len(result[1]) > 0

    for attempts in [3, 4, 9]:
        rec = _rec(panel_verdict="Deny", remediation_attempts=attempts)
        result = remediation_pending(rec, CONFIG)
        assert result[0] == False
        assert isinstance(result[1], str) and len(result[1]) > 0

def test_garbage_attempt_values_do_not_raise():
    rec = _rec(panel_verdict="Deny", remediation_attempts="abc")
    config = dict(CONFIG)
    config["pr_auto_remediate_max_attempts"] = "xyz"
    result = remediation_pending(rec, config)
    assert result[0] == True
    assert isinstance(result[1], str) and len(result[1]) > 0
