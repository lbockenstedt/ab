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
    src = _extract("app_state.py", set(), {"record_pr_review", "_panel_dissent_stats"})
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
    {% set comp_verdict = pr.get('composite_verdict') or pr.get('panel_verdict') %}
    {% set avg_conf = pr.get('panel_avg_confidence') if pr.get('panel_avg_confidence') is not none else pr.get('panel_confidence') %}
    {% set p1_conf = pr.get('panel_confidence') %}
    {% set p2_conf = pr.get('panel2_confidence') %}
    {% set am_score = pr.get('auto_merge_score') %}
    {% if comp_verdict %}
    <span class="badge">ADVISORY {{ comp_verdict|upper }}{% if avg_conf is not none %} · {{ (avg_conf * 100)|round|int }}%{% endif %}</span>
    {% endif %}
    {% if pr.get('panel_verdict') and not pr.get('panel_status') %}
    <span>🧠 {{ pr.panel_verdict|upper }}{% if p1_conf is not none %} · {{ (p1_conf * 100)|round|int }}%{% endif %}</span>
    {% endif %}
    {% if pr.get('panel2_verdict') and not pr.get('panel2_status') %}
    <span>🔀 {{ pr.panel2_verdict|upper }}{% if p2_conf is not none %} · {{ (p2_conf * 100)|round|int }}%{% endif %}</span>
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

    # Test legacy PR records without panel_avg_confidence (e.g. pre-existing state entries)
    legacy_out = tmpl.render(pr={
        "panel_verdict": "Approve",
        "panel_confidence": 0.90,
    })
    assert "ADVISORY APPROVE · 90%" in legacy_out
    assert "🧠 APPROVE · 90%" in legacy_out

    # Test completely empty PR record does not raise UndefinedError
    empty_out = tmpl.render(pr={})
    assert empty_out.strip() == ""


@pytest.mark.parametrize("review, runs", [
    ({"verdict": "Approve", "confidence": 0.95}, True),
    ({"verdict": "Reject", "confidence": 0.9}, False),
    ({"verdict": "Retry", "confidence": 0.5}, False),
    ({"status": "queue_for_retry"}, False),
    (None, True),  # panel 1 gave no review (no diff, import failure): panel 2 runs as before
])
def test_state_panel_runs_only_once_panel_1_approves(monkeypatch, review, runs):
    """Panel 2 costs as much as panel 1; a PR panel 1 did not approve cannot
    merge, and its fix re-runs both, so panel 2 waits for panel 1's Approve.
    The skipped panel carries a status, so the merge gate never reads it as
    approved."""
    import main  # noqa: F401
    import pr_remediate
    import pr_review
    calls = []
    monkeypatch.setattr(pr_review, "_state_logic_review",
                        lambda *a, **k: calls.append(1) or {"verdict": "Approve", "confidence": 0.95})
    state = pr_review._state_panel_after(review, None, [], {"pr_review_llm_enabled": True,
                                                            "pr_review_state_logic_enabled": True})
    assert bool(calls) is runs
    if not runs:
        assert state["status"] == "skipped"
        rec = {"panel_verdict": "Approve", "panel_confidence": 1.0,
               "panel2_status": state["status"]}
        assert pr_remediate._clears_merge_bar(rec, {})[0] is False


def test_a_skipped_panel_2_shows_panel_1s_verdict_not_an_outage(pr_env):
    """A panel 2 waiting on panel 1 is not a panel that could not run: the
    PR's headline keeps panel 1's REJECT instead of 'PANEL N/A'. Renders the
    real template lines for a real record."""
    record_pr_review, state = pr_env
    record_pr_review("o/r", 5, "PR 5", "http://pr/5", [], "h5",
                     review={"verdict": "Reject", "confidence": 0.6, "critique": "bug"},
                     review2={"status": "skipped", "reason": "runs once panel 1 Approves"})
    pr = state["pr_reviews"]["o/r#5"]
    src = open("templates/index.html", encoding="utf-8").read()
    start = src.index("{% set comp_verdict")
    headline = src[start:src.index("{% endif %}", src.index("</span>", src.index("ADVISORY {{", start))) + len("{% endif %}")]
    start = src.index("{% if pr.get('panel2_status') == 'skipped' %}")
    panel2 = src[start:src.index("{% elif pr.get('panel2_critique') %}", start)] + "{% endif %}"
    env = jinja2.Environment()

    out = env.from_string(headline + panel2).render(pr=pr)

    assert "ADVISORY REJECT" in out and "N/A" not in out
    assert "runs once the skeptical review Approves" in out and "unavailable" not in out


def test_panel_2_still_runs_alone_when_panel_1_is_off(monkeypatch):
    """The two panels are enabled independently: with panel 1 off, panel 2
    must not wait for an Approve that can never come."""
    import main  # noqa: F401
    import pr_review
    monkeypatch.setattr(pr_review, "_state_logic_review", lambda *a, **k: {"verdict": "Approve"})
    reject = {"verdict": "Reject", "confidence": 0.9}
    on = {"pr_review_llm_enabled": True, "pr_review_state_logic_enabled": True}
    assert pr_review._state_panel_after(reject, None, [], dict(on, pr_review_llm_enabled=False)) == {"verdict": "Approve"}
    assert pr_review._state_panel_after(reject, None, [], on)["status"] == "skipped"


def test_the_wait_has_a_knob_registered_on_by_default(monkeypatch):
    """Fleet rule 10.4: every AB feature has a config knob. Off, both panels
    run on every pass, as before the wait."""
    import main  # noqa: F401
    import pr_review
    monkeypatch.setattr(pr_review, "_state_logic_review", lambda *a, **k: {"verdict": "Approve"})
    on = {"pr_review_llm_enabled": True, "pr_review_state_logic_enabled": True}
    reject = {"verdict": "Reject", "confidence": 0.9}
    assert pr_review._state_panel_after(reject, None, [], dict(on, pr_review_state_panel_waits=False)) == {"verdict": "Approve"}
    routes = open("routes.py", encoding="utf-8").read()
    assert routes.count('config.setdefault("pr_review_state_panel_waits", True)') == 2  # both default blocks


def test_a_switched_off_panel_2_is_off_not_skipped(monkeypatch):
    """With panel 2 disabled it must stay off (None from _state_logic_review),
    not be recorded as waiting for panel 1 when it never will run."""
    import main  # noqa: F401
    import pr_review
    monkeypatch.setattr(pr_review, "_state_logic_review", lambda *a, **k: None)
    assert pr_review._state_panel_after({"verdict": "Reject", "confidence": 0.9}, None, [],
                                        {"pr_review_llm_enabled": True}) is None


def test_the_pr_comment_says_a_skipped_panel_2_waits_not_that_it_is_unavailable():
    import main  # noqa: F401
    import pr_review
    body = "\n".join(pr_review._render_panel({"status": "skipped", "reason": "runs once panel 1 Approves"}))
    assert "runs once panel 1 Approves" in body and "unavailable" not in body


def test_panel_2s_first_retry_is_not_counted_as_panel_1s_second(pr_env):
    """Panel 1 queues, then on its retry Approves and panel 2 runs for the
    first time and queues: that is panel 2's first retry, so the shared cap
    must not already read 2."""
    record_pr_review, state = pr_env
    record_pr_review("o/r", 7, "PR 7", "http://pr/7", [], "h7",
                     review={"status": "queue_for_retry"}, review2={"status": "skipped"})
    assert state["pr_reviews"]["o/r#7"]["queue_retry_count"] == 1
    record_pr_review("o/r", 7, "PR 7", "http://pr/7", [], "h7",
                     review={"verdict": "Approve", "confidence": 0.95}, review2={"status": "queue_for_retry"})
    assert state["pr_reviews"]["o/r#7"]["queue_retry_count"] == 1
    record_pr_review("o/r", 7, "PR 7", "http://pr/7", [], "h7",
                     review={"verdict": "Approve", "confidence": 0.95}, review2={"status": "queue_for_retry"})
    assert state["pr_reviews"]["o/r#7"]["queue_retry_count"] == 2


def test_panels_taking_turns_to_queue_still_reach_the_retry_cap(pr_env):
    """Panel 1 down, then panel 2 down, then panel 1 down again at one head:
    the cap must still bind, or AB re-runs both panels every poll."""
    record_pr_review, state = pr_env
    up = {"verdict": "Approve", "confidence": 0.95}
    down = {"status": "queue_for_retry"}
    for r1, r2 in ((down, {"status": "skipped"}), (up, down), (down, {"status": "skipped"})):
        record_pr_review("o/r", 8, "PR 8", "u", [], "h8", review=r1, review2=r2)
    assert state["pr_reviews"]["o/r#8"]["queue_retry_count"] == 2
    record_pr_review("o/r", 8, "PR 8", "u", [], "h8", review=up, review2=up)
    assert state["pr_reviews"]["o/r#8"]["queue_retry_count"] == 0


def test_the_retry_cap_reads_the_panel_that_is_queued_now(pr_env):
    """Panel 1 used both retries, then Approves and panel 2 queues for the
    first time: panel 2 still gets its second try."""
    record_pr_review, state = pr_env
    down, skip = {"status": "queue_for_retry"}, {"status": "skipped"}
    for r1, r2 in ((down, skip), (down, skip), ({"verdict": "Approve", "confidence": 0.95}, down)):
        record_pr_review("o/r", 11, "PR 11", "u", [], "h", review=r1, review2=r2)
    assert state["pr_reviews"]["o/r#11"]["queue_retry_count"] == 1


def test_panel_1_down_and_panel_2_waiting_is_panels_na():
    """Neither panel gave a verdict: the headline says both are missing."""
    src = open("templates/index.html", encoding="utf-8").read()
    start = src.index("{% set p2_down")
    badge = src[start:src.index("{% elif comp_verdict %}", start)] + "{% endif %}"
    out = jinja2.Environment().from_string(badge).render(
        pr={"panel_status": "queue_for_retry", "panel2_status": "skipped"})
    assert "PANELS N/A" in out


def test_the_wait_knob_has_a_settings_control():
    """Fleet rule: UI for everything, so the knob is not config-file only."""
    src = open("templates/index.html", encoding="utf-8").read()
    assert 'name="pr_review_state_panel_waits"' in src
    assert ('config_data["pr_review_state_panel_waits"] = data.get("pr_review_state_panel_waits") == "on"'
            in open("routes.py", encoding="utf-8").read())
