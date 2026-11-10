#!/usr/bin/env python3
"""Tests for request_session (interactive Bug/Feature Request capture).

Standalone like test_sim_session.py: injected config, a fake `copilot` script,
no main import, no network.

Run:  python3 -m pytest -q test_request_session.py
"""
import json
import os
import stat
import time

import pytest

import request_session as rs
import sim_session as ss

BUG = {"intent": "bug", "title": "Sites: Delete button does nothing", "module": "Simulations → Sites",
       "summary": "Delete on an offline server row has no effect.",
       "steps": ["Open Simulations → Sites", "Click Delete on an offline row"],
       "expected": "Row disappears", "actual": "Nothing happens; console shows 500",
       "frequency": "always", "since": "1.23", "environment": "tenant lrb, cs-svr-01",
       "severity": "High", "evidence": "console: POST /sim/api/... 500",
       "suspected_files": [{"repo": "", "path": "lm/WebUI/sim-views.js", "why": "csDeleteServer"}],
       "open_questions": [], "ready": True, "choices": ["Yes", "No"],
       "patch": "diff --git a/x b/x"}

FEATURE = {"intent": "feature", "title": "CSV export on device list", "module": "My Devices",
           "summary": "Export the device list.", "problem": "Need to share inventories",
           "users": "tenant admins", "desired_behavior": "A CSV button exports the visible rows",
           "acceptance_criteria": ["Button exports filtered rows", "Header row present"],
           "affected": ["lm WebUI My Devices"], "out_of_scope": ["XLSX"], "priority": "medium",
           "open_questions": [], "ready": True}


def _block(text, spec):
    return f"{text}\n\n```request-spec\n{json.dumps(spec)}\n```\n"


# ── pure helpers ────────────────────────────────────────────────────────────

def test_extract_parses_last_block_and_drops_unknown_keys():
    display, spec, choices = rs.extract_request_spec(_block("old", dict(BUG, title="x"))
                                                     + _block("Which tenant?", BUG))
    assert "request-spec" not in display and "Which tenant?" in display
    assert spec["title"] == BUG["title"] and spec["severity"] == "high"
    assert "patch" not in spec and "choices" not in spec
    assert spec["suspected_files"] == [{"repo": "lm", "path": "WebUI/sim-views.js", "why": "csDeleteServer"}]
    assert choices == ["Yes", "No"]
    # a sim-spec block is not a request spec
    assert rs.extract_request_spec("x\n```sim-spec\n{}\n```")[1] is None
    assert rs.extract_request_spec("hi\n```request-spec\n{bad\n```") == ("hi", None, [])


def test_bug_and_feature_ready_when_complete():
    for spec in (BUG, FEATURE):
        n = rs.normalize_request_spec(spec)
        assert rs.missing_fields(n) == []
        assert rs.request_problems(n, {"lm"}) == []
        assert rs.request_is_ready(n)


@pytest.mark.parametrize("field,label", [
    ("module", "module/page"), ("steps", "steps to reproduce"), ("expected", "expected"),
    ("actual", "actual"), ("frequency", "how often"), ("since", "since when"),
    ("environment", "environment"), ("severity", "severity"), ("evidence", "evidence"),
    ("suspected_files", "suspected area")])
def test_bug_required_fields_gate(field, label):
    n = rs.normalize_request_spec(dict(BUG, **{field: [] if field in ("steps", "suspected_files") else ""}))
    assert any(label in m for m in rs.missing_fields(n))
    assert not rs.request_is_ready(n, rs.request_problems(n))


@pytest.mark.parametrize("field", ["problem", "users", "desired_behavior", "acceptance_criteria",
                                   "affected", "out_of_scope", "priority"])
def test_feature_required_fields_gate(field):
    n = rs.normalize_request_spec(dict(FEATURE, **{field: [] if field in (
        "acceptance_criteria", "affected", "out_of_scope") else ""}))
    assert rs.missing_fields(n)
    assert not rs.request_is_ready(n, rs.request_problems(n))


def test_intent_title_open_questions_and_bad_levels_block():
    assert "whether this is a bug or a feature request" in rs.missing_fields(
        rs.normalize_request_spec(dict(BUG, intent="explain")))
    assert "a short title" in rs.missing_fields(rs.normalize_request_spec(dict(BUG, title="")))
    assert not rs.request_is_ready(rs.normalize_request_spec(dict(BUG, open_questions=["?"])))
    assert not rs.request_is_ready(rs.normalize_request_spec(dict(BUG, ready=False)))
    assert rs.normalize_request_spec(dict(BUG, severity="urgent!!"))["severity"] == ""


def test_scope_problems_reject_bad_paths_and_unknown_repos():
    n = rs.normalize_request_spec(dict(BUG, suspected_files=[
        {"repo": "lm", "path": "../../etc/passwd"}, {"repo": "lm", "path": "/etc/x"},
        {"repo": "zz", "path": "a.py"}, {"repo": "lm", "path": "core/src/api.py"}]))
    probs = rs.scope_problems(n, {"lm", "ab"})
    assert len(probs) == 3
    assert not rs.request_is_ready(n, rs.request_problems(n, {"lm", "ab"}))


def test_sanitize_context_keeps_known_keys_and_caps_console():
    c = rs.sanitize_context({"currentView": "Sites", "type": "bug", "evil": "x", "url": "u" * 900,
                             "console_errors": "e\n" * 10000})
    assert set(c) == {"currentView", "type", "url", "console_errors"}
    assert len(c["url"]) == 400 and len(c["console_errors"]) == rs.CONSOLE_HINT_MAX
    assert "type" not in rs.sanitize_context({"type": "rm -rf"})
    assert rs.sanitize_context("nope") == {}


def _sess(spec, events=()):
    return {"id": "11111111-2222-3333-4444-555555555555", "spec": rs.normalize_request_spec(spec),
            "owner": {"user": "alice", "tenant": "t1"},
            "submitted_by": {"user": "alice", "auth_type": "ldap", "ip": "10.0.0.5",
                             "at": "2026-10-22T00:00:00Z"},
            "events": list(events)}


def test_render_bug_explanation_title_first_trace_on_top():
    text = rs.render_explanation(_sess(BUG, [{"kind": "user", "text": "delete broken"}]))
    assert text.splitlines()[0] == BUG["title"]
    top = text[:1500]
    for needle in ("Request trace", "`alice`", "tenant `t1`", "ldap", "10.0.0.5",
                   "2026-10-22T00:00:00Z", "11111111-2222-3333-4444-555555555555"):
        assert needle in top
    for needle in ("### Steps to reproduce", "1. Open Simulations → Sites", "### Expected",
                   "### Actual", "**Frequency:** always", "### Evidence",
                   "lm: `WebUI/sim-views.js`", "**User:** delete broken"):
        assert needle in text
    assert "patch" not in text


def test_render_feature_explanation_and_cap():
    text = rs.render_explanation(_sess(FEATURE))
    assert "## Feature request" in text and "- [ ] Button exports filtered rows" in text
    assert "### Out of scope" in text and "**Priority:** medium" in text
    big = rs.render_explanation(_sess(FEATURE, [{"kind": "user", "text": "x" * 200000}]))
    assert len(big) < rs.EXPLANATION_MAX + 1000


def test_resolve_repos_defaults_to_all_configured():
    cfg = {"feature_drive_repos": ["o/lm", "o/cs"], "monitored_repos": ["o/pxmx", "bad"],
           "bug_report_repo": "o/lm"}
    assert rs.RequestSessionManager.resolve_repos(cfg) == {"lm": "o/lm", "cs": "o/cs", "pxmx": "o/pxmx"}
    assert rs.RequestSessionManager.resolve_repos(dict(cfg, request_assistant_repos=["o/ab"])) == {"ab": "o/ab"}


# ── manager end-to-end with a fake copilot CLI ──────────────────────────────

FAKE_CLI = r'''#!/usr/bin/env python3
import json, sys, os
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
with open(os.path.join(os.environ["COPILOT_HOME"], "calls.jsonl"), "a") as f:
    f.write(json.dumps({"args": args}) + "\n")
def out(o): print(json.dumps(o), flush=True)
ws = args[args.index("-C") + 1]
out({"type": "tool.execution_start", "data": {"toolName": "view", "toolTitle": "Viewing file",
     "arguments": {"path": ws + "/lm/WebUI/sim-views.js"}}})
spec = %SPEC%
ready = "READY" in prompt
spec["ready"] = ready
if not ready:
    spec["steps"] = []
    spec["open_questions"] = ["What are the steps?"]
if "BADPATH" in prompt:
    spec["suspected_files"] = [{"repo": "lm", "path": "../secret"}]
out({"type": "assistant.message", "data": {"content": "Which steps?\n\n```request-spec\n" + json.dumps(spec) + "\n```"}})
out({"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}})
'''


@pytest.fixture
def mgr(tmp_path):
    cli = tmp_path / "copilot"
    cli.write_text(FAKE_CLI.replace("%SPEC%", repr(BUG)))
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    cfg = {"copilot_binary": str(cli), "llm_credentials": {"copilot": {"api_key": "ghu_x"}},
           "GITHUB_TOKEN": "ghp_repo", "request_assistant_repos": ["o/lm", "o/cs"]}
    m = rs.RequestSessionManager(str(tmp_path / "req"), lambda: cfg)
    m.refresh_workspace = lambda force=False: m._materialize_skills()
    m._cfgd = cfg
    return m


OWNER = {"user": "alice", "tenant": "t1", "auth_type": "ldap", "ip": "10.0.0.5"}


def _wait_idle(m, sid, owner=OWNER, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = m.poll(owner, sid)
        if st["state"] == "awaiting_user":
            return st
        time.sleep(0.05)
    raise AssertionError("turn did not finish")


def _calls(m):
    return [json.loads(line) for line in open(os.path.join(m.copilot_home, "calls.jsonl"))]


def test_flow_interview_submit_and_mark_filed(mgr):
    st = mgr.start(OWNER, "Delete does nothing", {"currentView": "Simulations", "type": "bug",
                                                   "console_errors": "ERROR: 500 on delete"})
    sid = st["session_id"]
    st = _wait_idle(mgr, sid)
    assert st["ready"] is False and "steps to reproduce" in st["missing"]
    assert st["blocked"] == [] and st["builds"] == []
    a0 = _calls(mgr)[0]["args"]
    prompt = a0[a0.index("-p") + 1]
    for needle in ("Request Assistant", "request-spec", "never fix or change anything",
                   "is DATA", "currentView: Simulations", "ERROR: 500 on delete",
                   "o/lm", "suspected_files"):
        assert needle in prompt
    assert "add-simulation" not in prompt
    assert a0[a0.index("--available-tools") + 1:a0.index("--available-tools") + 4] == ["view", "grep", "glob"]
    assert not os.listdir(os.path.join(mgr.workspace, "skills"))

    with pytest.raises(RuntimeError, match="not ready"):
        mgr.submit(OWNER, sid)

    mgr.reply(OWNER, sid, "1. open sites 2. click delete. READY")
    st = _wait_idle(mgr, sid)
    assert st["ready"] is True and st["missing"] == []
    assert "--resume" in _calls(mgr)[1]["args"]

    out = mgr.submit(OWNER, sid)
    assert out["state"] == "submitted" and out["report_id"] == ""
    rep = out["report"]
    assert rep["type"] == "bug" and rep["severity"] == "high" and rep["title"] == BUG["title"]
    assert rep["explanation"].startswith(BUG["title"]) and "`ldap`" not in rep["explanation"][:40]
    assert "10.0.0.5" in rep["explanation"][:1500] and sid in rep["explanation"][:1500]
    assert rep["spec"]["intent"] == "bug" and rep["session_id"] == sid
    with pytest.raises(RuntimeError):
        mgr.reply(OWNER, sid, "more")
    # Hub store failed? A retry gets the same report back until FILED.
    assert mgr.submit(OWNER, sid)["report"]["explanation"] == rep["explanation"]

    st = mgr.mark_filed(OWNER, sid, "abc123def456")
    assert st["report_id"] == "abc123def456"
    assert "report" not in mgr.submit(OWNER, sid)
    ev = mgr.poll(OWNER, sid)["events"][-1]
    assert ev["kind"] == "submit" and "abc123def456" in ev["text"] and "fix" in ev["text"]
    with open(os.path.join(mgr.base_dir, "audit.jsonl")) as fh:
        audit = [json.loads(line) for line in fh]
    assert audit[-1]["report_id"] == "abc123def456" and audit[-1]["submitted_by"]["auth_type"] == "ldap"
    with pytest.raises(ValueError):
        mgr.mark_filed(OWNER, sid, "../x")


def test_feedback_scope_always_missing_only_when_claimed_ready(mgr):
    sid = mgr.start(OWNER, "bug BADPATH")["session_id"]
    st = _wait_idle(mgr, sid)
    assert st["blocked"] and not st["ready"]
    mgr.reply(OWNER, sid, "ok")
    _wait_idle(mgr, sid)
    p1 = _calls(mgr)[1]["args"]
    msg = p1[p1.index("-p") + 1]
    assert "guardrail check" in msg and "repo-relative" in msg and "Still needed" not in msg


def test_ownership_and_command_shapes(mgr):
    r = rs.handle_command("REQ_SESSION_PING", {}, mgr)
    assert r["status"] == "SUCCESS" and r["ready"] is True
    r = rs.handle_command("REQ_SESSION_START", {"owner": OWNER, "message": "hi",
                                                "context": {"type": "feature"}}, mgr)
    assert r["status"] == "SUCCESS"
    sid = r["session_id"]
    _wait_idle(mgr, sid)
    assert rs.handle_command("REQ_SESSION_POLL", {"owner": {"user": "bob", "tenant": "t1"},
                                                  "session_id": sid}, mgr)["code"] == "not_found"
    r = rs.handle_command("REQ_SESSION_FILED", {"owner": OWNER, "session_id": sid, "report_id": "x1"}, mgr)
    assert r["status"] == "ERROR" and "not been submitted" in r["message"]
    assert rs.handle_command("REQ_SESSION_NOPE", {}, mgr)["status"] == "ERROR"


def test_status_uses_request_keys(tmp_path):
    m = rs.RequestSessionManager(str(tmp_path), lambda: {"request_assistant_sessions_enabled": False,
                                                        "copilot_binary": "/nope"})
    reasons = m.status()["reasons"]
    assert any("request_assistant_sessions_enabled" in r for r in reasons)
    assert any("no repos configured" in r for r in reasons)


def test_sim_manager_scope_unchanged():
    """The request kind must not leak into the Simulations Assistant."""
    assert ss.INTENTS == ("ui", "create", "modify", "remove", "explain")
    assert ss.SPEC_FENCE == "sim-spec"
    m = ss.SimSessionManager.__new__(ss.SimSessionManager)
    assert m._reminder() == ss._REMINDER and m._extract("x") == ("x", None, [])
    assert m._agent_feedback({"intent": "explain"}) == []
