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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
