"""
model_registry.py — capability + cost-tier registry for BugFixer's LLM
model-selection picker (model_selection.py). Pure, standalone module (no
`main` import) so it's directly unit-testable, matching feature_boundary.py's
pattern.

A "rule" describes one family of models (e.g. "anthropic claude-sonnet-*")
with its cost tier and capabilities. `resolve(provider, model, config)` finds
the MOST SPECIFIC matching rule for a real (provider, model) pair and returns
its capabilities as a plain dict. Curated rules live in config["model_registry"]
(operator-editable in Settings, seeded from DEFAULT_MODEL_RULES on first run —
see routes.py's settings_page). Auto-discovered models that match no curated
rule are written to a SEPARATE key, config["model_registry_auto"], never into
the operator's hand-edited list — a background scan appending to the same
list the operator has open in a Settings textarea is a lost-update race; two
keys make it impossible.

`match` is always an fnmatch glob, NEVER a dated model ID. This is the direct
lesson from the model this module replaces (model_router.py's `_DEFAULT_MODELS
["google"] = {}`, deliberately left empty after a pinned Gemini model ID went
stale and 404'd every routed call account-wide) — a specific model release
must never be required for the picker to keep working.
"""
import fnmatch
import re

# --- provider family helpers -----------------------------------------------
# Deliberately duplicated from llm_client.py rather than imported (llm_client
# imports `main`, which fully boots the live app as an import side effect in
# this checkout — see test_skills_loader.py's docstring for the discovery).
# check_tooltips.py made the same call for the same reason. Keep these in
# lockstep BY HAND with _is_ollama/_is_ollama_cloud/_is_lmstudio/
# _provider_is_nokey in llm_client.py if those ever change.

OLLAMA_CLOUD_PROVIDER = "ollama_cloud"


def _is_ollama(provider):
    return (provider or "").lower().strip().startswith("ollama")


def _is_ollama_cloud(provider):
    return (provider or "").lower().strip() == OLLAMA_CLOUD_PROVIDER


def _is_lmstudio(provider):
    return (provider or "").lower().strip().startswith("lmstudio")


def is_nokey_provider(provider):
    """Mirrors llm_client._provider_is_nokey exactly: true for a provider that
    authenticates without an API key (claude_cli session auth, LM Studio,
    self-hosted Ollama — NOT Ollama Cloud, which does take a key)."""
    p = (provider or "").lower().strip()
    return p == "claude_cli" or _is_lmstudio(p) or (_is_ollama(p) and not _is_ollama_cloud(p))


COMPLEXITY_RANK = {"trivial": 0, "small": 1, "medium": 2, "large": 3}
COST_TIER_RANK = {"free": 0, "cheap": 1, "frontier": 2, "unknown": 3}

_CAP_FIELDS = (
    "cost_tier", "max_complexity", "context_window", "supports_tools",
    "native_agentic_tools", "supports_mutating_agent", "supports_structured_output",
    "supports_batch", "supports_streaming",
)

# A model that matches no curated rule at all — sorts LAST on cost (after
# frontier) so it's genuinely last-resort, but starts at max_complexity=
# "trivial" so it also fails the capability filter for anything bigger unless
# the caller falls back to the permissive pass (see model_selection.py).
UNKNOWN_CAPS = {
    "cost_tier": "unknown", "max_complexity": "trivial", "context_window": 8192,
    "supports_tools": False, "native_agentic_tools": False,
    "supports_mutating_agent": False, "supports_structured_output": False,
    "supports_batch": False, "supports_streaming": True, "_source": "unknown",
}

# Seeded via config.setdefault("model_registry", DEFAULT_MODEL_RULES) in
# routes.py's settings_page — after first run, config["model_registry"] IS
# the effective list (starting as a copy of this, then operator-edited), so
# resolve() reads config, never this constant directly, except as the
# fallback when the key is genuinely absent (e.g. a test harness).
DEFAULT_MODEL_RULES = [
    {"id": "ollama-local", "provider": "ollama", "match": "*", "label": "Self-hosted Ollama",
     "cost_tier": "free", "max_complexity": "medium", "context_window": 32768,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "max_complexity is refined per-model by parameter count in resolve()"},
    {"id": "ollama2-local", "provider": "ollama2", "match": "*", "label": "Self-hosted Ollama (2nd endpoint / GPU)",
     "cost_tier": "free", "max_complexity": "medium", "context_window": 32768,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "second local Ollama endpoint (e.g. a GPU box); free, mirrors ollama-local"},
    {"id": "lmstudio-local", "provider": "lmstudio", "match": "*", "label": "Self-hosted LM Studio",
     "cost_tier": "free", "max_complexity": "medium", "context_window": 32768,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "claude-cli", "provider": "claude_cli", "match": "*", "label": "Claude CLI (session auth)",
     "cost_tier": "free", "max_complexity": "large", "context_window": 200000,
     "supports_tools": False, "native_agentic_tools": True, "supports_mutating_agent": True,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": False,
     "enabled": True,
     "notes": "session-auth, no per-call billing; the only provider with native_agentic_tools/supports_mutating_agent"},
    {"id": "ollama-cloud", "provider": "ollama_cloud", "match": "*", "label": "Ollama Cloud",
     "cost_tier": "cheap", "max_complexity": "large", "context_window": 32768,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "the one ollama* provider that takes a key"},
    {"id": "openrouter-free", "provider": "openrouter", "match": "*:free", "label": "OpenRouter free models",
     "cost_tier": "free", "max_complexity": "medium", "context_window": 32768,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "openrouter-paid", "provider": "openrouter", "match": "*", "label": "OpenRouter (paid)",
     "cost_tier": "cheap", "max_complexity": "large", "context_window": 32768,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "less specific than openrouter-free's *:free pattern"},
    {"id": "groq", "provider": "groq", "match": "*", "label": "Groq",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 32768,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "copilot", "provider": "copilot", "match": "*", "label": "GitHub Copilot",
     "cost_tier": "cheap", "max_complexity": "large", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "anthropic-haiku", "provider": "anthropic", "match": "*haiku*", "label": "Claude Haiku class",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 200000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "anthropic-sonnet", "provider": "anthropic", "match": "*sonnet*", "label": "Claude Sonnet class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 200000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "anthropic-opus", "provider": "anthropic", "match": "*opus*", "label": "Claude Opus class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 200000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "google-flash", "provider": "google", "match": "*flash*", "label": "Gemini Flash class",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 1000000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": False,
     "enabled": True, "notes": "supports_streaming=False -- _request_google hardcodes stream=False"},
    {"id": "google-pro", "provider": "google", "match": "*pro*", "label": "Gemini Pro class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 2000000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": False,
     "enabled": True, "notes": "supports_streaming=False -- _request_google hardcodes stream=False"},
    {"id": "openai-mini", "provider": "openai", "match": "*mini*", "label": "OpenAI mini class",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 128000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "openai-gpt", "provider": "openai", "match": "gpt-*", "label": "OpenAI GPT class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 128000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "enabled": True, "notes": ""},
]


# Providers whose DEFAULT rule is a self-hosted / no-per-call-cost "free" tier.
# A missing default here (the `ollama2` GPU-endpoint gap that classified as
# `unknown` instead of `free`) can't be back-filled by DEFAULT_MODEL_RULES
# alone: once an operator saves Settings, config["model_registry"] is frozen
# WITHOUT any rule added to DEFAULT_MODEL_RULES afterwards. upgrade_local_free_rules
# tops those frozen configs up idempotently — see routes.py settings_page / save
# and llm_migrate.migrate().


def upgrade_local_free_rules(rules):
    """Return (new_rules, added_ids): a COPY of the operator's curated `rules`
    list with any missing local/free DEFAULT_MODEL_RULES appended.

    Conservative by design — it ONLY appends, and only a default whose:
      * provider is a no-key self-hosted/free provider (ollama*, lmstudio,
        claude_cli — see is_nokey_provider), AND
      * cost_tier is "free", AND
      * `id` is absent from `rules`, AND
      * `provider` is ENTIRELY absent from `rules` (so we never second-guess an
        operator who already has their own rule for that provider).

    Existing rules are never modified, removed, or reordered. Idempotent: a
    second call adds nothing (the provider is now present). This is what makes
    an operator's GPU `ollama2` endpoint gain its `free` rule on upgrade
    instead of being stuck at cost tier `unknown`.
    """
    rules = list(rules or [])
    existing_ids = {r.get("id") for r in rules if isinstance(r, dict)}
    existing_providers = {
        (r.get("provider") or "").lower().strip()
        for r in rules if isinstance(r, dict)
    }
    added_ids = []
    for default in DEFAULT_MODEL_RULES:
        provider = (default.get("provider") or "").lower().strip()
        if not is_nokey_provider(provider):
            continue
        if default.get("cost_tier") != "free":
            continue
        if default.get("id") in existing_ids:
            continue
        if provider in existing_providers:
            continue
        rules.append(dict(default))
        added_ids.append(default.get("id"))
        existing_ids.add(default.get("id"))
        existing_providers.add(provider)
    return rules, added_ids


def _model_param_b(name):
    """Parse a model's parameter count in billions from its name, e.g.
    'qwen2.5-coder:14b' -> 14.0. None if not determinable. Verbatim copy of
    fix_engine._model_param_b — moved here since capability derivation is
    this module's job now, kept in fix_engine.py too until Phase 6 deletes
    the ensemble code that also uses it."""
    m = re.search(r'(\d+(?:\.\d+)?)b\b', str(name).lower())
    return float(m.group(1)) if m else None


def _complexity_from_param_b(param_b):
    """<8B -> trivial, 8-20B -> small, >20B -> medium. NEVER 'large' for a
    local model from size alone -- an operator who wants that must add an
    explicit, more-specific override rule."""
    if param_b is None:
        return "small"  # unparseable size -- assume modest capability, not zero
    if param_b < 8:
        return "trivial"
    if param_b <= 20:
        return "small"
    return "medium"


def _specificity(pattern):
    """More literal (non-wildcard) characters = more specific. '*' has
    specificity 0; 'claude-sonnet-*' beats '*'; 'claude-sonnet-5-20260101'
    (no wildcard at all) beats both. Ties broken by rule list order (the
    caller preserves list order, so an operator's own rules -- typically
    appended after the seeded defaults -- naturally win ties against a
    same-specificity built-in)."""
    return len(re.sub(r'[*?\[\]]', '', pattern or ''))


def _effective_rules(config):
    rules = (config or {}).get("model_registry")
    if not rules:
        rules = DEFAULT_MODEL_RULES
    return rules


def find_matching_rules(provider, model, config):
    """All ENABLED rules whose provider matches exactly and whose `match`
    glob matches the model name (case-insensitive), from config["model_registry"]
    (falling back to DEFAULT_MODEL_RULES if that key is absent)."""
    provider = (provider or "").lower().strip()
    model_l = (model or "").lower().strip()
    out = []
    for rule in _effective_rules(config):
        if not rule.get("enabled", True):
            continue
        if (rule.get("provider") or "").lower().strip() != provider:
            continue
        if fnmatch.fnmatch(model_l, (rule.get("match") or "*").lower()):
            out.append(rule)
    return out


def resolve(provider, model, config):
    """The capability dict for one real (provider, model) pair: the MOST
    SPECIFIC matching enabled rule's capability fields, or UNKNOWN_CAPS if
    nothing matches. Self-hosted ollama gets one extra refinement: unless a
    MORE SPECIFIC operator rule than the generic 'ollama-local' default
    matched, max_complexity is derived from the model's parameter count
    instead of the flat default -- a 7b and a 70b local model should never
    share one capability ceiling."""
    matches = find_matching_rules(provider, model, config)
    if not matches:
        return dict(UNKNOWN_CAPS)

    best = max(matches, key=lambda r: _specificity(r.get("match") or ""))
    caps = {k: best.get(k) for k in _CAP_FIELDS}
    caps["_source"] = "curated"
    caps["_matched_rule_id"] = best.get("id")

    if _is_ollama(provider) and not _is_ollama_cloud(provider) and best.get("id") == "ollama-local":
        caps["max_complexity"] = _complexity_from_param_b(_model_param_b(model))

    return caps


def registry_sync(seen, config):
    """Given `seen` (an iterable of (provider, model) pairs observed live,
    e.g. from a catalog fetch), return the list of NEW auto-discovery stub
    entries for any pair matching no curated rule and not already present in
    config["model_registry_auto"]. Pure -- the caller persists the result;
    this never mutates config. Deliberately a SEPARATE list from the curated
    config["model_registry"] (see module docstring) so a background sync can
    never race an operator's open Settings edit."""
    existing_auto = {(e.get("provider"), e.get("model")) for e in (config.get("model_registry_auto") or [])}
    new_stubs = []
    added_this_call = set()
    for provider, model in seen:
        key = ((provider or "").lower().strip(), (model or "").strip())
        if key in existing_auto or key in added_this_call:
            continue
        if find_matching_rules(provider, model, config):
            continue  # a curated rule already covers it
        added_this_call.add(key)
        new_stubs.append({"provider": key[0], "model": key[1], **UNKNOWN_CAPS})
    return new_stubs


def local_models_for_preload(installed, config):
    """Given `installed` (a list of {"name": str, "size": bytes-or-None}
    dicts for models actually present on a self-hosted ollama endpoint --
    the caller supplies this, e.g. from the /api/tags-derived list
    fix_engine._ollama_models_detailed already builds), return the names
    worth preloading: registry-resolvable, enabled, and NOT max_complexity=
    'trivial' (a tiny model isn't worth keeping resident), sorted smallest
    first. Replaces _filter_ensemble_models' role for the preload gate
    (workers.preload_ollama_models) -- the ensemble itself retires in Phase 6."""
    scored = []
    for m in installed or []:
        name = m.get("name")
        if not name:
            continue
        caps = resolve("ollama", name, config)
        if caps.get("max_complexity") == "trivial":
            continue
        scored.append((m.get("size") or 0, name))
    scored.sort(key=lambda t: t[0])
    return [name for _, name in scored]
