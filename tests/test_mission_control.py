"""Dashboard Mission Control: what needs a person, what AppBuilder still owns,
and what finished recently (mission_control.build), and the dashboard that
renders it."""
import os
from datetime import datetime, timedelta, timezone

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
    assert mc["kpis"] == {"needs_you": 8, "running": 1, "in_flight": 1,
                          "fixed_7d": 1, "auto_merged_7d": 0, "failed_7d": 1}
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
    assert mc["working"] == [{"id": "t1", "name": "RepoScan", "kind": "scan", "started": start.astimezone(timezone.utc).isoformat()}]


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
