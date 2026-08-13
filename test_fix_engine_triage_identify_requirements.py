#!/usr/bin/env python3
"""Self-test for fix_engine.py's triage (site #1) and identify_files (site #2)
requirements= conversions (LLM Selection Redesign, Phase 5).

Run:  python3 test_fix_engine_triage_identify_requirements.py

fix_engine.py cannot be imported directly (it transitively pulls in main.py's
circular import chain), so this extracts analyze_issue/identify_files_to_fix
via ast and execs them with stubbed dependencies — the established
convention in this repo (see test_dedup_llm_adjudication.py).

Covers:
1. analyze_issue: call_llm is invoked with requirements=LlmRequirements(
   complexity="trivial", needs_structured_output=True) instead of
   task_kind="triage"; existing actionable/non-actionable JSON parsing is
   unaffected.
2. identify_files_to_fix: call_llm is invoked with requirements=LlmRequirements(
   complexity="small", needs_structured_output=True) instead of
   task_kind="identify_files", with min_context_tokens scaling with the
   actual (file-list-inclusive) prompt size.
"""
import ast
import json
import os
import re


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load_fix_engine_ns(want_funcs, extra_ns=None):
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_BUG_REPORT_ID_RE" for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    ns = {"re": re, "json": json, "os": os, "logger": _NoLog()}
    if extra_ns:
        ns.update(extra_ns)
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


class _FakeIssue:
    def __init__(self, title, body):
        self.title = title
        self.body = body

    def get_comments(self):
        return []


def main():
    ok = True

    # ---- 1. analyze_issue (site #1, triage) ----
    captured = {}

    def _capturing_call_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return '{"actionable": true, "request": ""}'

    ns1 = _load_fix_engine_ns(
        {"analyze_issue"},
        {
            "call_llm": _capturing_call_llm,
            "load_config": lambda: {"TRIAGE_STRICTNESS": "Moderate"},
            "_robust_json_loads": json.loads,
            "is_llm_cooldown_error": lambda e: False,
        },
    )
    actionable, request = ns1["analyze_issue"](_FakeIssue("A crash", "Stack trace: NPE at line 5"))
    kw = captured.get("kwargs", {})
    ok &= _check("analyze_issue: call_llm invoked with requirements= (not task_kind=)",
                 "requirements" in kw and "task_kind" not in kw)
    reqs1 = kw.get("requirements")
    ok &= _check("analyze_issue: requirements.complexity == 'trivial'",
                 reqs1 is not None and reqs1.complexity == "trivial")
    ok &= _check("analyze_issue: requirements.needs_structured_output is True",
                 reqs1 is not None and reqs1.needs_structured_output is True)
    ok &= _check("analyze_issue: min_context_tokens scales with the prompt",
                 reqs1 is not None and reqs1.min_context_tokens == len(captured["prompt"]) // 4 > 0)
    ok &= _check("analyze_issue: existing actionable-JSON parsing still works",
                 actionable is True and request == "")

    # A "File a Bug" report skips the LLM triage call entirely (existing behavior) —
    # confirm that path still short-circuits and never touches call_llm.
    captured2 = {}
    ns1b = _load_fix_engine_ns(
        {"analyze_issue"},
        {
            "call_llm": lambda *a, **k: captured2.setdefault("called", True) and "{}",
            "load_config": lambda: {"TRIAGE_STRICTNESS": "Moderate"},
            "_robust_json_loads": json.loads,
            "is_llm_cooldown_error": lambda e: False,
        },
    )
    actionable2, _ = ns1b["analyze_issue"](
        _FakeIssue("Bug report", "<!-- bug-report-id: abc123 -->\nSome details"))
    ok &= _check("analyze_issue: a 'File a Bug' report skips the LLM call entirely",
                 actionable2 is True and "called" not in captured2)

    # ---- 2. identify_files_to_fix (site #2) ----
    captured3 = {}

    def _capturing_call_llm2(prompt, **kwargs):
        captured3["prompt"] = prompt
        captured3["kwargs"] = kwargs
        return '["src/a.py", "src/b.py"]'

    ns2 = _load_fix_engine_ns(
        {"identify_files_to_fix"},
        {
            "call_llm": _capturing_call_llm2,
            "_robust_json_loads": json.loads,
            "_extract_issue_identifiers": lambda body: [],
            "_grep_files_for_identifiers": lambda repo_path, all_files, identifiers: [],
            "_extract_error_symbols": lambda body: [],
        },
    )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
        with open(os.path.join(tmp, "src", "a.py"), "w") as f:
            f.write("pass\n")
        with open(os.path.join(tmp, "src", "b.py"), "w") as f:
            f.write("pass\n")
        files = ns2["identify_files_to_fix"](tmp, "Something is broken in a.py")

    kw2 = captured3.get("kwargs", {})
    ok &= _check("identify_files_to_fix: call_llm invoked with requirements= (not task_kind=)",
                 "requirements" in kw2 and "task_kind" not in kw2)
    reqs2 = kw2.get("requirements")
    ok &= _check("identify_files_to_fix: requirements.complexity == 'small'",
                 reqs2 is not None and reqs2.complexity == "small")
    ok &= _check("identify_files_to_fix: requirements.needs_structured_output is True",
                 reqs2 is not None and reqs2.needs_structured_output is True)
    ok &= _check("identify_files_to_fix: min_context_tokens scales with the (file-list-inclusive) prompt",
                 reqs2 is not None and reqs2.min_context_tokens == len(captured3["prompt"]) // 4 > 0)
    ok &= _check("identify_files_to_fix: existing JSON-array parsing still returns the LLM's file list",
                 files == ["src/a.py", "src/b.py"])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running fix_engine.py triage/identify_files requirements= self-test...")
    import sys
    sys.exit(main())
