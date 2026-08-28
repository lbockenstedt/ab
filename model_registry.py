"""
model_registry.py — capability + cost-tier registry for AppBuilder's LLM
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
    "supports_batch", "supports_streaming", "capability_rank",
)

#: Fallback `capability_rank` derived from max_complexity, for any rule that
#: predates the field (operator-authored rules, auto-discovery stubs). Keeps an
#: un-ranked registry behaving exactly as it did before the field existed.
_CAPABILITY_RANK_BY_COMPLEXITY = {"trivial": 10, "small": 30, "medium": 50, "large": 70}

#: Neither cost_tier nor max_complexity can express "Opus is stronger than
#: Sonnet": both are `frontier`/`large`, and eight default rules collapse onto
#: that same pair. Tier decides WHICH BUDGET to spend and max_complexity is a
#: HARD filter (raise it and a model is excluded from work it could do), so
#: neither can carry a within-tier ordering. `capability_rank` is that missing
#: axis: 0-100, higher = smarter, compared ONLY between models already in the
#: same tier. It is a pure ranking hint — it never filters, so a wrong value
#: costs ordering, never availability.
#:
#: Deliberately NOT the premium-request multiplier: that would rank GPT-5.5
#: (57x) above Opus (27x), which is a price signal, not a capability one.
def capability_rank(caps):
    """The 0-100 within-tier capability score for a resolved capability dict.

    Falls back to a max_complexity-derived default when the rule carries no
    explicit rank, so a registry written before this field existed keeps its
    previous relative ordering instead of collapsing to zero."""
    raw = (caps or {}).get("capability_rank")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return _CAPABILITY_RANK_BY_COMPLEXITY.get((caps or {}).get("max_complexity"), 0)
    return max(0, min(100, int(raw)))

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
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 200000,
     "supports_tools": False, "native_agentic_tools": True, "supports_mutating_agent": True,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": False,
     "capability_rank": 70, "enabled": True,
     "notes": "session auth (no API key), but every call bills the operator's Anthropic "
              "account — cost_tier=frontier so the picker RESERVES it (free/GPU wins first, "
              "claude_cli is used only when a call needs its native_agentic_tools/"
              "supports_mutating_agent, e.g. feature_build)"},
    # Per-model capability tiers for the claude_cli roster — same agentic
    # runtime (native tools + mutating agent), same 200k context, all frontier
    # (they bill the operator's account), categorized as a strength LADDER so
    # each model is distinct and the hardest work escalates to Opus: Haiku=small
    # (light/cheap), Sonnet=medium (balanced), Opus=large (hardest — the default
    # for feature_build and any large agentic/mutating task). More-specific
    # match beats the generic `*` above.
    {"id": "claude-cli-haiku", "provider": "claude_cli", "match": "*haiku*", "label": "Claude Haiku (via CLI)",
     "cost_tier": "frontier", "max_complexity": "medium", "context_window": 200000,
     "supports_tools": False, "native_agentic_tools": True, "supports_mutating_agent": True,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": False,
     "capability_rank": 55, "enabled": True,
     "notes": "fastest Claude; medium mirrors anthropic-haiku. Preference for Sonnet/Opus "
              "is carried by capability_rank (55 < 78 < 95) rather than a max_complexity "
              "cap, which would hard-exclude it rather than rank it last"},
    {"id": "claude-cli-sonnet", "provider": "claude_cli", "match": "*sonnet*", "label": "Claude Sonnet (via CLI)",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 200000,
     "supports_tools": False, "native_agentic_tools": True, "supports_mutating_agent": True,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": False,
     "capability_rank": 78, "enabled": True,
     "notes": "balanced Claude; large mirrors anthropic-sonnet. Escalation to Opus is "
              "expressed by capability_rank (95 > 78), NOT by capping max_complexity — "
              "that is a hard filter, so the cap EXCLUDED Sonnet from large work instead "
              "of ranking it second, and a claude_cli-only install had nothing left to "
              "escalate TO"},
    {"id": "claude-cli-opus", "provider": "claude_cli", "match": "*opus*", "label": "Claude Opus (via CLI)",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 200000,
     "supports_tools": False, "native_agentic_tools": True, "supports_mutating_agent": True,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": False,
     "capability_rank": 95, "enabled": True,
     "notes": "most capable Claude — the ONLY claude_cli model at large complexity, so it is "
              "the default for feature_build and the hardest large agentic/mutating work"},
    {"id": "ollama-cloud", "provider": "ollama_cloud", "match": "*", "label": "Ollama Cloud",
     "cost_tier": "cheap", "max_complexity": "large", "context_window": 131072,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True,
     "notes": "the one ollama* provider that takes a key. supports_tools=True: "
              "_request_ollama sets payload['tools'] and parses tool_calls for cloud "
              "exactly as for local (identical wire protocol), and every model in the "
              "current cloud library is tool-native. context_window is AUTHORITATIVE "
              "for cloud: _request_ollama derives options.num_ctx from this value, so "
              "the hard selection filter in model_selection and the window actually "
              "requested on the wire cannot desync. 131072 is the common floor across "
              "the current cloud library; per-model rules below raise it where the "
              "model genuinely supports more. Override with ollama_cloud_num_ctx."},
    {"id": "openrouter-free", "provider": "openrouter", "match": "*:free", "label": "OpenRouter free models",
     "cost_tier": "free", "max_complexity": "medium", "context_window": 32768,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "openrouter-free-router", "provider": "openrouter", "match": "openrouter/free",
     "label": "OpenRouter Free Models Router",
     "cost_tier": "free", "max_complexity": "medium", "context_window": 32768,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True,
     "notes": "the 'openrouter/free' Free Models Router auto-routes each call to whatever "
              ":free model is currently available/capable, transparently absorbing per-model "
              "rate-limits + outages. Its id has no ':free' suffix so it would otherwise fall "
              "through to openrouter-paid (cheap); this literal match (more specific than *:free "
              "and *) keeps it in the free tier so offloadable work prefers it over the GPU."},
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
     "enabled": True,
     "notes": "generic fallback for Copilot models with no class rule below. Copilot is NOT "
              "free — every call spends a premium request — so this stays 'cheap', never 'free'."},
    # Per-model capability tiers for the GitHub Copilot roster. Copilot bills a
    # premium request per call and the multiplier is PER MODEL, spanning ~80x:
    # Opus 27x and GPT-5.5 57x at the top, GPT-4o / mini / Haiku 0.33x at the
    # bottom. The single `*` rule above therefore priced Opus-via-Copilot the
    # same as GPT-4o-via-Copilot, which broke the picker in BOTH directions:
    # `cheap` made it reach for Opus on trivial work (burning the priciest
    # request there is), while prefer_capable ranks frontier ABOVE cheap, so the
    # strongest model was skipped for exactly the hard jobs it exists for. These
    # mirror the anthropic/claude_cli ladders so one model is tiered the same way
    # whichever provider serves it. More-specific match beats the generic `*`.
    {"id": "copilot-opus", "provider": "copilot", "match": "*opus*", "label": "Claude Opus (via Copilot)",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "capability_rank": 95, "enabled": True,
     "notes": "27x premium-request multiplier — the most expensive model Copilot serves. "
              "frontier/large matches anthropic-opus and claude-cli-opus so the picker "
              "RESERVES it for the hardest work instead of spending it on triage."},
    {"id": "copilot-sonnet", "provider": "copilot", "match": "*sonnet*", "label": "Claude Sonnet (via Copilot)",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "capability_rank": 78, "enabled": True, "notes": "9x multiplier; frontier/large mirrors anthropic-sonnet"},
    {"id": "copilot-haiku", "provider": "copilot", "match": "*haiku*", "label": "Claude Haiku (via Copilot)",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "capability_rank": 55, "enabled": True, "notes": "0.33x multiplier; cheap/medium mirrors anthropic-haiku"},
    {"id": "copilot-gemini-pro", "provider": "copilot", "match": "*gemini*pro*", "label": "Gemini Pro (via Copilot)",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "capability_rank": 80, "enabled": True, "notes": "6x multiplier; frontier/large mirrors google-pro"},
    # `gpt-5*mini*` is deliberately MORE specific than `gpt-5*` so gpt-5-mini
    # (0.33x) stays cheap instead of inheriting the gpt-5 frontier tier.
    {"id": "copilot-gpt5-mini", "provider": "copilot", "match": "gpt-5*mini*", "label": "GPT-5 mini (via Copilot)",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "capability_rank": 50, "enabled": True, "notes": "0.33x multiplier — the cheapest GPT-5 variant"},
    {"id": "copilot-gpt5", "provider": "copilot", "match": "gpt-5*", "label": "GPT-5 class (via Copilot)",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "capability_rank": 85, "enabled": True, "notes": "6x base / 57x for GPT-5.5 — priced as frontier so it is reserved"},
    {"id": "copilot-gpt4", "provider": "copilot", "match": "gpt-4*", "label": "GPT-4 class (via Copilot)",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 64000,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "capability_rank": 45, "enabled": True, "notes": "0.33x multiplier (gpt-4o / gpt-4o-mini)"},
    {"id": "anthropic-haiku", "provider": "anthropic", "match": "*haiku*", "label": "Claude Haiku class",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 200000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "capability_rank": 55, "enabled": True, "notes": ""},
    {"id": "anthropic-sonnet", "provider": "anthropic", "match": "*sonnet*", "label": "Claude Sonnet class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 200000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "capability_rank": 78, "enabled": True, "notes": ""},
    {"id": "anthropic-opus", "provider": "anthropic", "match": "*opus*", "label": "Claude Opus class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 200000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "capability_rank": 95, "enabled": True, "notes": ""},
    {"id": "google-flash", "provider": "google", "match": "*flash*", "label": "Gemini Flash class",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 1000000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": False,
     "enabled": True, "notes": "supports_streaming=False -- _request_google hardcodes stream=False"},
    {"id": "google-pro", "provider": "google", "match": "*pro*", "label": "Gemini Pro class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 2000000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": False,
     "capability_rank": 80, "enabled": True, "notes": "supports_streaming=False -- _request_google hardcodes stream=False"},
    {"id": "openai-mini", "provider": "openai", "match": "*mini*", "label": "OpenAI mini class",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 128000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "enabled": True, "notes": ""},
    {"id": "openai-gpt", "provider": "openai", "match": "gpt-*", "label": "OpenAI GPT class",
     "cost_tier": "frontier", "max_complexity": "large", "context_window": 128000,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": True, "supports_streaming": True,
     "capability_rank": 85, "enabled": True, "notes": ""},

    # --- Capable self-hosted models -------------------------------------
    # The generic `ollama`/`ollama2` defaults are deliberately conservative
    # (tools=False, max_complexity refined to at most `medium`, 32k context)
    # because capability can't be inferred from a model name alone. These
    # families, however, are known-capable: they support Ollama tool-calling
    # and long context, so a self-hoster's GPU can serve tool/large/long-
    # context work for free instead of falling through to a paid tier. The
    # large `context_window` also matters — the generic 32k default fails the
    # picker's `context_window >= min_context_tokens * 1.25` gate for the ~81k
    # planner/agent calls, which is why those "resolved nothing" and fell to a
    # paid model. Duplicated per local provider because provider match is exact.
    # (supports_mutating_agent stays False — Ollama has NO agentic file-edit
    # runtime; only claude_cli does. Flagging it would pick a model for
    # feature_build that then changes zero files.)
    {"id": "ollama-qwen3-coder", "provider": "ollama", "match": "qwen3-coder*", "label": "Qwen3-Coder (self-hosted)",
     "cost_tier": "free", "max_complexity": "large", "context_window": 262144,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "strong local coder with native tool-calling + long context"},
    {"id": "ollama2-qwen3-coder", "provider": "ollama2", "match": "qwen3-coder*", "label": "Qwen3-Coder (GPU)",
     "cost_tier": "free", "max_complexity": "large", "context_window": 262144,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "strong local coder with native tool-calling + long context"},
    {"id": "ollama-llama33", "provider": "ollama", "match": "llama3.3*", "label": "Llama 3.3 (self-hosted)",
     "cost_tier": "free", "max_complexity": "large", "context_window": 131072,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "native tool-calling; capable general planner/coordinator"},
    {"id": "ollama2-llama33", "provider": "ollama2", "match": "llama3.3*", "label": "Llama 3.3 (GPU)",
     "cost_tier": "free", "max_complexity": "large", "context_window": 131072,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "native tool-calling; capable general planner/coordinator"},
    {"id": "ollama-gemma4", "provider": "ollama", "match": "gemma4*", "label": "Gemma 4 (self-hosted)",
     "cost_tier": "free", "max_complexity": "large", "context_window": 131072,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "capable reasoning + JSON; tools left off (Gemma native tool-calling is unreliable)"},
    {"id": "ollama2-gemma4", "provider": "ollama2", "match": "gemma4*", "label": "Gemma 4 (GPU)",
     "cost_tier": "free", "max_complexity": "large", "context_window": 131072,
     "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": True, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "capable reasoning + JSON; tools left off (Gemma native tool-calling is unreliable)"},
    # --- Ollama Cloud per-model ladder ------------------------------------
    # The generic ollama-cloud `*` rule gives EVERY cloud model max_complexity
    # "large". That is right for the frontier-class cloud models (nemotron-3-
    # ultra, the kimi-k2/k3 and deepseek-v4-pro class, mistral-large-3,
    # qwen3.5) but wrong for the small/fast end of the library, which the `*`
    # rule would otherwise let the picker choose for the hardest jobs. These
    # rules only DOWNGRADE that end; the flagship models keep the `*` default.
    # context_window here is authoritative: _request_ollama derives the cloud
    # options.num_ctx from the resolved capability, so these values are what is
    # actually requested on the wire -- see the ollama-cloud rule's notes.
    {"id": "ollama-cloud-nano", "provider": "ollama_cloud", "match": "*nano*",
     "label": "Ollama Cloud (nano class)",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 131072,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "smallest cloud tier (e.g. nemotron-3-nano:30b) -- fast/cheap, not a large-job model"},
    {"id": "ollama-cloud-flash", "provider": "ollama_cloud", "match": "*flash*",
     "label": "Ollama Cloud (flash class)",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 131072,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "latency-optimised cloud variants (e.g. deepseek-v4-flash, glm-5.3-flash)"},
    {"id": "ollama-cloud-20b", "provider": "ollama_cloud", "match": "*:20b*",
     "label": "Ollama Cloud (20B class)",
     "cost_tier": "cheap", "max_complexity": "medium", "context_window": 131072,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True, "notes": "e.g. gpt-oss:20b -- the small open-weight cloud tier"},
    {"id": "ollama-cloud-ultra", "provider": "ollama_cloud", "match": "*ultra*",
     "label": "Ollama Cloud (ultra class)",
     "cost_tier": "cheap", "max_complexity": "large", "context_window": 262144,
     "supports_tools": True, "native_agentic_tools": False, "supports_mutating_agent": False,
     "supports_structured_output": False, "supports_batch": False, "supports_streaming": True,
     "enabled": True,
     "notes": "nemotron-3-ultra: 550B total / 55B active MoE, explicitly built for "
              "long-running agents across hundreds of tool calls. Stated as an explicit "
              "rule (rather than inheriting `*`) so the flagship is not silently "
              "downgraded if the generic cloud default is ever retuned. Cheap+large is "
              "intentional: cost_tier tracks PRICE, not capability, so the cost-first "
              "picker reaches this frontier-grade model early -- which is the point. "
              "262144 is the window Ollama Cloud serves; the model card advertises up "
              "to 1M, so this is the deliberately conservative end of the reported "
              "range -- understating only forgoes capacity, overstating truncates."},
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


# The known-capable self-hosted model rules (see DEFAULT_MODEL_RULES). Unlike
# upgrade_local_free_rules, these are appended even when the provider already
# has rules — the generic `ollama*` `*` rule is almost always present, so a
# provider-absence gate would never let these through. They only add capability
# for SPECIFIC model families and are skipped if the operator already curated a
# rule for that same (provider, match), so an operator's own choices always win.
_CAPABLE_LOCAL_RULE_IDS = frozenset({
    "ollama-qwen3-coder", "ollama2-qwen3-coder",
    "ollama-llama33", "ollama2-llama33",
    "ollama-gemma4", "ollama2-gemma4",
})


def upgrade_capable_local_rules(rules):
    """Return (new_rules, added_ids): a COPY of `rules` with any missing
    known-capable self-hosted model rules (_CAPABLE_LOCAL_RULE_IDS) appended.

    These promote specific local model families (qwen3-coder, llama3.3, gemma4)
    to their real capabilities — tool-calling, long context, large complexity —
    so a self-hoster's GPU is used for capable work for free instead of falling
    through to a paid tier. Unlike upgrade_local_free_rules this does NOT gate on
    provider-absence (the generic `*` rule is almost always present); instead it
    skips a default whose:
      * `id` is already in `rules`, OR
      * (provider, match) pair is already curated by the operator — so an
        operator who wrote their own rule for that exact model keeps it.

    Append-only, idempotent, never reorders or modifies existing rules. Because
    the appended rules carry a more-specific `match` than the generic `*`, they
    win by specificity for those model families (see resolve()/_specificity)."""
    rules = list(rules or [])
    existing_ids = {r.get("id") for r in rules if isinstance(r, dict)}
    existing_pairs = {
        ((r.get("provider") or "").lower().strip(), (r.get("match") or "").lower().strip())
        for r in rules if isinstance(r, dict)
    }
    added_ids = []
    for default in DEFAULT_MODEL_RULES:
        if default.get("id") not in _CAPABLE_LOCAL_RULE_IDS:
            continue
        if default.get("id") in existing_ids:
            continue
        pair = ((default.get("provider") or "").lower().strip(),
                (default.get("match") or "").lower().strip())
        if pair in existing_pairs:
            continue
        rules.append(dict(default))
        added_ids.append(default.get("id"))
        existing_ids.add(default.get("id"))
        existing_pairs.add(pair)
    return rules, added_ids


def reclassify_claude_cli_paid(rules):
    """Return (new_rules, changed): a COPY of `rules` with any claude_cli rule
    still marked cost_tier="free" bumped to "frontier".

    claude_cli authenticates via a local session (no API key), so an earlier
    registry classified it "free" alongside the self-hosted GPU — but every
    claude_cli call actually bills the operator's Anthropic account. Marking it
    frontier makes the cost-first picker RESERVE it: free/GPU wins first, and
    claude_cli is reached only when a call needs its native_agentic_tools /
    supports_mutating_agent (e.g. feature_build) or nothing cheaper qualifies.

    Only touches rules whose provider is claude_cli AND whose cost_tier is
    exactly "free" — an operator who deliberately set another tier is left
    alone. Idempotent (a second run finds nothing to change); never reorders,
    adds, or removes rules."""
    rules = list(rules or [])
    changed = False
    out = []
    for r in rules:
        if (isinstance(r, dict)
                and (r.get("provider") or "").lower().strip() == "claude_cli"
                and (r.get("cost_tier") or "").lower().strip() == "free"):
            r = {**r, "cost_tier": "frontier"}
            changed = True
        out.append(r)
    return out, changed


# Per-model claude_cli capability rules (see DEFAULT_MODEL_RULES). Like the
# capable-local rules, these are appended even when the generic claude_cli `*`
# rule is present (it almost always is), because they only refine SPECIFIC
# model families and win by match specificity. Skipped if the operator already
# curated a rule for that same (provider, match).
_CLAUDE_CLI_MODEL_RULE_IDS = frozenset({
    "claude-cli-haiku", "claude-cli-sonnet", "claude-cli-opus",
})


def _append_missing_default_rules(rules, wanted_ids):
    """Return (new_rules, added_ids): a COPY of `rules` with any DEFAULT rule
    whose id is in `wanted_ids` appended when it is missing.

    Shared by the per-model upgrade helpers (claude_cli, copilot). Append-only
    and skipped when the rule id already exists OR the operator already curated
    a rule for that exact (provider, match), so a hand-tuned registry is never
    overridden. Never reorders or mutates existing rules; the more-specific
    match wins by specificity (see resolve())."""
    rules = list(rules or [])
    existing_ids = {r.get("id") for r in rules if isinstance(r, dict)}
    existing_pairs = {
        ((r.get("provider") or "").lower().strip(), (r.get("match") or "").lower().strip())
        for r in rules if isinstance(r, dict)
    }
    added_ids = []
    for default in DEFAULT_MODEL_RULES:
        if default.get("id") not in wanted_ids:
            continue
        if default.get("id") in existing_ids:
            continue
        pair = ((default.get("provider") or "").lower().strip(),
                (default.get("match") or "").lower().strip())
        if pair in existing_pairs:
            continue
        rules.append(dict(default))
        added_ids.append(default.get("id"))
        existing_ids.add(default.get("id"))
        existing_pairs.add(pair)
    return rules, added_ids


def upgrade_claude_cli_model_rules(rules):
    """Return (new_rules, added_ids): a COPY of `rules` with any missing
    per-model claude_cli rules (_CLAUDE_CLI_MODEL_RULE_IDS) appended.

    These categorize the claude_cli roster by model strength so Haiku (capped
    at medium) is never treated like Opus/Sonnet (large) — the generic `*`
    claude_cli rule alone gives every model the same large ceiling."""
    return _append_missing_default_rules(rules, _CLAUDE_CLI_MODEL_RULE_IDS)


#: Per-model copilot capability rules (see DEFAULT_MODEL_RULES). Appended to an
#: already-persisted registry for the same reason as the claude_cli set: without
#: them the generic copilot `*` rule prices EVERY Copilot model as "cheap",
#: including Claude Opus — whose Copilot premium-request multiplier (27x) makes
#: it the single most expensive model available.
_COPILOT_MODEL_RULE_IDS = frozenset({
    "copilot-opus", "copilot-sonnet", "copilot-haiku", "copilot-gemini-pro",
    "copilot-gpt5-mini", "copilot-gpt5", "copilot-gpt4",
})


def upgrade_copilot_model_rules(rules):
    """Return (new_rules, added_ids): a COPY of `rules` with any missing
    per-model copilot rules (_COPILOT_MODEL_RULE_IDS) appended.

    An operator who configured Copilot before this ladder existed has the old
    single `*` rule frozen into config["model_registry"], which (a) let the
    cost-first picker spend a 27x Opus request on trivial work and (b) ranked
    Opus BELOW frontier models under prefer_capable, skipping it for the hard
    jobs it exists for. Append-only and idempotent, exactly like the claude_cli
    upgrade — an operator's own copilot rule for the same match is left
    untouched."""
    return _append_missing_default_rules(rules, _COPILOT_MODEL_RULE_IDS)


#: Per-model ollama_cloud capability rules (see DEFAULT_MODEL_RULES). Appended
#: to an already-persisted registry so the small end of the cloud library
#: (nano / flash / 20b) stops inheriting the generic `*` rule's "large"
#: ceiling, and so the nemotron-3-ultra flagship is pinned explicitly.
_OLLAMA_CLOUD_MODEL_RULE_IDS = frozenset({
    "ollama-cloud-nano", "ollama-cloud-flash", "ollama-cloud-20b",
    "ollama-cloud-ultra",
})


def upgrade_ollama_cloud_model_rules(rules):
    """Return (new_rules, added_ids): a COPY of `rules` with any missing
    per-model ollama_cloud rules (_OLLAMA_CLOUD_MODEL_RULE_IDS) appended.

    Append-only and idempotent, exactly like the claude_cli and copilot
    upgrades; an operator's own rule for the same (provider, match) wins."""
    return _append_missing_default_rules(rules, _OLLAMA_CLOUD_MODEL_RULE_IDS)


def enable_ollama_cloud_tools(rules):
    """Return (new_rules, changed): a COPY of `rules` with the shipped
    ollama_cloud rule's supports_tools flipped False -> True.

    An append-only top-up cannot fix this: the stale flag lives ON the existing
    "ollama-cloud" rule, so a frozen registry keeps claiming Ollama Cloud has
    no tool support. That is factually wrong -- _request_ollama sets
    payload["tools"] and parses tool_calls for the cloud endpoint over the same
    wire protocol as the local one, and the current cloud library is tool-native
    (nemotron-3-ultra is marketed for "hundreds of tool calls"). Left uncorrected
    it makes model_selection filter Ollama Cloud out of every tool-requiring job.

    Scoped deliberately narrowly: only the rule whose id is exactly
    "ollama-cloud" (the one AppBuilder ships) AND whose supports_tools is still
    False. An operator who added their own ollama_cloud rule, or who
    deliberately turned tools off on some other rule, is left alone. Idempotent;
    never reorders, adds, or removes rules."""
    rules = list(rules or [])
    changed = False
    out = []
    for r in rules:
        if (isinstance(r, dict) and r.get("id") == "ollama-cloud"
                and r.get("supports_tools") is False):
            r = {**r, "supports_tools": True}
            changed = True
        out.append(r)
    return out, changed


def backfill_capability_ranks(rules):
    """Return (new_rules, changed): a COPY of `rules` with `capability_rank`
    backfilled onto the shipped default rules, and the two claude_cli
    max_complexity caps corrected.

    An append-only top-up cannot do either job -- both live ON rules an existing
    install already persisted, so a frozen registry would keep ordering the
    frontier tier on raw speed (Sonnet beating Opus for the hardest work, since
    within-tier perf scores are min-max normalised and the faster model takes
    the whole range).

    The max_complexity repair matters more than the ordering. claude_cli Sonnet
    shipped capped at "medium" and Haiku at "small" to express "escalate to
    Opus", but max_complexity is a HARD filter -- so the cap did not rank them
    lower, it removed them. On a claude_cli-only install (session auth, no API
    keys) a complexity="large" request matched NOTHING and select_model returned
    None: there was no Opus to escalate to. capability_rank now carries that
    preference without costing availability.

    Scoped narrowly, exactly like enable_ollama_cloud_tools: only rules whose id
    is one AppBuilder ships, only where the value still matches the stale
    default we are replacing. An operator's own rule, or one they have already
    retuned, is left untouched. Idempotent; never reorders, adds or removes."""
    shipped_ranks = {r.get("id"): r.get("capability_rank")
                     for r in DEFAULT_MODEL_RULES if r.get("capability_rank") is not None}
    # (rule id -> the stale value we replace) — only corrected if unchanged.
    stale_complexity = {"claude-cli-sonnet": ("medium", "large"),
                        "claude-cli-haiku": ("small", "medium")}
    out = []
    changed = False
    for r in rules or []:
        if not isinstance(r, dict):
            out.append(r)
            continue
        rid = r.get("id")
        new = r
        if rid in shipped_ranks and new.get("capability_rank") is None:
            new = {**new, "capability_rank": shipped_ranks[rid]}
            changed = True
        if rid in stale_complexity:
            was, now = stale_complexity[rid]
            if new.get("max_complexity") == was:
                new = {**new, "max_complexity": now}
                changed = True
        out.append(new)
    return out, changed


def upgrade_openrouter_free_router_rule(rules):
    """Return (new_rules, added): a COPY of `rules` with the OpenRouter Free
    Models Router rule ("openrouter-free-router") appended when it is missing.

    The Free Models Router model id is "openrouter/free" — it has no ":free"
    suffix, so the generic openrouter-free (`*:free`) rule does NOT match it and
    it falls through to openrouter-paid (`*` -> cost_tier "cheap"). That hides
    the single most reliable free option from the cost-first picker (the router
    auto-routes around per-model rate-limits/outages). This appends a literal
    `openrouter/free` rule (more specific than both `*:free` and `*`) that puts
    it in the free tier, so offloadable log/batch work prefers it over the GPU.

    Append-only + idempotent: skipped when the rule id already exists OR the
    operator already curated a rule for that exact (provider, match). Never
    reorders or mutates existing rules; the more-specific match wins (resolve)."""
    rules = list(rules or [])
    existing_ids = {r.get("id") for r in rules if isinstance(r, dict)}
    existing_pairs = {
        ((r.get("provider") or "").lower().strip(), (r.get("match") or "").lower().strip())
        for r in rules if isinstance(r, dict)
    }
    default = next((d for d in DEFAULT_MODEL_RULES
                    if d.get("id") == "openrouter-free-router"), None)
    if default is None:
        return rules, False
    if default.get("id") in existing_ids:
        return rules, False
    pair = ((default.get("provider") or "").lower().strip(),
            (default.get("match") or "").lower().strip())
    if pair in existing_pairs:
        return rules, False
    rules.append(dict(default))
    return rules, True


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
