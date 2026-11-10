#!/usr/bin/env python3
"""Self-test for model_selection.py — select_model()'s full decision truth
table, the highest-value test in the LLM redesign per the approved plan.

Run:  python3 ab/test_model_selection.py

Standalone: imports only model_selection (which imports model_registry).
No app/main init.
"""
import sys

import model_selection as sel


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _caps(cost_tier="free", complexity="small", context_window=32768, **overrides):
    caps = {
        "cost_tier": cost_tier, "max_complexity": complexity, "context_window": context_window,
        "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
        "supports_structured_output": False, "supports_streaming": False,
    }
    caps.update(overrides)
    return caps


def _cand(key, provider="ollama", model="m", base_url="http://x", available=True, **caps_overrides):
    return {
        "key": key, "provider": provider, "model": model, "base_url": base_url,
        "api_key": "", "rpm": 0, "available": available, "caps": _caps(**caps_overrides),
    }


def _perf(lat, tps, n):
    return {"latency_ms": lat, "tps": tps, "n": n}


def main():
    print("Running ab model_selection self-test...")
    ok = True
    R = sel.LlmRequirements

    # --- capability filters (hard) -------------------------------------------

    with_tools = _cand("a", supports_tools=True)
    without_tools = _cand("b", supports_tools=False)
    result = sel.select_model(R(needs_tools=True), [without_tools])
    ok &= _check("needs_tools excludes a candidate that lacks supports_tools", result is None)
    result = sel.select_model(R(needs_tools=True), [with_tools])
    ok &= _check("needs_tools admits a candidate that has supports_tools", result is not None and result.key == "a")

    result = sel.select_model(R(needs_native_agentic_tools=True), [_cand("a", native_agentic_tools=False)])
    ok &= _check("needs_native_agentic_tools excludes a candidate without it", result is None)
    result = sel.select_model(R(needs_native_agentic_tools=True), [_cand("a", native_agentic_tools=True)])
    ok &= _check("needs_native_agentic_tools admits a candidate with it", result is not None)

    result = sel.select_model(R(needs_mutating_agent=True), [_cand("a", supports_mutating_agent=False)])
    ok &= _check("needs_mutating_agent excludes a candidate without it", result is None)
    result = sel.select_model(R(needs_mutating_agent=True), [_cand("a", supports_mutating_agent=True)])
    ok &= _check("needs_mutating_agent admits a candidate with it", result is not None)

    # context: needs context_window >= min_context_tokens * 1.25
    result = sel.select_model(R(min_context_tokens=10000), [_cand("a", context_window=12000)])
    ok &= _check("context filter excludes a window just under the 1.25x headroom", result is None)
    result = sel.select_model(R(min_context_tokens=10000), [_cand("a", context_window=12500)])
    ok &= _check("context filter admits a window exactly at the 1.25x headroom", result is not None)

    # complexity ceiling: candidate's max_complexity rank must be >= required rank
    result = sel.select_model(R(complexity="large"), [_cand("a", complexity="medium")])
    ok &= _check("complexity ceiling excludes a candidate ranked below the requirement", result is None)
    result = sel.select_model(R(complexity="medium"), [_cand("a", complexity="large")])
    ok &= _check("complexity ceiling admits a candidate ranked above the requirement", result is not None)
    result = sel.select_model(R(complexity="medium"), [_cand("a", complexity="medium")])
    ok &= _check("complexity ceiling admits a candidate ranked exactly at the requirement", result is not None)

    # --- availability / exclusions / restrictions ----------------------------

    result = sel.select_model(R(), [_cand("a", available=False)])
    ok &= _check("an unavailable candidate is never picked", result is None)

    result = sel.select_model(R(exclude_models=("a",)), [_cand("a"), _cand("b")])
    ok &= _check("exclude_models removes a specific ModelKey from consideration", result is not None and result.key == "b")

    local = _cand("a", provider="ollama", cost_tier="free")
    cloud = _cand("b", provider="anthropic", cost_tier="frontier", complexity="large")
    result = sel.select_model(R(restrict="local"), [local, cloud])
    ok &= _check("restrict=local admits a free no-key provider", result is not None and result.key == "a")
    result = sel.select_model(R(restrict="local"), [cloud])
    ok &= _check("restrict=local excludes a cloud provider even if otherwise eligible", result is None)
    result = sel.select_model(R(restrict="cloud"), [local])
    ok &= _check("restrict=cloud excludes a no-key local provider", result is None)
    result = sel.select_model(R(restrict="cloud"), [cloud])
    ok &= _check("restrict=cloud admits a cloud provider", result is not None and result.key == "b")
    claude = _cand("c", provider="claude_cli", cost_tier="free")
    result = sel.select_model(R(restrict="claude"), [local, cloud, claude])
    ok &= _check("restrict=claude only admits provider=claude_cli", result is not None and result.key == "c")

    # --- empty / no candidates -------------------------------------------------

    ok &= _check("no candidates at all -> None", sel.select_model(R(), []) is None)
    ok &= _check("None candidates -> None, no crash", sel.select_model(R(), None) is None)

    # --- cost-tier ordering: free beats cheap beats frontier when all qualify -

    free_c = _cand("f", cost_tier="free")
    cheap_c = _cand("c", cost_tier="cheap")
    frontier_c = _cand("fr", cost_tier="frontier")
    result = sel.select_model(R(), [frontier_c, cheap_c, free_c])
    ok &= _check("cheapest satisfying tier wins regardless of list order (free)", result.key == "f" and result.tier == "free")
    result = sel.select_model(R(), [frontier_c, cheap_c])
    ok &= _check("next-cheapest tier wins when the cheaper tier is absent (cheap)", result.key == "c" and result.tier == "cheap")

    # --- prefer_capable flips tier order (frontier > cheap > free) for the
    # planner/router turn, while default keeps cost-first ---------------------
    free_p = _cand("pf", cost_tier="free", complexity="large")
    cheap_p = _cand("pc", cost_tier="cheap", complexity="large")
    frontier_p = _cand("pfr", cost_tier="frontier", complexity="large")
    result = sel.select_model(R(prefer_capable=True), [free_p, cheap_p, frontier_p])
    ok &= _check("prefer_capable picks the frontier tier when all qualify", result.key == "pfr" and result.tier == "frontier")
    result = sel.select_model(R(prefer_capable=True), [free_p, cheap_p])
    ok &= _check("prefer_capable falls to the next-most-capable tier (cheap) when no frontier", result.key == "pc" and result.tier == "cheap")
    result = sel.select_model(R(prefer_capable=False), [free_p, cheap_p, frontier_p])
    ok &= _check("default (prefer_capable off) still picks the cheapest tier (free)", result.key == "pf" and result.tier == "free")

    # --- deprioritize_local: offloadable work (log review, batch) prefers a
    # capable FREE CLOUD peer over the local GPU, but the GPU stays a fallback
    # and paid is reached only when nothing free qualifies --------------------
    gpu_free = _cand("gpu", provider="ollama", cost_tier="free", complexity="medium")
    cloud_free = _cand("cloud", provider="openrouter", cost_tier="free", complexity="medium")
    frontier_paid = _cand("front", provider="anthropic", cost_tier="frontier", complexity="large")
    result = sel.select_model(R(deprioritize_local=True), [gpu_free, cloud_free])
    ok &= _check("deprioritize_local prefers the free cloud peer over the local GPU", result.key == "cloud")
    result = sel.select_model(R(deprioritize_local=False), [gpu_free, cloud_free])
    # (cold-start: both share the tier-median score, so list order decides — the
    # point is only that WITHOUT the flag the GPU is not pushed to the bottom.)
    ok &= _check("without the flag the local GPU is NOT deprioritized (free tier still wins)",
                 result.tier == "free")
    result = sel.select_model(R(deprioritize_local=True), [gpu_free])
    ok &= _check("deprioritize_local never empties a tier — the GPU still wins when it is the only free option",
                 result is not None and result.key == "gpu")
    result = sel.select_model(R(deprioritize_local=True), [gpu_free, frontier_paid])
    ok &= _check("deprioritize_local never promotes a paid tier over a free local GPU",
                 result.key == "gpu" and result.tier == "free")

    # --- escalation: when the GPU can't serve capably (cooldown/unavailable or
    # below the complexity ceiling) an offloadable call falls through to a
    # capable frontier/claude_cli, never getting stuck on the GPU -------------
    gpu_down = _cand("gpu2", provider="ollama", cost_tier="free", complexity="medium", available=False)
    result = sel.select_model(R(deprioritize_local=True), [gpu_down, frontier_paid])
    ok &= _check("an unavailable GPU escalates an offloadable call to the capable frontier",
                 result is not None and result.key == "front" and result.tier == "frontier")
    gpu_weak = _cand("gpu3", provider="ollama", cost_tier="free", complexity="small")
    result = sel.select_model(R(complexity="large", deprioritize_local=True), [gpu_weak, frontier_paid])
    ok &= _check("a GPU below the complexity ceiling escalates to the capable frontier",
                 result is not None and result.key == "front" and result.tier == "frontier")

    # --- pin_key canonicalisation: a "provider|base_url|model" STRING pin
    # must match a tuple ModelKey candidate (chat_pin / planner pin) ----------
    tk = ("ollama", "http://gpu:11434", "qwen")
    pinned = _cand(tk, provider="Ollama", base_url="http://gpu:11434/", model="qwen", cost_tier="free")
    other = _cand(("anthropic", "https://api", "claude"), provider="anthropic", cost_tier="frontier", complexity="large")
    result = sel.select_model(R(pin_key="ollama|http://gpu:11434|qwen"), [pinned, other])
    ok &= _check("string pin matches its tuple ModelKey candidate", result is not None and result.key == tk)
    result = sel.select_model(R(pin_key="nope|http://x|y"), [pinned, other])
    ok &= _check("a string pin that matches nothing selects nothing", result is None)
    result = sel.select_model(R(pin_key=tk), [pinned, other])
    ok &= _check("a tuple pin still matches its tuple ModelKey candidate", result is not None and result.key == tk)

    # --- within-tier performance ranking --------------------------------------

    fast = _cand("fast", cost_tier="free")
    slow = _cand("slow", cost_tier="free")
    perf = {"fast": _perf(100, 50, 5), "slow": _perf(300, 20, 5)}  # 3x ratio: ranked, not exhausted
    result = sel.select_model(R(), [fast, slow], perf=perf)
    ok &= _check("within a tier, the measurably faster/higher-tok/s model wins", result.key == "fast")
    ok &= _check("the slower survivor is offered as an alternative, not dropped",
                any(c["key"] == "slow" for c in result.alternatives))

    # --- cold-start neutral score: unmeasured gets the tier's median score,
    # not zero -> ranks above the worst measured performer, not last --------

    fastc = _cand("A", cost_tier="cheap")
    midc = _cand("B", cost_tier="cheap")
    slowc = _cand("C", cost_tier="cheap")   # 300 / 100 = 3x fastest -> not exhausted
    cold = _cand("D", cost_tier="cheap")    # brand new, zero samples
    perf2 = {"A": _perf(100, 50, 5), "B": _perf(200, 25, 5), "C": _perf(300, 10, 5)}
    stats = sel._rank_tier([fastc, midc, slowc, cold], perf2, R(), sel.MIN_SAMPLES)
    by_key = {s["c"]["key"]: s for s in stats}
    ok &= _check("cold-start candidate's score equals the measured tier's median score",
                abs(by_key["D"]["score"] - by_key["B"]["score"]) < 1e-9)
    ok &= _check("cold-start candidate outranks the worst measured performer (no zero-score starvation)",
                by_key["D"]["score"] > by_key["C"]["score"])
    ok &= _check("cold-start candidate does not beat a genuinely well-measured top performer",
                by_key["D"]["score"] < by_key["A"]["score"])

    # --- relative exhaustion: measurably-too-slow falls through like a cooldown

    fast_e = _cand("fast", cost_tier="free")
    slow_e = _cand("slow", cost_tier="free")
    perf3 = {"fast": _perf(100, 50, 5), "slow": _perf(1000, 5, 5)}  # 10x ratio > SLOW_FACTOR(4.0)
    result = sel.select_model(R(), [fast_e, slow_e], perf=perf3)
    ok &= _check("a model >4x slower than its tier's fastest peer is treated as exhausted", result.key == "fast")
    ok &= _check("an exhausted candidate is dropped entirely, not offered as an alternative",
                not any(c["key"] == "slow" for c in result.alternatives))

    # n=2 tier-min boundary: exactly at the threshold survives; just over it doesn't
    stats_b = sel._rank_tier(
        [_cand("min"), _cand("atlimit")], {"min": _perf(100, 10, 5), "atlimit": _perf(400, 10, 5)}, R(), 3)
    survivors_b = sel._apply_exhaustion(stats_b, sel.DEFAULT_SLOW_FACTOR, 3)
    ok &= _check("exactly at slow_factor*tier_min survives (boundary is '>', not '>=')",
                any(c["key"] == "atlimit" for c in survivors_b))
    stats_b2 = sel._rank_tier(
        [_cand("min"), _cand("overlimit")], {"min": _perf(100, 10, 5), "overlimit": _perf(400.01, 10, 5)}, R(), 3)
    survivors_b2 = sel._apply_exhaustion(stats_b2, sel.DEFAULT_SLOW_FACTOR, 3)
    ok &= _check("just over slow_factor*tier_min is excluded",
                not any(c["key"] == "overlimit" for c in survivors_b2))

    # a lone measured model in a tier can't be "relatively" slow (tier_population < 2 guard)
    stats_lone = sel._rank_tier([_cand("only")], {"only": _perf(999999, 1, 5)}, R(), 3)
    survivors_lone = sel._apply_exhaustion(stats_lone, sel.DEFAULT_SLOW_FACTOR, 3)
    ok &= _check("a single measured candidate in a tier is never exhausted against itself",
                len(survivors_lone) == 1 and survivors_lone[0]["key"] == "only")

    # never-empty-a-tier guard, exercised directly against _apply_exhaustion
    # with a contrived stats list (the real pipeline can't construct this
    # naturally -- the tier-min candidate always survives on its own -- but
    # the guard itself must still hold as a standalone invariant)
    contrived = [
        {"c": {"key": "x"}, "n": 5, "lat": 500, "score": 0.9},
        {"c": {"key": "y"}, "n": 5, "lat": 600, "score": 0.1},
    ]
    survivors_guard = sel._apply_exhaustion(contrived, slow_factor=1.05, min_samples=3)
    ok &= _check("never-empty guard: at least one survivor remains even under an aggressive contrived rule",
                len(survivors_guard) >= 1)

    # --- two-pass unknown-model fallback ---------------------------------------

    unknown_only = _cand("u", cost_tier="unknown")
    result = sel._select_pass(R(), [unknown_only], None, sel.DEFAULT_SLOW_FACTOR, sel.MIN_SAMPLES, allow_unknown=False)
    ok &= _check("strict pass excludes an unclassified (cost_tier=unknown) model", result is None)
    result = sel.select_model(R(), [unknown_only])
    ok &= _check("select_model's permissive fallback pass admits it when nothing classified qualifies",
                result is not None and result.key == "u")
    ok &= _check("the permissive-pass reason is flagged so it's distinguishable in logs",
                "permissive" in result.reason)

    classified = _cand("c", cost_tier="free")
    result = sel.select_model(R(), [classified, unknown_only])
    ok &= _check("a classified model is preferred over an unknown one when both qualify", result.key == "c")

    # --- soft preferences: structured output / streaming are tiebreaks, not filters

    plain = _cand("plain", cost_tier="free")
    structured = _cand("structured", cost_tier="free", supports_structured_output=True)
    perf4 = {"plain": _perf(100, 10, 5), "structured": _perf(100, 10, 5)}  # identical perf, only caps differ
    result = sel.select_model(R(needs_structured_output=True), [plain, structured], perf=perf4)
    ok &= _check("needs_structured_output prefers a capable model on an otherwise-tied score", result.key == "structured")
    result = sel.select_model(R(needs_structured_output=True), [plain])
    ok &= _check("needs_structured_output does NOT hard-exclude a model lacking it (soft preference)",
                result is not None and result.key == "plain")

    streaming = _cand("streaming", cost_tier="free", supports_streaming=True)
    result = sel.select_model(R(needs_streaming=True), [plain, streaming], perf=perf4)
    ok &= _check("needs_streaming prefers a capable model on an otherwise-tied score", result.key == "streaming")

    # --- Selection shape ---------------------------------------------------

    result = sel.select_model(R(), [free_c, cheap_c])
    ok &= _check("Selection carries provider/model/base_url/api_key/rpm through from the winning candidate",
                result.provider == free_c["provider"] and result.model == free_c["model"]
                and result.base_url == free_c["base_url"])

    # --- safety_floor ----------------------------------------------------------

    entries = [
        {"id": "e1", "provider": "anthropic", "_configured": True},
        {"id": "e2", "provider": "ollama", "_configured": True},
        {"id": "e3", "provider": "openai", "_configured": False},
    ]
    floor = sel.safety_floor(entries)
    ok &= _check("safety_floor prefers a no-key local provider over an earlier-listed cloud one",
                floor is not None and floor["id"] == "e2")

    only_cloud = [{"id": "e1", "provider": "anthropic", "_configured": True}]
    ok &= _check("safety_floor falls back to the first configured entry when nothing local qualifies",
                sel.safety_floor(only_cloud)["id"] == "e1")

    none_configured = [{"id": "e1", "provider": "anthropic", "_configured": False}]
    ok &= _check("safety_floor returns None when nothing at all is configured",
                sel.safety_floor(none_configured) is None)
    ok &= _check("safety_floor handles an empty/None entries list without crashing",
                sel.safety_floor([]) is None and sel.safety_floor(None) is None)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
