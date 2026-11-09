#!/usr/bin/env python3
"""Escalated hub-log issues must be titled after the actual problem, not the
analysis boilerplate "1. **Is there a problem?** Yes." that every analysis
opens with (which folded unrelated problems into lm#1228).

log_scan.py can't be imported directly (circular import via main.py), so the
helper is extracted via ast, as in test_log_scan_requirements.py.
"""
import ast
import re


def _summary_fn():
    tree = ast.parse(open("log_scan.py").read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_escalation_summary_line")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_ESCALATION_HEADER"
                                                  for t in n.targets))]
    ns = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "log_scan.py", "exec"), ns)
    return ns["_escalation_summary_line"]


ANALYSIS = """1. **Is there a problem?** Yes.

2. **What is going wrong:**
Two distinct issues require attention:

* **CPPM agent timeouts:** Multiple `LIST_ENDPOINTS` requests timed out after 20 seconds.
"""


def test_skips_boilerplate_headers():
    assert _summary_fn()(ANALYSIS) == "Two distinct issues require attention:"


def test_strips_markdown_from_problem_line():
    out = _summary_fn()("1. **Is there a problem?** Yes.\n\n**Certificate push to `ab` fails**")
    assert out == "Certificate push to ab fails"


def test_empty_analysis():
    assert _summary_fn()("") == ""


def _recent_fn():
    import logging
    from datetime import datetime
    tree = ast.parse(open("log_scan.py").read())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "recent_deploy_changes"]
    ns = {"datetime": datetime, "logger": logging.getLogger("t")}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "log_scan.py", "exec"), ns)
    return ns["recent_deploy_changes"]


def test_post_deploy_watch_lists_recent_commits_on_the_deployed_branch():
    from types import SimpleNamespace as N
    seen = {}

    class Repo:
        default_branch = "main"

        def get_commits(self, sha, since):
            seen.update(sha=sha, since=since)
            return [N(sha="abcdef1234", commit=N(message="nw: dedupe rows\n\nbody"))]

    out = _recent_fn()(Repo(), {"post_deploy_window_h": 2})
    assert out == ["- `abcdef12` nw: dedupe rows"] and seen["sha"] == "main"
    assert _recent_fn()(Repo(), {"post_deploy_window_h": 0}) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
