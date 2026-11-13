"""Dashboard Mission Control: what needs a person, what AppBuilder still owns,
and what finished recently (mission_control.build), and the dashboard that
renders it."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import auth
import main
import mission_control
from app_state import state

NOW = datetime(2026, 11, 12, 12, 0, 0)


def _ago(hours, iso=True):
    t = NOW - timedelta(hours=hours)
    return t.isoformat() if iso else t.strftime("%Y-%m-%d %H:%M:%S")


def _pr(number, hours=1, **kw):
    return {"repo": "o/lm", "number": number, "title": "PR %d" % number, "url": "u",
            "reviewed_at": _ago(hours, iso=False), "items": [], **kw}


def test_open_prs_split_between_a_person_and_appbuilder():
    prs = {
        "o/lm#1": _pr(1, auto_merge_blocked_reason="CI is failing on head abc — needs human review"),
        "o/lm#2": _pr(2, auto_merge_blocked_reason="holding the merge while AppBuilder improves this PR"),
        "o/lm#3": _pr(3, auto_merge_blocked_reason="waiting on GitHub Actions — outage"),
        "o/lm#4": _pr(4, panel_status="queue_for_retry",
                      auto_merge_blocked_reason="panel 1 (skeptical review) did not Approve"),
        "o/lm#5": _pr(5, approved=True, auto_merge_blocked_reason="repo is not in feature_automerge_repos"),
        "o/lm#6": _pr(6, auto_merge_blocked_reason="holding the merge while AppBuilder improves",
                      auto_remediate_status="exhausted_human_review", auto_remediate_reason="no gain in 2 passes"),
        "o/lm#11": _pr(11, approved=True, auto_merge_reason="both panels approve; docs only"),
        "o/lm#12": _pr(12, approved=True, auto_merge_reason="both panels approve",
                       auto_merge_refusal="branch protection requires a review"),
        "o/lm#7": _pr(7, auto_merged=True, merged=True),
        "o/lm#8": _pr(8, denied=True),
        # Last reviewed 10 days ago, merged an hour ago: a recent outcome.
        "o/lm#10": _pr(10, hours=240, merged=True, outcome_at=_ago(1, iso=False)),
    }
    mc = mission_control.build({}, prs, now=NOW)
    needs = {i["number"]: i for i in mc["attention"]}
    # AppBuilder's own approval with a deferred merge stays with AppBuilder;
    # a merge GitHub refused needs a person.
    assert set(needs) == {"1", "5", "6", "12"}
    assert needs["12"]["label"] == "Merge refused" and "branch protection" in needs["12"]["why"]
    assert needs["1"]["why"].startswith("CI is failing")
    assert needs["5"]["label"] == "Approved"
    assert needs["6"]["why"] == "no gain in 2 passes"
    assert {i["number"] for i in mc["in_flight"]} == {"2", "3", "4", "11"}
    assert [(o["number"], o["text"]) for o in mc["outcomes"]] == [("7", "auto-merged"), ("8", "denied"), ("10", "merged")]
    assert mc["kpis"]["auto_merged_7d"] == 1


def test_issues_route_by_status_and_only_recent_finishes_count():
    processed = {
        "o/nw:1": {"status": "ops-alert", "timestamp": _ago(50), "reason": "device down"},
        "o/lm:2": {"status": "pending_verification", "timestamp": _ago(2)},
        "o/lm:3": {"status": "failed", "timestamp": _ago(5), "error": "no edits"},
        "o/lm:4": {"status": "feature_flagged", "timestamp": _ago(9), "boundary_ids": ["auth-rbac"]},
        "o/lm:5": {"status": "processing", "timestamp": _ago(1)},
        "o/lm:6": {"status": "resolved", "timestamp": _ago(3)},
        "o/lm:7": {"status": "closed", "timestamp": _ago(24 * 10)},
        "o/lm:8": {"status": "closed", "timestamp": _ago(4)},
        "o/lm:9": {"status": "awaiting_human", "timestamp": _ago(60)},
        "o/lm:10": {"status": "feature_failed", "timestamp": _ago(70), "reason": "tests failed"},
        "o/lm:11": {"status": "triaged", "timestamp": _ago(80)},
    }
    prs = {"o/lm#9": _pr(9, hours=30, auto_merge_blocked_reason="panel 1 (skeptical review) did not Approve")}
    mc = mission_control.build(processed, prs, {"t": {}}, now=NOW)
    # Held PRs first, then failed, verify, feature, other.
    assert [i["id"] for i in mc["attention"]] == ["o/lm#9", "o/lm:3", "o/lm:2", "o/lm:10", "o/lm:4",
                                                  "o/lm:11", "o/lm:9", "o/nw:1"]
    assert "auth-rbac" in mc["attention"][4]["why"]
    assert mc["attention"][7]["repo"] == "nw" and mc["attention"][7]["age"] == "2d"
    assert mc["attention"][7]["url"] == "https://github.com/o/nw/issues/1"
    assert [i["id"] for i in mc["in_flight"]] == ["o/lm:5"]
    # A plain close is an outcome but not a fix.
    assert [o["id"] for o in mc["outcomes"]] == ["o/lm:6", "o/lm:8"]
    assert mc["kpis"] == {"needs_you": 8, "running": 1, "in_flight": 1, "in_progress": 4,
                          "fixed_7d": 1, "fixed_delta": 1, "auto_merged_7d": 0, "failed_7d": 1,
                          "success_pct": 50, "median_fix_min": None}
    assert mc["groups"] == {"pr": 1, "failed": 1, "verify": 1, "feature": 2, "other": 3}


def test_failed_qa_shows_the_headline_not_the_test_log():
    processed = {"o/lm:1": {"status": "failed", "timestamp": _ago(1), "failure_kind": "qa_failed",
                            "error": "Fix failed QA tests after 3 attempt(s) — == ruff (new errors only)\nruff: 0"}}
    why = mission_control.build(processed, {}, now=NOW)["attention"][0]["why"]
    assert why.startswith("Fix failed QA tests after 3 attempt(s).") and "ruff" not in why


def test_dashboard_renders_attention_with_actions_and_api(monkeypatch):
    monkeypatch.setattr(auth, "any_users", lambda: True)
    monkeypatch.setattr(auth, "verify_session", lambda token: "admin")
    monkeypatch.setitem(state, "processed", {
        "o/lm:3": {"status": "pending_verification", "timestamp": datetime.now().isoformat(), "title": "Heartbeat"}})
    monkeypatch.setitem(state, "pr_reviews", {
        "o/lm#5": _pr(5, auto_merge_blocked_reason="CI is failing on head abc — needs human review")})
    monkeypatch.setitem(state, "active_tasks", {})
    client = TestClient(main.app)
    import routes
    monkeypatch.setattr(routes, "load_config", lambda: {"dashboard_mission_control": False})
    off = client.get("/").text
    assert 'id="mc-attention"' not in off and 'id="activity"' in off
    monkeypatch.setattr(routes, "load_config", lambda: {})
    html = client.get("/").text
    assert "Needs your attention" in html
    assert "CI is failing on head abc" in html
    assert 'data-mc="approve" data-repo="o/lm" data-num="5"' in html
    assert 'data-mc="resolve" data-id="o/lm:3"' in html
    assert "approve: function (b) { approvePr(" in html
    assert client.get("/api/mission-control").json()["kpis"]["needs_you"] == 2


def test_self_clearing_holds_match_the_wording_pr_review_writes():
    src = open(os.path.join(os.path.dirname(mission_control.__file__), "pr_review.py")).read()
    for marker in mission_control._SELF_CLEARING_HOLDS:
        assert marker in src, marker


def test_first_merge_or_deny_stamps_outcome_at(monkeypatch):
    import app_state
    monkeypatch.setattr(app_state, "save_pr_reviews", lambda reviews: None)
    monkeypatch.setitem(state, "pr_reviews", {"o/lm#1": _pr(1)})
    monkeypatch.setattr(app_state, "_record_merge_milestone", lambda key, fields: None)
    app_state.update_pr_review("o/lm", 1, findings=2)
    assert "outcome_at" not in state["pr_reviews"]["o/lm#1"]
    app_state.update_pr_review("o/lm", 1, merged=True)
    first = state["pr_reviews"]["o/lm#1"]["outcome_at"]
    app_state.update_pr_review("o/lm", 1, auto_merged=True)
    assert state["pr_reviews"]["o/lm#1"]["outcome_at"] == first


def test_exhausted_panel_retries_wait_on_a_person_and_requester_questions_stay_in_flight():
    prs = {
        "o/lm#1": _pr(1, panel_status="queue_for_retry", queue_retry_count=1),
        "o/lm#2": _pr(2, panel_status="queue_for_retry", queue_retry_count=2),
    }
    processed = {
        "o/lm:3": {"status": "feature_needs_info", "timestamp": _ago(1)},
        "o/lm:4": {"status": "reopened", "timestamp": _ago(1)},
    }
    start = datetime(2026, 11, 13, 4, 0)
    mc = mission_control.build(processed, prs, {"t1": {"name": "RepoScan", "kind": "scan", "start_time": start}},
                               now=NOW)
    needs = {i["id"]: i for i in mc["attention"]}
    # Retries left: AppBuilder's. Retries used up: nothing retries until a person acts.
    assert needs["o/lm#2"]["label"] == "Panel unavailable"
    # feature_drive re-asks the classifier every cycle: not on the operator.
    assert "o/lm:3" not in needs
    assert sorted(i["id"] for i in mc["in_flight"]) == ["o/lm#1", "o/lm:3", "o/lm:4"]
    assert [(c["id"], c["title"], c["started"]) for c in mc["working"]] == [
        ("t1", "RepoScan", start.astimezone(timezone.utc).isoformat())]


def test_approving_a_pr_swaps_in_a_delegated_merge_button():
    html = open(os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")).read()
    approve = html[html.index("async function approvePr"):]
    approve = approve[:approve.index("\n        }\n")]
    assert "onclick=" not in approve
    assert 'data-mc="merge" data-repo="${repo}" data-num="${number}"' in approve


def test_re_evaluate_accepts_a_feature_waiting_on_its_requester(monkeypatch):
    import routes
    store = {"o/lm:3": {"status": "feature_needs_info", "timestamp": _ago(1)}}
    monkeypatch.setattr(auth, "any_users", lambda: True)
    monkeypatch.setattr(auth, "verify_session", lambda token: "admin")
    monkeypatch.setattr(routes, "load_processed", lambda: store)
    monkeypatch.setattr(routes, "save_processed", lambda p: None)
    monkeypatch.setattr(routes, "recompute_issue_counters", lambda p: None)
    monkeypatch.setitem(state, "processed", store)
    r = TestClient(main.app).post("/reevaluate_feature", json={"issue_id": "o/lm:3"})
    assert r.status_code == 200, r.text
    assert store["o/lm:3"]["status"] == "feature_classify_retry"


def test_a_later_hold_beats_an_old_auto_merge_approval_and_off_switches_read_as_review():
    prs = {
        # AppBuilder approved an older decision; the latest one holds the merge.
        "o/lm#1": _pr(1, approved=True, auto_merge_reason="both panels approve",
                      auto_merge_blocked_reason="CI is failing on head abc — needs human review"),
        # Auto-merge is switched off: a normal review, not a held merge.
        "o/lm#2": _pr(2, auto_merge_blocked_reason="feature_automerge_enabled is off"),
        "o/lm#3": _pr(3, auto_merge_blocked_reason="target branch 'main' is a release branch — never eligible for auto-merge"),
    }
    needs = {i["number"]: i for i in mission_control.build({}, prs, now=NOW)["attention"]}
    assert needs["1"]["label"] == "Auto-merge held"
    assert needs["2"]["label"] == needs["3"]["label"] == "Awaiting review"
    assert "feature_automerge_enabled is off" in needs["2"]["why"]


def test_not_applicable_holds_match_the_wording_pr_review_writes():
    src = open(os.path.join(os.path.dirname(mission_control.__file__), "pr_review.py")).read()
    for marker in mission_control._NOT_APPLICABLE_HOLDS:
        assert marker in src, marker


def test_release_lock_blackout_and_approved_improving_are_appbuilders():
    prs = {
        "o/lm#1": _pr(1, auto_merge_blocked_reason="target branch 'qa' is under a release lock — holding"),
        "o/lm#2": _pr(2, auto_merge_blocked_reason="AppBuilder is in a blackout window"),
        # A person approved it while AppBuilder is still improving it.
        "o/lm#3": _pr(3, approved=True,
                      auto_merge_blocked_reason="holding the merge while AppBuilder improves this PR"),
    }
    mc = mission_control.build({}, prs, now=NOW)
    assert mc["attention"] == []
    assert sorted(i["number"] for i in mc["in_flight"]) == ["1", "2", "3"]


# ---- Working on now: live task cards, in-flight cards and the progress hooks ----

def _states(steps):
    return [s["state"][0] for s in steps]  # d(one) / c(urrent) / t(odo)


@pytest.fixture
def tasks(monkeypatch):
    """A clean active_tasks store for the progress hooks."""
    monkeypatch.setitem(state, "active_tasks", {})
    return state["active_tasks"]


def test_fix_task_before_the_panels_marks_triage_done_and_fix_current(tasks):
    import app_state
    app_state.update_task_state("o/lm:5", "Triaging o/lm:5", stage="triage")
    app_state.update_task_state("o/lm:5", "Fix Attempt 1/3 for o/lm:5", stage="fix")
    app_state.set_task_step("o/lm:5", "attempt 1/3: generating the fix", model="qwen3.8-27b")
    card = mission_control.task_card("o/lm:5", tasks["o/lm:5"], {"o/lm:5": {"status": "processing", "title": "T"}}, {}, NOW)
    assert _states(card["steps"]) == ["d", "c", "t", "t", "t", "t"]
    assert (card["kind"], card["title"], card["desc"]) == ("Bug fix", "lm #5", "T")
    assert card["now"] == "attempt 1/3: generating the fix"


def test_tests_after_review_keep_the_review_step_done(tasks):
    import app_state
    for name, stage in (("Triaging", "triage"), ("Fix Attempt 1/3", "fix"), ("Reviewing", "review"), ("Verifying", "tests")):
        app_state.update_task_state("o/lm:5", name, stage=stage)
    card = mission_control.task_card("o/lm:5", tasks["o/lm:5"], now=NOW)
    assert _states(card["steps"]) == ["d", "d", "c", "d", "t", "t"]


def test_a_retry_attempt_forgets_the_steps_the_last_attempt_failed(tasks):
    import app_state
    for name, stage in (("Triaging", "triage"), ("Fix Attempt 1/3", "fix"), ("Reviewing", "review"),
                        ("Verifying", "tests"), ("Fix Attempt 2/3", "fix")):
        app_state.update_task_state("o/lm:5", name, stage=stage)
    card = mission_control.task_card("o/lm:5", tasks["o/lm:5"], now=NOW)
    assert _states(card["steps"]) == ["d", "c", "t", "t", "t", "t"]


def test_a_task_with_no_recorded_stage_takes_it_from_its_phase_name():
    card = mission_control.task_card("o/lm:5", {"name": "Reviewing o/lm:5"}, now=NOW)
    assert _states(card["steps"]) == ["d", "d", "d", "c", "t", "t"]
    # Not a pipeline task: no stepper.
    assert mission_control.task_card("Scan", {"name": "Repo scan"}, now=NOW)["steps"] is None


def test_single_repo_pr_scan_shows_the_pr_without_a_repo_counter(tasks):
    import app_state
    app_state.update_task_state("PRReview", "Reviewing o/nw PR #91 (1/1)", kind="pr")
    app_state.set_task_step("PRReview", "repo 1/1 — o/nw: listing open PRs", scope="")
    app_state.set_task_step("PRReview", "o/nw #91 — checking CI", subject="o/nw#91", stage="triage")
    app_state.set_task_step("PRReview", "o/nw #91 — panel 1 reviewing", stage="review", model="claude-opus-5")
    prs = {"o/nw#91": {"title": "Fix MAC", "url": "u91", "head_ref": "dev", "base_ref": "qa"}}
    card = mission_control.task_card("PRReview", tasks["PRReview"], {}, prs, NOW)
    assert (card["kind"], card["title"], card["desc"], card["url"]) == ("Promotion", "nw #91", "Fix MAC", "u91")
    assert card["now"] == "o/nw #91 — panel 1 reviewing"
    # Triaged and in review; the PR exists; nothing was fixed or tested by this scan.
    assert _states(card["steps"]) == ["d", "t", "t", "c", "d", "t"]
    assert card["models"] == "claude-opus-5"


def test_now_line_joins_the_scan_scope_and_falls_back_to_the_model_stream():
    card = mission_control.task_card("PRReview", {"name": "x", "scope": "repo 3/4", "step": "netbox: nw #91 checking CI"}, now=NOW)
    assert card["now"] == "repo 3/4 — netbox: nw #91 checking CI"
    card = mission_control.task_card("t", {"name": "x", "stream": "thinking\nchecking the parser\n\n"}, now=NOW)
    assert card["now"] == "checking the parser"


def test_model_chain_lists_each_model_once_in_order(tasks):
    import app_state
    app_state.update_task_state("o/lm:5", "Fix Attempt 1/3", stage="fix")
    for m in ("qwen3.8-27b", "claude-opus-5", "qwen3.8-27b"):
        app_state.set_task_step("o/lm:5", model=m)
    assert mission_control.task_card("o/lm:5", tasks["o/lm:5"], now=NOW)["models"] == "qwen3.8-27b → claude-opus-5"


def test_a_new_subject_starts_a_fresh_stage_and_model_history(tasks):
    import app_state
    app_state.update_task_state("PRReview", "scan", kind="pr")
    app_state.set_task_step("PRReview", subject="o/lm#1", stage="review", model="m1")
    app_state.set_task_step("PRReview", subject="o/lm#2")
    assert (tasks["PRReview"]["stage"], tasks["PRReview"]["stages"], tasks["PRReview"]["models"]) == ("", [], [])


def test_ci_checks_seen_are_cached_for_the_dashboard(monkeypatch):
    import pr_actions
    monkeypatch.setattr(pr_actions, "CI_SEEN", {})
    runs = [{"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "cancelled"},
            {"status": "completed", "conclusion": "failure"}, {"status": "in_progress"}]
    pr_actions._remember_ci("o/lm", "abc", runs, ["lint: failure"])
    assert {k: v for k, v in pr_actions.CI_SEEN[("o/lm", "abc")].items() if k != "at"} == {
        "done": 3, "passed": 1, "total": 4, "failed": 1}


@pytest.mark.parametrize("pr, ci, step, now_text", [
    (dict(auto_merge_blocked_reason="holding the merge while AppBuilder improves this PR", remediation_attempts=1),
     None, "fix", "improving: attempt 2/3"),
    (dict(auto_merge_blocked_reason="holding the merge while AppBuilder improves this PR", head="h",
          auto_merge_reason="both approve"), {"done": 2, "total": 6, "failed": 0}, "fix", "improving: attempt 1/3"),
    (dict(approved=True, auto_merge_reason="both panels approve", head="h"), {"done": 2, "total": 6, "failed": 0},
     "tests", "waiting for CI (2/6 checks done)"),
    (dict(approved=True, auto_merge_reason="both panels approve", head="h"),
     {"done": 6, "passed": 0, "total": 6, "failed": 0}, "tests", "CI finished without a verdict (0/6 passed) — Re-run CI"),
    (dict(panel_status="queue_for_retry", queue_retry_count=1), None, "review", "queued: next PR scan in 12m"),
    (dict(auto_merge_blocked_reason="holding the merge while AppBuilder improves", remediation_attempts=4,
          auto_remediate_status="exhausted_human_review"), None, "fix", "held: remediation limit 4/3, needs human"),
    (dict(auto_merge_blocked_reason="CI is failing on head h — needs human review", head="h"),
     {"done": 6, "total": 6, "failed": 1}, "tests", "held: CI failing (1 of 6 checks failed)"),
    (dict(approved=True, auto_merge_blocked_reason="repo is not in feature_automerge_repos"), None, "merge",
     "approved — waiting for a Merge click"),
    (dict(head_ref="dev", base_ref="qa"), None, "review", "awaiting review before promotion to qa"),
])
def test_each_open_pr_state_maps_to_its_step_and_now_text(pr, ci, step, now_text):
    prs = {"o/lm#1": _pr(1, hours=2, **pr)}
    ci_seen = {("o/lm", "h"): ci} if ci else {}
    cards = mission_control.flight_cards({}, prs, {}, ci_seen, NOW.timestamp() + 12 * 60, 3, NOW)
    current = [s["key"] for s in cards[0]["steps"] if s["state"] == "current"]
    assert (current, cards[0]["now"], cards[0]["for"]) == ([step], now_text, "2h 0m")


@pytest.mark.parametrize("status, step, now_text", [
    ("pending_verification", None, "merged, awaiting verification"),
    ("processing", "fix", "fixing — continues on the next scan (in 12m)"),
    ("reopened", "triage", "reopened — fixing again on the next scan (in 12m)"),
    ("awaiting_review", "review", "queued: waiting for reviewers"),
    ("feature_built", "review", "feature built — PR open, awaiting review"),
])
def test_each_issue_in_flight_maps_to_its_step_and_now_text(status, step, now_text):
    processed = {"o/lm:4": {"status": status, "timestamp": _ago(1)}}
    cards = mission_control.flight_cards(processed, {}, {}, {}, NOW.timestamp() + 12 * 60, 3, NOW)
    current = [s["key"] for s in cards[0]["steps"] if s["state"] == "current"]
    assert (current, cards[0]["now"], cards[0]["for"]) == ([step] if step else [], now_text, "1h 0m")


def test_running_items_and_finished_prs_are_not_in_flight():
    prs = {"o/lm#1": _pr(1), "o/lm#2": _pr(2, merged=True)}
    cards = mission_control.flight_cards({"o/lm:3": {"status": "processing"}}, prs,
                                         {"o/lm:3": {}, "PRReview": {"subject": "o/lm#1"}}, now=NOW)
    assert cards == []


def test_time_in_state_resets_only_when_the_state_changes():
    held = _pr(1, auto_merge_blocked_reason="CI is failing on head h — needs human review")
    prev = dict(held)
    mission_control.stamp_state_since(prev, None, NOW.replace(hour=9))
    rec = dict(held)
    mission_control.stamp_state_since(rec, prev, NOW)
    assert rec["state_since"] == "2026-11-12 09:00:00"
    rec = dict(held, approved=True, auto_merge_blocked_reason="", auto_merge_reason="ok")
    mission_control.stamp_state_since(rec, prev, NOW)
    assert rec["state_since"] == "2026-11-12 12:00:00" and not rec["state_label"].startswith("Auto-merge held")
    # Same label, new reason: a new state.
    rec = dict(held, auto_merge_blocked_reason="panel 2 did not Approve")
    mission_control.stamp_state_since(rec, prev, NOW)
    assert rec["state_since"] == "2026-11-12 12:00:00"


def test_kpis_compare_weeks_and_take_the_median_fix_time():
    processed = {
        "o/lm:1": {"status": "fixed", "timestamp": _ago(1), "opened_at": _ago(2)},
        "o/lm:2": {"status": "verified", "timestamp": _ago(5), "opened_at": _ago(5.5)},
        "o/lm:3": {"status": "resolved", "timestamp": _ago(6)},
        "o/lm:4": {"status": "failed", "timestamp": _ago(3)},
        "o/lm:5": {"status": "fixed", "timestamp": _ago(24 * 9)},
    }
    k = mission_control.build(processed, {}, now=NOW)["kpis"]
    assert (k["fixed_7d"], k["fixed_delta"], k["failed_7d"], k["success_pct"]) == (3, 2, 1, 75)
    assert k["median_fix_min"] == 45 and mission_control.fmt_minutes(k["median_fix_min"]) == "45m"
    assert mission_control.fmt_minutes(mission_control.build({}, {}, now=NOW)["kpis"]["median_fix_min"]) == "—"


def test_attention_labels_fit_the_state():
    processed = {
        "o/lm:1": {"status": "failed", "timestamp": _ago(1), "attempts": 2, "max_attempts": 3,
                   "failure_raw_excerpt": "model said no"},
        "o/lm:2": {"status": "feature_flagged", "timestamp": _ago(1), "flag_kind": "boundary", "boundary_ids": ["auth-rbac"]},
    }
    prs = {
        "o/lm#3": _pr(3, panel_verdict="Approve", panel_confidence=0.8, panel2_verdict="Reject", panel2_confidence=0.7,
                      auto_merge_blocked_reason="panel 2 did not Approve"),
        "o/lm#4": _pr(4, hours=2, head="h", state_since=_ago(2.5),
                      auto_merge_blocked_reason="CI is failing on head h — needs human review"),
        "o/lm#5": _pr(5, panel_verdict="Approve", panel2_verdict="Reject", head="h",
                      auto_merge_blocked_reason="CI is failing on head h — needs human review"),
    }
    items = {i["id"]: i for i in mission_control.build(
        processed, prs, now=NOW, ci_seen={("o/lm", "h"): {"done": 6, "total": 6, "failed": 1}})["attention"]}
    assert (items["o/lm:1"]["label"], items["o/lm:1"]["reply"]) == ("Fix failed · attempt 2/3", "model said no")
    assert (items["o/lm:2"]["label"], items["o/lm:2"]["boundary"]) == ("Feature flagged · crosses boundary", "auth-rbac")
    assert items["o/lm#3"]["label"] == "Split verdict" and items["o/lm#3"]["split"]
    assert (items["o/lm#4"]["ci"], items["o/lm#4"]["ci_tone"], items["o/lm#4"]["held_for"]) == ("CI ✕ 1/6", "bad", "2h 30m")
    # Split panels on a PR held for failing CI: the CI hold stays the label and actions.
    assert items["o/lm#5"]["label"] == "Auto-merge held" and not items["o/lm#5"].get("split")


@pytest.mark.parametrize("ci, chip", [
    ({"done": 6, "passed": 6, "total": 6, "failed": 0}, ("CI ✓ 6/6", "ok")),
    ({"done": 6, "passed": 4, "total": 6, "failed": 0}, ("CI … 4/6", "wait")),
    ({"done": 2, "passed": 2, "total": 6, "failed": 0}, ("CI … 2/6", "wait")),
])
def test_ci_chip_is_green_only_when_every_check_passed(ci, chip):
    assert mission_control._ci_chip(ci) == chip


def test_promotion_lanes_show_the_open_promotion_and_the_last_one():
    prs = {
        "o/lm#7": _pr(7, head_ref="qa", base_ref="main", auto_merge_blocked_reason="CI is failing on head h — needs human review"),
        "o/lm#6": _pr(6, hours=5, head_ref="dev", base_ref="qa", merged=True),
        "o/nw#2": _pr(2, hours=3, head_ref="dev", base_ref="qa", auto_merged=True, merged=True),
        "o/nw#3": _pr(3, head_ref="feature/x", base_ref="dev"),
    }
    rows = {r["repo"]: r for r in mission_control.promotion_lanes(["o/nw", "o/cs"], prs, NOW)}
    assert list(rows) == ["nw", "cs", "lm"]
    assert rows["lm"]["main"]["number"] == "7" and rows["lm"]["main"]["held"] and rows["lm"]["qa"] is None
    assert rows["lm"]["note"] == "#7 qa → main: auto-merge held"
    assert rows["nw"]["note"] == "last promoted to qa 3h ago"
    assert rows["cs"]["note"] == "no promotion PRs seen"


def test_next_poll_pill_text():
    assert mission_control.next_poll_label(1000 + 185, 1000) == "Next poll 3m"
    assert mission_control.next_poll_label(1000 + 20, 1000) == "Next poll now"
    assert mission_control.next_poll_label(None) == ""
