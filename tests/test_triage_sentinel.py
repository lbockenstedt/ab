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
          "load_config": lambda: {}, "_robust_json_loads": json.loads,
          "is_llm_cooldown_error": lambda e: False}
    exec("\n\n".join(segs), ns)
    return ns["analyze_issue"]


class _Issue:
    title = "Hub log alert (hub): Health check: slow page GET /api/nw/topology"

    def __init__(self, body):
        self.body = body

    def get_comments(self):
        return []


def test_sentinel_escalation_skips_llm_triage():
    def _no_llm(*a, **k):
        raise AssertionError("LLM triage must not run")
    body = "**Escalated by the LM hub log sentinel** (verdict: `health-check`, module: `hub`)."
    assert _analyze_issue(_no_llm)(_Issue(body)) == (True, "")


def test_triage_prompt_says_the_fixer_has_the_code():
    seen = {}

    def _llm(prompt, **k):
        seen["prompt"] = prompt
        return '{"actionable": false, "request": "x"}'
    _analyze_issue(_llm)(_Issue("ERROR something broke"))
    assert "never ask for source code" in seen["prompt"]
