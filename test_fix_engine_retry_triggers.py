#!/usr/bin/env python3
"""Self-test for fix_engine.py's next_attempt_requirements() (LLM Selection
Redesign, Phase 5, call site #12 -- the fix-generation/escalation ladder).

Run:  python3 test_fix_engine_retry_triggers.py

fix_engine.py cannot be imported directly (it transitively pulls in main.py's
circular import chain), so this extracts next_attempt_requirements (+ its
_COMPLEXITY_RANK_ORDER constant dependency) via ast and execs it with the
real model_selection.LlmRequirements -- the established convention in this
repo (see test_fix_engine_reviewer_panel.py, test_fix_engine_triage_identify_
requirements.py).

Background: process_single_issue's retry loop used to walk a fixed
[(slot, model_override)] ladder built from every configured provider's
escalation_models (`ladder[(attempt-1) % len(ladder)]`), escalating "to the
next provider" purely by position. The redesign replaces that with
next_attempt_requirements(prev_reqs, failure_kind, tried_key, config): EVERY
failure kind excludes the just-tried ModelKey (so a retry can never repeat
it -- exclusions make the old wraparound modulus unnecessary), and ONLY
low_confidence additionally raises `complexity` one rank, pushing the picker
into a costlier tier. That one sentence is the whole ladder redesign.

Covers, for each of the 5 failure kinds the retry loop now recognizes
(invalid_json, review_rejected, low_confidence, qa_failed, error):
1. The just-tried ModelKey is always added to exclude_models.
2. Complexity is raised one rank ONLY for low_confidence; every other kind
   leaves complexity unchanged.
3. Complexity never exceeds "large" (already-capped stays capped).
4. Prior exclusions accumulate across successive calls (never lost).
5. tried_key=None (e.g. a resumed pending_fix with no fresh builder) doesn't
   crash and doesn't add a bogus exclusion.
6. restrict/needs_structured_output/min_context_tokens/must_escalate_to_human
   are carried through unchanged (only complexity/exclude_models mutate).
"""
import ast


def _load_ns():
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    segs = []
    want_funcs = {"next_attempt_requirements"}
    want_assigns = {"_COMPLEXITY_RANK_ORDER"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in want_assigns:
                    segs.append(ast.get_source_segment(src, node))
    ns = {}
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True
    from model_selection import LlmRequirements

    ns = _load_ns()
    next_attempt_requirements = ns["next_attempt_requirements"]

    base = LlmRequirements(complexity="large", needs_structured_output=True,
                           min_context_tokens=500, restrict="cloud",
                           must_escalate_to_human=True)
    key1 = ("groq", "", "llama-70b")

    # ---- exclusion added for every failure kind ----
    for kind in ("invalid_json", "review_rejected", "low_confidence", "qa_failed", "error"):
        r = next_attempt_requirements(base, kind, key1, {})
        ok &= _check(f"{kind}: excludes the just-tried model", key1 in r.exclude_models)

    # ---- complexity: ONLY low_confidence raises it ----
    mid = LlmRequirements(complexity="small")
    for kind in ("invalid_json", "review_rejected", "qa_failed", "error"):
        r = next_attempt_requirements(mid, kind, key1, {})
        ok &= _check(f"{kind}: complexity unchanged ({r.complexity} == small)", r.complexity == "small")
    r = next_attempt_requirements(mid, "low_confidence", key1, {})
    ok &= _check("low_confidence: complexity raised one rank (small -> medium)", r.complexity == "medium")

    # ---- complexity cap: already at 'large' stays at 'large' ----
    at_cap = LlmRequirements(complexity="large")
    r = next_attempt_requirements(at_cap, "low_confidence", key1, {})
    ok &= _check("low_confidence at complexity=large: stays capped at large", r.complexity == "large")

    # ---- exclusions accumulate across successive calls ----
    key2 = ("anthropic", "", "claude-sonnet")
    r1 = next_attempt_requirements(base, "qa_failed", key1, {})
    r2 = next_attempt_requirements(r1, "qa_failed", key2, {})
    ok &= _check("exclusions accumulate: both prior models excluded",
                 key1 in r2.exclude_models and key2 in r2.exclude_models)

    # ---- tried_key=None: no crash, no bogus exclusion ----
    r = next_attempt_requirements(base, "error", None, {})
    ok &= _check("tried_key=None: exclude_models unchanged from prev_reqs",
                 set(r.exclude_models) == set(base.exclude_models))

    # ---- untouched fields carried through unchanged ----
    r = next_attempt_requirements(base, "qa_failed", key1, {})
    ok &= _check("restrict carried through unchanged", r.restrict == "cloud")
    ok &= _check("needs_structured_output carried through unchanged", r.needs_structured_output is True)
    ok &= _check("min_context_tokens carried through unchanged", r.min_context_tokens == 500)
    ok &= _check("must_escalate_to_human carried through unchanged", r.must_escalate_to_human is True)

    print("\nALL CASES PASSED" if ok else "\nSOME CASES FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    print("Running fix_engine.py next_attempt_requirements self-test...")
    sys.exit(main())
