#!/usr/bin/env python3
"""Self-test for model_registry.py — the capability/cost-tier registry for
AppBuilder's LLM model-selection picker.

Run:  python3 ab/test_model_registry.py

Standalone: imports only model_registry (no app/main init).
"""
import sys

import model_registry as reg
import model_selection as sel


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
    # The ladder is now carried by capability_rank, NOT by capping
    # max_complexity. The cap was a hard filter, so it excluded Haiku/Sonnet
    # from work they can do instead of ranking them below Opus — and on a
    # claude_cli-only install that left complexity="large" with no candidate
    # at all. Both now report their honest ceiling and mirror their anthropic
    # twins; ordering is asserted via capability_rank below.
    ok &= _check("claude_cli Haiku reports its honest ceiling (medium, mirrors anthropic-haiku)",
                reg.resolve("claude_cli", "claude-haiku-4-5-20251001", {})["max_complexity"] == "medium")
    ok &= _check("claude_cli Sonnet reports its honest ceiling (large, mirrors anthropic-sonnet)",
                reg.resolve("claude_cli", "claude-sonnet-4-6", {})["max_complexity"] == "large")
    ok &= _check("the Haiku<Sonnet<Opus ladder survives as capability_rank",
                reg.capability_rank(reg.resolve("claude_cli", "claude-haiku-4-5-20251001", {}))
                < reg.capability_rank(reg.resolve("claude_cli", "claude-sonnet-4-6", {}))
                < reg.capability_rank(reg.resolve("claude_cli", "claude-opus-5", {})))
    ok &= _check("claude_cli Opus resolves to large (the hardest-work default)",
                reg.resolve("claude_cli", "claude-opus-5", {})["max_complexity"] == "large")
    ok &= _check("an unrecognized claude_cli model falls back to the generic rule (large)",
                reg.resolve("claude_cli", "claude-future-99", {})["max_complexity"] == "large")
    _h = reg.resolve("claude_cli", "claude-haiku-4-5-20251001", {})
    ok &= _check("Haiku keeps the agentic caps (mutating agent)",
                _h["supports_mutating_agent"] is True and _h["cost_tier"] == "frontier")

    # feature_build (large + mutating) must still escalate to Opus. This used to
    # fall out of the hard complexity filter (only Opus was "large"); now all
    # three qualify, and Opus wins because complexity="large" makes
    # capability_rank the primary ordering key in _rank_tier.
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
    ok &= _check("after top-up, Haiku resolves via the frozen config and ranks below Opus",
                reg.resolve("claude_cli", "claude-haiku-4-5-20251001",
                            {"model_registry": cc_top})["max_complexity"] == "medium"
                and reg.capability_rank(reg.resolve("claude_cli", "claude-haiku-4-5-20251001",
                                                    {"model_registry": cc_top}))
                    < reg.capability_rank(reg.resolve("claude_cli", "claude-opus-5",
                                                      {"model_registry": cc_top})))
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

    # --- ollama_cloud: tools flag + per-model ladder ------------------------

    ok &= _check("ollama_cloud advertises tool support (the client has always sent tools)",
                reg.resolve("ollama_cloud", "nemotron-3-ultra", {})["supports_tools"] is True)
    ok &= _check("ollama_cloud small tiers are capped at medium; ultra stays large",
                reg.resolve("ollama_cloud", "nemotron-3-nano:30b", {})["max_complexity"] == "medium"
                and reg.resolve("ollama_cloud", "gpt-oss:20b", {})["max_complexity"] == "medium"
                and reg.resolve("ollama_cloud", "glm-5.3-flash", {})["max_complexity"] == "medium"
                and reg.resolve("ollama_cloud", "nemotron-3-ultra", {})["max_complexity"] == "large")
    ok &= _check("ollama_cloud frontier-class models still inherit the generic large default",
                reg.resolve("ollama_cloud", "kimi-k3", {})["max_complexity"] == "large"
                and reg.resolve("ollama_cloud", "mistral-large-3:675b", {})["max_complexity"] == "large")
    ok &= _check("ollama_cloud context_window is authoritative and per-class: ultra gets the "
                 "largest window, the small tiers the common cloud floor",
                reg.resolve("ollama_cloud", "nemotron-3-ultra", {})["context_window"] == 262144
                and reg.resolve("ollama_cloud", "gpt-oss:20b", {})["context_window"] == 131072
                and reg.resolve("ollama_cloud", "kimi-k3", {})["context_window"] == 131072)
    ok &= _check("every ollama_cloud rule now beats the old flat 32768 local default",
                all(r.get("context_window", 0) > 32768
                    for r in reg.DEFAULT_MODEL_RULES
                    if r.get("provider") == "ollama_cloud"))
    ok &= _check("the generic local ollama rule keeps its 32768 window — the cloud context "
                 "change must not move the shared-GPU local default",
                reg.resolve("ollama", "some-unlisted-model", {})["context_window"] == 32768
                and reg.resolve("ollama_cloud", "nemotron-3-ultra", {})["context_window"] != 32768)

    frozen_oc = [{"id": "ollama-cloud", "provider": "ollama_cloud", "match": "*",
                  "cost_tier": "cheap", "max_complexity": "large", "context_window": 32768,
                  "supports_tools": False, "enabled": True}]
    ok &= _check("a frozen ollama-cloud rule wrongly reports no tool support (the bug)",
                reg.resolve("ollama_cloud", "nemotron-3-ultra",
                            {"model_registry": frozen_oc})["supports_tools"] is False)
    oc_fixed, oc_changed = reg.enable_ollama_cloud_tools(frozen_oc)
    ok &= _check("enable_ollama_cloud_tools repairs the frozen rule in place",
                oc_changed is True
                and reg.resolve("ollama_cloud", "nemotron-3-ultra",
                                {"model_registry": oc_fixed})["supports_tools"] is True)
    _, oc_changed2 = reg.enable_ollama_cloud_tools(oc_fixed)
    ok &= _check("enable_ollama_cloud_tools is idempotent", oc_changed2 is False)
    oc_own = [{"id": "my-cloud", "provider": "ollama_cloud", "match": "*",
               "supports_tools": False, "enabled": True}]
    _, oc_own_changed = reg.enable_ollama_cloud_tools(oc_own)
    ok &= _check("enable_ollama_cloud_tools leaves an operator's own ollama_cloud rule alone",
                oc_own_changed is False)
    oc_top, oc_added = reg.upgrade_ollama_cloud_model_rules(frozen_oc)
    ok &= _check("ollama_cloud per-model top-up injects the ladder into a frozen registry",
                "ollama-cloud-nano" in oc_added
                and reg.resolve("ollama_cloud", "nemotron-3-nano:30b",
                                {"model_registry": oc_top})["max_complexity"] == "medium")
    _, oc_added2 = reg.upgrade_ollama_cloud_model_rules(oc_top)
    ok &= _check("ollama_cloud per-model top-up is idempotent", not oc_added2)

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

    # ---------------------------------------------------------- capability_rank
    # cost_tier and max_complexity cannot express "Opus beats Sonnet": both are
    # frontier/large. capability_rank is the within-tier ordering axis.
    _cfg = {}
    _rank = lambda p, m: reg.capability_rank(reg.resolve(p, m, _cfg))
    for _p in ("copilot", "anthropic", "claude_cli"):
        ok &= _check(f"{_p}: Opus outranks Sonnet on capability_rank",
                    _rank(_p, "claude-opus-5") > _rank(_p, "claude-sonnet-5"))
    ok &= _check("Opus carries the SAME capability_rank whichever provider serves it",
                len({_rank(p, "claude-opus-5")
                     for p in ("copilot", "anthropic", "claude_cli")}) == 1)
    ok &= _check("Opus is the ceiling — no default rule outranks it",
                max(reg.capability_rank(r) for r in reg.DEFAULT_MODEL_RULES)
                    == _rank("copilot", "claude-opus-5"))
    ok &= _check("capability_rank never exceeds a cheaper tier's job: it is compared "
                "only within a tier, so Opus stays frontier (reserved), not promoted",
                reg.resolve("copilot", "claude-opus-5", _cfg)["cost_tier"] == "frontier")
    # Backward compatibility: a rule written before the field existed must keep
    # its previous relative order rather than collapsing to zero.
    ok &= _check("a rule with no capability_rank falls back to a max_complexity default",
                reg.capability_rank({"max_complexity": "large"}) >
                reg.capability_rank({"max_complexity": "medium"}) >
                reg.capability_rank({"max_complexity": "trivial"}))
    ok &= _check("a non-numeric/bool capability_rank falls back rather than crashing",
                reg.capability_rank({"max_complexity": "large", "capability_rank": True})
                    == reg.capability_rank({"max_complexity": "large"})
                and reg.capability_rank({"max_complexity": "large",
                                         "capability_rank": "high"})
                    == reg.capability_rank({"max_complexity": "large"}))
    ok &= _check("capability_rank is clamped to 0..100",
                reg.capability_rank({"capability_rank": 999}) == 100
                and reg.capability_rank({"capability_rank": -5}) == 0)

    # The behaviour the axis exists for. Perf scores are min-max normalised
    # within a tier, so the faster model takes the full 1.0 and the slower 0.0:
    # before capability became the primary key, Sonnet's speed always beat Opus
    # for precisely the hard work Opus is reserved for.
    _cands = [{"key": f"copilot|x|{m}", "provider": "copilot", "model": m,
               "caps": reg.resolve("copilot", m, _cfg), "available": True}
              for m in ("claude-sonnet-5", "claude-opus-5")]
    _perf = {"copilot|x|claude-sonnet-5": {"n": 50, "tps": 80.0, "latency_ms": 4411},
             "copilot|x|claude-opus-5":   {"n": 50, "tps": 20.0, "latency_ms": 7414}}
    def _pick(complexity, pc):
        got = sel.select_model(
            sel.LlmRequirements(complexity=complexity, prefer_capable=pc), _cands, _perf)
        return getattr(got, "model", None)
    ok &= _check("prefer_capable picks Opus over a MUCH faster Sonnet in the same tier",
                _pick("medium", True) == "claude-opus-5")
    ok &= _check("complexity=large alone also reaches for Opus (no prefer_capable needed)",
                _pick("large", False) == "claude-opus-5")
    # Below "large" and without prefer_capable, light work must still take the
    # fastest model in the tier rather than reaching for the smartest.
    ok &= _check("ordinary (non-large) work is still ordered on speed — Sonnet wins",
                _pick("medium", False) == "claude-sonnet-5")

    # capability_rank replaced max_complexity as the escalation mechanism. The
    # cap was a HARD filter, so it EXCLUDED claude_cli Sonnet/Haiku from work
    # they can do rather than ranking them second — and on a claude_cli-only
    # install (session auth, no API keys) there was no Opus to escalate TO.
    _cc = lambda m: {"key": f"claude_cli|x|{m}", "provider": "claude_cli", "model": m,
                     "caps": reg.resolve("claude_cli", m, _cfg), "available": True}
    ok &= _check("claude_cli Sonnet is no longer hard-excluded from complexity=large",
                getattr(sel.select_model(sel.LlmRequirements(complexity="large"),
                                         [_cc("claude-sonnet-5")], {}), "model", None)
                    == "claude-sonnet-5")
    ok &= _check("escalation still lands on Opus when it IS available",
                getattr(sel.select_model(
                    sel.LlmRequirements(complexity="large", prefer_capable=True),
                    [_cc("claude-sonnet-5"), _cc("claude-haiku-4.5"), _cc("claude-opus-5")],
                    {}), "model", None) == "claude-opus-5")

    # Backfill migration: the ranks and the two lifted caps live ON rules an
    # existing install already persisted, so an append-only top-up can't do it.
    _frozen = [dict(r) for r in reg.DEFAULT_MODEL_RULES]
    for _r in _frozen:
        _r.pop("capability_rank", None)
        if _r.get("id") == "claude-cli-sonnet":
            _r["max_complexity"] = "medium"
        if _r.get("id") == "claude-cli-haiku":
            _r["max_complexity"] = "small"
    _fixed, _bf = reg.backfill_capability_ranks(_frozen)
    _, _bf2 = reg.backfill_capability_ranks(_fixed)
    _byid = {r["id"]: r for r in _fixed}
    ok &= _check("backfill repairs a frozen pre-capability_rank registry", _bf is True)
    ok &= _check("backfill is idempotent — a second run changes nothing", _bf2 is False)
    ok &= _check("backfill lifts the claude_cli sonnet/haiku max_complexity caps",
                _byid["claude-cli-sonnet"]["max_complexity"] == "large"
                and _byid["claude-cli-haiku"]["max_complexity"] == "medium")
    ok &= _check("backfill restores Opus to the top rank on a frozen registry",
                _byid["copilot-opus"]["capability_rank"] == 95)
    ok &= _check("backfill never adds, drops or reorders rules",
                [r["id"] for r in _fixed] == [r["id"] for r in _frozen])
    _, _bf_default = reg.backfill_capability_ranks(reg.DEFAULT_MODEL_RULES)
    ok &= _check("DEFAULT_MODEL_RULES already ship ranked — backfill is a no-op",
                _bf_default is False)
    _op = [{"id": "claude-cli-sonnet", "provider": "claude_cli", "match": "*sonnet*",
            "cost_tier": "frontier", "max_complexity": "small",
            "capability_rank": 10, "enabled": True}]
    _, _op_changed = reg.backfill_capability_ranks(_op)
    ok &= _check("backfill leaves an operator's own retuned rule alone",
                _op_changed is False)

    # ------------------------------------------------------ copilot tool support
    # _request_copilot has always sent payload["tools"] and parsed tool_calls;
    # the registry claiming otherwise made needs_tools (a HARD filter) drop
    # every Copilot endpoint from every tool job.
    ok &= _check("every shipped copilot rule advertises supports_tools",
                all(r.get("supports_tools") is True
                    for r in reg.DEFAULT_MODEL_RULES if r.get("provider") == "copilot"))
    ok &= _check("copilot is selectable for a needs_tools job",
                getattr(sel.select_model(
                    sel.LlmRequirements(complexity="large", needs_tools=True),
                    [{"key": "copilot|x|claude-opus-5", "provider": "copilot",
                      "model": "claude-opus-5",
                      "caps": reg.resolve("copilot", "claude-opus-5", _cfg),
                      "available": True}], {}), "model", None) == "claude-opus-5")
    # native_agentic_tools means "the provider runs its own agent loop with
    # built-in Read/Grep/Glob" — true of the claude_cli harness, NOT of the
    # Copilot chat-completions API that ab actually drives. Copilot CLI being
    # agentic as a product says nothing about that surface.
    _cp = reg.resolve("copilot", "claude-opus-5", _cfg)
    _cli = reg.resolve("claude_cli", "claude-opus-5", _cfg)
    ok &= _check("copilot does OpenAI-style tools but is NOT native-agentic",
                _cp["supports_tools"] is True
                and _cp["native_agentic_tools"] is False
                and _cp["supports_mutating_agent"] is False)
    ok &= _check("claude_cli is the inverse — native-agentic, no OpenAI tool schema",
                _cli["supports_tools"] is False
                and _cli["native_agentic_tools"] is True
                and _cli["supports_mutating_agent"] is True)
    _frozen_cp = [dict(r) for r in reg.DEFAULT_MODEL_RULES]
    for _r in _frozen_cp:
        if _r.get("provider") == "copilot":
            _r["supports_tools"] = False
    _cpf, _cpc = reg.enable_copilot_tools(_frozen_cp)
    _, _cpc2 = reg.enable_copilot_tools(_cpf)
    ok &= _check("copilot tools repair fixes a frozen registry and is idempotent",
                _cpc is True and _cpc2 is False
                and all(r["supports_tools"] for r in _cpf if r.get("provider") == "copilot"))
    ok &= _check("copilot tools repair never adds, drops or reorders rules",
                [r["id"] for r in _cpf] == [r["id"] for r in _frozen_cp])
    _, _cp_default = reg.enable_copilot_tools(reg.DEFAULT_MODEL_RULES)
    ok &= _check("DEFAULT_MODEL_RULES already ship copilot tools — repair is a no-op",
                _cp_default is False)
    _, _cp_op = reg.enable_copilot_tools(
        [{"id": "my-copilot", "provider": "copilot", "match": "*",
          "supports_tools": False, "enabled": True}])
    ok &= _check("copilot tools repair leaves an operator's own rule alone",
                _cp_op is False)

    # ── speed_tier: the declared speed class that routes latency-sensitive work ──
    ok &= _check("every shipped rule declares a valid speed_tier",
                all(r.get("speed_tier") in reg.SPEED_RANK for r in reg.DEFAULT_MODEL_RULES))
    ok &= _check("speed_rank orders fast < standard < slow",
                reg.speed_rank({"speed_tier": "fast"}) < reg.speed_rank({"speed_tier": "standard"})
                < reg.speed_rank({"speed_tier": "slow"}))
    # A rule with no explicit value must still order sensibly, or an operator's
    # own rule would be treated as exactly as fast as everything else.
    ok &= _check("speed_tier falls back to a capability_rank-derived prior",
                reg.speed_tier({"capability_rank": 95}) == "slow"
                and reg.speed_tier({"capability_rank": 70}) == "standard"
                and reg.speed_tier({"capability_rank": 30}) == "fast")
    ok &= _check("speed_tier ignores a garbage value rather than trusting it",
                reg.speed_tier({"speed_tier": "quick", "capability_rank": 95}) == "slow")
    ok &= _check("the big reasoning models are not classed fast",
                all(reg.speed_tier(reg.resolve(p, m, _cfg)) != "fast"
                    for p, m in (("copilot", "claude-opus-5"), ("claude_cli", "claude-opus-5"),
                                 ("ollama_cloud", "nemotron-3-ultra"))))
    ok &= _check("the small/cheap models are classed fast",
                all(reg.speed_tier(reg.resolve(p, m, _cfg)) == "fast"
                    for p, m in (("copilot", "claude-haiku-4.5"), ("copilot", "gpt-4o-2024-08-06"),
                                 ("google", "gemini-3.5-flash-lite"), ("openrouter", "openrouter/free"))))
    ok &= _check("speed_tier is resolved as a capability, not dropped by resolve()",
                reg.resolve("copilot", "claude-haiku-4.5", _cfg).get("speed_tier") == "fast")

    # Ranking. The slow-class model is given the BETTER measured latency, so a
    # pure perf sort would take it — proving the declared class actually leads.
    _sp = [{"key": f"copilot|x|{m}", "provider": "copilot", "model": m,
            "caps": reg.resolve("copilot", m, _cfg), "available": True}
           for m in ("claude-opus-5", "claude-sonnet-5")]
    _spperf = {"copilot|x|claude-opus-5":   {"n": 20, "tps": 90.0, "latency_ms": 1000},
               "copilot|x|claude-sonnet-5": {"n": 20, "tps": 40.0, "latency_ms": 2500}}
    _sppick = lambda **kw: getattr(
        sel.select_model(sel.LlmRequirements(**kw), _sp, _spperf), "model", None)
    ok &= _check("without latency_sensitive, measured perf still decides",
                _sppick(complexity="small") == "claude-opus-5")
    ok &= _check("latency_sensitive picks the FAST class over a better-measured slow one",
                _sppick(complexity="small", latency_sensitive=True) == "claude-sonnet-5")
    # Precedence: a stale speed flag must never flatten the escalation ladder
    # for chat, builds, feature requests or bugfixes.
    ok &= _check("complexity=large beats latency_sensitive",
                _sppick(complexity="large", latency_sensitive=True) == "claude-opus-5")
    ok &= _check("prefer_capable beats latency_sensitive",
                _sppick(complexity="small", latency_sensitive=True,
                        prefer_capable=True) == "claude-opus-5")
    ok &= _check("prefer_capable_within_tier beats latency_sensitive",
                _sppick(complexity="small", latency_sensitive=True,
                        prefer_capable_within_tier=True) == "claude-opus-5")
    # Cold start: with no samples every candidate sits on the tier MEDIAN, so
    # before speed_tier a brand-new fast endpoint could not win on merit.
    ok &= _check("latency_sensitive still works with ZERO perf samples",
                getattr(sel.select_model(
                    sel.LlmRequirements(complexity="small", latency_sensitive=True),
                    _sp, {}), "model", None) == "claude-sonnet-5")

    # Measured perf must still decide INSIDE a speed class — the class is a
    # prior, not a replacement for data.
    _tf = [{"key": f"copilot|x|{m}", "provider": "copilot", "model": m,
            "caps": reg.resolve("copilot", m, _cfg), "available": True}
           for m in ("claude-haiku-4.5", "gpt-4o-2024-08-06")]
    _tfperf = {"copilot|x|claude-haiku-4.5":  {"n": 20, "tps": 50.0, "latency_ms": 2500},
               "copilot|x|gpt-4o-2024-08-06": {"n": 20, "tps": 90.0, "latency_ms": 1000}}
    ok &= _check("between two fast models the measurably faster one wins",
                getattr(sel.select_model(
                    sel.LlmRequirements(complexity="small", latency_sensitive=True),
                    _tf, _tfperf), "model", None) == "gpt-4o-2024-08-06")
    ok &= _check("prefer_capable_within_tier takes the stronger of the two instead",
                getattr(sel.select_model(
                    sel.LlmRequirements(complexity="small", prefer_capable_within_tier=True),
                    _tf, _tfperf), "model", None) == "claude-haiku-4.5")

    # "Free but capable" (log analysis): capability ordering INSIDE the tier the
    # cost preference already chose, WITHOUT prefer_capable's tier inversion.
    _mix = [{"key": f"{p}|x|{m}", "provider": p, "model": m,
             "caps": reg.resolve(p, m, _cfg), "available": True}
            for p, m in (("copilot", "claude-opus-5"), ("openrouter", "openrouter/free"))]
    _mixperf = {"copilot|x|claude-opus-5":       {"n": 20, "tps": 90.0, "latency_ms": 1000},
                "openrouter|x|openrouter/free":  {"n": 20, "tps": 30.0, "latency_ms": 2500}}
    ok &= _check("prefer_capable_within_tier does NOT buy a more expensive tier",
                getattr(sel.select_model(
                    sel.LlmRequirements(complexity="small", prefer_capable_within_tier=True),
                    _mix, _mixperf), "model", None) == "openrouter/free")
    ok &= _check("prefer_capable, by contrast, DOES invert to the frontier tier",
                getattr(sel.select_model(
                    sel.LlmRequirements(complexity="small", prefer_capable=True),
                    _mix, _mixperf), "model", None) == "claude-opus-5")

    # The picker must be able to explain which axis ordered the tier.
    _ex = sel.explain_selection(
        sel.LlmRequirements(complexity="small", latency_sensitive=True), _sp, _spperf)
    ok &= _check("explain_selection exposes speed and capability_rank per row",
                all("speed" in r and "capability_rank" in r for r in _ex["rows"]))
    ok &= _check("the selection reason names the axis that ordered the tier",
                "ranked by speed" in (_ex["selected"] or {}).get("reason", ""))
    ok &= _check("a capability-ordered pick says so instead",
                "ranked by capability" in (sel.explain_selection(
                    sel.LlmRequirements(complexity="large"), _sp, _spperf
                )["selected"] or {}).get("reason", ""))

    # Backfill migration: speed_tier lives ON already-persisted rules.
    _frozen_sp = [dict(r) for r in reg.DEFAULT_MODEL_RULES]
    for _r in _frozen_sp:
        _r.pop("speed_tier", None)
    _spf, _spc = reg.backfill_speed_tiers(_frozen_sp)
    _, _spc2 = reg.backfill_speed_tiers(_spf)
    ok &= _check("speed backfill repairs a frozen registry and is idempotent",
                _spc is True and _spc2 is False
                and all(r.get("speed_tier") in reg.SPEED_RANK for r in _spf))
    ok &= _check("speed backfill never adds, drops or reorders rules",
                [r["id"] for r in _spf] == [r["id"] for r in _frozen_sp])
    _, _sp_default = reg.backfill_speed_tiers(reg.DEFAULT_MODEL_RULES)
    ok &= _check("DEFAULT_MODEL_RULES already ship speed_tier — backfill is a no-op",
                _sp_default is False)
    _, _sp_op = reg.backfill_speed_tiers(
        [{"id": "my-rule", "provider": "copilot", "match": "*", "enabled": True}])
    ok &= _check("speed backfill leaves an operator's own rule alone", _sp_op is False)
    _sp_tuned, _sp_tc = reg.backfill_speed_tiers(
        [{"id": "copilot-opus", "provider": "copilot", "match": "*opus*",
          "speed_tier": "fast", "enabled": True}])
    ok &= _check("speed backfill never overwrites a value the operator retuned",
                _sp_tc is False and _sp_tuned[0]["speed_tier"] == "fast")

    # ── Help-assistant role ladder ───────────────────────────────────────────
    # The hub's Ask-AI loop tags each turn with a `role` and hub_agent.py maps it
    # to requirements. Before this, EVERY turn routed identically (cost-first +
    # latency_sensitive), so the free model both picked the tools and wrote the
    # user-visible answer. These pin the three rungs against the real measured
    # endpoint mix, because the mapping is only correct relative to a registry:
    # the escalated rung in particular is a silent no-op unless complexity rises.
    _ha = [("claude_cli", "claude-opus-5", 70.1, 110263, 19),
           ("google", "gemini-3.5-flash-lite", 41.3, 233189, 2),
           ("ollama_cloud", "nemotron-3-ultra", 16.1, 59989, 49),
           ("openrouter", "openrouter/free", None, 9533, 50),
           ("copilot", "claude-haiku-4.5", None, 9923, 5),
           ("copilot", "claude-sonnet-5", None, 4411, 4),
           ("copilot", "claude-opus-5", None, 7414, 4),
           ("copilot", "gpt-4o-2024-08-06", None, 4453, 2)]
    _hac = [{"key": f"{p}|x|{m}", "provider": p, "model": m,
             "caps": reg.resolve(p, m, _cfg), "available": True}
            for p, m, _t, _l, _n in _ha]
    _hap = {f"{p}|x|{m}": {"n": n, "tps": t, "latency_ms": l}
            for p, m, t, l, n in _ha}
    _hapick = lambda **kw: getattr(
        sel.select_model(sel.LlmRequirements(**kw), _hac, _hap), "model", None)
    _r_tool = _hapick(complexity="medium", needs_tools=True, latency_sensitive=True)
    _r_hard = _hapick(complexity="large", needs_tools=True,
                      prefer_capable_within_tier=True)
    _r_final = _hapick(complexity="large", needs_tools=False, prefer_capable=True)
    ok &= _check("help role=tool stays on the cheap/fast model",
                _r_tool == "openrouter/free")
    ok &= _check("help role=final buys the strongest model",
                _r_final == "claude-opus-5")
    ok &= _check("help role=tool_hard escalates off the fast model",
                _r_hard != _r_tool)
    # The escalation is driven by max_complexity, which is a hard FILTER, not by
    # capability_rank, which only orders (and is unset on several shipped rules).
    # Asserting the ceiling is therefore asserting the actual mechanism: the
    # escalated turn lands on a model that can take work the fast one cannot.
    _hacap = lambda model: next(
        reg.resolve(p, m, _cfg).get("max_complexity")
        for p, m, _t, _l, _n in _ha if m == model)
    ok &= _check("help role=tool_hard escalates past the fast model's ceiling",
                _hacap(_r_tool) == "medium" and _hacap(_r_hard) == "large")
    ok &= _check("help role=tool_hard stops SHORT of the frontier answer model",
                _r_hard != _r_final)
    # Regression: prefer_capable_within_tier ALONE cannot escalate a tier that
    # holds a single endpoint — this is why the mapping raises complexity too.
    ok &= _check("within-tier preference alone is a no-op in a one-model tier",
                _hapick(complexity="medium", needs_tools=True,
                        prefer_capable_within_tier=True) == _r_tool)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
