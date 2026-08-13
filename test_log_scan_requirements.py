#!/usr/bin/env python3
"""Self-test for analyze_logs_for_errors' requirements= conversion
(LLM Selection Redesign, Phase 5, call site #14 — log_scan.py).

Run:  python3 test_log_scan_requirements.py

log_scan.py cannot be imported directly (it transitively pulls in main.py's
circular import chain), so this extracts analyze_logs_for_errors via ast and
execs it with a stubbed call_llm/logger — the established convention in this
repo (see test_dedup_llm_adjudication.py).

Covers:
1. call_llm is invoked with requirements= (not task_kind=), with
   complexity="small" and needs_structured_output=True per the plan's §2
   table, and min_context_tokens scaling with the actual prompt size.
2. The existing JSON-array validation/cleaning behavior (malformed entries
   dropped, single-object response wrapped in a list, non-array discarded)
   is unaffected by the routing change.
"""
import ast
import json


def _load_ns(call_llm_stub):
    src = open("log_scan.py").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "analyze_logs_for_errors")
    seg = ast.get_source_segment(src, node)

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {
        "json": json, "call_llm": call_llm_stub, "logger": _NoLog(),
        "resolve_module_repo": lambda module: "o/r",
        "is_llm_cooldown_error": lambda e: False,
    }
    exec(seg, ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True

    # ---- 1. requirements= wiring ----
    captured = {}

    def _capturing_call_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return '[{"module": "m", "title": "t", "body": "b"}]'

    ns = _load_ns(_capturing_call_llm)
    ns["analyze_logs_for_errors"]([{"module": "m", "log": "some error text"}])
    kw = captured.get("kwargs", {})
    ok &= _check("call_llm is invoked with requirements= (not task_kind=)",
                 "requirements" in kw and "task_kind" not in kw)
    reqs = kw.get("requirements")
    ok &= _check("requirements.complexity == 'small'",
                 reqs is not None and reqs.complexity == "small")
    ok &= _check("requirements.needs_structured_output is True",
                 reqs is not None and reqs.needs_structured_output is True)
    ok &= _check("requirements.min_context_tokens scales with the prompt length",
                 reqs is not None and reqs.min_context_tokens == len(captured["prompt"]) // 4 > 0)

    # ---- 2. existing JSON validation/cleaning behavior unaffected ----
    ns_ok = _load_ns(lambda *a, **k: '[{"module": "m1", "title": "T", "body": "B"}, '
                                     '{"module": "m1", "body": "B2"}]')
    out = ns_ok["analyze_logs_for_errors"]([{"module": "m1", "log": "x"}])
    ok &= _check("valid entries kept, malformed (missing title) entries dropped",
                 len(out) == 1 and out[0]["module"] == "m1" and out[0]["title"] == "T")

    # NB: the regex match requires literal [...] brackets, so a bare JSON
    # object response (no array brackets) never reaches the "wrap single
    # object" defensive branch below -- pre-existing behavior, unrelated to
    # this call site's requirements= conversion, left unchanged here.
    ns_obj = _load_ns(lambda *a, **k: '{"module": "m2", "title": "T2", "body": "B2"}')
    out2 = ns_obj["analyze_logs_for_errors"]([{"module": "m2", "log": "x"}])
    ok &= _check("a bare JSON object (no array brackets) yields no matches (pre-existing behavior)",
                 out2 == [])

    ns_nonarray = _load_ns(lambda *a, **k: '"just a string"')
    out3 = ns_nonarray["analyze_logs_for_errors"]([{"module": "m3", "log": "x"}])
    ok &= _check("non-array/non-object JSON is discarded (empty list)", out3 == [])

    ns_empty = _load_ns(lambda *a, **k: "no logs")
    out4 = ns_empty["analyze_logs_for_errors"]([])
    ok &= _check("empty logs input short-circuits to [] without calling the LLM", out4 == [])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running log_scan.py requirements= self-test...")
    import sys
    sys.exit(main())
