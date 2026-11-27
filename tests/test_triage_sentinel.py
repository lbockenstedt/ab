"""LM hub sentinel escalations were triaged non-actionable because the LLM
asked for "the code" (lm#1876, ldap#63); the sentinel already triaged them."""
import ast
import json
import re


def _analyze_issue(call_llm):
    src = open("fix_engine.py").read()
    segs = [ast.get_source_segment(src, n) for n in ast.parse(src).body
            if (isinstance(n, ast.FunctionDef) and n.name == "analyze_issue")
            or (isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) in ("_BUG_REPORT_ID_RE", "_SENTINEL_ESCALATION")
                for t in n.targets))]
    log = type("L", (), {"__getattr__": lambda s, _: (lambda *a, **k: None)})()
    ns = {"re": re, "json": json, "logger": log, "call_llm": call_llm,
          "load_config": lambda: {"TRIAGE_STRICTNESS": "Strict"}, "_robust_json_loads": json.loads,
          "is_llm_cooldown_error": lambda e: False}
    exec("\n\n".join(segs), ns)
    return ns["analyze_issue"]


class _Issue:
    title = "Hub log alert (hub): Health check: slow page GET /api/nw/topology"

    def __init__(self, body):
        self.body = body

    def get_comments(self):
        return []


SENTINEL = "**Escalated by the LM hub log sentinel** (verdict: `escalate`, module: `nw`)."


def test_sentinel_with_code_cause_stays_actionable():
    assert _analyze_issue(lambda *a, **k: '{"actionable": true}')(_Issue(SENTINEL))[0] is True


def test_sentinel_operational_analysis_is_not_actionable():
    def _llm(prompt, **k):
        assert "log-sentinel escalation" in prompt
        return '{"actionable": false, "operational": true, "request": "check agent health and load"}'
    assert _analyze_issue(_llm)(_Issue(SENTINEL)) == (False, "check agent health and load")


def test_sentinel_empty_verdict_fails_safe_to_the_fixer():
    for reply in ("{}", '{"actionable": null}', '{"actionable": ""}', '{"actionable": 0}', '{"actionable": []}'):
        assert _analyze_issue(lambda *a, **k: reply)(_Issue(SENTINEL))[0] is True


def test_no_change_hands_the_issue_to_ops_and_redacts_the_reason(monkeypatch):
    src = open("fix_engine.py").read()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "_stop_no_code_change")
    store, comments, labelled = {"o/r:1": {"title": "T", "auto_retries": 1}}, [], []
    log = type("L", (), {"__getattr__": lambda s, _: (lambda *a, **k: None)})()
    scan = type("S", (), {"mark_no_code_change": staticmethod(lambda i: labelled.append(i) or True)})
    import sys
    monkeypatch.setitem(sys.modules, "log_scan", scan)
    ns = {"datetime": __import__("datetime").datetime, "timedelta": __import__("datetime").timedelta, "logger": log,
          "github_ops": type("G", (), {"redact_identity": staticmethod(lambda t: t.replace("10.0.0.5", "<ip>"))}),
          "update_processed": lambda fn: fn(store), "update_task_state": lambda **k: None}
    ns["_public_reason"] = lambda t: ns["github_ops"].redact_identity(t)
    exec(ast.get_source_segment(src, node), ns)
    issue = type("I", (), {"create_comment": lambda s, t: comments.append(t)})()
    repo = type("R", (), {"git": type("G", (), {"reset": lambda *a, **k: None, "clean": lambda *a, **k: None})()})()
    assert ns["_stop_no_code_change"](issue, "o/r:1", "device 10.0.0.5 is slow", repo)[0] is False
    rec = dict(store["o/r:1"])
    assert rec["status"] == "ops-alert" and rec["title"] == "T" and rec["auto_retries"] == 1
    # With no checkout (the triage path) and GitHub refusing the handoff, it is recorded failed (bounded retry, then a person).
    monkeypatch.setitem(sys.modules, "log_scan", type("S", (), {"mark_no_code_change": staticmethod(lambda i: False)}))
    ns["_stop_no_code_change"](issue, "o/r:1", "rotate code ABC123DEF456", None)
    assert "ABC123" not in store["o/r:1"]["reason"]  # _hand_to_human posts a failed record's reason
    assert store["o/r:1"]["status"] == "failed"
    assert store["o/r:1"]["last_recurrence_at"] > store["o/r:1"]["timestamp"]  # the bounded retry applies
    assert "10.0.0.5" not in rec["reason"] and "10.0.0.5" not in comments[0] and labelled == [issue]


def test_sentinel_prompt_ignores_the_strict_evidence_rule():
    seen = {}

    def _llm(prompt, **k):
        seen["p"] = prompt
        return '{"actionable": true}'
    _analyze_issue(_llm)(_Issue(SENTINEL))
    assert "MUST have full console logs" not in seen["p"]


def test_sentinel_prompt_does_not_ask_for_more_information():
    seen = {}

    def _llm(prompt, **k):
        seen["p"] = prompt
        return '{"actionable": true}'
    _analyze_issue(_llm)(_Issue(SENTINEL))
    assert "enough information" not in seen["p"] and "never ask for more" in seen["p"]


def test_public_reason_redacts_credentials_and_identities():
    src = open("fix_engine.py").read()
    segs = [ast.get_source_segment(src, n) for n in ast.parse(src).body
            if (isinstance(n, ast.FunctionDef) and n.name == "_public_reason")
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_CREDENTIAL_RE")]
    ns = {"re": re, "github_ops": type("G", (), {"redact_identity": staticmethod(lambda t: t.replace("10.0.0.5", "<ip>"))})}
    exec("\n".join(segs), ns)
    out = ns["_public_reason"]("device 10.0.0.5 rejects the poll and times out")
    assert "10.0.0.5" not in out and "rejects" in out
    for t in ("the password is hunter4, rejected", "sent Authorization: Bearer abc123XYZ",
              "password: 'my secret' failed", 'config {"api_key": "k-1"} bad',
              "token ghp_" + "a" * 30, "fetch https://user:pw@host/x failed", "Cookie: sid=abc"):
        assert "withheld" in ns["_public_reason"](t) and "hunter4" not in ns["_public_reason"](t)


def test_null_request_becomes_text():
    out = _analyze_issue(lambda *a, **k: '{"actionable": false, "request": null}')(_Issue("ERROR x"))
    assert out[0] is False and isinstance(out[1], str) and out[1]


def test_sentinel_false_that_is_not_operational_stays_with_the_fixer():
    reply = '{"actionable": false, "request": "send the source of nw.py"}'
    assert _analyze_issue(lambda *a, **k: reply)(_Issue(SENTINEL))[0] is True


def test_sentinel_operational_verdict_needs_a_request():
    reply = '{"actionable": false, "operational": true, "request": ""}'
    assert _analyze_issue(lambda *a, **k: reply)(_Issue(SENTINEL))[0] is True


def test_non_sentinel_reply_without_actionable_is_not_actionable():
    assert _analyze_issue(lambda *a, **k: '{"request": "x"}')(_Issue("ERROR x"))[0] is False


def test_sentinel_triage_fails_safe_to_the_fixer():
    assert _analyze_issue(lambda *a, **k: "no json")(_Issue(SENTINEL)) == (True, "")

    def _boom(*a, **k):
        raise RuntimeError("down")
    assert _analyze_issue(_boom)(_Issue(SENTINEL)) == (True, "")


def test_triage_prompt_says_the_fixer_has_the_code():
    seen = {}

    def _llm(prompt, **k):
        seen["prompt"] = prompt
        return '{"actionable": false, "request": "x"}'
    _analyze_issue(_llm)(_Issue("ERROR something broke"))
    assert "never ask for source code" in seen["prompt"]
