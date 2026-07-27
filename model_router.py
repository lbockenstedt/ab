"""
model_router.py — pick the RIGHT-SIZED model per task, so BugFixer doesn't burn
Opus on a log summary.

Maps a ``task_kind`` → a tier (small / medium / large) → a concrete model per cloud
family (Anthropic / Google Gemini). LOCAL providers (ollama / lmstudio / claude_cli)
are left on their configured model — the router only re-targets the CLOUD model
choice, which is where cost + capability actually differ.

Everything is config-overridable so exact model IDs / task tiers can be tuned
without a code change:
  config["router_enabled"]      bool (default True)
  config["router_task_tiers"]   {task_kind: "small|medium|large"}  (merged over defaults)
  config["router_models"]       {"anthropic": {tier: model}, "google": {tier: model}}

Used by llm_client.call_llm(..., task_kind=...): for a cloud slot it swaps the
slot's model for the task-tier model; a local slot keeps its configured model.
"""

# ── task_kind → tier ─────────────────────────────────────────────────────────
# Cheap/fast tier for classification + summarization; medium for judgement;
# large only for the hard, low-confidence code work.
_DEFAULT_TASK_TIERS = {
    "triage":         "small",   # is-this-actionable / classify an issue
    "log_review":     "small",   # per-module log error analysis
    "log_analysis":   "small",   # service self-log analysis
    "pr_summary":     "small",   # "what changed" PR change summary
    "identify_files": "small",   # which files to touch
    "classify":       "small",
    "pr_confidence":  "medium",  # PR safety/coherence assessment
    "review":         "medium",  # skeptical fix reviewer panel
    "chat":           "medium",  # interactive chat
    "fix":            "large",   # generate the actual code fix
    "default":        "medium",
}

# ── tier → model, per cloud family ───────────────────────────────────────────
# Sensible defaults for the current Claude 5 / Gemini 2.5 era. EXACT IDs depend on
# your API access — override via config["router_models"]. Kept conservative:
# small = cheapest capable, medium = balanced, large = top reasoning.
_DEFAULT_MODELS = {
    "anthropic": {
        "small":  "claude-haiku-4-5-20251001",   # Haiku 4.5 — cheap/fast
        "medium": "claude-sonnet-5",              # Sonnet 5 — balanced
        "large":  "claude-opus-4-8",              # Opus 4.8 — top reasoning
    },
    "google": {
        "small":  "gemini-2.5-flash-lite",
        "medium": "gemini-2.5-flash",
        "large":  "gemini-2.5-pro",
    },
}


def _family(provider):
    p = (provider or "").lower().strip()
    if p == "anthropic":
        return "anthropic"
    if p in ("google", "gemini"):
        return "google"
    return None  # local / other → not routed


def task_tier(task_kind, config=None):
    tiers = dict(_DEFAULT_TASK_TIERS)
    if config:
        try:
            tiers.update(config.get("router_task_tiers") or {})
        except Exception:
            pass
    return tiers.get(task_kind or "default", tiers["default"])


def pick_model(task_kind, provider, config=None, default=None):
    """Model to use for ``task_kind`` on ``provider``.

    Cloud families (anthropic / google) → the tier-appropriate model. Anything
    else (local) → ``default`` (its configured model). Router disabled → default.
    """
    config = config or {}
    try:
        if not config.get("router_enabled", True):
            return default
        fam = _family(provider)
        if not fam:
            return default
        tier = task_tier(task_kind, config)
        catalog = dict(_DEFAULT_MODELS[fam])
        catalog.update((config.get("router_models") or {}).get(fam) or {})
        return catalog.get(tier) or default
    except Exception:
        return default


def recommendations(config=None):
    """Task → tier → (anthropic model, gemini model) table — for docs/UI/tooltips."""
    out = []
    for task in sorted(_DEFAULT_TASK_TIERS, key=lambda t: (t == "default", t)):
        if task == "default":
            continue
        tier = task_tier(task, config)
        out.append({
            "task": task,
            "tier": tier,
            "anthropic": pick_model(task, "anthropic", config),
            "google": pick_model(task, "google", config),
        })
    return out
