#!/usr/bin/env python3
"""Regression guard: filter_error_logs must honour a line's DECLARED level.

Run:  python3 test_log_level_gating.py

filter_error_logs documents its intent plainly -- "WARNINGs are excluded: the
LLM task is to find actionable *errors*, not routine warnings" -- but nothing
implemented it. The inclusion test was a signature regex

    \\[(ERROR|CRITICAL)\\]|Traceback|Exception|Error[: ]|Failed

searched against the WHOLE line. So any line at any level was kept as an
"error" if its message text merely contained the word "error".

Reviewer rejections are the worst case, because the reviewer's explanation is
free prose that routinely says things like "The reported error originates from
`lm.self_update` runtime, but the proposed fix modifies `install_all.sh`".
That is a WARNING about a rejected patch -- normal, healthy pipeline behaviour
-- and it was filed as a product fault. ab#137, #138, #173 and #174 are all
exactly this, and an INFO line saying "no Error: here" would have been too.

The fix reads the level the line declares (``[ERROR]`` or the python-logging
``- AppBuilder - WARNING -`` form) from the line PREFIX only, and:

  * a declared ERROR/CRITICAL is kept;
  * any other declared level is dropped regardless of its prose;
  * a line declaring NO level falls through to the old signature test, so a
    bare traceback or an uncaught-exception dump is unaffected.

Reading only the prefix is the crux: scanning the whole line is precisely what
let prose masquerade as a severity.
"""
import ast
import json
import re
import sys


def _load():
    src = open("log_scan.py").read()
    tree = ast.parse(src)
    want_assigns = ("_SELF_SCAN_NOISE", "_LOG_LEVEL_NAMES", "_LOG_LEVEL_RE",
                    "_LOG_LEVEL_PREFIX_CHARS")
    want_funcs = ("_explicit_log_level", "filter_error_logs")
    assigns = [n for n in tree.body
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) in want_assigns for t in n.targets)]
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in want_funcs]

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {"json": json, "re": re, "logger": _NoLog(), "load_config": lambda: {}}
    exec(compile(ast.Module(assigns, []), "<consts>", "exec"), ns)
    for fn in fns:
        exec(ast.get_source_segment(src, fn), ns)
    return ns["_explicit_log_level"], ns["filter_error_logs"]


def main():
    print("Running filter_error_logs level-gating self-test...")
    lvl, fel = _load()
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    # ---- _explicit_log_level ------------------------------------------
    check("dash-style level found past the logger name",
          lvl("2026-08-30 12:00:00 - AppBuilder - WARNING - x") == "WARNING")
    check("logger name is NOT mistaken for a level",
          lvl("2026-08-30 12:00:00 - AppBuilder - ERROR - x") == "ERROR")
    check("bracketed level found", lvl("[ERROR] boom") == "ERROR")
    check("lowercase tag found", lvl("[warning] x") == "WARNING")
    check("WARN normalizes to WARNING", lvl("- WARN - x") == "WARNING")
    check("FATAL normalizes to CRITICAL", lvl("[FATAL] x") == "CRITICAL")
    check("no tag -> None", lvl("no level here at all") is None)
    check("substring is not a tag", lvl("x TERRORS y") is None)
    check("ERRORS is not a level", lvl("2026 - svc - ERRORS - nope") is None)
    check("empty/None -> None", lvl("") is None and lvl(None) is None)
    check("a level mentioned only LATE in prose is not the line's level",
          lvl("2026-08-30 - AppBuilder - WARNING - " + "x" * 200
              + " - ERROR - late") == "WARNING")

    # ---- filter_error_logs --------------------------------------------
    # The four real issue lines this was filed from.
    rejections = [
        {"module": "ab", "log":
         "2026-08-30 12:00:00 - AppBuilder - WARNING - Reviewer REJECTED fix for "
         "lbockenstedt/lm:452: The reported error originates from `lm.self_update` "
         "runtime (`core/src/messaging/self_update.py:733`), but the proposed fix "
         "modifies `install_all.sh`."},
        {"module": "ab", "log":
         "2026-08-30 12:00:01 - AppBuilder - WARNING - Reviewer REJECTED fix: The "
         "pgrep pattern is unlikely to match; the 'no other git process' check "
         "Failed to be meaningful."},
        {"module": "ab", "log":
         "2026-08-30 12:00:02 - AppBuilder - WARNING - Reviewer REJECTED fix: this "
         "workaround has several critical flaws and raises an Exception risk."},
        {"module": "ab", "log":
         "2026-08-30 12:00:03 - AppBuilder - INFO - tenant switch committed, no "
         "Error: here"},
    ]
    # These must SURVIVE -- dropping them would be a far worse failure.
    genuine = [
        {"module": "ab", "log":
         "2026-08-30 12:00:04 - AppBuilder - ERROR - Genuine KeyError in handler"},
        {"module": "ab", "log":
         "2026-08-30 12:00:05 - AppBuilder - CRITICAL - disk full, aborting"},
        {"module": "sp", "log":
         "Traceback (most recent call last): ZeroDivisionError"},
        {"module": "sp", "log":
         "UNCAUGHT EXCEPTION: Did not find CR at end of boundary (48)"},
        {"module": "sp", "log": "[ERROR] device sync aborted"},
    ]

    kept = fel(rejections + genuine)
    kept_texts = [k["log"] for k in kept]

    check("WARNING reviewer-rejection prose dropped",
          not any("REJECTED fix" in t for t in kept_texts))
    check("INFO line mentioning 'Error:' dropped",
          not any("tenant switch committed" in t for t in kept_texts))
    check("declared ERROR kept", any("Genuine KeyError" in t for t in kept_texts))
    check("declared CRITICAL kept", any("disk full" in t for t in kept_texts))
    check("untagged traceback still kept",
          any("ZeroDivisionError" in t for t in kept_texts))
    check("untagged uncaught exception still kept",
          any("Did not find CR" in t for t in kept_texts))
    check("bracketed ERROR kept", any("device sync aborted" in t for t in kept_texts))
    check("exactly the 5 genuine errors survive", len(kept) == 5)

    # A WARNING that is genuinely alarming is still a WARNING: the contract is
    # about the DECLARED level, not about second-guessing severity. Pinned so a
    # future change cannot quietly reintroduce prose-sniffing.
    only_warn = fel([{"module": "ab", "log":
                      "2026-08-30 - AppBuilder - WARNING - Exception while "
                      "retrying: Failed to connect"}])
    check("a WARNING is dropped even with error signatures in its text",
          only_warn == [])

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
