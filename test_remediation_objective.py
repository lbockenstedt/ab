"""
These tests pin AppBuilder's remediation objective: keep driving a PR toward a ~100% panel approval score,
and never act when there is nothing to act on.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pr_remediate import should_remediate

def test_empty_record_does_not_remediate():
    rec = {}
    should, reason, deficit = should_remediate(rec, {})
    assert not should
    assert reason == "no review record yet"
    assert deficit == 0.0

def test_panel_unavailable_does_not_remediate():
    rec = {
        "panel_status": "queue_for_retry",
        "panel2_status": "queue_for_retry",
        "panel_verdict": None,
        "panel2_verdict": None,
        "panel_confidence": None,
        "panel2_confidence": None,
        "errors": 0,
        "warnings": 0
    }
    should, reason, deficit = should_remediate(rec, {})
    assert not should
    assert "could not run" in reason
    assert deficit == 0.0

def test_no_verdict_recorded_does_not_remediate():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": None,
        "panel2_verdict": None,
        "panel_confidence": None,
        "panel2_confidence": None,
        "errors": 0,
        "warnings": 0
    }
    should, reason, deficit = should_remediate(rec, {})
    assert not should
    assert reason == "no panel verdict recorded"
    assert deficit == 0.0

def test_deny_verdict_triggers_remediation():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "Deny",
        "panel2_verdict": "Deny",
        "panel_confidence": 0.85,
        "panel2_confidence": 0.67,
        "errors": 0,
        "warnings": 0
    }
    should, reason, deficit = should_remediate(rec, {})
    assert should
    assert deficit >= 0.95

def test_reject_verdict_is_case_insensitive():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "reject",
        "panel2_verdict": None,
        "panel_confidence": 0.85,
        "panel2_confidence": None,
        "errors": 0,
        "warnings": 0
    }
    should, reason, deficit = should_remediate(rec, {})
    assert should

def test_approve_below_target_triggers_remediation():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "Approve",
        "panel2_verdict": "Approve",
        "panel_confidence": 0.91,
        "panel2_confidence": 0.90,
        "errors": 0,
        "warnings": 0
    }
    should, reason, deficit = should_remediate(rec, {})
    assert should
    assert abs(deficit - 0.05) < 1e-6

def test_approve_at_target_is_left_alone():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "Approve",
        "panel2_verdict": "Approve",
        "panel_confidence": 0.96,
        "panel2_confidence": 0.96,
        "errors": 0,
        "warnings": 0
    }
    should, reason, deficit = should_remediate(rec, {})
    assert not should
    assert deficit == 0.0

def test_tier1_findings_trigger_remediation_even_at_target():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "Approve",
        "panel2_verdict": "Approve",
        "panel_confidence": 0.99,
        "panel2_confidence": 0.99,
        "errors": 1,
        "warnings": 0
    }
    should, reason, deficit = should_remediate(rec, {})
    assert should

def test_custom_target_score_respected():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "Approve",
        "panel2_verdict": "Approve",
        "panel_confidence": 0.91,
        "panel2_confidence": 0.91,
        "errors": 0,
        "warnings": 0
    }
    config = {"pr_remediate_target_score": 0.90}
    should, reason, deficit = should_remediate(rec, config)
    assert not should

def test_malformed_target_falls_back_to_default():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "Approve",
        "panel2_verdict": "Approve",
        "panel_confidence": 0.99,
        "panel2_confidence": 0.99,
        "errors": 0,
        "warnings": 0
    }
    config = {"pr_remediate_target_score": "not-a-number"}
    should, reason, deficit = should_remediate(rec, config)
    assert not should

def test_target_score_is_clamped():
    rec = {
        "panel_status": None,
        "panel2_status": None,
        "panel_verdict": "Approve",
        "panel2_verdict": "Approve",
        "panel_confidence": 0.99,
        "panel2_confidence": 0.99,
        "errors": 0,
        "warnings": 0
    }
    config = {"pr_remediate_target_score": 5.0}
    should, reason, deficit = should_remediate(rec, config)
    assert should
