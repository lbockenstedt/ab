"""Tests for dual-panel review display and composite average confidence scoring."""
import ast
import threading
from datetime import datetime
import jinja2
import pytest


def _extract(path, assigns, funcs):
    src = open(path, encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in assigns for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    return "\n\n".join(segs)


class _DummyLogger:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def exception(self, *a, **k): pass


@pytest.fixture
def pr_env():
    src = _extract("app_state.py", set(), {"record_pr_review"})
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
    return ns["record_pr_review"], state


def test_dual_panel_both_approve(pr_env):
    record_pr_review, state = pr_env
    review1 = {"verdict": "Approve", "confidence": 90, "critique": "Looks great"}
    review2 = {"verdict": "Approve", "confidence": 90, "critique": "Logic sound"}
    record_pr_review("test/repo", 1, "PR 1", "http://pr/1", [], "h1", review=review1, review2=review2)
    
    rec = state["pr_reviews"]["test/repo#1"]
    assert rec["composite_verdict"] == "Approve"
    assert rec["panel_avg_confidence"] == 0.90
    assert rec["panel_verdict"] == "Approve"
    assert rec["panel_confidence"] == 0.90
    assert rec["panel2_verdict"] == "Approve"
    assert rec["panel2_confidence"] == 0.90
    assert rec["panel_critique"] == "Looks great"
    assert rec["panel2_critique"] == "Logic sound"


def test_dual_panel_split_decision(pr_env):
    record_pr_review, state = pr_env
    review1 = {"verdict": "Approve", "confidence": 90, "critique": "Diff is clean"}
    review2 = {"verdict": "Deny", "confidence": 40, "critique": "Unreachable code detected"}
    record_pr_review("test/repo", 2, "PR 2", "http://pr/2", [], "h2", review=review1, review2=review2)
    
    rec = state["pr_reviews"]["test/repo#2"]
    assert rec["composite_verdict"] == "Split"
    assert round(rec["panel_avg_confidence"], 4) == 0.6500
    assert rec["panel_verdict"] == "Approve"
    assert rec["panel_confidence"] == 0.90
    assert rec["panel2_verdict"] == "Deny"
    assert rec["panel2_confidence"] == 0.40


def test_dual_panel_both_deny(pr_env):
    record_pr_review, state = pr_env
    review1 = {"verdict": "Deny", "confidence": 30, "critique": "Syntax error"}
    review2 = {"verdict": "Reject", "confidence": 40, "critique": "State conflation"}
    record_pr_review("test/repo", 3, "PR 3", "http://pr/3", [], "h3", review=review1, review2=review2)
    
    rec = state["pr_reviews"]["test/repo#3"]
    assert rec["composite_verdict"] == "Deny"
    assert round(rec["panel_avg_confidence"], 4) == 0.3500


def test_single_panel_only(pr_env):
    record_pr_review, state = pr_env
    review1 = {"verdict": "Approve", "confidence": 85, "critique": "Panel 1 only"}
    review2 = {"status": "disabled", "reason": "Panel 2 off"}
    record_pr_review("test/repo", 4, "PR 4", "http://pr/4", [], "h4", review=review1, review2=review2)
    
    rec = state["pr_reviews"]["test/repo#4"]
    assert rec["composite_verdict"] == "Approve"
    assert rec["panel_avg_confidence"] == 0.85
    assert rec["panel_status"] == ""
    assert rec["panel2_status"] == "disabled"


def test_template_syntax_and_rendering():
    with open("templates/index.html") as f:
        content = f.read()

    env = jinja2.Environment()
    parsed = env.parse(content)
    assert parsed is not None

    # Render a small slice with the same logic to verify output formatting
    template_str = """
    {% set comp_verdict = pr.composite_verdict or pr.panel_verdict %}
    {% set avg_conf = pr.panel_avg_confidence if pr.panel_avg_confidence is not none else pr.panel_confidence %}
    {% if comp_verdict %}
    <span class="badge">ADVISORY {{ comp_verdict|upper }}{% if avg_conf is not none %} · {{ (avg_conf * 100)|round|int }}%{% endif %}</span>
    {% endif %}
    {% if pr.panel_verdict and not pr.panel_status %}
    <span>🧠 {{ pr.panel_verdict|upper }} · {{ (pr.panel_confidence * 100)|round|int }}%</span>
    {% endif %}
    {% if pr.panel2_verdict and not pr.panel2_status %}
    <span>🔀 {{ pr.panel2_verdict|upper }} · {{ (pr.panel2_confidence * 100)|round|int }}%</span>
    {% endif %}
    """
    tmpl = env.from_string(template_str)
    
    out = tmpl.render(pr={
        "composite_verdict": "Split",
        "panel_avg_confidence": 0.65,
        "panel_verdict": "Approve",
        "panel_confidence": 0.90,
        "panel_status": "",
        "panel2_verdict": "Deny",
        "panel2_confidence": 0.40,
        "panel2_status": "",
    })
    assert "ADVISORY SPLIT · 65%" in out
    assert "🧠 APPROVE · 90%" in out
    assert "🔀 DENY · 40%" in out
