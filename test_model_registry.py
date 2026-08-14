#!/usr/bin/env python3
"""Self-test for model_registry.py — the capability/cost-tier registry for
BugFixer's LLM model-selection picker.

Run:  python3 bugfixer/test_model_registry.py

Standalone: imports only model_registry (no app/main init).
"""
import sys

import model_registry as reg


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running bugfixer model_registry self-test...")
    ok = True

    # --- glob matching + most-specific-wins --------------------------------

    config = {"model_registry": [
        {"id": "generic", "provider": "anthropic", "match": "*", "cost_tier": "frontier",
         "max_complexity": "large", "context_window": 200000, "supports_tools": True,
         "native_agentic_tools": False, "supports_mutating_agent": False,
         "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
         "enabled": True},
        {"id": "haiku-family", "provider": "anthropic", "match": "*haiku*", "cost_tier": "cheap",
         "max_complexity": "medium", "context_window": 200000, "supports_tools": True,
         "native_agentic_tools": False, "supports_mutating_agent": False,
         "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
         "enabled": True},
        {"id": "haiku-exact", "provider": "anthropic", "match": "claude-haiku-4-5-20251001",
         "cost_tier": "free", "max_complexity": "small", "context_window": 100000,
         "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
         "supports_structured_output": False, "supports_batch": False, "supports_streaming": False,
         "enabled": True},
    ]}

    caps = reg.resolve("anthropic", "claude-opus-4-8", config)
    ok &= _check("wildcard-only rule matches when nothing more specific does",
                caps["_matched_rule_id"] == "generic" and caps["cost_tier"] == "frontier")

    caps = reg.resolve("anthropic", "claude-haiku-4-5-20251001", config)
    ok &= _check("the MOST specific of three matching rules wins (exact > family > wildcard)",
                caps["_matched_rule_id"] == "haiku-exact" and caps["cost_tier"] == "free")

    caps = reg.resolve("anthropic", "claude-haiku-4-1-20240101", config)
    ok &= _check("a mid-specificity rule wins over the wildcard when the exact rule doesn't match",
                caps["_matched_rule_id"] == "haiku-family" and caps["cost_tier"] == "cheap")

    ok &= _check("matching is case-insensitive",
                reg.resolve("ANTHROPIC", "CLAUDE-HAIKU-4-5-20251001", config)["_matched_rule_id"] == "haiku-exact")

    # --- unknown model policy ------------------------------------------------

    caps = reg.resolve("openai", "some-brand-new-model", config)
    ok &= _check("no matching provider -> unknown caps", caps == reg.UNKNOWN_CAPS)
    ok &= _check("unknown sorts after frontier on cost",
                reg.COST_TIER_RANK["unknown"] > reg.COST_TIER_RANK["frontier"])
    ok &= _check("unknown starts at trivial max_complexity (fails the capability filter by default)",
                caps["max_complexity"] == "trivial")

    disabled_config = {"model_registry": [{**config["model_registry"][0], "enabled": False}]}
    ok &= _check("a disabled rule never matches, even with an otherwise-perfect glob",
                reg.resolve("anthropic", "claude-opus-4-8", disabled_config) == reg.UNKNOWN_CAPS)

    ok &= _check("empty/absent model_registry key falls back to DEFAULT_MODEL_RULES",
                reg.resolve("claude_cli", "sonnet", {})["cost_tier"] == "free")

    # --- per-family seed defaults (DEFAULT_MODEL_RULES itself) --------------

    ok &= _check("ollama defaults to free (reuses the no-key concept)",
                reg.resolve("ollama", "qwen2.5-coder:14b", {})["cost_tier"] == "free")
    ok &= _check("lmstudio defaults to free",
                reg.resolve("lmstudio", "some-model", {})["cost_tier"] == "free")
    ok &= _check("claude_cli defaults to free",
                reg.resolve("claude_cli", "sonnet", {})["cost_tier"] == "free")
    ok &= _check("ollama_cloud (the one ollama* that takes a key) is cheap, not free",
                reg.resolve("ollama_cloud", "qwen2.5-coder:14b", {})["cost_tier"] == "cheap")
    ok &= _check("claude_cli is the only provider with native_agentic_tools",
                reg.resolve("claude_cli", "sonnet", {})["native_agentic_tools"] is True
                and reg.resolve("anthropic", "claude-sonnet-x", {})["native_agentic_tools"] is False)
    ok &= _check("claude_cli is the only provider with supports_mutating_agent",
                reg.resolve("claude_cli", "sonnet", {})["supports_mutating_agent"] is True)
    ok &= _check("google models have supports_streaming=False (the API is hardcoded non-streaming)",
                reg.resolve("google", "gemini-2.5-flash", {})["supports_streaming"] is False)
    ok &= _check("openrouter's :free suffix is more specific than the bare openrouter wildcard",
                reg.resolve("openrouter", "some-model:free", {})["cost_tier"] == "free"
                and reg.resolve("openrouter", "some-model", {})["cost_tier"] == "cheap")
    ok &= _check("anthropic haiku family is cheap, sonnet/opus are frontier",
                reg.resolve("anthropic", "claude-haiku-4-5", {})["cost_tier"] == "cheap"
                and reg.resolve("anthropic", "claude-sonnet-5", {})["cost_tier"] == "frontier"
                and reg.resolve("anthropic", "claude-opus-4-8", {})["cost_tier"] == "frontier")

    # --- _model_param_b / _complexity_from_param_b (ollama size derivation) -

    ok &= _check("_model_param_b parses 'qwen2.5-coder:14b' -> 14.0",
                reg._model_param_b("qwen2.5-coder:14b") == 14.0)
    ok &= _check("_model_param_b parses 'llama3.1:8b' -> 8.0", reg._model_param_b("llama3.1:8b") == 8.0)
    ok &= _check("_model_param_b returns None when unparseable", reg._model_param_b("mystery-model") is None)

    ok &= _check("<8B -> trivial", reg._complexity_from_param_b(7.0) == "trivial")
    ok &= _check("8B (boundary) -> small", reg._complexity_from_param_b(8.0) == "small")
    ok &= _check("20B (boundary) -> small", reg._complexity_from_param_b(20.0) == "small")
    ok &= _check(">20B -> medium", reg._complexity_from_param_b(20.1) == "medium")
    ok &= _check("NEVER 'large' from size alone, even for a huge local model",
                reg._complexity_from_param_b(405.0) == "medium")
    ok &= _check("unparseable size doesn't crash, isn't trivial (assume modest, not zero, capability)",
                reg._complexity_from_param_b(None) == "small")

    ok &= _check("resolve() derives ollama max_complexity from param count via the default rule",
                reg.resolve("ollama", "qwen2.5-coder:32b", {})["max_complexity"] == "medium"
                and reg.resolve("ollama", "qwen2.5-coder:3b", {})["max_complexity"] == "trivial")

    # An operator override MORE SPECIFIC than the generic ollama-local default
    # must win outright — size-derivation only applies to the built-in default.
    override_cfg = {"model_registry": [
        {**reg.DEFAULT_MODEL_RULES[0]},  # the generic ollama-local rule, id="ollama-local"
        {"id": "my-big-box", "provider": "ollama", "match": "qwen2.5-coder:32b", "cost_tier": "free",
         "max_complexity": "large", "context_window": 65536, "supports_tools": False,
         "native_agentic_tools": False, "supports_mutating_agent": False,
         "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
         "enabled": True},
    ]}
    ok &= _check("an operator's specific override rule wins over param-count derivation",
                reg.resolve("ollama", "qwen2.5-coder:32b", override_cfg)["max_complexity"] == "large")

    # --- effective context window: ollama's num_ctx cap (documented in the plan,
    # implemented downstream in model_selection.py's context filter — model_registry
    # itself just reports the registry's context_window; this pins that the field
    # exists and is a plain int, not something model_selection has to derive itself) -
    ok &= _check("ollama rule reports a numeric context_window",
                isinstance(reg.resolve("ollama", "qwen2.5-coder:14b", {})["context_window"], int))

    # --- registry_sync (auto-discovery) --------------------------------------

    seen = [("openai", "gpt-9-turbo"), ("anthropic", "claude-opus-4-8"), ("newprovider", "mystery-model")]
    stubs = reg.registry_sync(seen, {})
    stub_keys = {(s["provider"], s["model"]) for s in stubs}
    ok &= _check("registry_sync only stubs pairs with NO curated match",
                ("newprovider", "mystery-model") in stub_keys)
    ok &= _check("registry_sync skips pairs a curated rule already covers",
                ("anthropic", "claude-opus-4-8") not in stub_keys and ("openai", "gpt-9-turbo") not in stub_keys)
    ok &= _check("auto-discovered stubs carry cost_tier=unknown", stubs[0]["cost_tier"] == "unknown")

    already_known = {"model_registry_auto": [{"provider": "newprovider", "model": "mystery-model"}]}
    stubs2 = reg.registry_sync(seen, already_known)
    ok &= _check("registry_sync doesn't re-stub something already in model_registry_auto",
                ("newprovider", "mystery-model") not in {(s["provider"], s["model"]) for s in stubs2})

    dup_seen = [("newprovider", "dup-model"), ("newprovider", "dup-model")]
    stubs3 = reg.registry_sync(dup_seen, {})
    ok &= _check("registry_sync dedupes duplicate pairs within a single call",
                len(stubs3) == 1)

    # --- local_models_for_preload --------------------------------------------

    installed = [
        {"name": "qwen2.5-coder:32b", "size": 20_000_000_000},
        {"name": "qwen2.5-coder:3b", "size": 2_000_000_000},   # trivial -> excluded
        {"name": "llama3.1:8b", "size": 5_000_000_000},
    ]
    preload = reg.local_models_for_preload(installed, {})
    ok &= _check("trivial-complexity local models are excluded from preload",
                "qwen2.5-coder:3b" not in preload)
    ok &= _check("non-trivial models are included, sorted smallest-first",
                preload == ["llama3.1:8b", "qwen2.5-coder:32b"])
    ok &= _check("empty installed list -> empty preload list, no crash",
                reg.local_models_for_preload([], {}) == [])
    ok &= _check("a model dict missing 'name' is skipped, not a crash",
                reg.local_models_for_preload([{"size": 1}], {}) == [])

    # --- upgrade_local_free_rules (idempotent local/free top-up) -------------

    # A config frozen before ollama2 got a default rule: has ollama-local but
    # no ollama2 rule at all. (Strip EVERY ollama2 rule — incl. the capable
    # qwen3/llama3.3/gemma4 defaults — so the provider is genuinely absent, the
    # precondition upgrade_local_free_rules gates on.)
    frozen = [dict(r) for r in reg.DEFAULT_MODEL_RULES
              if (r.get("provider") or "").lower() != "ollama2"]
    n_before = len(frozen)
    topped, added = reg.upgrade_local_free_rules(frozen)
    ok &= _check("top-up appends exactly one ollama2 free rule when it's missing",
                added == ["ollama2-local"])
    o2 = [r for r in topped if r.get("provider") == "ollama2"]
    ok &= _check("the appended ollama2 rule is cost_tier free, match '*'",
                len(o2) == 1 and o2[0]["cost_tier"] == "free" and o2[0]["match"] == "*")
    ok &= _check("top-up only appends (length grows by exactly one)",
                len(topped) == n_before + 1)
    ok &= _check("top-up never mutates the caller's list in place",
                len(frozen) == n_before)
    ok &= _check("existing rules are preserved in order (append-only, no reorder)",
                [r["id"] for r in topped[:n_before]] == [r["id"] for r in frozen])

    # Idempotent: a second run adds nothing.
    topped2, added2 = reg.upgrade_local_free_rules(topped)
    ok &= _check("top-up is idempotent — a second run adds nothing",
                added2 == [] and len(topped2) == len(topped))

    # Conservative: an operator who already has their OWN ollama2 rule (even a
    # non-default id / non-free tier) is left untouched — provider present.
    custom = [{"id": "my-gpu", "provider": "ollama2", "match": "*", "cost_tier": "cheap",
               "max_complexity": "large", "enabled": True}]
    topped3, added3 = reg.upgrade_local_free_rules(custom)
    ok &= _check("top-up never overrides an operator's existing ollama2 rule",
                "ollama2-local" not in added3 and topped3[0]["cost_tier"] == "cheap")

    # A brand-new config (no ollama2, and no ollama either) gets both local/free
    # providers, but a paid provider like anthropic is never injected.
    empty_top, empty_added = reg.upgrade_local_free_rules([])
    ok &= _check("a paid provider (anthropic) is never injected by the local-free top-up",
                all(r["provider"] != "anthropic" for r in empty_top))
    ok &= _check("all injected top-up rules are cost_tier free",
                all(r["cost_tier"] == "free" for r in empty_top))

    # The shipped DEFAULT_MODEL_RULES already contains ollama2 → top-up is a no-op.
    default_top, default_added = reg.upgrade_local_free_rules(reg.DEFAULT_MODEL_RULES)
    ok &= _check("DEFAULT_MODEL_RULES already ships ollama2 — top-up is a no-op on it",
                default_added == [])

    # And resolve() now classifies an ollama2 model as free by default.
    ok &= _check("resolve() classifies an ollama2 model as free with the shipped defaults",
                reg.resolve("ollama2", "qwen2.5-coder:14b", {})["cost_tier"] == "free")

    # --- capable self-hosted model rules + upgrade_capable_local_rules --------

    # The shipped defaults promote specific local families to their real caps:
    # tool-calling, large complexity, long context — while staying free and
    # NOT mutating-agent (Ollama has no agentic file-edit runtime).
    qwen = reg.resolve("ollama2", "qwen3-coder:30b", {})
    ok &= _check("qwen3-coder on the GPU resolves free + large + tools + structured",
                qwen["cost_tier"] == "free" and qwen["max_complexity"] == "large"
                and qwen["supports_tools"] is True and qwen["supports_structured_output"] is True)
    ok &= _check("qwen3-coder gets a long context window (clears the ~81k*1.25 gate)",
                (qwen["context_window"] or 0) >= 101000)
    ok &= _check("qwen3-coder is NOT flagged mutating-agent (no ollama agentic runtime)",
                qwen["supports_mutating_agent"] is False)
    llama = reg.resolve("ollama2", "llama3.3:70b", {})
    ok &= _check("llama3.3 on the GPU resolves free + large + tools",
                llama["cost_tier"] == "free" and llama["max_complexity"] == "large"
                and llama["supports_tools"] is True)
    gemma = reg.resolve("ollama2", "gemma4:26b", {})
    ok &= _check("gemma4 resolves free + large + structured but tools stays off",
                gemma["max_complexity"] == "large" and gemma["supports_structured_output"] is True
                and gemma["supports_tools"] is False)
    # A more-specific capable rule beats the generic ollama2 '*' rule.
    generic = reg.resolve("ollama2", "some-unlisted-model:8b", {})
    ok &= _check("an unlisted ollama2 model still falls back to the generic (medium, no tools) rule",
                generic["max_complexity"] == "medium" and generic["supports_tools"] is False)

    # upgrade_capable_local_rules injects the capable rules even when the
    # provider already has its generic rule (unlike the free top-up).
    frozen_no_cap = [r for r in reg.DEFAULT_MODEL_RULES
                     if r["id"] not in reg._CAPABLE_LOCAL_RULE_IDS]
    capped, cap_added = reg.upgrade_capable_local_rules(frozen_no_cap)
    ok &= _check("capable top-up appends all missing capable-local rules",
                set(cap_added) == set(reg._CAPABLE_LOCAL_RULE_IDS))
    ok &= _check("capable top-up appends even though ollama/ollama2 providers already exist",
                reg.resolve("ollama2", "qwen3-coder:30b", {"model_registry": capped})["max_complexity"] == "large")
    capped2, cap_added2 = reg.upgrade_capable_local_rules(capped)
    ok &= _check("capable top-up is idempotent — a second run adds nothing", cap_added2 == [])
    # An operator who curated their OWN rule for that (provider, match) keeps it.
    own = [{"id": "my-qwen", "provider": "ollama2", "match": "qwen3-coder*",
            "cost_tier": "free", "max_complexity": "medium", "supports_tools": False,
            "supports_structured_output": False, "context_window": 32768, "enabled": True}]
    own_top, own_added = reg.upgrade_capable_local_rules(own)
    ok &= _check("capable top-up never overrides an operator's own (provider, match) rule",
                "ollama2-qwen3-coder" not in own_added)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
