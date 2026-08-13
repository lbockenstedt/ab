"""
model_selection.py — pure model-selection algorithm for BugFixer's LLM
picker. No I/O, no config reads: given a candidate list (already resolved by
the impure caller, llm_client.py's enumerate_candidates) and a performance
snapshot (from llm_perf.py), decides which model to use for one call.

Pipeline: availability -> restriction -> capability filter -> group by cost
tier (free < cheap < frontier < unknown) -> within-tier performance ranking
(cold-start neutral) -> relative-exhaustion check (never empties a tier) ->
first surviving tier's best candidate wins. Two-pass: a strict pass excludes
unclassified ("unknown" cost_tier) models; if that yields nothing, a
permissive pass re-admits them, so a fresh/lightly-curated registry still
resolves something instead of a cold-install dead end.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import model_registry as registry

MIN_SAMPLES = 3
DEFAULT_SLOW_FACTOR = 4.0


@dataclass(frozen=True)
class LlmRequirements:
    complexity: str = "small"                 # trivial|small|medium|large
    needs_tools: bool = False                  # OpenAI-style function calling — HARD filter
    needs_native_agentic_tools: bool = False   # claude_cli Read/Grep/Glob — HARD filter
    needs_mutating_agent: bool = False         # profile="build" — HARD filter
    needs_structured_output: bool = False      # SOFT preference (regex-scrape fallback always exists)
    min_context_tokens: int = 0                # HARD filter: context_window >= this * 1.25
    batch_ok: bool = False                     # consumed by the caller (llm_client), not this module
    needs_streaming: bool = False              # SOFT preference
    latency_sensitive: bool = False            # informational; ranking already favors low latency
    must_escalate_to_human: bool = False       # consumed by the caller when select_model returns None
    restrict: str | None = None                # "local"|"cloud"|"claude" — HARD filter
    exclude_models: tuple = ()                 # ModelKeys already burned this run


@dataclass
class Selection:
    key: tuple
    provider: str
    model: str
    api_key: str
    base_url: str
    rpm: int
    tier: str
    reason: str
    alternatives: list = field(default_factory=list)


def _passes_restriction(candidate, restrict):
    if not restrict:
        return True
    provider = candidate.get("provider")
    caps = candidate.get("caps") or {}
    if restrict == "local":
        return caps.get("cost_tier") == "free" and registry.is_nokey_provider(provider)
    if restrict == "cloud":
        return not registry.is_nokey_provider(provider)
    if restrict == "claude":
        return (provider or "").lower().strip() == "claude_cli"
    return True


def _passes_capability(caps, reqs):
    if reqs.needs_tools and not caps.get("supports_tools"):
        return False
    if reqs.needs_native_agentic_tools and not caps.get("native_agentic_tools"):
        return False
    if reqs.needs_mutating_agent and not caps.get("supports_mutating_agent"):
        return False
    context_window = caps.get("context_window") or 0
    if context_window < reqs.min_context_tokens * 1.25:
        return False
    have = registry.COMPLEXITY_RANK.get(caps.get("max_complexity"), -1)
    need = registry.COMPLEXITY_RANK.get(reqs.complexity, 0)
    if have < need:
        return False
    return True


def _rank_tier(tier_candidates, perf, reqs, min_samples):
    """Returns tier_candidates' stats, best-first. Each stat dict:
    {"c": candidate, "n": int, "lat": float|None, "score": float}.
    A candidate with fewer than min_samples real perf samples gets the TIER
    MEDIAN score of the measured candidates (not zero) — a newly added model
    must still get picked sometimes, or it never accumulates the samples
    needed to earn a real ranking (cold-start starvation)."""
    stats = []
    for c in tier_candidates:
        s = (perf or {}).get(c.get("key")) or {}
        n = s.get("n", 0) or 0
        tps = s.get("tps")
        lat = s.get("latency_ms")
        stats.append({"c": c, "n": n, "tps": tps, "lat": lat})

    measured = [s for s in stats if s["n"] >= min_samples and s["tps"] is not None
               and s["lat"] is not None and s["lat"] > 0]
    if measured:
        tps_vals = [s["tps"] for s in measured]
        inv_lat_vals = [1.0 / s["lat"] for s in measured]
        tmin, tmax = min(tps_vals), max(tps_vals)
        lmin, lmax = min(inv_lat_vals), max(inv_lat_vals)

        def _norm(v, lo, hi):
            return 0.5 if hi <= lo else (v - lo) / (hi - lo)

        for s in measured:
            s["score"] = 0.5 * _norm(s["tps"], tmin, tmax) + 0.5 * _norm(1.0 / s["lat"], lmin, lmax)
        median_score = sorted(s["score"] for s in measured)[len(measured) // 2]
    else:
        median_score = 0.5

    for s in stats:
        if "score" not in s:
            s["score"] = median_score

    # Soft preferences: a small bonus, never enough to override a real
    # performance gap or jump a cost tier (tiers are grouped before this
    # runs) — just a tiebreak among otherwise-similar candidates.
    for s in stats:
        caps = s["c"].get("caps") or {}
        bonus = 0.0
        if reqs.needs_structured_output and caps.get("supports_structured_output"):
            bonus += 0.05
        if reqs.needs_streaming and caps.get("supports_streaming"):
            bonus += 0.05
        s["score"] += bonus

    stats.sort(key=lambda s: s["score"], reverse=True)
    return stats


def _apply_exhaustion(ranked_stats, slow_factor, min_samples):
    """Drops a candidate that is reachable but measurably too slow RELATIVE
    TO ITS TIER PEERS (latency > tier_min * slow_factor), treating it exactly
    like a cooldown — it falls through to the next tier. Guards: needs >= 2
    measured peers in the tier (a lone model can't be "relatively" slow), and
    NEVER empties a tier — if the rule would drop every candidate, the
    fastest one survives anyway."""
    measured_lats = [s["lat"] for s in ranked_stats if s["n"] >= min_samples and s["lat"]]
    if len(measured_lats) < 2:
        return [s["c"] for s in ranked_stats]

    tier_min_lat = min(measured_lats)
    survivors = []
    for s in ranked_stats:
        if s["n"] >= min_samples and s["lat"] and s["lat"] > tier_min_lat * slow_factor:
            continue
        survivors.append(s["c"])

    if not survivors:
        fastest = min(ranked_stats, key=lambda s: s["lat"] if s["lat"] is not None else float("inf"))
        survivors = [fastest["c"]]
    return survivors


def _select_pass(reqs, candidates, perf, slow_factor, min_samples, allow_unknown):
    pool = []
    for c in candidates or []:
        if not c.get("available", True):
            continue
        if c.get("key") in (reqs.exclude_models or ()):
            continue
        if not _passes_restriction(c, reqs.restrict):
            continue
        caps = c.get("caps") or {}
        if not _passes_capability(caps, reqs):
            continue
        if not allow_unknown and caps.get("cost_tier") == "unknown":
            continue
        pool.append(c)

    if not pool:
        return None

    tiers = {}
    for c in pool:
        tiers.setdefault((c.get("caps") or {}).get("cost_tier", "unknown"), []).append(c)

    for tier_name in sorted(tiers.keys(), key=lambda t: registry.COST_TIER_RANK.get(t, 99)):
        tier_candidates = tiers[tier_name]
        ranked = _rank_tier(tier_candidates, perf, reqs, min_samples)
        survivors = _apply_exhaustion(ranked, slow_factor, min_samples)
        if not survivors:
            continue
        best = survivors[0]
        other_tiers = [c for name, group in tiers.items() if name != tier_name for c in group]
        alternatives = survivors[1:] + other_tiers
        reason = f"tier={tier_name}, complexity>={reqs.complexity}"
        if allow_unknown:
            reason += " (permissive pass: unclassified models admitted)"
        return Selection(
            key=best.get("key"), provider=best.get("provider"), model=best.get("model"),
            api_key=best.get("api_key", ""), base_url=best.get("base_url", ""),
            rpm=best.get("rpm", 0), tier=tier_name, reason=reason, alternatives=alternatives,
        )
    return None


def select_model(reqs, candidates, perf=None, tuning=None):
    """The public entry point. `candidates` — each a dict with at least
    {key, provider, model, api_key, base_url, rpm, caps, available} — is
    fully resolved by the caller (cooldowns, live-catalog checks, everything
    impure already applied via `available`). `perf` — {ModelKey: {"n": int,
    "tps": float|None, "latency_ms": float|None}} — comes from
    llm_perf.snapshot(). Returns a Selection, or None if nothing resolves
    even on the permissive pass (the caller falls to the safety floor)."""
    tuning = tuning or {}
    slow_factor = float(tuning.get("slow_factor", DEFAULT_SLOW_FACTOR))
    min_samples = int(tuning.get("min_samples", MIN_SAMPLES))

    result = _select_pass(reqs, candidates, perf, slow_factor, min_samples, allow_unknown=False)
    if result is not None:
        return result
    return _select_pass(reqs, candidates, perf, slow_factor, min_samples, allow_unknown=True)


def safety_floor(entries):
    """The rule-based last resort (never a hardcoded model ID — see
    model_registry.py's module docstring for why): the first entry, in
    `entries` order, that's already been established as configured
    (`_configured: True`, set by the impure caller), preferring a no-key
    local provider. `entries` mirrors config["llm_entries"]' shape. Returns
    the chosen entry dict, or None if nothing at all is configured."""
    configured = [e for e in (entries or []) if e.get("_configured")]
    if not configured:
        return None
    local = [e for e in configured if registry.is_nokey_provider(e.get("provider"))]
    return (local or configured)[0]
