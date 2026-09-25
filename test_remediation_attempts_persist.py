"""Self-test: remediation bookkeeping survives the per-scan record rebuild.

`record_pr_review` REBUILDS the whole per-PR record on every poll, carrying only
an explicit set of fields forward from the previous one. The remediation fields
are written by `pr_remediate` through `update_pr_review` and were not in that
set, so every scan reset the attempt counter, the terminal
"exhausted_human_review" state and the failed-model exclusion list.

The effect was a livelock rather than a crash: `remediation_pending` never saw
the ceiling bind, so it held the auto-merge forever while re-remediating the
same PR with the same model each cycle. Sixteen promotion PRs across the fleet
sat open that way.

The head-change case below is the load-bearing one: a remediation attempt PUSHES
A COMMIT, so the head always moves between attempts. Resetting the budget on head
movement would make it un-spendable and reinstate the loop.
"""
import ast
import threading
from datetime import datetime

import pytest

from pr_remediate import remediation_pending


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
    # Isolated AST extraction matches test_dual_panel_composite_scoring to test
    # record_pr_review / update_pr_review without importing main.py's full runtime;
    # _panel_dissent_stats is extracted because record_pr_review calls it directly
    # to compute reviewer dissent and rating aggregates.
    src = _extract("app_state.py",
                   {"record_pr_review", "_panel_dissent_stats", "update_pr_review"})
    state = {"pr_reviews": {}}
    ns = {
        "state": state,
        "_task_state_lock": threading.Lock(),
        "datetime": datetime,
        "save_pr_reviews": lambda x: None,
        "_PR_REVIEWS_MAX": 100,
        "logger": _DummyLogger(),
    }
    exec(src, ns)
    return ns["record_pr_review"], ns["update_pr_review"], state


def test_attempts_survive_rescan_at_same_head(pr_env):
    record, update, state = pr_env
    record("o/r", 1, "t", "u", [], "sha1")
    update("o/r", 1, remediation_attempts=2, auto_remediate_status="failed",
           excluded_models=["m1"])
    record("o/r", 1, "t", "u", [], "sha1")
    rec = state["pr_reviews"]["o/r#1"]
    assert rec["remediation_attempts"] == 2
    assert rec["auto_remediate_status"] == "failed"
    assert rec["excluded_models"] == ["m1"]


def test_attempts_survive_head_change(pr_env):
    """The core of the livelock: our own fix commit moves the head."""
    record, update, state = pr_env
    record("o/r", 1, "t", "u", [], "sha1")
    update("o/r", 1, remediation_attempts=2, auto_remediate_status="failed",
           excluded_models=["m1"])
    record("o/r", 1, "t", "u", [], "sha2")
    rec = state["pr_reviews"]["o/r#1"]
    assert rec["remediation_attempts"] == 2
    assert rec["excluded_models"] == ["m1"]


def test_terminal_exhausted_state_survives_head_change(pr_env):
    record, update, state = pr_env
    record("o/r", 1, "t", "u", [], "sha1")
    update("o/r", 1, auto_remediate_status="exhausted_human_review")
    record("o/r", 1, "t", "u", [], "sha2")
    assert state["pr_reviews"]["o/r#1"]["auto_remediate_status"] == "exhausted_human_review"


def test_guardrail_block_survives_head_change(pr_env):
    record, update, state = pr_env
    record("o/r", 1, "t", "u", [], "sha1")
    update("o/r", 1, auto_remediate_blocked=True)
    record("o/r", 1, "t", "u", [], "sha2")
    assert state["pr_reviews"]["o/r#1"]["auto_remediate_blocked"] is True


def test_fresh_pr_starts_with_a_full_budget(pr_env):
    record, _update, state = pr_env
    record("o/r", 2, "t", "u", [], "sha1")
    rec = state["pr_reviews"]["o/r#2"]
    assert rec["remediation_attempts"] == 0
    assert rec["excluded_models"] == []
    assert rec["auto_remediate_status"] is None


def test_ceiling_binds_once_attempts_persist(pr_env):
    """What the wiped counter prevented: the gate releasing the merge once attempts persist."""
    record, update, state = pr_env
    record("o/r", 1, "t", "u", [], "sha1")
    update("o/r", 1, remediation_attempts=3)
    # Record rebuild across a push (head moves sha1 -> sha2) carries the attempt count forward
    review = {"verdict": "Approve", "confidence": 0.90, "critique": "ok"}
    record("o/r", 1, "t", "u", [], "sha2", review=review, review2=review)
    rec = state["pr_reviews"]["o/r#1"]
    assert rec["remediation_attempts"] == 3
    pending, why = remediation_pending(
        rec, {"pr_auto_remediate_enabled": True, "pr_auto_remediate_max_attempts": 3})
    assert pending is False
    assert "exhausted" in why
