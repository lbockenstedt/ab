#!/usr/bin/env python3
"""Self-test for model_registry.py — the capability/cost-tier registry for
AppBuilder's LLM model-selection picker.

Run:  python3 ab/test_model_registry.py

Standalone: imports only model_registry (no app/main init).
"""
import sys

import model_registry as reg


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running ab model_registry self-test...")
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
                reg.resolve("claude_cli", "sonnet", {})["cost_tier"] == "frontier")

    # --- per-family seed defaults (DEFAULT_MODEL_RULES itself) --------------

    ok &= _check("ollama defaults to free (reuses the no-key concept)",
                reg.resolve("ollama", "qwen2.5-coder:14b", {})["cost_tier"] == "free")
    ok &= _check("lmstudio defaults to free",
                reg.resolve("lmstudio", "some-model", {})["cost_tier"] == "free")
    ok &= _check("claude_cli defaults to frontier (session auth, but bills the operator's account)",
                reg.resolve("claude_cli", "sonnet", {})["cost_tier"] == "frontier")
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
    ok &= _check("the OpenRouter Free Models Router (openrouter/free) resolves to the FREE tier",
                reg.resolve("openrouter", "openrouter/free", {})["cost_tier"] == "free"
                and reg.resolve("openrouter", "openrouter/free", {})["_matched_rule_id"]
                    == "openrouter-free-router")
    ok &= _check("anthropic haiku family is cheap, sonnet/opus are frontier",
                reg.resolve("anthropic", "claude-haiku-4-5", {})["cost_tier"] == "cheap"
                and reg.resolve("anthropic", "claude-sonnet-5", {})["cost_tier"] == "frontier"
                and reg.resolve("anthropic", "claude-opus-4-8", {})["cost_tier"] == "frontier")

    # --- copilot per-model ladder (Copilot is NOT free — premium request per
    # call, multiplier is per-model: Opus 27x vs gpt-4o 0.33x) ---------------

    ok &= _check("copilot opus/sonnet are frontier, haiku/gpt-4 are cheap",
                reg.resolve("copilot", "claude-opus-4.6", {})["cost_tier"] == "frontier"
                and reg.resolve("copilot", "claude-sonnet-4.6", {})["cost_tier"] == "frontier"
                and reg.resolve("copilot", "claude-haiku-4.5", {})["cost_tier"] == "cheap"
                and reg.resolve("copilot", "gpt-4o", {})["cost_tier"] == "cheap")
    ok &= _check("copilot Opus is tiered the SAME as anthropic/claude_cli Opus (frontier+large)",
                reg.resolve("copilot", "claude-opus-4.6", {})["cost_tier"]
                == reg.resolve("anthropic", "claude-opus-4-8", {})["cost_tier"]
                == reg.resolve("claude_cli", "opus", {})["cost_tier"] == "frontier"
                and reg.resolve("copilot", "claude-opus-4.6", {})["max_complexity"] == "large")
    ok &= _check("copilot gpt-5-mini (0.33x) stays cheap — 'gpt-5*mini*' outranks 'gpt-5*'",
                reg.resolve("copilot", "gpt-5-mini", {})["cost_tier"] == "cheap"
                and reg.resolve("copilot", "gpt-5", {})["cost_tier"] == "frontier")
    ok &= _check("copilot is never 'free' — an unmatched model still falls back to cheap",
                reg.resolve("copilot", "some-unknown-model", {})["cost_tier"] == "cheap")

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

    # --- reclassify_claude_cli_paid: a frozen free claude_cli rule -> frontier
    frozen_claude = [
        {"id": "claude-cli", "provider": "claude_cli", "match": "*", "cost_tier": "free",
         "max_complexity": "large", "context_window": 200000, "enabled": True},
        {"id": "gpu", "provider": "ollama2", "match": "*", "cost_tier": "free",
         "max_complexity": "medium", "context_window": 32768, "enabled": True},
    ]
    repaired, changed = reg.reclassify_claude_cli_paid(frozen_claude)
    _claude = next(r for r in repaired if r.get("provider") == "claude_cli")
    _gpu = next(r for r in repaired if r.get("provider") == "ollama2")
    ok &= _check("reclassify bumps a free claude_cli rule to frontier", changed and _claude["cost_tier"] == "frontier")
    ok &= _check("reclassify leaves the free GPU rule untouched", _gpu["cost_tier"] == "free")
    _, changed2 = reg.reclassify_claude_cli_paid(repaired)
    ok &= _check("reclassify is idempotent — a second run changes nothing", changed2 is False)
    # An operator who deliberately set another claude_cli tier is left alone.
    op = [{"id": "claude-cli", "provider": "claude_cli", "match": "*", "cost_tier": "cheap",
           "max_complexity": "large", "context_window": 200000, "enabled": True}]
    op_out, op_changed = reg.reclassify_claude_cli_paid(op)
    ok &= _check("reclassify only touches cost_tier=free — a curated 'cheap' is preserved",
                op_changed is False and op_out[0]["cost_tier"] == "cheap")

    # --- per-model claude_cli rules: a strength ladder (Haiku<Sonnet<Opus) ----
    ok &= _check("claude_cli Haiku resolves to small (the light/cheap model)",
                reg.resolve("claude_cli", "claude-haiku-4-5-20251001", {})["max_complexity"] == "small")
    ok &= _check("claude_cli Sonnet resolves to medium (balanced)",
                reg.resolve("claude_cli", "claude-sonnet-4-6", {})["max_complexity"] == "medium")
    ok &= _check("claude_cli Opus resolves to large (the hardest-work default)",
                reg.resolve("claude_cli", "claude-opus-5", {})["max_complexity"] == "large")
    ok &= _check("an unrecognized claude_cli model falls back to the generic rule (large)",
                reg.resolve("claude_cli", "claude-future-99", {})["max_complexity"] == "large")
    _h = reg.resolve("claude_cli", "claude-haiku-4-5-20251001", {})
    ok &= _check("Haiku keeps the agentic caps (mutating agent) while capped at small",
                _h["supports_mutating_agent"] is True and _h["cost_tier"] == "frontier")

    # feature_build (large + mutating) must escalate to Opus — Haiku (small) and
    # Sonnet (medium) are both below the large ceiling, so only Opus qualifies.
    import model_selection as _sel
    def _cc_cand(m):
        return {"key": ("claude_cli", "", m), "provider": "claude_cli", "model": m,
                "caps": reg.resolve("claude_cli", m, {}), "available": True}
    _cc = [_cc_cand("claude-haiku-4-5-20251001"), _cc_cand("claude-sonnet-4-6"), _cc_cand("claude-opus-5")]
    _fb = _sel.select_model(_sel.LlmRequirements(complexity="large", needs_mutating_agent=True), _cc)
    ok &= _check("feature_build (large mutating) resolves to Opus, not Haiku/Sonnet",
                _fb is not None and _fb.model == "claude-opus-5")
    _sm = _sel.select_model(_sel.LlmRequirements(complexity="small", needs_mutating_agent=True), _cc)
    ok &= _check("a small mutating task still uses the cheapest capable Claude (Haiku)",
                _sm is not None and _sm.model == "claude-haiku-4-5-20251001")

    # upgrade_claude_cli_model_rules injects the per-model rules into a frozen
    # config that only has the generic claude_cli `*` rule.
    frozen_generic = [
        {"id": "claude-cli", "provider": "claude_cli", "match": "*", "cost_tier": "frontier",
         "max_complexity": "large", "context_window": 200000, "supports_mutating_agent": True,
         "enabled": True},
    ]
    cc_top, cc_added = reg.upgrade_claude_cli_model_rules(frozen_generic)
    ok &= _check("claude_cli per-model top-up appends Haiku/Sonnet/Opus to a generic-only config",
                set(cc_added) == {"claude-cli-haiku", "claude-cli-sonnet", "claude-cli-opus"})
    ok &= _check("after top-up, Haiku resolves to small via the frozen config",
                reg.resolve("claude_cli", "claude-haiku-4-5-20251001",
                            {"model_registry": cc_top})["max_complexity"] == "small")
    _, cc_added2 = reg.upgrade_claude_cli_model_rules(cc_top)
    ok &= _check("claude_cli per-model top-up is idempotent — a second run adds nothing", cc_added2 == [])
    cc_own = [{"id": "my-haiku", "provider": "claude_cli", "match": "*haiku*", "cost_tier": "frontier",
               "max_complexity": "small", "enabled": True}]
    _, cc_own_added = reg.upgrade_claude_cli_model_rules(cc_own)
    ok &= _check("claude_cli per-model top-up never overrides an operator's own (provider, match) rule",
                "claude-cli-haiku" not in cc_own_added)

    # --- upgrade_copilot_model_rules: an install frozen with only the generic
    # copilot `*` rule priced Opus (27x premium request) as "cheap" ----------

    frozen_copilot = [{"id": "copilot", "provider": "copilot", "match": "*",
                       "cost_tier": "cheap", "max_complexity": "large",
                       "context_window": 64000, "enabled": True}]
    ok &= _check("frozen generic copilot rule mis-prices Opus as cheap (the bug)",
                reg.resolve("copilot", "claude-opus-4.6",
                            {"model_registry": frozen_copilot})["cost_tier"] == "cheap")
    cp_top, cp_added = reg.upgrade_copilot_model_rules(frozen_copilot)
    ok &= _check("copilot top-up injects the per-model rules into a frozen registry",
                "copilot-opus" in cp_added
                and reg.resolve("copilot", "claude-opus-4.6",
                                {"model_registry": cp_top})["cost_tier"] == "frontier")
    _, cp_added2 = reg.upgrade_copilot_model_rules(cp_top)
    ok &= _check("copilot top-up is idempotent — a second run adds nothing", not cp_added2)
    _, cp_default_added = reg.upgrade_copilot_model_rules(reg.DEFAULT_MODEL_RULES)
    ok &= _check("DEFAULT_MODEL_RULES already ships the copilot ladder — top-up is a no-op",
                not cp_default_added)
    cp_own = [{"id": "my-copilot-opus", "provider": "copilot", "match": "*opus*",
               "cost_tier": "cheap", "max_complexity": "small", "enabled": True}]
    _, cp_own_added = reg.upgrade_copilot_model_rules(cp_own)
    ok &= _check("copilot top-up never overrides an operator's own (provider, match) rule",
                "copilot-opus" not in cp_own_added)

    # --- OpenRouter Free Models Router rule + upgrade_openrouter_free_router_rule ---

    # Injected into a frozen config that predates the rule (append-only).
    frozen_no_router = [r for r in reg.DEFAULT_MODEL_RULES
                        if r["id"] != "openrouter-free-router"]
    routed, router_added = reg.upgrade_openrouter_free_router_rule(frozen_no_router)
    ok &= _check("free-router top-up appends the openrouter-free-router rule when missing",
                router_added is True
                and any(r["id"] == "openrouter-free-router" for r in routed))
    _, router_added2 = reg.upgrade_openrouter_free_router_rule(routed)
    ok &= _check("free-router top-up is idempotent — a second run adds nothing",
                router_added2 is False)
    _, router_default_added = reg.upgrade_openrouter_free_router_rule(reg.DEFAULT_MODEL_RULES)
    ok &= _check("DEFAULT_MODEL_RULES already ships the free-router rule — top-up is a no-op",
                router_default_added is False)
    router_own = [{"id": "my-router", "provider": "openrouter", "match": "openrouter/free",
                   "cost_tier": "cheap", "max_complexity": "small", "enabled": True}]
    _, router_own_added = reg.upgrade_openrouter_free_router_rule(router_own)
    ok &= _check("free-router top-up never overrides an operator's own (provider, openrouter/free) rule",
                router_own_added is False)
    # After the top-up, a frozen config resolves openrouter/free to the free tier.
    ok &= _check("post-top-up, openrouter/free resolves free (offload prefers it over the GPU)",
                reg.resolve("openrouter", "openrouter/free", {"model_registry": routed})["cost_tier"]
                    == "free")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
