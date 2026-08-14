#!/usr/bin/env python3
"""Regression guard: filter_error_logs must drop BugFixer's own fix-engine
operational chatter so the self-log scanner cannot file phantom issues about
BugFixer fixing its own fix attempts (bugfixer#834).

#834 was auto-created verbatim from a single fix-engine log line —
"Edit search snippet not found in 'src/spoke.py'; skipping this edit ..." —
which is handled, expected control flow (a non-matching edit anchor), not a
product defect. Because it is logged at ERROR, the scanner treated it as an
actionable error, filed an issue, and the fix pipeline failed it 3× with
"AI generated invalid JSON format" (there is no code to fix), then recurred
the next time any edit anchor missed. filter_error_logs now suppresses that
chatter while still surfacing genuine product errors.

log_scan.py can't be imported directly (it transitively pulls in main.py's
circular import chain), so this extracts _SELF_SCAN_NOISE + filter_error_logs
via ast and execs them with stubbed load_config/logger — the established
convention in this repo (see test_log_scan_requirements.py).

Run:  python3 test_self_scan_noise.py
"""
import ast
import json
import re
import sys


def _load_filter():
    src = open("log_scan.py").read()
    tree = ast.parse(src)
    noise_node = next(
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "_SELF_SCAN_NOISE" for t in n.targets)
    )
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "filter_error_logs"
    )

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {"json": json, "re": re, "logger": _NoLog(), "load_config": lambda: {}}
    exec(compile(ast.Module([noise_node], []), "<noise>", "exec"), ns)
    exec(ast.get_source_segment(src, fn), ns)
    return ns["filter_error_logs"]


def main():
    fel = _load_filter()

    noise = [
        # the exact bugfixer#834 seed line
        {"module": "bugfixer-core",
         "log": "2026-08-13 22:50:01 [ERROR] Edit search snippet not found in "
                "'src/spoke.py'; skipping this edit (search starts with: 'None, "
                "lambda: self.queries.get_recent_sessions(...)')"},
        {"module": "bugfixer-core",
         "log": "[ERROR] No fixes could be applied (src/spoke.py: search snippet not found)."},
        {"module": "bugfixer-core",
         "log": "[ERROR] Error parsing or applying JSON fix: invalid syntax"},
        {"module": "bugfixer-core",
         "log": "[ERROR] AI generated invalid JSON format"},
        {"module": "bugfixer-core",
         "log": "[ERROR] ABORTING fix: new content contains a truncation marker"},
        {"module": "bugfixer-core",
         "log": "[ERROR] No verified fix found after 3 attempt(s) — AI generated invalid JSON format"},
    ]
    real = [
        {"module": "spoke",
         "log": "2026-08-13 22:50:06 [ERROR] KeyError: 'lookback_minutes' in get_recent_sessions"},
        {"module": "spoke",
         "log": "2026-08-13 22:50:07 [ERROR] Traceback (most recent call last): ZeroDivisionError"},
    ]

    kept = fel(noise + real)
    kept_texts = [k["log"] for k in kept]

    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    check("all fix-engine chatter dropped",
          not any(n["log"] in kept_texts for n in noise))
    check("exact #834 anchor-miss line dropped",
          not any("search snippet not found" in t for t in kept_texts))
    check("genuine KeyError preserved",
          any("KeyError" in t for t in kept_texts))
    check("genuine traceback preserved",
          any("ZeroDivisionError" in t for t in kept_texts))
    check("only the 2 real errors survive", len(kept) == 2)

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
