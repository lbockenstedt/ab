#!/usr/bin/env python3
"""Self-test for call_llm's new requirements= path (Phase 4 of the LLM
Selection Redesign): candidate enumeration, the identity-keyed circuit
breakers, and _call_llm_with_requirements' selection + failover + safety-
floor logic.

Run:  python3 bugfixer/test_llm_client_requirements_path.py

llm_client.py imports `main` (app-init side effects — see
test_skills_loader.py's docstring), so this extracts the relevant pieces by
source via ast and execs them with stubbed dependencies, following
test_llm_concurrency.py's established pattern for extracting call_llm's
supporting machinery as one linked segment.
"""
import ast
import sys
import threading
import time

import model_registry
import model_selection


def _load_ns():
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    want_funcs = {
        "_endpoint_key", "_cb_trip", "_cb_remaining", "_model_lock",
        "_iter_configured_endpoints", "_enumerate_candidates", "_configured_entries",
        "_try_candidate", "_call_llm_with_requirements",
        "_model_key", "get_llm_perf_snapshot", "_get_llm_perf_store",
        "_provider_configured", "_provider_is_nokey", "_is_ollama", "_is_ollama_cloud", "_is_lmstudio",
        "_routed_model_dead", "_get_category_semaphore",
    }
    want_assign = {
        "_ALL_SLOTS", "_CODE_SLOTS", "_LOG_SLOTS", "_REVIEW_SLOTS",
        "_ENDPOINT_CB_LOCK", "_ENDPOINT_CREDIT_CB", "_MODEL_RATE_CB",
        "_MODEL_LOCKS_LOCK", "_MODEL_LOCKS",
        "_LLM_PERF_STORE", "_LLM_PERF_LOCK",
        "_CATEGORY_SEMAPHORES", "_CATEGORY_SEM_LOCK",
        "_CREDIT_COOLDOWN_SECONDS", "_RATELIMIT_COOLDOWN_SECONDS",
        "_ROUTED_404", "_ROUTED_404_LOCK",
        "OLLAMA_CLOUD_PROVIDER", "OLLAMA_CLOUD_BASE_URL",
    }
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in want_assign:
                    segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") in want_assign:
            segs.append(ast.get_source_segment(src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    class LLMCreditExhausted(Exception):
        pass

    class LlmHumanEscalationNeeded(Exception):
        pass

    calls = {"provider_calls": [], "raise_exc": None, "usage": {}}

    def _call_provider_timed(provider, model, api_key, base_url, messages, tools, effective_stream, task_id,
                             config, **kwargs):
        calls["provider_calls"].append((provider, model, base_url))
        exc = calls["raise_exc"]
        if callable(exc):
            exc = exc(provider, model)
        if exc is not None:
            raise exc
        return f"ok from {provider}/{model}"

    import os as _os

    ns = {
        "threading": threading, "time": time, "os": _os,
        "logger": _NoLog(), "datetime": __import__("datetime").datetime,
        "model_registry": model_registry, "model_selection": model_selection,
        "main": type("M", (), {"state": {}})(),
        "LLMCreditExhausted": LLMCreditExhausted,
        "LlmHumanEscalationNeeded": LlmHumanEscalationNeeded,
        "_call_provider_timed": _call_provider_timed,
        "llm_perf": type("FakeLlmPerf", (), {
            "snapshot": staticmethod(lambda store: {}),
            "load": staticmethod(lambda path: {}),
        })(),
        "config_store": type("FakeConfigStore", (), {"LLM_PERF_FILE": "/dev/null"})(),
        "load_config": lambda: {},
    }
    exec("\n\n".join(segs), ns)
    ns["_test_calls"] = calls
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _entry(id_, provider, model, api_key="k", base_url="", rpm=0, enabled=True):
    return {"id": id_, "provider": provider, "model": model, "api_key": api_key,
           "base_url": base_url, "rpm": rpm, "enabled": enabled}


def main():
    print("Running bugfixer call_llm requirements= path self-test...")
    ok = True

    # --- _enumerate_candidates -------------------------------------------------

    ns = _load_ns()
    config = {"llm_entries": [_entry("e1", "ollama", "qwen2.5-coder:14b", api_key=""),
                              _entry("e2", "anthropic", "claude-sonnet-5")]}
    candidates = ns["_enumerate_candidates"](config)
    ok &= _check("enumerate_candidates returns one candidate per configured entry",
                len(candidates) == 2)
    ok &= _check("a no-key local provider (ollama) with no api_key is still configured",
                any(c["provider"] == "ollama" and c["available"] for c in candidates))
    ok &= _check("each candidate carries resolved capability data from model_registry",
                all("cost_tier" in c["caps"] for c in candidates))

    disabled_cfg = {"llm_entries": [_entry("e1", "ollama", "m", api_key="", enabled=False)]}
    ok &= _check("an entry with enabled=False is excluded from candidates",
                ns["_enumerate_candidates"](disabled_cfg) == [])

    unconfigured_cfg = {"llm_entries": [_entry("e1", "anthropic", "", api_key="")]}
    ok &= _check("an entry missing a model is excluded (not configured)",
                ns["_enumerate_candidates"](unconfigured_cfg) == [])

    dup_cfg = {"llm_entries": [_entry("e1", "openai", "gpt-9", api_key="k", base_url="https://x"),
                               _entry("e2", "openai", "gpt-9", api_key="k2", base_url="https://x")]}
    ok &= _check("two entries resolving to the same ModelKey collapse into one candidate",
                len(ns["_enumerate_candidates"](dup_cfg)) == 1)

    # --- legacy env-var slots (live-read) --------------------------------------

    env_cfg = {"LLM_PROVIDER_1": "ollama", "LLM_MODEL_1": "llama3.1:8b"}
    env_candidates = ns["_enumerate_candidates"](env_cfg)
    ok &= _check("a legacy LLM_PROVIDER_N/LLM_MODEL_N pair (no llm_entries at all) still yields a candidate",
                any(c["provider"] == "ollama" and c["model"] == "llama3.1:8b" for c in env_candidates))

    # --- identity-keyed credit cooldown marks a candidate unavailable ----------

    cb_cfg = {"llm_entries": [_entry("e1", "anthropic", "claude-sonnet-5", base_url="https://api.anthropic.com")]}
    ns["_cb_trip"](ns["_ENDPOINT_CREDIT_CB"], ns["_endpoint_key"]("anthropic", "https://api.anthropic.com"),
                  "test trip", 3600, "credit", "anthropic")
    cb_candidates = ns["_enumerate_candidates"](cb_cfg)
    ok &= _check("a credit-tripped endpoint's candidate is present but marked unavailable",
                len(cb_candidates) == 1 and cb_candidates[0]["available"] is False
                and cb_candidates[0]["unavailable_reason"] == "credit_cooldown")

    # A no-key local provider's credit trip is IGNORED (mirrors the slot-based
    # behavior — a local server has no billing to exhaust).
    ns["_cb_trip"](ns["_ENDPOINT_CREDIT_CB"], ns["_endpoint_key"]("ollama", ""), "bogus", 3600, "credit", "ollama")
    ok &= _check("a credit trip against a no-key local provider is ignored, not recorded",
                ns["_endpoint_key"]("ollama", "") not in ns["_ENDPOINT_CREDIT_CB"])

    # --- _configured_entries feeds safety_floor ---------------------------------

    floor_cfg = {"llm_entries": [_entry("e1", "anthropic", "claude-sonnet-5"),
                                 _entry("e2", "ollama", "qwen", api_key="")]}
    entries = ns["_configured_entries"](floor_cfg)
    floor = model_selection.safety_floor(entries)
    ok &= _check("safety_floor (fed by _configured_entries) prefers the no-key local entry",
                floor is not None and floor["id"] == "e2")

    # --- _try_candidate: success records nothing extra, returns the result -----

    ns["_test_calls"]["raise_exc"] = None
    candidate = {"key": ("anthropic", "", "claude-sonnet-5"), "provider": "anthropic",
                "model": "claude-sonnet-5", "base_url": "", "api_key": "k"}
    result, err = ns["_try_candidate"](candidate, [], None, True, None, {})
    ok &= _check("_try_candidate returns the wrapped call's result on success", result == "ok from anthropic/claude-sonnet-5")
    ok &= _check("_try_candidate returns err=None on success", err is None)

    # --- _try_candidate: credit exhaustion trips the ENDPOINT cooldown ----------

    ns["_test_calls"]["raise_exc"] = lambda p, m: ns["LLMCreditExhausted"]()
    result, err = ns["_try_candidate"](candidate, [], None, True, None, {})
    ok &= _check("_try_candidate reports credit_exhausted and returns no result", result is None and err == "credit_exhausted")
    ok &= _check("the endpoint-level credit CB is now tripped for (anthropic, '')",
                ns["_cb_remaining"](ns["_ENDPOINT_CREDIT_CB"], ns["_endpoint_key"]("anthropic", "")) > 0)

    # --- _try_candidate: a 429 trips the MODEL-level rate CB, not the endpoint one

    ns["_ENDPOINT_CREDIT_CB"].clear()
    ns["_MODEL_RATE_CB"].clear()
    ns["_test_calls"]["raise_exc"] = lambda p, m: Exception("429 Too Many Requests")
    result, err = ns["_try_candidate"](candidate, [], None, True, None, {})
    ok &= _check("a 429 is reported as rate_limited", err == "rate_limited")
    ok &= _check("a 429 trips the per-MODEL rate CB (not the endpoint credit CB)",
                ns["_cb_remaining"](ns["_MODEL_RATE_CB"], candidate["key"]) > 0
                and ns["_cb_remaining"](ns["_ENDPOINT_CREDIT_CB"], ns["_endpoint_key"]("anthropic", "")) == 0)

    # --- _call_llm_with_requirements: end-to-end selection + success -----------

    ns["_ENDPOINT_CREDIT_CB"].clear()
    ns["_MODEL_RATE_CB"].clear()
    ns["_test_calls"]["raise_exc"] = None
    ns["_test_calls"]["provider_calls"].clear()
    reqs = model_selection.LlmRequirements(complexity="trivial")
    e2e_cfg = {"llm_entries": [_entry("e1", "ollama", "llama3.1:8b", api_key="")]}
    result = ns["_call_llm_with_requirements"](reqs, "hi", "sys", None, None, None, None, e2e_cfg)
    ok &= _check("end-to-end: a single configured candidate is selected and called",
                result == "ok from ollama/llama3.1:8b")

    # --- _call_llm_with_requirements: failover to the next alternative ----------

    ns["_ENDPOINT_CREDIT_CB"].clear()
    ns["_MODEL_RATE_CB"].clear()
    fail_first = {"n": 0}

    def _fail_first_provider(p, m):
        fail_first["n"] += 1
        return Exception("boom") if fail_first["n"] == 1 else None
    ns["_test_calls"]["raise_exc"] = _fail_first_provider
    two_cfg = {"llm_entries": [_entry("e1", "ollama", "llama3.1:8b", api_key=""),
                               _entry("e2", "lmstudio", "some-model", api_key="")]}
    result = ns["_call_llm_with_requirements"](reqs, "hi", "sys", None, None, None, None, two_cfg)
    ok &= _check("failover: the first candidate's failure doesn't sink the whole call "
                "when an alternative exists", "ok from" in (result or ""))

    # --- _call_llm_with_requirements: nothing satisfies -> safety floor --------

    ns["_test_calls"]["raise_exc"] = None
    strict_reqs = model_selection.LlmRequirements(complexity="large", min_context_tokens=10_000_000)
    floor_only_cfg = {"llm_entries": [_entry("e1", "ollama", "tiny-model", api_key="")]}
    result = ns["_call_llm_with_requirements"](strict_reqs, "hi", "sys", None, None, None, None, floor_only_cfg)
    ok &= _check("when no candidate satisfies the requirements, the safety floor still resolves something",
                result == "ok from ollama/tiny-model")

    # --- _call_llm_with_requirements: nothing configured at all -> raises ------

    threw = False
    try:
        ns["_call_llm_with_requirements"](reqs, "hi", "sys", None, None, None, None, {})
    except Exception as e:
        threw = "No LLM providers configured" in str(e)
    ok &= _check("a fully empty config raises a clear 'No LLM providers configured' error", threw)

    # --- must_escalate_to_human: nothing satisfies -> raises LlmHumanEscalationNeeded,
    # NOT a silent safety-floor fallback (only opted into by must_escalate_to_human=True) --

    ns["_test_calls"]["raise_exc"] = None
    human_reqs = model_selection.LlmRequirements(complexity="large", min_context_tokens=10_000_000,
                                                 must_escalate_to_human=True)
    threw_human = False
    try:
        ns["_call_llm_with_requirements"](human_reqs, "hi", "sys", None, None, None, None, floor_only_cfg)
    except ns["LlmHumanEscalationNeeded"]:
        threw_human = True
    except Exception:
        threw_human = False
    ok &= _check("must_escalate_to_human=True + nothing satisfies -> raises "
                "LlmHumanEscalationNeeded instead of falling to the safety floor", threw_human)

    # A satisfiable requirement with must_escalate_to_human=True still resolves
    # normally -- the flag only changes behavior on the "nothing satisfies" path.
    ns["_test_calls"]["raise_exc"] = None
    result = ns["_call_llm_with_requirements"](
        model_selection.LlmRequirements(complexity="trivial", must_escalate_to_human=True),
        "hi", "sys", None, None, None, None, e2e_cfg)
    ok &= _check("must_escalate_to_human=True with a satisfiable requirement resolves normally",
                result == "ok from ollama/llama3.1:8b")

    # --- used_model_out: populated with the winning candidate's identity -------

    ns["_test_calls"]["raise_exc"] = None
    used = {}
    result = ns["_call_llm_with_requirements"](reqs, "hi", "sys", None, None, None, None, e2e_cfg,
                                               used_model_out=used)
    ok &= _check("used_model_out is populated with the winning candidate's provider/model",
                used.get("provider") == "ollama" and used.get("model") == "llama3.1:8b"
                and used.get("key") is not None)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
