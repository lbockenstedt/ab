"""
model_selection.py — pure model-selection algorithm for AppBuilder's LLM
picker. No I/O, no config reads: given a candidate list (already resolved by
the impure caller, llm_client.py's enumerate_candidates) and a performance
snapshot (from llm_perf.py), decides which model to use for one call.

Pipeline: availability -> restriction -> capability filter -> group by cost
tier (free < cheap < frontier < unknown) -> within-tier performance ranking
(cold-start neutral; reqs.deprioritize_local pushes local/no-key endpoints to
the bottom of their tier so offloadable work prefers a free cloud peer) ->
relative-exhaustion check (never empties a tier) -> first surviving tier's
best candidate wins. Two-pass: a strict pass excludes
unclassified ("unknown" cost_tier) models; if that yields nothing, a
permissive pass re-admits them, so a fresh/lightly-curated registry still
resolves something instead of a cold-install dead end.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import model_registry as registry

MIN_SAMPLES = 3
DEFAULT_SLOW_FACTOR = 4.0

# Intra-tier score penalty applied to no-key/local endpoints when a call sets
# reqs.deprioritize_local. Far larger than the [0, ~1.1] score range so ANY
# non-local peer in the same tier outranks every local one — yet applied
# uniformly, so a tier that is all-local keeps its internal order and still
# resolves.
_LOCAL_OFFLOAD_PENALTY = 100.0


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
    pin_key: str | None = None                 # exact ModelKey "provider|base_url|model" — HARD pin (chat_pin)
    exclude_models: tuple = ()                 # ModelKeys already burned this run
    prefer_capable: bool = False               # invert tier preference: pick the SMARTEST tier
                                               # first (frontier>cheap>free) instead of cheapest —
                                               # for the planner/router turn ("fast AND smart")
    deprioritize_local: bool = False           # SOFT within-tier bias: rank no-key/local endpoints
                                               # (self-hosted GPU, LM Studio, claude_cli session) LAST
                                               # so offloadable work (log review, batch) prefers a
                                               # capable FREE CLOUD peer and leaves the GPU idle for
                                               # the coordinator. Never excludes: if the GPU is the
                                               # only free option it still wins its tier (paid stays
                                               # a fallback, reached only when nothing free qualifies).


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


def _canon_key(k):
    """Canonicalise a ModelKey for comparison. Candidate keys are tuples
    ``(provider, base_url, model)`` but pins (chat_pin / planner pin) arrive as
    ``"provider|base_url|model"`` STRINGS from config/UI — so normalise BOTH to
    the same tuple (lowered provider, trailing-slash-stripped base_url) or a
    string pin can never match a tuple key. Returns a 3-tuple."""
    if isinstance(k, (tuple, list)):
        parts = list(k) + ["", "", ""]
        p, b, m = parts[0], parts[1], parts[2]
    else:
        p, _, rest = str(k).partition("|")
        b, _, m = rest.partition("|")
    return ((p or "").lower().strip(), (b or "").strip().rstrip("/"), (m or "").strip())


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

    # Offload bias: push no-key/local endpoints (self-hosted GPU, LM Studio,
    # claude_cli session auth) to the BOTTOM of their tier so offloadable work
    # (log review, batch summaries) prefers a capable free CLOUD peer and the
    # GPU stays idle for the coordinator/planner. This is intra-tier only — the
    # penalty is applied equally to every local candidate, so an all-local tier
    # keeps its relative order and still resolves (never empties a tier), and a
    # paid tier is never promoted over it.
    if reqs.deprioritize_local:
        for s in stats:
            if registry.is_nokey_provider(s["c"].get("provider")):
                s["score"] -= _LOCAL_OFFLOAD_PENALTY

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
        if reqs.pin_key and _canon_key(c.get("key")) != _canon_key(reqs.pin_key):
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

    # Normally the cheapest tier wins (cost-first). For the planner/router turn
    # (prefer_capable) we invert it: the SMARTEST tier wins first (frontier >
    # cheap > free), with unknown always last — a fast, capable model makes the
    # routing decision. Within whichever tier is chosen, _rank_tier still ranks
    # by measured throughput/latency, so it stays "fast" too.
    _CAPABILITY_TIER_ORDER = {"frontier": 0, "cheap": 1, "free": 2, "unknown": 3}
    if reqs.prefer_capable:
        tier_keys = sorted(tiers.keys(), key=lambda t: _CAPABILITY_TIER_ORDER.get(t, 99))
    else:
        tier_keys = sorted(tiers.keys(), key=lambda t: registry.COST_TIER_RANK.get(t, 99))

    for tier_name in tier_keys:
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


def _exclusion_reason(c, reqs, allow_unknown):
    """The FIRST gate a candidate fails, in the same order _select_pass
    applies them, as human-readable text — or None if the candidate passes
    every filter (i.e. it reached tier ranking). Pure; used by
    explain_selection to make the dry-run picker legible."""
    if not c.get("available", True):
        return "unavailable: " + str(c.get("unavailable_reason") or "cooldown")
    if c.get("key") in (reqs.exclude_models or ()):
        return "excluded this run (already tried/burned)"
    if reqs.pin_key and _canon_key(c.get("key")) != _canon_key(reqs.pin_key):
        return "not the pinned endpoint (chat_pin)"
    if not _passes_restriction(c, reqs.restrict):
        return "restrict=" + str(reqs.restrict)
    caps = c.get("caps") or {}
    if reqs.needs_tools and not caps.get("supports_tools"):
        return "no tool support"
    if reqs.needs_native_agentic_tools and not caps.get("native_agentic_tools"):
        return "no native agentic tools"
    if reqs.needs_mutating_agent and not caps.get("supports_mutating_agent"):
        return "no mutating-agent support"
    context_window = caps.get("context_window") or 0
    if context_window < reqs.min_context_tokens * 1.25:
        return "context %d < required %d" % (context_window, int(reqs.min_context_tokens * 1.25))
    have = registry.COMPLEXITY_RANK.get(caps.get("max_complexity"), -1)
    need = registry.COMPLEXITY_RANK.get(reqs.complexity, 0)
    if have < need:
        return "max_complexity %s < %s" % (caps.get("max_complexity"), reqs.complexity)
    if not allow_unknown and caps.get("cost_tier") == "unknown":
        return "unclassified (unknown cost tier — admitted only if nothing else resolves)"
    return None


def explain_selection(reqs, candidates, perf=None, tuning=None):
    """Dry-run picker: run select_model over `candidates`, then classify EVERY
    candidate as selected / alternative / excluded with a reason, so an
    operator (Diagnostics → LLM picker) can audit the routing for one
    requirement set without spending a token. Pure — no network, no config
    reads. Returns:

        {"selected": {key, provider, model, tier, reason}|None,
         "permissive": bool,        # True if the winner came from the unclassified-admitting pass
         "rows": [{key, provider, model, tier, status, reason, n, tps, latency_ms}, ...]}

    Rows are ordered selected → alternatives (rank order) → excluded."""
    perf = perf or {}
    selection = select_model(reqs, candidates, perf, tuning)
    permissive = bool(selection and "permissive" in (selection.reason or ""))
    allow_unknown = permissive or selection is None  # None → both passes failed; explain permissively

    winner_key = selection.key if selection else None
    alt_order = [a.get("key") for a in (selection.alternatives if selection else [])]
    alt_rank = {k: i for i, k in enumerate(alt_order)}

    rows = []
    for c in candidates or []:
        key = c.get("key")
        caps = c.get("caps") or {}
        s = perf.get(key) or {}
        row = {
            "key": key, "provider": c.get("provider"), "model": c.get("model"),
            "tier": caps.get("cost_tier"),
            "n": s.get("n", 0) or 0, "tps": s.get("tps"), "latency_ms": s.get("latency_ms"),
        }
        if key == winner_key:
            row["status"] = "selected"
            row["reason"] = selection.reason
        elif key in alt_rank:
            row["status"] = "alternative"
            if reqs.deprioritize_local and registry.is_nokey_provider(c.get("provider")):
                row["reason"] = ("local/GPU deprioritized for offloadable work — "
                                 "fallback if no free cloud (tier=%s)" % (caps.get("cost_tier") or "?"))
            else:
                row["reason"] = "capable fallback (tier=%s)" % (caps.get("cost_tier") or "?")
        else:
            row["status"] = "excluded"
            row["reason"] = (_exclusion_reason(c, reqs, allow_unknown)
                             or "ranked below the selection in its tier")
        rows.append(row)

    _order = {"selected": 0, "alternative": 1, "excluded": 2}
    rows.sort(key=lambda r: (_order[r["status"]],
                             alt_rank.get(r["key"], 0) if r["status"] == "alternative" else 0))

    selected = None
    if selection:
        selected = {"key": selection.key, "provider": selection.provider,
                    "model": selection.model, "tier": selection.tier,
                    "reason": selection.reason}
    return {"selected": selected, "permissive": permissive, "rows": rows}
