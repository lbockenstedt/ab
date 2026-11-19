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
    # Exhausted means EVERY budget is spent: reaching the ordinary ceiling
    # still buys one premium escalation turn (premium_turn_available) and one
    # consolidated rework (rework_pending).
    rec = _rec(panel_verdict="Deny", remediation_attempts=3, premium_attempts=1,
               remediation_reworks=1)
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
        rec = _rec(panel_verdict="Deny", remediation_attempts=attempts,
                   premium_attempts=1, remediation_reworks=1)
        result = remediation_pending(rec, CONFIG)
        assert result[0] == False
        assert isinstance(result[1], str) and len(result[1]) > 0


def test_premium_turn_extends_the_ceiling_exactly_once():
    """The premium tier adds one rung past the ordinary ceiling -- and the
    ladder must still terminate, at max_attempts + the premium budget (the
    consolidated rework spent too)."""
    for attempts in [3, 4, 9]:
        rec = _rec(panel_verdict="Deny", remediation_attempts=attempts,
                   premium_attempts=0)
        assert remediation_pending(rec, CONFIG)[0] is True, attempts
        spent = _rec(panel_verdict="Deny", remediation_attempts=attempts,
                     premium_attempts=1, remediation_reworks=1)
        pending, why = remediation_pending(spent, CONFIG)
        assert pending is False and "exhausted" in why


def test_premium_turn_can_be_switched_off():
    cfg = dict(CONFIG, pr_remediate_premium_attempts=0)
    rec = _rec(panel_verdict="Deny", remediation_attempts=3, premium_attempts=0,
               remediation_reworks=1)
    assert remediation_pending(rec, cfg)[0] is False

def test_garbage_attempt_values_do_not_raise():
    rec = _rec(panel_verdict="Deny", remediation_attempts="abc")
    config = dict(CONFIG)
    config["pr_auto_remediate_max_attempts"] = "xyz"
    result = remediation_pending(rec, config)
    assert result[0] == True
    assert isinstance(result[1], str) and len(result[1]) > 0


def test_two_heads_without_a_gain_stop_remediation():
    """Soft stop: once the consolidated rework is spent, fixes that stop
    raising the best review hand the PR to a human."""
    rec = _rec(panel_verdict="Deny", remediation_stalls=2, remediation_reworks=1)
    assert remediation_pending(rec, CONFIG) == (False, "remediation stalled: no gain over the best review")
    assert remediation_pending(rec, dict(CONFIG, pr_remediate_stall_limit=0))[0] is True
    assert remediation_pending(_rec(panel_verdict="Deny", remediation_stalls=1), CONFIG)[0] is True


def test_the_stall_limit_has_a_settings_control():
    """Fleet rule: UI for everything."""
    routes = open("routes.py", encoding="utf-8").read()
    assert routes.count('config.setdefault("pr_remediate_stall_limit", 2)') == 2
    assert '"pr_remediate_stall_limit": lambda v:' in routes
    assert 'name="pr_remediate_stall_limit"' in open("templates/index.html", encoding="utf-8").read()


@pytest.mark.parametrize("knob, default", [("pr_remediate_min_gain", 0.03),
                                           ("pr_remediate_rework_limit", 1)])
def test_the_thrash_knobs_have_settings_controls(knob, default):
    """Fleet rule: UI for everything, with a tooltip, and the same default."""
    routes = open("routes.py", encoding="utf-8").read()
    assert routes.count('config.setdefault("%s", %s)' % (knob, default)) == 2
    assert '"%s": lambda v:' % knob in routes
    html = open("templates/index.html", encoding="utf-8").read()
    tag = html[html.rindex("<input", 0, html.index('name="%s"' % knob)):]
    assert "title=" in tag[:tag.index(">")]
    assert "settings.get('%s', %s)" % (knob, default) in tag


@pytest.mark.parametrize("knob, raw, expected", [
    ("pr_remediate_min_gain", "0.05", 0.05), ("pr_remediate_min_gain", "2", 0.5),
    ("pr_remediate_min_gain", "-1", 0.0), ("pr_remediate_min_gain", "x", 0.03),
    ("pr_remediate_rework_limit", "2", 2), ("pr_remediate_rework_limit", "-1", 0),
    ("pr_remediate_rework_limit", "", 1), ("pr_remediate_rework_limit", "9", 3),
])
def test_the_thrash_knobs_are_clamped_on_save(knob, raw, expected):
    """The form value is clamped by the same helper that reads it."""
    from test_feature_settings_roundtrip import _FakeRequest, _base_pairs, _load_ns, _run
    ns = _load_ns()
    ns["_config_holder"]["config"] = {}
    _run(ns["save_settings"](_FakeRequest(_base_pairs(**{knob: raw}))))
    assert ns["_config_holder"]["config"][knob] == expected
