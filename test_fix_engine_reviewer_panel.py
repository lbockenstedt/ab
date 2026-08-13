#!/usr/bin/env python3
"""Self-test for fix_engine.py's reviewer-panel conversion (LLM Selection
Redesign, Phase 5, call site #10 -- fix_engine.py's _select_review_panel/
_run_reviewer_turn, the multi-model panel used by review_fix).

Run:  python3 test_fix_engine_reviewer_panel.py

fix_engine.py cannot be imported directly (it transitively pulls in main.py's
circular import chain), so this extracts _select_review_panel/
_run_reviewer_turn (+ their constant/helper dependencies) via ast and execs
them with stubbed dependencies -- the established convention in this repo
(see test_fix_engine_triage_identify_requirements.py).

Background: review_fix used to iterate a static, operator-configured slot
pool (_REVIEW_SLOTS, falling back to _CODE_SLOTS) and pin each reviewer to a
specific provider via force_provider=provider_n. LlmRequirements has no
per-call specific-provider-pin equivalent by design, so this conversion
replaces the pool with _select_review_panel: N successive calls to
model_selection.select_model, each excluding every previously-picked model
(+ the builder's model), naturally producing a panel of distinct models.
Each reviewer's turn is then dispatched directly against its own already-
picked candidate via llm_client._try_candidate (the picker path's own
single-candidate executor) -- not a fresh call_llm(requirements=...) pick,
since the panel's diversity decision was already made up front.

Covers:
1. _select_review_panel: picks up to _REVIEW_PANEL_MAX distinct candidates,
   excludes the resolved builder-slot's ModelKey, stops when select_model
   returns nothing further, and returns [] when nothing is configured.
2. _run_reviewer_turn (tools-blind branch, repo/head_sha absent): dispatches
   via llm_client._try_candidate with the exact reviewer_candidate given, not
   a fresh pick and not force_provider=.
3. _run_reviewer_turn (native claude_cli branch, repo_checkout_path given):
   same dispatch mechanism, with enable_native_tools=True/json_schema set,
   for a claude_cli reviewer_candidate.
4. _run_reviewer_turn (tool-calling loop, repo+head_sha given, non-claude_cli
   provider): the tools=_REVIEW_TOOLS loop also dispatches via
   llm_client._try_candidate against the same fixed candidate every turn (no
   re-picking mid-loop).
5. _run_reviewer_turn raises when _try_candidate reports an error (so the
   caller's existing failed-reviewer bookkeeping in review_fix is unaffected).
6. _run_reviewer_turn(reviewer_candidate=None) (the "Default Reviewer"
   no-candidates-configured fallback) uses a plain call_llm with no pin.
"""
import ast
import json
import re


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load_ns(want_funcs, extra_ns=None):
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    segs = []
    want_assigns = {
        "_REVIEW_TOOLS", "_REVIEW_TOOL_MAX_ITER", "_REVIEW_TOOL_MAX_FILES",
        "_REVIEW_FILE_MAX_CHARS", "_REVIEWER_JSON_SCHEMA", "_DIFF_FILE_HEADER_RE",
        "_REVIEW_PANEL_MAX",
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in want_assigns:
                    segs.append(ast.get_source_segment(src, node))
    ns = {"re": re, "json": json, "logger": _NoLog()}
    if extra_ns:
        ns.update(extra_ns)
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _candidate(provider, model, key=None):
    return {
        "key": key or (provider, "", model), "provider": provider, "model": model,
        "base_url": "", "api_key": "", "rpm": 0, "available": True, "unavailable_reason": None,
        "caps": {"supports_tools": True, "native_agentic_tools": provider == "claude_cli",
                "supports_mutating_agent": provider == "claude_cli",
                "supports_structured_output": True, "cost_tier": "cheap",
                "max_complexity": "large", "context_window": 200000},
    }


def main():
    ok = True
    import model_selection

    # ---- 1. _select_review_panel ----
    all_candidates = [
        _candidate("groq", "llama-70b"),
        _candidate("anthropic", "claude-sonnet"),
        _candidate("ollama", "qwen-32b"),
        _candidate("claude_cli", "claude"),
    ]

    class _FakeLlmClient:
        def _enumerate_candidates(self, config):
            return list(all_candidates)
        def get_llm_perf_snapshot(self):
            return {}
        def _model_key(self, provider, base_url, model):
            return (provider, base_url, model)

    def _get_provider_config(n, config):
        # slot 1 == the first candidate (groq), matching real _get_provider_config's shape.
        if n == 1:
            return ("groq", "key", "llama-70b", "")
        return (None, None, None, None)

    ns_panel = _load_ns(
        {"_select_review_panel"},
        {"llm_client": _FakeLlmClient(), "_get_provider_config": _get_provider_config,
         "load_config": lambda: {}},
    )

    panel = ns_panel["_select_review_panel"]({}, builder_n=1)
    ok &= _check("_select_review_panel: excludes the builder's model (groq/llama-70b)",
                all(c["provider"] != "groq" for c in panel))
    ok &= _check("_select_review_panel: returns the other 3 distinct configured models",
                len(panel) == 3 and {c["provider"] for c in panel} == {"anthropic", "ollama", "claude_cli"})

    panel_no_builder = ns_panel["_select_review_panel"]({}, builder_n=0)
    ok &= _check("_select_review_panel: builder_n=0 excludes nothing -> all 4 configured models",
                len(panel_no_builder) == 4)

    class _EmptyLlmClient:
        def _enumerate_candidates(self, config):
            return []
        def get_llm_perf_snapshot(self):
            return {}
        def _model_key(self, provider, base_url, model):
            return (provider, base_url, model)

    ns_panel_empty = _load_ns(
        {"_select_review_panel"},
        {"llm_client": _EmptyLlmClient(), "_get_provider_config": _get_provider_config,
         "load_config": lambda: {}},
    )
    ok &= _check("_select_review_panel: nothing configured -> []",
                ns_panel_empty["_select_review_panel"]({}, builder_n=1) == [])

    # ---- 2-6. _run_reviewer_turn ----
    def _make_turn_ns(try_candidate_stub, call_llm_stub):
        class _FakeLlmClient2:
            def _try_candidate(self, candidate, messages, tools, stream, task_id, config, **kwargs):
                return try_candidate_stub(candidate, messages, tools, stream, task_id, config, **kwargs)

        return _load_ns(
            {"_run_reviewer_turn", "_parse_review_text_tool_calls", "_fetch_repo_file_for_review"},
            {
                "llm_client": _FakeLlmClient2(),
                "load_config": lambda: {},
                "call_llm": call_llm_stub,
            },
        )

    # 2. tools-blind branch (no repo/head_sha): dispatches via _try_candidate.
    captured2 = {}

    def _try_candidate2(candidate, messages, tools, stream, task_id, config, **kwargs):
        captured2["args"] = (candidate, messages, tools, kwargs)
        return '{"confidence": 0.9, "verdict": "Approve", "critique": "fine"}', None

    ns2 = _make_turn_ns(_try_candidate2, call_llm_stub=None)
    rc2 = _candidate("groq", "llama-70b")
    res2 = ns2["_run_reviewer_turn"]("Review this diff.", "system prompt", rc2, "task1", None, None)
    ok &= _check("tools-blind: dispatches via llm_client._try_candidate with the EXACT reviewer_candidate",
                captured2["args"][0] is rc2)
    ok &= _check("tools-blind: no tools= passed (repo/head_sha absent)", captured2["args"][2] is None)
    ok &= _check("tools-blind: returns the candidate's raw text result",
                res2 == '{"confidence": 0.9, "verdict": "Approve", "critique": "fine"}')

    # 3. native claude_cli branch (repo_checkout_path given).
    captured3 = {}

    def _try_candidate3(candidate, messages, tools, stream, task_id, config, **kwargs):
        captured3["args"] = (candidate, messages, tools, kwargs)
        return '{"confidence": 0.85, "verdict": "Approve", "critique": "ok"}', None

    ns3 = _make_turn_ns(_try_candidate3, call_llm_stub=None)
    rc3 = _candidate("claude_cli", "claude")
    res3 = ns3["_run_reviewer_turn"]("Review this diff.", "system prompt", rc3, "task1", None, None,
                                     repo_checkout_path="/tmp/checkout")
    ok &= _check("native claude_cli: dispatches via llm_client._try_candidate with the claude_cli candidate",
                captured3["args"][0] is rc3)
    ok &= _check("native claude_cli: enable_native_tools=True, json_schema set",
                captured3["args"][3].get("enable_native_tools") is True
                and captured3["args"][3].get("json_schema") is not None)

    # 4. tool-calling loop (repo+head_sha given, non-claude_cli provider):
    #    dispatches every turn against the SAME fixed candidate, no re-pick.
    captured4 = {"calls": []}

    def _try_candidate4(candidate, messages, tools, stream, task_id, config, **kwargs):
        captured4["calls"].append((candidate, tools))
        return {"text": '{"confidence": 0.8, "verdict": "Approve", "critique": "done"}', "tool_calls": []}, None

    ns4 = _make_turn_ns(_try_candidate4, call_llm_stub=None)
    rc4 = _candidate("anthropic", "claude-sonnet")
    res4 = ns4["_run_reviewer_turn"]("diff --git a/x.py b/x.py\n...", "system prompt", rc4, "task1",
                                     repo="fake-repo", head_sha="deadbeef")
    ok &= _check("tool loop: exactly one turn taken (no tool_calls -> returns immediately)",
                len(captured4["calls"]) == 1)
    ok &= _check("tool loop: dispatched against the SAME fixed candidate", captured4["calls"][0][0] is rc4)
    ok &= _check("tool loop: tools=_REVIEW_TOOLS passed", captured4["calls"][0][1] is not None)

    # 5. _try_candidate reports an error -> _run_reviewer_turn raises (so
    #    review_fix's existing except-block bookkeeping still triggers).
    def _try_candidate5(candidate, messages, tools, stream, task_id, config, **kwargs):
        return None, Exception("provider unavailable")

    ns5 = _make_turn_ns(_try_candidate5, call_llm_stub=None)
    rc5 = _candidate("groq", "llama-70b")
    raised = False
    try:
        ns5["_run_reviewer_turn"]("Review this diff.", "system prompt", rc5, "task1", None, None)
    except Exception as e:
        raised = "provider unavailable" in str(e)
    ok &= _check("_try_candidate error -> _run_reviewer_turn raises (caller's except block still fires)", raised)

    # 6. reviewer_candidate=None -> Default Reviewer fallback uses plain call_llm.
    captured6 = {}

    def _capturing_call_llm6(prompt, **kwargs):
        captured6["kwargs"] = kwargs
        return '{"confidence": 0.7, "verdict": "Approve", "critique": "meh"}'

    ns6 = _make_turn_ns(try_candidate_stub=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("_try_candidate should not be called for the Default Reviewer fallback")),
        call_llm_stub=_capturing_call_llm6)
    res6 = ns6["_run_reviewer_turn"]("Review this diff.", "system prompt", None, "task1", None, None)
    ok &= _check("Default Reviewer (candidate=None): uses plain call_llm, not _try_candidate",
                "kwargs" in captured6)
    ok &= _check("Default Reviewer: no force_provider/model_override pin",
                "force_provider" not in captured6["kwargs"] and "model_override" not in captured6["kwargs"])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running fix_engine.py reviewer-panel self-test...")
    import sys
    sys.exit(main())
