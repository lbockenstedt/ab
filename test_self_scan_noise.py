#!/usr/bin/env python3
"""Regression guard: filter_error_logs must drop AppBuilder's own fix-engine
operational chatter so the self-log scanner cannot file phantom issues about
AppBuilder fixing its own fix attempts (ab#834).

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
    # filter_error_logs now calls _explicit_log_level, which in turn needs the
    # _LOG_LEVEL_* constants -- an extracted function only sees what is execed
    # into its namespace, so omitting any of these is a NameError at run time.
    _want_assigns = ("_SELF_SCAN_NOISE", "_LOG_LEVEL_NAMES", "_LOG_LEVEL_RE",
                     "_LOG_LEVEL_PREFIX_CHARS")
    _want_funcs = ("_explicit_log_level", "filter_error_logs")
    assign_nodes = [
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) in _want_assigns for t in n.targets)
    ]
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in _want_funcs]
    assert len(assign_nodes) == len(_want_assigns), "missing a module constant"
    assert len(fns) == len(_want_funcs), "missing a module function"

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {"json": json, "re": re, "logger": _NoLog(), "load_config": lambda: {}}
    exec(compile(ast.Module(assign_nodes, []), "<consts>", "exec"), ns)
    for fn in fns:
        exec(ast.get_source_segment(src, fn), ns)
    return ns["filter_error_logs"]


def main():
    fel = _load_filter()

    noise = [
        # the exact ab#834 seed line
        {"module": "ab-core",
         "log": "2026-08-13 22:50:01 [ERROR] Edit search snippet not found in "
                "'src/spoke.py'; skipping this edit (search starts with: 'None, "
                "lambda: self.queries.get_recent_sessions(...)')"},
        {"module": "ab-core",
         "log": "[ERROR] No fixes could be applied (src/spoke.py: search snippet not found)."},
        {"module": "ab-core",
         "log": "[ERROR] Error parsing or applying JSON fix: invalid syntax"},
        {"module": "ab-core",
         "log": "[ERROR] AI generated invalid JSON format"},
        {"module": "ab-core",
         "log": "[ERROR] ABORTING fix: new content contains a truncation marker"},
        {"module": "ab-core",
         "log": "[ERROR] No verified fix found after 3 attempt(s) — AI generated invalid JSON format"},
        # config/state I/O operational errors (ab#806) — environment
        # conditions load_config/save_config already handle, not code defects.
        {"module": "ab-core",
         "log": "2026-08-13 02:05:49 [ERROR] Error reading persistent config "
                "/etc/ab/config.json: Expecting value: line 1 column 1 (char 0)"},
        {"module": "ab-core",
         "log": "[ERROR] Critical failure saving config: [Errno 28] No space left on device"},
        # (3) reviewer-panel verdict chatter — RECOVERED in the same call: the
        # reply is retried once against the same candidate, and a raised parse
        # error escalates the seat to a cloud fallback. ab#191/#202/#213/#214/
        # #215/#216 are six issues asserting the same non-fact.
        {"module": "ab-core",
         "log": "2026-09-20 16:42:54 [ERROR] Reviewer (copilot) JSON parse failed "
                "(Expecting property name enclosed in double quotes: line 1 column 2 "
                "(char 1)) — raw response: 'The actual file at this commit looks clean'"},
        {"module": "ab-core",
         "log": "[ERROR] Reviewer (openrouter/qwen) JSON parse failed (Extra data: line 2)"},
        {"module": "ab-core",
         "log": "[WARNING] Reviewer (copilot/claude-opus-5) returned no parseable verdict "
                "after retry - raw response: 'Looks fine to me.'"},
        {"module": "ab-core",
         "log": "[INFO] Reviewer (copilot) reply had no parseable verdict — retrying once"},
        {"module": "ab-core",
         "log": "[WARNING] Reviewer (ollama-local) deferred — LLM providers cooling down: 429"},
        # (4) terminal pipeline OUTCOMES — the decision is already recorded on
        # the issue and shown in the status table (ab#198/#199/#203/#210).
        {"module": "ab-core",
         "log": "2026-09-17 15:36:36 [ERROR] Manual retry failed for lbockenstedt/lm:440: "
                "Held for human review (no model meets this fix's requirements)"},
        {"module": "ab-core",
         "log": "[ERROR] Manual retry failed for lbockenstedt/lm:487: AI returned no edits "
                "after 3 attempt(s) — The JSON parsed but contained no applicable changes."},
        {"module": "ab-core",
         "log": "[ERROR] Manual retry failed for lbockenstedt/lm:440: Reviewers rejected the "
                "fix after 3 attempt(s) — confidence 57%"},
        # (5) provider rate-limiting the LLM client already absorbs (Retry-After,
        # breaker trip, entry backoff, failover to the next candidate).
        {"module": "ab-core",
         "log": "2026-08-22 11:03:12 - AppBuilder - ERROR - LLM 429 at "
                "https://ollama.com/api/chat after 6 attempts."},
        {"module": "ab-core",
         "log": "[ERROR] LLM 429 at https://api.githubcopilot.com/chat/completions "
                "after 4 attempts."},
        {"module": "ab-core",
         "log": "[ERROR] LLM HTTPError 429 at https://ollama.com/api/chat. Backing off 12.0s."},
    ]
    real = [
        {"module": "spoke",
         "log": "2026-08-13 22:50:06 [ERROR] KeyError: 'lookback_minutes' in get_recent_sessions"},
        {"module": "spoke",
         "log": "2026-08-13 22:50:07 [ERROR] Traceback (most recent call last): ZeroDivisionError"},
        # Discriminators: a PRODUCT error may legitimately mention JSON parsing
        # or a rejection. Only AppBuilder's OWN reviewer/pipeline wording is
        # noise, so these must survive — over-filtering would hide real bugs.
        {"module": "spoke",
         "log": "[ERROR] JSON parse failed reading /etc/lm/state.json — device sync aborted"},
        {"module": "spoke",
         "log": "[ERROR] Switch rejected the fix-up VLAN push: insufficient privilege"},
        {"module": "spoke",
         "log": "[ERROR] Manual retry failed for the SNMP walk on 10.0.0.5: timeout"},
        # A PRODUCT error that merely mentions 429 is not the LLM client's own
        # handled rate-limit outcome, and must still be reported.
        {"module": "spoke",
         "log": "[ERROR] NetBox API returned 429 while pushing prefixes; sync incomplete"},
    ]

    kept = fel(noise + real)
    kept_texts = [k["log"] for k in kept]

    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    check("all fix-engine + config-I/O chatter dropped",
          not any(n["log"] in kept_texts for n in noise))
    check("exact #834 anchor-miss line dropped",
          not any("search snippet not found" in t for t in kept_texts))
    check("exact #806 config-read error dropped",
          not any("Error reading persistent config" in t for t in kept_texts))
    check("genuine KeyError preserved",
          any("KeyError" in t for t in kept_texts))
    check("genuine traceback preserved",
          any("ZeroDivisionError" in t for t in kept_texts))
    check("reviewer JSON-parse chatter dropped",
          not any("Reviewer (copilot) JSON parse failed" in t for t in kept_texts))
    check("no-parseable-verdict chatter dropped",
          not any("no parseable verdict" in t for t in kept_texts))
    check("cooldown deferral dropped",
          not any("providers cooling down" in t for t in kept_texts))
    check("terminal pipeline outcomes dropped",
          not any(t.startswith("2026-09-17") or "Manual retry failed for lbockenstedt" in t
                  for t in kept_texts))
    # Over-filtering is the dangerous failure mode: these three read like the
    # suppressed wording but come from a SPOKE and are genuine product errors.
    check("product JSON-parse error preserved",
          any("device sync aborted" in t for t in kept_texts))
    check("product rejection error preserved",
          any("insufficient privilege" in t for t in kept_texts))
    check("product 'manual retry' error preserved",
          any("SNMP walk" in t for t in kept_texts))
    check("handled 429 exhaustion dropped",
          not any("after 6 attempts" in t or "after 4 attempts" in t for t in kept_texts))
    check("handled 429 backoff line dropped",
          not any("LLM HTTPError 429" in t for t in kept_texts))
    check("product 429 error preserved",
          any("sync incomplete" in t for t in kept_texts))
    check("only the 6 real errors survive", len(kept) == 6)

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
