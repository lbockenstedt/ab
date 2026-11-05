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
# Lets the "str | None" style type hints below work as plain annotations.
from __future__ import annotations

# dataclass builds the simple record types used for requirements and the selection result.
from dataclasses import dataclass, field

# The model registry supplies capability/cost/speed rankings and provider classification.
import model_registry as registry

# A model needs at least 3 real performance samples before its measured speed is trusted.
MIN_SAMPLES = 3
# A model more than 4x slower than its fastest tier peer is treated as exhausted.
DEFAULT_SLOW_FACTOR = 4.0

# Intra-tier score penalty applied to no-key/local endpoints when a call sets
# reqs.deprioritize_local. Far larger than the [0, ~1.1] score range so ANY
# non-local peer in the same tier outranks every local one — yet applied
# uniformly, so a tier that is all-local keeps its internal order and still
# resolves.
_LOCAL_OFFLOAD_PENALTY = 100.0


@dataclass(frozen=True)
class LlmRequirements:
    """What one LLM call needs: hard filters (must match) plus soft ranking preferences."""
    complexity: str = "small"                 # trivial|small|medium|large
    needs_tools: bool = False                  # OpenAI-style function calling — HARD filter
    needs_native_agentic_tools: bool = False   # claude_cli Read/Grep/Glob — HARD filter
    needs_mutating_agent: bool = False         # profile="build" — HARD filter
    needs_structured_output: bool = False      # SOFT preference (regex-scrape fallback always exists)
    min_context_tokens: int = 0                # HARD filter: context_window >= this * 1.25
    batch_ok: bool = False                     # consumed by the caller (llm_client), not this module
    needs_streaming: bool = False              # SOFT preference
    latency_sensitive: bool = False            # SOFT: order the chosen tier FASTEST-first
                                               # (declared speed_tier, then measured perf).
                                               # For chat's first turn, which must feel instant
                                               # and can escalate afterwards. Yields to a
                                               # capability request — see _rank_tier.
    must_escalate_to_human: bool = False       # consumed by the caller when select_model returns None
    restrict: str | None = None                # "local"|"cloud"|"claude" — HARD filter
    pin_key: str | None = None                 # exact ModelKey "provider|base_url|model" — HARD pin (chat_pin)
    exclude_models: tuple = ()                 # ModelKeys already burned this run
    prefer_capable: bool = False               # invert tier preference: pick the SMARTEST tier
                                               # first (frontier>cheap>free) instead of cheapest —
                                               # for the planner/router turn ("fast AND smart")
    prefer_capable_within_tier: bool = False   # SOFT: order by capability_rank INSIDE whichever
                                               # tier the cost preference already chose, WITHOUT
                                               # inverting that preference. This is the "free but
                                               # capable" knob: prefer_capable would spend frontier
                                               # money, plain cheapest-first would take the fastest
                                               # weakest model in the free tier. Log analysis wants
                                               # neither — it wants the best FREE model.
    deprioritize_local: bool = False           # SOFT within-tier bias: rank no-key/local endpoints
                                               # (self-hosted GPU, LM Studio, claude_cli session) LAST
                                               # so offloadable work (log review, batch) prefers a
                                               # capable FREE CLOUD peer and leaves the GPU idle for
                                               # the coordinator. Never excludes: if the GPU is the
                                               # only free option it still wins its tier (paid stays
                                               # a fallback, reached only when nothing free qualifies).


@dataclass
class Selection:
    """The model chosen for one call, plus why it was chosen and fallback alternatives."""
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
    # Tuple/list keys: pad with blanks so short keys still unpack into three parts.
    if isinstance(k, (tuple, list)):
        parts = list(k) + ["", "", ""]
        p, b, m = parts[0], parts[1], parts[2]
    # String keys look like "provider|base_url|model"; split them on the "|" separators.
    else:
        p, _, rest = str(k).partition("|")
        b, _, m = rest.partition("|")
    # Lower-case the provider and strip whitespace and trailing "/" so equal keys compare equal.
    return ((p or "").lower().strip(), (b or "").strip().rstrip("/"), (m or "").strip())


def _passes_restriction(candidate, restrict):
    """Return True if the candidate is allowed under the caller's local/cloud/claude restriction."""
    # No restriction requested: every candidate passes.
    if not restrict:
        return True
    # The provider name and capability record decide the restriction checks.
    provider = candidate.get("provider")
    caps = candidate.get("caps") or {}
    # "local" means a free, no-API-key (self-hosted) endpoint.
    if restrict == "local":
        return caps.get("cost_tier") == "free" and registry.is_nokey_provider(provider)
    # "cloud" means anything that needs an API key.
    if restrict == "cloud":
        return not registry.is_nokey_provider(provider)
    # "claude" means the local Claude CLI only.
    if restrict == "claude":
        return (provider or "").lower().strip() == "claude_cli"
    # Unknown restriction values do not filter anything out.
    return True


def _passes_capability(caps, reqs):
    """Return True if the model's capabilities meet every hard requirement of the call."""
    # The call needs function calling but the model lacks it.
    if reqs.needs_tools and not caps.get("supports_tools"):
        return False
    # The call needs Claude CLI's built-in Read/Grep/Glob tools.
    if reqs.needs_native_agentic_tools and not caps.get("native_agentic_tools"):
        return False
    # The call needs an agent that can edit files (build profile).
    if reqs.needs_mutating_agent and not caps.get("supports_mutating_agent"):
        return False
    # Require 25% headroom over the requested context size.
    context_window = caps.get("context_window") or 0
    if context_window < reqs.min_context_tokens * 1.25:
        return False
    # The model's maximum task complexity must be at least what the call needs.
    have = registry.COMPLEXITY_RANK.get(caps.get("max_complexity"), -1)
    need = registry.COMPLEXITY_RANK.get(reqs.complexity, 0)
    if have < need:
        return False
    # Every hard requirement is satisfied.
    return True


def _rank_tier(tier_candidates, perf, reqs, min_samples):
    """Returns tier_candidates' stats, best-first. Each stat dict:
    {"c": candidate, "n": int, "lat": float|None, "score": float}.
    A candidate with fewer than min_samples real perf samples gets the TIER
    MEDIAN score of the measured candidates (not zero) — a newly added model
    must still get picked sometimes, or it never accumulates the samples
    needed to earn a real ranking (cold-start starvation)."""
    # Gather each candidate's sample count, throughput (tokens/s) and latency.
    stats = []
    for c in tier_candidates:
        # Missing perf data simply means zero samples for that model.
        s = (perf or {}).get(c.get("key")) or {}
        n = s.get("n", 0) or 0
        tps = s.get("tps")
        lat = s.get("latency_ms")
        stats.append({"c": c, "n": n, "tps": tps, "lat": lat})

    # Only models with enough valid samples get a measured score.
    measured = [s for s in stats if s["n"] >= min_samples and s["tps"] is not None
               and s["lat"] is not None and s["lat"] > 0]
    # Score measured models half on throughput and half on (inverse) latency, each scaled
    # to 0..1 across the tier.
    if measured:
        tps_vals = [s["tps"] for s in measured]
        inv_lat_vals = [1.0 / s["lat"] for s in measured]
        tmin, tmax = min(tps_vals), max(tps_vals)
        lmin, lmax = min(inv_lat_vals), max(inv_lat_vals)

        def _norm(v, lo, hi):
            """Scale v into 0..1 between lo and hi (0.5 when all values are equal)."""
            return 0.5 if hi <= lo else (v - lo) / (hi - lo)

        # Combine both scaled metrics into the model's score.
        for s in measured:
            s["score"] = 0.5 * _norm(s["tps"], tmin, tmax) + 0.5 * _norm(1.0 / s["lat"], lmin, lmax)
        # The median measured score is given to models that are not measured yet.
        median_score = sorted(s["score"] for s in measured)[len(measured) // 2]
    # Nobody in the tier is measured yet: everyone starts at a neutral 0.5.
    else:
        median_score = 0.5

    # Fill in the cold-start score for any model without its own measured score.
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

    # Capability ordering. Perf scores are min-max normalised WITHIN the tier,
    # so between two candidates the faster one takes 1.0 and the slower 0.0 —
    # a full-range gap that no additive bonus could ever overcome. Copilot Opus
    # and Sonnet are both frontier/large, so before this the tier was ordered on
    # speed alone and Sonnet (much faster) always beat Opus for exactly the hard
    # work Opus is reserved for.
    #
    # Capability therefore becomes the PRIMARY key for demanding work, with perf
    # only breaking ties at equal rank. "Demanding" is either an explicit
    # prefer_capable (the planner/router turn) or complexity="large" — the
    # latter is what keeps feature_build landing on Opus now that the strength
    # ladder is expressed as a rank instead of as a max_complexity cap that
    # hard-excluded the smaller models.
    #
    # prefer_capable_within_tier asks for the same capability-primary ordering
    # WITHOUT prefer_capable's tier inversion, which is how "free but capable"
    # work (log analysis) gets the strongest model in the free tier instead of
    # the fastest weakest one.
    #
    # Everything below large orders on SPEED. latency_sensitive makes that
    # explicit and adds the declared speed_tier as the primary key, ahead of the
    # measured score: a brand-new fast endpoint has no samples yet and would
    # otherwise sit on the tier median, and measured latency alone cannot tell
    # "inherently big and slow" apart from "fast model having a bad minute".
    # The measured score still breaks ties inside a speed class, so real data
    # decides between two fast models.
    #
    # Precedence is deliberate: capability WINS over latency_sensitive. A caller
    # that asks for speed and then escalates (chat) must actually get a stronger
    # model on the escalated call, so the escalation ladder for chat, builds,
    # feature requests and bugfixes cannot be flattened by a stale speed flag.
    # Decide how the tier is ordered: by capability, by declared speed, or by measured score.
    demanding = (reqs.prefer_capable or reqs.prefer_capable_within_tier
                 or reqs.complexity == "large")
    if demanding:
        stats.sort(key=lambda s: (registry.capability_rank(s["c"].get("caps") or {}), s["score"]),
                   reverse=True)
    elif reqs.latency_sensitive:
        stats.sort(key=lambda s: (-registry.speed_rank(s["c"].get("caps") or {}), s["score"]),
                   reverse=True)
    else:
        stats.sort(key=lambda s: s["score"], reverse=True)
    return stats


def _apply_exhaustion(ranked_stats, slow_factor, min_samples):
    """Drops a candidate that is reachable but measurably too slow RELATIVE
    TO ITS TIER PEERS (latency > tier_min * slow_factor), treating it exactly
    like a cooldown — it falls through to the next tier. Guards: needs >= 2
    measured peers in the tier (a lone model can't be "relatively" slow), and
    NEVER empties a tier — if the rule would drop every candidate, the
    fastest one survives anyway."""
    # Latencies of the models that have enough samples to judge.
    measured_lats = [s["lat"] for s in ranked_stats if s["n"] >= min_samples and s["lat"]]
    # Fewer than two measured peers: nothing can be "relatively" slow; keep everyone.
    if len(measured_lats) < 2:
        return [s["c"] for s in ranked_stats]

    # The fastest measured latency in the tier is the baseline.
    tier_min_lat = min(measured_lats)
    # Keep every candidate except measured ones slower than baseline x slow_factor.
    survivors = []
    for s in ranked_stats:
        if s["n"] >= min_samples and s["lat"] and s["lat"] > tier_min_lat * slow_factor:
            continue
        survivors.append(s["c"])

    # The rule removed everyone: keep the fastest one so the tier never empties.
    if not survivors:
        fastest = min(ranked_stats, key=lambda s: s["lat"] if s["lat"] is not None else float("inf"))
        survivors = [fastest["c"]]
    # Return the surviving candidates, still in rank order.
    return survivors


def _select_pass(reqs, candidates, perf, slow_factor, min_samples, allow_unknown):
    """Run one selection pass; allow_unknown admits models with an unclassified cost tier."""
    # Candidates that pass every hard filter.
    pool = []
    # Apply each hard filter in turn; a candidate failing any of them is skipped.
    for c in candidates or []:
        # Unavailable (cooldown, missing from the live catalog, ...).
        if not c.get("available", True):
            continue
        # Already tried and failed during this run.
        if c.get("key") in (reqs.exclude_models or ()):
            continue
        # A pin is set and this is not the pinned endpoint.
        if reqs.pin_key and _canon_key(c.get("key")) != _canon_key(reqs.pin_key):
            continue
        # Outside the local/cloud/claude restriction.
        if not _passes_restriction(c, reqs.restrict):
            continue
        # Missing a required capability.
        caps = c.get("caps") or {}
        if not _passes_capability(caps, reqs):
            continue
        # Unclassified cost tier, excluded on the strict pass.
        if not allow_unknown and caps.get("cost_tier") == "unknown":
            continue
        # Passed every filter.
        pool.append(c)

    # Nothing qualifies on this pass.
    if not pool:
        return None

    # Group the qualifying candidates by cost tier.
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

    # Try tiers in preference order; the first tier with a survivor wins.
    for tier_name in tier_keys:
        # Rank this tier's candidates, then drop the relatively-too-slow ones.
        tier_candidates = tiers[tier_name]
        ranked = _rank_tier(tier_candidates, perf, reqs, min_samples)
        survivors = _apply_exhaustion(ranked, slow_factor, min_samples)
        # Every candidate in this tier was dropped; fall through to the next tier.
        if not survivors:
            continue
        # The best-ranked survivor is the choice.
        best = survivors[0]
        # Fallbacks: the rest of this tier first, then every other tier's candidates.
        other_tiers = [c for name, group in tiers.items() if name != tier_name for c in group]
        alternatives = survivors[1:] + other_tiers
        # Human-readable explanation of the choice, shown in Diagnostics.
        reason = f"tier={tier_name}, complexity>={reqs.complexity}"
        # Name the axis that actually ordered the tier, so an operator reading
        # Diagnostics can tell "picked for speed" from "picked for strength".
        _bcaps = best.get("caps") or {}
        if reqs.prefer_capable or reqs.prefer_capable_within_tier or reqs.complexity == "large":
            reason += f", ranked by capability (rank={registry.capability_rank(_bcaps)})"
        elif reqs.latency_sensitive:
            reason += f", ranked by speed (speed={registry.speed_tier(_bcaps)})"
        # Note when the choice only came from the permissive pass.
        if allow_unknown:
            reason += " (permissive pass: unclassified models admitted)"
        # Package the winning candidate's details as the result.
        return Selection(
            key=best.get("key"), provider=best.get("provider"), model=best.get("model"),
            api_key=best.get("api_key", ""), base_url=best.get("base_url", ""),
            rpm=best.get("rpm", 0), tier=tier_name, reason=reason, alternatives=alternatives,
        )
    # No tier produced a survivor.
    return None


def select_model(reqs, candidates, perf=None, tuning=None):
    """The public entry point. `candidates` — each a dict with at least
    {key, provider, model, api_key, base_url, rpm, caps, available} — is
    fully resolved by the caller (cooldowns, live-catalog checks, everything
    impure already applied via `available`). `perf` — {ModelKey: {"n": int,
    "tps": float|None, "latency_ms": float|None}} — comes from
    llm_perf.snapshot(). Returns a Selection, or None if nothing resolves
    even on the permissive pass (the caller falls to the safety floor)."""
    # Read the tuning knobs, falling back to the module defaults.
    tuning = tuning or {}
    slow_factor = float(tuning.get("slow_factor", DEFAULT_SLOW_FACTOR))
    min_samples = int(tuning.get("min_samples", MIN_SAMPLES))

    # Strict pass first (unclassified models excluded).
    result = _select_pass(reqs, candidates, perf, slow_factor, min_samples, allow_unknown=False)
    if result is not None:
        return result
    # Nothing found: retry admitting unclassified models.
    return _select_pass(reqs, candidates, perf, slow_factor, min_samples, allow_unknown=True)


def safety_floor(entries):
    """The rule-based last resort (never a hardcoded model ID — see
    model_registry.py's module docstring for why): the first entry, in
    `entries` order, that's already been established as configured
    (`_configured: True`, set by the impure caller), preferring a no-key
    local provider. `entries` mirrors config["llm_entries"]' shape. Returns
    the chosen entry dict, or None if nothing at all is configured."""
    # Only entries the caller has confirmed are configured are eligible.
    configured = [e for e in (entries or []) if e.get("_configured")]
    # Nothing configured at all: there is no fallback.
    if not configured:
        return None
    # Prefer a local no-key provider; otherwise take the first configured entry.
    local = [e for e in configured if registry.is_nokey_provider(e.get("provider"))]
    return (local or configured)[0]


def _exclusion_reason(c, reqs, allow_unknown):
    """The FIRST gate a candidate fails, in the same order _select_pass
    applies them, as human-readable text — or None if the candidate passes
    every filter (i.e. it reached tier ranking). Pure; used by
    explain_selection to make the dry-run picker legible."""
    # Same checks, in the same order, as _select_pass, each returning a readable reason.
    if not c.get("available", True):
        return "unavailable: " + str(c.get("unavailable_reason") or "cooldown")
    if c.get("key") in (reqs.exclude_models or ()):
        return "excluded this run (already tried/burned)"
    if reqs.pin_key and _canon_key(c.get("key")) != _canon_key(reqs.pin_key):
        return "not the pinned endpoint (chat_pin)"
    if not _passes_restriction(c, reqs.restrict):
        return "restrict=" + str(reqs.restrict)
    # The capability checks mirror _passes_capability, but say which check failed.
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
    # Passed every filter.
    return None


def explain_selection(reqs, candidates, perf=None, tuning=None):
    """Dry-run picker: run select_model over `candidates`, then classify EVERY
    candidate as selected / alternative / excluded with a reason, so an
    operator (Diagnostics → LLM picker) can audit the routing for one
    requirement set without spending a token. Pure — no network, no config
    reads. Returns:

        {"selected": {key, provider, model, tier, reason}|None,
         "permissive": bool,        # True if the winner came from the unclassified-admitting pass
         "rows": [{key, provider, model, tier, speed, capability_rank,
                    status, reason, n, tps, latency_ms}, ...]}

    Rows are ordered selected → alternatives (rank order) → excluded."""
    # Treat missing perf data as empty.
    perf = perf or {}
    # Run the real picker so the explanation matches what would actually happen.
    selection = select_model(reqs, candidates, perf, tuning)
    # Whether the winner came from the permissive pass (its reason text says so).
    permissive = bool(selection and "permissive" in (selection.reason or ""))
    allow_unknown = permissive or selection is None  # None → both passes failed; explain permissively

    # The winner's key and the rank of each alternative, for classifying rows.
    winner_key = selection.key if selection else None
    alt_order = [a.get("key") for a in (selection.alternatives if selection else [])]
    alt_rank = {k: i for i, k in enumerate(alt_order)}

    # One row per candidate describing its status and why.
    rows = []
    # Build a row for every candidate.
    for c in candidates or []:
        # The candidate's key, capabilities and perf stats.
        key = c.get("key")
        caps = c.get("caps") or {}
        s = perf.get(key) or {}
        # Basic identity, tier, speed, capability and perf numbers for the row.
        row = {
            "key": key, "provider": c.get("provider"), "model": c.get("model"),
            "tier": caps.get("cost_tier"),
            "speed": registry.speed_tier(caps),
            "capability_rank": registry.capability_rank(caps),
            "n": s.get("n", 0) or 0, "tps": s.get("tps"), "latency_ms": s.get("latency_ms"),
        }
        # The winning candidate.
        if key == winner_key:
            row["status"] = "selected"
            row["reason"] = selection.reason
        # A fallback candidate; explain whether it was pushed down for being local.
        elif key in alt_rank:
            row["status"] = "alternative"
            if reqs.deprioritize_local and registry.is_nokey_provider(c.get("provider")):
                row["reason"] = ("local/GPU deprioritized for offloadable work — "
                                 "fallback if no free cloud (tier=%s)" % (caps.get("cost_tier") or "?"))
            else:
                row["reason"] = "capable fallback (tier=%s)" % (caps.get("cost_tier") or "?")
        # Neither winner nor fallback: name the first filter it failed.
        else:
            row["status"] = "excluded"
            row["reason"] = (_exclusion_reason(c, reqs, allow_unknown)
                             or "ranked below the selection in its tier")
        rows.append(row)

    # Sort rows: selected first, then alternatives in rank order, then excluded.
    _order = {"selected": 0, "alternative": 1, "excluded": 2}
    rows.sort(key=lambda r: (_order[r["status"]],
                             alt_rank.get(r["key"], 0) if r["status"] == "alternative" else 0))

    # Summarise the winner (if any) for the response.
    selected = None
    if selection:
        selected = {"key": selection.key, "provider": selection.provider,
                    "model": selection.model, "tier": selection.tier,
                    "reason": selection.reason}
    # Return the full explanation.
    return {"selected": selected, "permissive": permissive, "rows": rows}
