"""Multi-provider LLM routing + circuit breakers for BugFixer.

Extracted verbatim from main.py (highest-value split): the provider constants,
message converters, per-provider config/rpm helpers, credit-exhaustion
and rate-limit circuit breakers, the shared LLM semaphore, the retry-aware POST
wrapper, the per-provider request functions, _call_provider, and call_llm. Pure
move, no behavior change.

main.py re-exports these via ``from llm_client import *`` placed early (right
after config_store) so the sibling modules keep resolving ``from main import
call_llm / _get_provider_config / validate_llm_config_on_startup`` etc.

The one adaptation: the shared ``state`` dict lives in app_state.py, which main
imports *after* llm_client (app_state builds `state` using this module's
_llm_cb_snapshot/_provider_credit_cb_snapshot). llm_client therefore cannot bind
`state` at import time; it references it lazily as ``main.state`` (resolved at
call time, by which point main has re-exported app_state's `state`).
"""
import collections
import json
import os
import random
import re
import shutil
import threading
import time
from datetime import datetime

import requests

import main  # lazy access to the shared app_state dict via main.state
from main import logger, load_config
import claude_cli_native_tools
import config_store
import llm_perf
import model_registry
import model_selection

# ============================================================================
# Multi-Provider LLM Routing
# ============================================================================

OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com"
# OpenRouter: a single OpenAI-compatible endpoint that routes to many backends —
# the "model" field is vendor-prefixed (e.g. "anthropic/claude-3.5-sonnet",
# "meta-llama/llama-3.1-70b-instruct"), which is why it needs its own model-listing
# branch (see _fetch_models_for_provider) instead of the generic openai one (that
# filters to gpt/o1/o3/o4 substrings and would drop nearly every OpenRouter model
# id). HTTP-Referer/X-Title are OpenRouter-specific attribution headers (optional,
# improve OpenRouter's own dashboard/rate-limit attribution — not required to work).
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/lbockenstedt/bugfixer",
    "X-Title": "BugFixer",
}
LMSTUDIO_BASE_URL = "http://localhost:1234/v1"  # LM Studio local OpenAI-compatible server
LMSTUDIO_DEFAULT_PORT = 1234
ANTHROPIC_API_VERSION = "2023-06-01"

# GitHub Copilot: OAuth device-flow → a long-lived GitHub OAuth token (stored as the
# provider's api_key) → exchanged for a short-lived Copilot API token → used against the
# OpenAI-compatible Copilot chat endpoint. Client id is the public VS Code / Copilot one.
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_API_BASE = "https://api.githubcopilot.com"
COPILOT_EDITOR_VERSION = "vscode/1.95.0"
COPILOT_PLUGIN_VERSION = "copilot-chat/0.22.0"
# gh_token -> (copilot_api_token, expires_at_epoch). The exchanged token is short-lived
# (~30 min) so we cache + refresh transparently.
_COPILOT_TOKEN_CACHE = {}
_COPILOT_TOKEN_LOCK = threading.Lock()


def _chat_defaults():
    """Lazily resolve CHAT_CONFIG_DEFAULTS. llm_client is imported *before*
    config_store (main import order), so a module-level import would be circular;
    at call time config_store is fully loaded."""
    try:
        from config_store import CHAT_CONFIG_DEFAULTS
        return CHAT_CONFIG_DEFAULTS
    except Exception:  # noqa: BLE001
        return {"FIX_MAX_OUTPUT_TOKENS": 8192}


def _normalize_lmstudio_url(base_url):
    """Normalize an LM Studio base URL to a full ``http://<host>:<port>/v1`` form.

    Why: the Settings vault stores whatever the user typed into the Base URL
    field. For LM Studio that is frequently a bare LAN IP ('192.168.1.50') or
    host:port ('192.168.1.50:1234') with no scheme and no ``/v1`` path. Passing
    that straight to requests.get('192.168.1.50/models') raises MissingSchema and
    silently yields zero models — which the UI then misreports as
    '— save credential first —'. LM Studio needs no API key, so the real fix is
    to build the URL the user meant: prepend http://, default port 1234, and
    force the OpenAI-compatible ``/v1`` mount point.

    Accepts a bare host/IP, host:port, or full URL and always returns a URL
    ending in ``/v1`` (without trailing slash).
    """
    from urllib.parse import urlsplit, urlunsplit

    base = (base_url or "").strip()
    if not base:
        return LMSTUDIO_BASE_URL
    if "://" not in base:
        base = "http://" + base
    parts = urlsplit(base)
    host = parts.hostname or ""
    port = parts.port
    if not port:
        port = LMSTUDIO_DEFAULT_PORT
    # LM Studio serves its OpenAI-compatible API under /v1. If the user
    # already included /v1 keep it; otherwise force it (any other path is
    # not a valid LM Studio endpoint).
    path = parts.path or ""
    if not path.rstrip("/").endswith("/v1"):
        path = "/v1"
    else:
        path = path.rstrip("/")
    netloc = f"{host}:{port}" if host else f":{port}"
    return urlunsplit((parts.scheme or "http", netloc, path, "", ""))


def _is_lmstudio(provider):
    """True for any LM Studio provider (``lmstudio``, ``lmstudio2``, ...).

    All LM Studio instances expose an OpenAI-compatible ``/v1`` API on a local
    server and need no API key. The vault keys credentials by provider name, so
    each ``lmstudio<N>`` carries its own base_url — letting multiple LM Studio
    servers coexist (e.g. a workstation instance and a second box). Matching by
    prefix means adding further instances needs no code change here.
    """
    return (provider or "").lower().strip().startswith("lmstudio")


def _is_ollama(provider):
    """True for any Ollama provider (``ollama``, ``ollama2``, ...).

    Ollama exposes a no-key local/LAN HTTP API (``/api/chat``, ``/api/tags``).
    A self-hosted instance needs no API key; Ollama Cloud (``https://ollama.com``)
    does take a key, but the key is optional at the configured-check layer so the
    local self-hosted case isn't forced to invent a dummy key. The vault keys
    credentials by provider name, so each ``ollama<N>`` carries its own base_url —
    letting a local instance and a remote instance coexist (e.g. ``ollama`` →
    ``http://localhost:11434`` for the box running bugfixer, ``ollama2`` → a
    remote host on the LAN/cloud). Matching by prefix means adding further
    instances needs no code change here.
    """
    return (provider or "").lower().strip().startswith("ollama")


#: Ollama Cloud (https://ollama.com) — the hosted Ollama service. Same wire API
#: as a self-hosted instance, so every ``_is_ollama`` prefix check already covers
#: it; what differs is that it REQUIRES an API key and defaults to a remote base
#: URL instead of localhost.
OLLAMA_CLOUD_PROVIDER = "ollama_cloud"
OLLAMA_CLOUD_BASE_URL = "https://ollama.com"


def _is_ollama_cloud(provider):
    """True for the hosted Ollama Cloud provider slot.

    Distinct from the no-key local/LAN instances that ``_is_ollama`` also matches:
    ollama.com authenticates with a Bearer key, so a key-less cloud slot is NOT
    usable and must be reported unconfigured rather than being tried and 401ing on
    every call. Kept as an exact-name check (not a prefix) so a self-hosted slot
    can never be accidentally forced to carry a key.
    """
    return (provider or "").lower().strip() == OLLAMA_CLOUD_PROVIDER


def _ollama_base_url(provider, base_url):
    """Resolve the endpoint for an Ollama slot: an explicit per-entry ``base_url``
    always wins; otherwise Ollama Cloud defaults to ollama.com and a self-hosted
    slot to localhost."""
    if base_url:
        return base_url
    return (OLLAMA_CLOUD_BASE_URL if _is_ollama_cloud(provider)
            else "http://localhost:11434")


#: Where Claude Code lands when it is NOT on the service PATH. bugfixer.service
#: sets User= but no Environment=PATH=, so it inherits systemd's minimal default
#: (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin). Claude Code's
#: native installer puts the binary under ~/.local/bin and npm/nvm installs land
#: in a user prefix — all invisible to the service even though `which claude`
#: succeeds in an interactive root shell. Probing these turns a confusing "not
#: found in PATH" into a working slot without hand-editing the unit file.
_CLAUDE_FALLBACK_PATHS = (
    "/usr/local/bin/claude",
    "/usr/bin/claude",
    "/opt/claude/bin/claude",
    "~/.local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.local/share/claude/bin/claude",
    "/root/.local/bin/claude",
)


def claude_bin(config=None):
    """Resolve the ``claude`` executable, or None when it genuinely isn't installed.

    Order: an explicit ``claude_binary`` config override (Settings) → PATH →
    the well-known install locations above. Returning an ABSOLUTE path means the
    subprocess no longer depends on the service's PATH matching an operator's
    interactive shell, which is the usual reason this slot reports "not found"
    on a box where Claude Code is plainly installed.
    """
    cfg = config if config is not None else load_config()
    override = str((cfg or {}).get("claude_binary") or "").strip()
    if override:
        p = os.path.expanduser(override)
        # Honour the override even if not executable so the UI can report WHY.
        return p if os.path.exists(p) else None
    found = shutil.which("claude")
    if found:
        return found
    for cand in _CLAUDE_FALLBACK_PATHS:
        p = os.path.expanduser(cand)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def claude_bin_or_raise(config=None):
    """``claude_bin`` but raising the operator-facing error when unresolved."""
    p = claude_bin(config)
    if not p:
        raise Exception(
            "'claude' binary not found. Looked on PATH (%s) and in: %s. Note the service "
            "runs as its own user with systemd's minimal PATH, so a binary that works in "
            "your shell may still be invisible here — set 'claude_binary' in Settings to "
            "the absolute path, or symlink it into /usr/local/bin."
            % (os.environ.get("PATH", "") or "unset", ", ".join(_CLAUDE_FALLBACK_PATHS)))
    return p


def _is_copilot(provider):
    """True for any GitHub Copilot provider (``copilot``, ``copilot2``, …)."""
    return (provider or "").lower().strip().startswith("copilot")


def _copilot_headers(bearer):
    """Headers the Copilot API requires (it rejects requests without the editor +
    integration identifiers)."""
    return {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "Editor-Version": COPILOT_EDITOR_VERSION,
        "Editor-Plugin-Version": COPILOT_PLUGIN_VERSION,
        "Copilot-Integration-Id": "vscode-chat",
        "User-Agent": "GitHubCopilotChat/0.22.0",
    }


def _copilot_api_token(gh_token):
    """Exchange the stored GitHub OAuth token for a short-lived Copilot API token,
    cached until ~1 min before expiry. Raises on failure (e.g. no Copilot subscription)."""
    if not gh_token:
        raise Exception("Copilot not authenticated — sign in with GitHub (device flow) first.")
    with _COPILOT_TOKEN_LOCK:
        cached = _COPILOT_TOKEN_CACHE.get(gh_token)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
    resp = requests.get(COPILOT_TOKEN_URL, headers={
        "Authorization": f"token {gh_token}",
        "Editor-Version": COPILOT_EDITOR_VERSION,
        "User-Agent": "GitHubCopilotChat/0.22.0",
    }, timeout=20)
    if resp.status_code != 200:
        raise Exception(f"Copilot token exchange failed ({resp.status_code}): {resp.text[:200]} "
                        "— is a GitHub Copilot subscription active for this account?")
    data = resp.json()
    token = data.get("token")
    expires = int(data.get("expires_at") or (time.time() + 1500))
    if not token:
        raise Exception("Copilot token exchange returned no token.")
    with _COPILOT_TOKEN_LOCK:
        _COPILOT_TOKEN_CACHE[gh_token] = (token, expires)
    return token


def _provider_is_nokey(provider):
    """True when ``provider`` authenticates without an API key: claude_cli (session
    auth), LM Studio and self-hosted Ollama (local/LAN servers). Ollama Cloud is
    explicitly excluded — it is the one ``ollama*`` slot that DOES need a key, and
    treating it as no-key would mark a key-less cloud slot 'configured' only for
    every call to 401."""
    return (provider == "claude_cli"
            or _is_lmstudio(provider)
            or (_is_ollama(provider) and not _is_ollama_cloud(provider)))


def _provider_configured(provider, key, model):
    """A provider is usable when it has a model, and either an API key or is a no-key
    provider (claude_cli session auth, LM Studio / self-hosted Ollama). Centralizes
    the no-key exception so every configured-check site agrees on what "configured"
    means."""
    return bool(model and (key or _provider_is_nokey(provider)))


def _record_provider_result(n, status, reason=""):
    """Record the last failover outcome for provider slot n.

    Surfaces silent skips (e.g. ``not_configured``) and per-provider failure reasons in
    the Diagnostics panel so they are visible without reading CLI logs. Best-effort:
    never raises into the failover path.
    """
    try:
        main.state["provider_last_result"][n] = {
            "status": status,
            "reason": (str(reason)[:300]) if reason else "",
            "at": datetime.now().isoformat(),
        }
    except Exception:
        pass


def _find_claude_cli_slot(config):
    """Return the provider slot number (1-4) configured as claude_cli, or None."""
    for n in (1, 2, 3, 4):
        provider, _, _, _ = _get_provider_config(n, config)
        if provider == "claude_cli":
            return n
    return None


#: Task kinds that are LOG analysis rather than code work. Log review wants a
#: different model from code repair — summarising noisy operational text is not
#: the same job as writing a patch — so it gets its own provider pool instead of
#: competing for the code slots.
_LOG_TASK_KINDS = frozenset({"log_review", "log_analysis"})
#: Slots 1-4 serve code work (build / review / triage). Slots 5-6 are reserved
#: for log analysis and are NEVER used for code, so a model chosen for reading
#: logs can't be dragged into writing a fix.
_CODE_SLOTS = (1, 2, 3, 4)
_LOG_SLOTS = (5, 6)
#: Slots 7-8 are the REVIEW pool: judging a fix wants the strongest model
#: available, but a strong model on a code slot also gets spent on triage,
#: file identification and PR summaries — minor work that a cheap model does
#: just as well. Keeping review on its own pool makes those models structurally
#: unreachable from that work rather than merely discouraged.
_REVIEW_SLOTS = (7, 8)
#: Tasks that are a judgement call on someone else's output, not production of
#: it. Both are worth a strong model; neither is high-volume.
_REVIEW_TASK_KINDS = frozenset({"review", "pr_confidence"})
#: Every slot the app knows about — circuit breakers and rate limiters are keyed
#: over this, not over a hardcoded 1-4.
_ALL_SLOTS = _CODE_SLOTS + _LOG_SLOTS + _REVIEW_SLOTS


#: Live model catalogue per (provider, base_url), so routing can check whether a
#: model actually EXISTS on this account before spending a call on it. Cached:
#: the listing is a network round-trip and routing runs on every task.
_MODEL_CATALOG: "dict" = {}
_MODEL_CATALOG_TTL_S = 3600
_MODEL_CATALOG_LOCK = threading.Lock()


def _live_models(provider, api_key, base_url):
    """Model names this provider/account can actually serve, or None if unknown.

    None means "could not determine" (no network, no key, provider has no listing
    endpoint) and callers MUST treat it as "do not block" — refusing to route on
    a failed lookup would be worse than the 404 it is trying to avoid.
    """
    key = ((provider or "").lower().strip(), (base_url or "").strip())
    now = time.time()
    with _MODEL_CATALOG_LOCK:
        hit = _MODEL_CATALOG.get(key)
        if hit and (now - hit[0]) < _MODEL_CATALOG_TTL_S:
            return hit[1]
    try:
        # Lazy import: workers imports llm_client, so a module-level import here
        # would be a cycle.
        from workers import _fetch_models_for_provider
        res = _fetch_models_for_provider(provider, api_key, base_url) or {}
        names = {m.get("name") for m in (res.get("models") or []) if m.get("name")}
        names = names or None          # empty list == could not determine
    except Exception as e:  # noqa: BLE001 — never let a lookup break routing
        logger.debug("live model lookup failed for %s: %s", provider, e)
        names = None
    with _MODEL_CATALOG_LOCK:
        _MODEL_CATALOG[key] = (now, names)
    return names


#: Routed models that have 404'd for a provider — (provider, model) pairs.
#: A router tier default the account cannot reach 404s on EVERY task of that
#: tier. The retry-on-configured-model fallback below keeps the work flowing, but
#: without remembering the failure every single small-tier task pays a wasted
#: round-trip to the same dead model first. Remembered process-wide (cleared on
#: restart, which is also when config/model access may have changed).
_ROUTED_404: "set" = set()
_ROUTED_404_LOCK = threading.Lock()


def _routed_model_dead(provider, model) -> bool:
    if not provider or not model:
        return False
    with _ROUTED_404_LOCK:
        return ((provider or "").lower().strip(), model) in _ROUTED_404


def _mark_routed_model_dead(provider, model) -> None:
    if not provider or not model:
        return
    with _ROUTED_404_LOCK:
        _ROUTED_404.add(((provider or "").lower().strip(), model))


def _get_provider_config(n, config):
    """Return (provider, api_key, model, base_url) for slot n.

    Supports the new vault-based config (llm_credentials + llm_entries + llm_slots)
    with a transparent fallback to the legacy flat LLM_PROVIDER_N / LLM_API_KEY_N keys.
    """
    slots = config.get("llm_slots") or {}
    entry_id = slots.get(str(n))
    if entry_id:
        entries_list = config.get("llm_entries") or []
        credentials = config.get("llm_credentials") or {}
        entry = next((e for e in entries_list if e.get("id") == entry_id), None)
        if entry:
            provider = (entry.get("provider") or "openai").lower().strip()
            model = (entry.get("model") or "").strip()
            cred = credentials.get(provider) or {}
            # Per-ENTRY base_url/api_key take precedence over the shared per-provider
            # credential, so multiple entries of the same provider can target
            # different endpoints — e.g. three `ollama` entries pointing at local CPU
            # (localhost), a remote-GPU box on the LAN, and Ollama Cloud. Falls back
            # to the shared credential when the entry doesn't override.
            api_key = (entry.get("api_key") or cred.get("api_key") or "").strip()
            base_url = (entry.get("base_url") or cred.get("base_url") or "").strip()
            return provider, api_key, model, base_url

    # Legacy flat config fallback.
    provider = (config.get(f"LLM_PROVIDER_{n}") or os.getenv(f"LLM_PROVIDER_{n}", "openai")).lower().strip()
    api_key = (config.get(f"LLM_API_KEY_{n}") or os.getenv(f"LLM_API_KEY_{n}", "")).strip()
    model = (config.get(f"LLM_MODEL_{n}") or os.getenv(f"LLM_MODEL_{n}", "")).strip()
    base_url = (config.get(f"LLM_BASE_URL_{n}") or os.getenv(f"LLM_BASE_URL_{n}", "")).strip()
    return provider, api_key, model, base_url


def _get_provider_rpm(n, config):
    """Return RPM cap for slot n, from vault entry or legacy LLM_RPM_N key."""
    slots = config.get("llm_slots") or {}
    entry_id = slots.get(str(n))
    if entry_id:
        for e in (config.get("llm_entries") or []):
            if e.get("id") == entry_id:
                return int(e.get("rpm") or 0)
    return int(config.get(f"LLM_RPM_{n}") or 0)


def _parse_retry_after(retry_after_header, backoff_base, backoff_max, attempt):
    """Parse a Retry-After header into a wait time in seconds."""
    if retry_after_header:
        try:
            return min(float(retry_after_header), backoff_max)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                retry_date = parsedate_to_datetime(retry_after_header)
                if retry_date:
                    wait = retry_date.timestamp() - datetime.now().timestamp()
                    return min(max(0, wait), backoff_max)
            except Exception:
                pass
    return min(backoff_base ** attempt, backoff_max) * random.uniform(0.5, 1.0)


def _tool_spec(t):
    """Normalize one tool entry to its flat {name, description, parameters} spec.

    Two shapes coexist in the codebase: the legacy flat one (chat.py's
    CHAT_TOOLS: {"name": ..., "description": ..., "parameters": ...}) and the
    standard OpenAI-nested one (fix_engine.py's _REVIEW_TOOLS:
    {"type": "function", "function": {"name": ..., ...}} — the shape Ollama's
    API itself expects, since _request_ollama passes `tools` through as-is).
    The converters below used to assume only the flat shape and did `t["name"]`
    directly, which KeyError'd('name') on every nested tool — breaking every
    non-Ollama reviewer (copilot, openai, anthropic, google) whenever the PR
    pre-review panel's fetch_repo_file tool was in play (bugfixer#727)."""
    fn = t.get("function")
    return fn if isinstance(fn, dict) else t


def _tools_to_openai(tools):
    """Convert either tool-schema shape (see _tool_spec) to OpenAI function-calling format."""
    return [
        {"type": "function", "function": {"name": (fn := _tool_spec(t)).get("name", ""),
                                          "description": fn.get("description", ""),
                                          "parameters": fn.get("parameters", {})}}
        for t in (tools or [])
    ]


def _tools_to_anthropic(tools):
    """Convert either tool-schema shape (see _tool_spec) to Anthropic tool format."""
    return [
        {"name": (fn := _tool_spec(t)).get("name", ""), "description": fn.get("description", ""),
         "input_schema": fn.get("parameters", {"type": "object", "properties": {}, "required": []})}
        for t in (tools or [])
    ]


def _to_openai_messages(messages):
    """Adapt internal messages for the OpenAI API (normalises tool roles and tool_calls)."""
    result = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            result.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id") or m.get("name") or "unknown",
                "content": m.get("content") or "",
            })
        elif role == "assistant" and m.get("tool_calls"):
            tcs = []
            for idx, tc in enumerate(m["tool_calls"]):
                fn = tc.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, dict):
                    args = json.dumps(args)
                tcs.append({
                    "id": tc.get("id") or f"call_{fn.get('name', 'unknown')}_{idx}",
                    "type": "function",
                    "function": {"name": fn.get("name") or "", "arguments": args},
                })
            result.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tcs})
        else:
            result.append({"role": role, "content": m.get("content") or ""})
    return result


def _to_anthropic_messages(messages):
    """Convert internal messages to Anthropic format. Returns (system_str, messages_list)."""
    system = ""
    converted = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")
        if role == "system":
            system += ("\n" if system else "") + (m.get("content") or "")
            i += 1
            continue
        if role == "assistant":
            content = m.get("content") or ""
            tcs = m.get("tool_calls") or []
            if tcs:
                parts = []
                if content:
                    parts.append({"type": "text", "text": content})
                for tc in tcs:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    parts.append({
                        "type": "tool_use",
                        "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
                        "name": fn.get("name") or "",
                        "input": args,
                    })
                converted.append({"role": "assistant", "content": parts})
            else:
                converted.append({"role": "assistant", "content": content})
            i += 1
        elif role == "tool":
            tool_results = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id") or tm.get("name") or "unknown",
                    "content": tm.get("content") or "",
                })
                i += 1
            if converted and converted[-1].get("role") == "user" and isinstance(converted[-1].get("content"), list):
                converted[-1]["content"].extend(tool_results)
            else:
                converted.append({"role": "user", "content": tool_results})
        else:
            converted.append({"role": role, "content": m.get("content") or ""})
            i += 1
    return system, converted


def _to_google_contents(messages):
    """Convert internal messages to Google Gemini contents format. Returns (system_text, contents)."""
    system_text = ""
    contents = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system_text += ("\n" if system_text else "") + (m.get("content") or "")
            continue
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                parts = []
                if m.get("content"):
                    parts.append({"text": m["content"]})
                for tc in tcs:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    parts.append({"functionCall": {"name": fn.get("name") or "", "args": args}})
                contents.append({"role": "model", "parts": parts})
            else:
                contents.append({"role": "model", "parts": [{"text": m.get("content") or ""}]})
        elif role == "tool":
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": m.get("name") or m.get("tool_call_id") or "unknown",
                "response": {"content": m.get("content") or ""},
            }}]})
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": m.get("content") or ""}]})
    return system_text, contents


def validate_llm_config_on_startup():
    """Validates that at least one LLM provider is fully configured."""
    config = load_config()
    ok = False
    for n in (1, 2):
        provider, api_key, model, _ = _get_provider_config(n, config)
        # claude_cli, lmstudio and SELF-HOSTED ollama don't need an API key
        # (session auth / local server); Ollama Cloud does — see _provider_is_nokey.
        if _provider_configured(provider, api_key, model):
            logger.info(f"LLM Provider {n}: provider={provider!r} model={model!r} — configured.")
            ok = True
        else:
            logger.info(f"LLM Provider {n}: not fully configured (provider={provider!r} model={model!r} key_set={bool(api_key)}).")
    if not ok:
        logger.warning(
            "\n" + "=" * 78 + "\n"
            "!!  LLM CONFIGURATION WARNING  !!\n"
            "Neither LLM provider is fully configured.\n"
            "HOW TO FIX:\n"
            "  1. Open the BugFixer dashboard: https://<this-host>/settings\n"
            "  2. Set LLM_PROVIDER_1, LLM_API_KEY_1, and LLM_MODEL_1\n"
            "  3. Optionally set LLM_PROVIDER_2, LLM_API_KEY_2, LLM_MODEL_2 for failover.\n"
            + "=" * 78
        )
    return ok

# ============================================================================
# LLM Circuit Breaker & Credit-Exhaustion Tracker
# ============================================================================

class LLMCreditExhausted(Exception):
    """Raised immediately (no retry) when a provider signals billing/credit exhaustion."""
    def __init__(self, status_code, body, provider):
        self.status_code = status_code
        self.body = body
        self.provider = provider
        super().__init__(f"Provider '{provider}' credit exhausted (HTTP {status_code}): {body[:200]}")


class LlmHumanEscalationNeeded(Exception):
    """Raised by _call_llm_with_requirements instead of silently falling to the
    rule-based safety floor, when the caller's requirements.must_escalate_to_human
    is True and select_model() found NOTHING that satisfies them (every tier
    exhausted). Only one call site opts into this today (fix_engine.py's fix
    generation, per the LLM Selection Redesign plan's requirement table) — every
    other requirements= call site still falls through to the safety floor
    exactly as before. Distinguishes "nothing capable enough exists right now"
    (terminal, should surface to a human) from "nothing at all is configured"
    (the existing bare "No LLM providers configured" exception)."""
    pass


_CREDIT_MSG_KEYWORDS = frozenset({
    "credit balance is too low", "credit balance too low",
    "insufficient credits", "insufficient_quota",
    "billing_hard_limit_reached", "billing not enabled",
    "quota exceeded", "out of credits", "payment required",
    "upgrade or purchase credits",
})

def _is_credit_exhaustion(status_code, body_text, provider):
    """Return True if the HTTP error indicates billing/quota exhaustion, not just rate limiting.

    Provider-specific signals:
      All            — HTTP 402 Payment Required
      OpenAI/Ollama  — error.code/type in {insufficient_quota, billing_hard_limit_reached}
      Anthropic      — error.type in {credit_balance_too_low, billing_error}
                       OR any error message containing credit/billing keywords
                       (Anthropic wraps credit errors as invalid_request_error)
      Google         — RESOURCE_EXHAUSTED with billing/quota keyword in message
    """
    if status_code == 402:
        return True

    try:
        body = json.loads(body_text) if body_text else {}
    except Exception:
        body = {}

    p = (provider or "openai").lower().strip()
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    err_msg = (err.get("message") or "").lower()

    if p in ("openai", "ollama", "openrouter"):
        if err.get("code") in {"insufficient_quota", "billing_hard_limit_reached"}:
            return True
        if err.get("type") in {"insufficient_quota", "billing_not_active"}:
            return True
        # OpenAI-compat providers sometimes put it in the message too. OpenRouter
        # proxies whichever upstream backend it routed to, so its error body
        # shape varies — the universal HTTP 402 check above already covers
        # OpenRouter's own "Insufficient credits" response; this keyword
        # fallback catches a proxied upstream credit error surfaced at a
        # different status code.
        if any(k in err_msg for k in _CREDIT_MSG_KEYWORDS):
            return True

    if p == "anthropic":
        # Anthropic sometimes uses credit_balance_too_low as error.type directly,
        # but also wraps it as invalid_request_error with the reason in the message.
        if err.get("type") in {"credit_balance_too_low", "billing_error"}:
            return True
        if body.get("type") in {"credit_balance_too_low", "billing_error"}:
            return True
        if any(k in err_msg for k in _CREDIT_MSG_KEYWORDS):
            return True

    if p == "google":
        if err.get("status") == "RESOURCE_EXHAUSTED":
            msg = (err.get("message") or "").lower()
            if any(k in msg for k in ("quota", "billing", "budget", "credit", "payment")):
                return True

    return False


# Per-provider 1-hour credit-exhaustion cooldown (independent of the global rate-limit CB)
_PROVIDER_CREDIT_CB_LOCK = threading.Lock()
# Keyed by SLOT, but each entry records the PROVIDER that earned it. A slot is a
# position an operator can reassign at any time, so a cooldown that outlives the
# provider which caused it is simply wrong: swapping slot 1 from google (429ed,
# 60-min credit cooldown) to a local ollama left the local model — which has no
# credits and cannot be rate-limited — sitting out the remainder of Google's
# penalty, logged as "Provider 1 (ollama) skipped ... (tripped by: Provider
# 'google' credit exhausted)". _provider_credit_cb_remaining voids the cooldown
# when the occupant changes.
_PROVIDER_CREDIT_CB = {
    n: {"cooldown_until": 0.0, "tripped_at": None, "reason": None, "cause": None,
        "provider": None}
    for n in _ALL_SLOTS
}
_CREDIT_COOLDOWN_SECONDS = 3600   # 1 hour for credit exhaustion
_RATELIMIT_COOLDOWN_SECONDS = 600  # 10 minutes for sustained 429 storms


def _provider_credit_cb_trip(n, reason, duration_s=None, cause="credit", provider=None):
    # A self-hosted server has no billing to exhaust and no quota to exceed, so a
    # CREDIT cooldown on one is always a mistake — either mis-attributed from
    # another provider or a misread error body. Parking a local model for an hour
    # over "credit" removes the one provider that is always available, exactly
    # when the paid ones are rate-limited and it is needed most. Rate-limit trips
    # are still honoured: a local server under load can legitimately push back.
    if cause == "credit" and provider and _provider_is_nokey(provider):
        logger.warning(
            "Ignoring CREDIT cooldown for provider %s (%s): it is a local/no-key "
            "provider with no billing to exhaust. Reason was: %s",
            n, provider, str(reason)[:160])
        return
    secs = duration_s if duration_s is not None else _CREDIT_COOLDOWN_SECONDS
    cd = time.time() + secs
    with _PROVIDER_CREDIT_CB_LOCK:
        _PROVIDER_CREDIT_CB[n]["cooldown_until"] = cd
        _PROVIDER_CREDIT_CB[n]["tripped_at"] = datetime.now().isoformat()
        _PROVIDER_CREDIT_CB[n]["reason"] = reason
        _PROVIDER_CREDIT_CB[n]["cause"] = cause
        # Bind the penalty to its owner so reassigning the slot clears it.
        _PROVIDER_CREDIT_CB[n]["provider"] = provider
    until_str = datetime.fromtimestamp(cd).strftime("%H:%M:%S")
    if cause == "rate_limit":
        logger.warning(
            f"Provider {n} RATE-LIMITED — pausing for {secs//60} min "
            f"(until ~{until_str}). Reason: {reason}"
        )
    else:
        logger.error(
            f"Provider {n} CREDIT EXHAUSTED — pausing for {secs//60} min "
            f"(until ~{until_str}). Reason: {reason}"
        )


def _provider_credit_cb_remaining(n, provider=None):
    """Seconds remaining on credit/rate-limit cooldown for provider n (0 if clear).

    ``provider`` is the slot's CURRENT occupant. If it differs from the one that
    tripped the cooldown, the slot was reassigned and the penalty no longer
    applies — clear it and report 0 rather than punishing the new provider for
    the old one's quota.
    """
    with _PROVIDER_CREDIT_CB_LOCK:
        entry = _PROVIDER_CREDIT_CB[n]
        owner = entry.get("provider")
        if (provider and owner and provider != owner
                and entry["cooldown_until"] > time.time()):
            logger.info(
                "Provider %s reassigned %s -> %s; clearing the %s cooldown it "
                "inherited (cooldowns follow the provider, not the slot).",
                n, owner, provider, entry.get("cause") or "credit")
            entry.update({"cooldown_until": 0.0, "tripped_at": None,
                          "reason": None, "cause": None, "provider": None})
            return 0.0
        return max(0.0, entry["cooldown_until"] - time.time())


def _provider_credit_cb_snapshot():
    result = {}
    with _PROVIDER_CREDIT_CB_LOCK:
        # _ALL_SLOTS: the map is keyed over every slot, and the dashboard reads
        # this per slot. Reporting only 1-4 left a log or review slot sitting in
        # a credit cooldown with nothing on screen to say so.
        for n in _ALL_SLOTS:
            rem = max(0.0, _PROVIDER_CREDIT_CB[n]["cooldown_until"] - time.time())
            result[n] = {
                "active": rem > 0,
                "cooldown_remaining_s": rem,
                "cooldown_remaining_min": round(rem / 60, 1),
                "tripped_at": _PROVIDER_CREDIT_CB[n]["tripped_at"],
                "reason": _PROVIDER_CREDIT_CB[n]["reason"],
                "cause": _PROVIDER_CREDIT_CB[n]["cause"],
            }
    return result


def _any_provider_available(config):
    """Return (available: bool, soonest_free_s: float).

    available=True  → at least one configured provider is not in cooldown.
    soonest_free_s  → seconds until the soonest cooldown expires (0 if available).
    """
    soonest = float("inf")
    any_free = False
    for n in (1, 2, 3, 4):
        provider, key, model, _ = _get_provider_config(n, config)
        configured = _provider_configured(provider, key, model)
        if not configured:
            continue  # not configured
        rem = _provider_credit_cb_remaining(n, provider)
        if rem <= 0:
            any_free = True
            soonest = 0.0
        else:
            soonest = min(soonest, rem)
    if soonest == float("inf"):
        soonest = 0.0  # no providers configured at all
    return any_free, soonest


_LLM_CB_LOCK = threading.Lock()
# Trips are counted in TWO classes, because they mean opposite things and want
# opposite responses:
#   rate_limit — the provider answered 429/5xx. Backing off genuinely helps.
#   transient  — a timeout / connection drop. NOTHING was rate-limited; on a local
#                Ollama it means the model is too slow for LLM_TIMEOUT (too big a
#                num_ctx, CPU inference, swapping). Backing off does not help; the
#                fix is tuning or a smaller model.
# Folding both into consecutive_429s/total_429s reported "429" against a local
# server that never rate-limits anything, which sends you looking for a quota
# problem that does not exist. The 429 keys are RETAINED (same meaning, now
# rate-limit-only) so existing snapshot consumers keep working.
_LLM_CB = {
    "cooldown_until": 0.0,
    "consecutive_429s": 0,
    "total_429s": 0,
    "consecutive_transient": 0,
    "total_transient": 0,
    "last_trip_reason": None,
    "last_trip_time": None,
    "last_trip_kind": None,
    "last_trip_provider": None,
}

def _llm_cb_wait(provider=None):
    """Honour the global cooldown — but ONLY for the provider that caused it.

    This breaker paused every LLM thread regardless of which provider tripped it,
    so one unreachable endpoint stalled calls to all the others and defeated the
    failover it sits in front of: a local ollama that is simply down would delay
    the paid cloud slots that were about to serve the request. A provider's
    trouble is evidence about that provider, not about the others.

    Passing no provider preserves the old fleet-wide behaviour for callers that
    genuinely want it.
    """
    while True:
        with _LLM_CB_LOCK:
            cd = _LLM_CB["cooldown_until"]
            owner = _LLM_CB.get("last_trip_provider")
        # Different provider than the one being penalised → not our cooldown.
        if provider and owner and provider != owner:
            return
        remaining = cd - time.time()
        if remaining <= 0:
            time.sleep(random.uniform(0, 1.5))
            return
        sleep_chunk = min(remaining, 5.0)
        # Name the ACTUAL cause. This said "rate-limit cooldown" unconditionally,
        # so a local endpoint that was simply down reported a rate limit that no
        # one had imposed — the same conflation the split counters fixed on the
        # trip side.
        with _LLM_CB_LOCK:
            _kind = _LLM_CB.get("last_trip_kind") or "rate_limit"
        _why = "endpoint-unreachable" if _kind == "transient" else "rate-limit"
        logger.warning(
            f"LLM circuit breaker active — pausing {sleep_chunk:.1f}s "
            f"({remaining:.1f}s remaining) to respect the {_why} cooldown."
        )
        time.sleep(sleep_chunk)

def _llm_cb_trip(wait_time, reason="429", kind="rate_limit", provider=None):
    """Trip the global breaker. ``kind`` is 'rate_limit' (provider said 429/5xx) or
    'transient' (timeout / connection drop — nothing was rate-limited)."""
    wait_time = max(0.5, min(wait_time, 3600.0))
    kind = "transient" if kind == "transient" else "rate_limit"
    _c_key = "consecutive_transient" if kind == "transient" else "consecutive_429s"
    _t_key = "total_transient" if kind == "transient" else "total_429s"
    with _LLM_CB_LOCK:
        new_cd = max(_LLM_CB["cooldown_until"], time.time() + wait_time)
        _LLM_CB["cooldown_until"] = new_cd
        _LLM_CB[_c_key] += 1
        _LLM_CB[_t_key] += 1
        _LLM_CB["last_trip_reason"] = reason
        _LLM_CB["last_trip_kind"] = kind
        _LLM_CB["last_trip_provider"] = provider
        _LLM_CB["last_trip_time"] = datetime.now().isoformat()
        consecutive = _LLM_CB[_c_key]
        total = _LLM_CB[_t_key]
    _label = "consecutive_timeouts" if kind == "transient" else "consecutive_429s"
    _tlabel = "total_timeouts" if kind == "transient" else "total_429s"
    if kind != "transient":
        _hint = ""
    elif any(t in str(reason) for t in ("ConnectionError", "NewConnectionError",
                                       "Connection refused", "RemoteDisconnected")):
        # Refused/closed means nothing is listening — model size and timeouts are
        # irrelevant, and pointing at them sends the operator tuning a server that
        # is not running.
        _hint = (" — nothing is LISTENING on that endpoint (is the server running? "
                 "e.g. `systemctl status ollama`); not a rate limit and not a "
                 "model-size problem")
    else:
        _hint = (" — the endpoint did not answer in time; nothing was rate-limited "
                 "(check LLM_TIMEOUT / ollama_num_ctx / model size)")
    logger.warning(
        f"LLM circuit breaker TRIPPED for {wait_time:.1f}s (reason={reason}){_hint}. "
        f"{_label}={consecutive}, {_tlabel}={total}. "
        f"All LLM threads will pause."
    )

def _llm_cb_reset():
    with _LLM_CB_LOCK:
        if _LLM_CB["consecutive_429s"] > 0 or _LLM_CB["consecutive_transient"] > 0:
            logger.info(
                f"LLM circuit breaker reset after successful request "
                f"(was consecutive_429s={_LLM_CB['consecutive_429s']}, "
                f"total_429s={_LLM_CB['total_429s']}, "
                f"consecutive_timeouts={_LLM_CB['consecutive_transient']}, "
                f"total_timeouts={_LLM_CB['total_transient']})."
            )
            _LLM_CB["consecutive_429s"] = 0
            _LLM_CB["consecutive_transient"] = 0

def _llm_cb_snapshot():
    with _LLM_CB_LOCK:
        cd = _LLM_CB["cooldown_until"]
        return {
            "active": cd > time.time(),
            "cooldown_remaining_s": max(0, cd - time.time()),
            "consecutive_429s": _LLM_CB["consecutive_429s"],
            "total_429s": _LLM_CB["total_429s"],
            # Timeouts/connection drops, counted apart from real rate limits.
            "consecutive_timeouts": _LLM_CB["consecutive_transient"],
            "total_timeouts": _LLM_CB["total_transient"],
            "last_trip_reason": _LLM_CB["last_trip_reason"],
            "last_trip_kind": _LLM_CB["last_trip_kind"],
            "last_trip_provider": _LLM_CB["last_trip_provider"],
            "last_trip_time": _LLM_CB["last_trip_time"],
        }


#: Per-CATEGORY (CODE / LOG / REVIEW) concurrency cap, sized from
#: LLM_MAX_CONCURRENT — this used to be ONE global limiter; now each pool gets
#: its OWN semaphore of that size, so N jobs can run in CODE and, independently,
#: a separate N in LOG and N in REVIEW at the same time. If LLM_MAX_CONCURRENT
#: exceeds a pool's configured slot count, the semaphore alone can't guarantee a
#: free slot — call_llm's busy-wait-and-rescan loop covers that case.
_CATEGORY_SEMAPHORES = {}
_CATEGORY_SEM_LOCK = threading.Lock()


def _get_category_semaphore(pool_name):
    with _CATEGORY_SEM_LOCK:
        sem = _CATEGORY_SEMAPHORES.get(pool_name)
        if sem is None:
            try:
                cfg = load_config()
                max_conc = int(cfg.get("LLM_MAX_CONCURRENT", 1))
            except Exception:
                max_conc = 1
            sem = threading.Semaphore(max(1, max_conc))
            _CATEGORY_SEMAPHORES[pool_name] = sem
            logger.info(f"LLM {pool_name} pool concurrency limiter initialised: max_concurrent={max(1, max_conc)}")
        return sem


def _reset_llm_semaphore():
    """Drop every cached per-category concurrency semaphore so each is rebuilt
    from the (possibly changed) LLM_MAX_CONCURRENT setting on next use. Name
    kept unchanged for the existing /save_settings caller in routes.py — it now
    resets all three category gates (CODE/LOG/REVIEW) instead of one global
    one; slot locks need no reset, they aren't sized by config."""
    global _CATEGORY_SEMAPHORES
    with _CATEGORY_SEM_LOCK:
        _CATEGORY_SEMAPHORES = {}

#: Headers whose value is a credential. A provider error body can echo the
#: request back at you, so the key is stripped before the body reaches a log
#: line or the UI -- surfacing the reason for a 403 must not leak the key.
_SECRETISH_HEADERS = ("authorization", "x-api-key", "x-goog-api-key", "api-key")


def _redact_secrets(text, headers=None):
    """Remove request credentials from a provider error body."""
    if not text:
        return text
    t = str(text)
    for name, val in (headers or {}).items():
        if not val or str(name).lower() not in _SECRETISH_HEADERS:
            continue
        for tok in (str(val), str(val).replace("Bearer ", "").strip()):
            # Short values cannot be a key and could be a common substring.
            if len(tok) >= 8:
                t = t.replace(tok, "***REDACTED***")
    return t


def _llm_retry_post(endpoint, payload, headers, config, stream=False, provider="openai"):
    """POST to endpoint with retry/backoff. Returns the response object on success.

    Raises LLMCreditExhausted immediately (no retry) when the provider signals
    billing/credit exhaustion so call_llm() can trip the per-provider 1-hour CB.
    """
    max_retries = int(config.get("LLM_MAX_RETRIES", 5))
    # 5xx (esp. 503 "model overloaded / high demand") won't clear in a few seconds
    # of backoff — a DIFFERENT provider is the fix. Cap same-provider 5xx retries
    # low so call_llm's failover moves to the next provider fast instead of
    # hammering the overloaded one through the full 429 retry budget. (429 = rate
    # limit still uses the full max_retries, since waiting genuinely helps there.)
    max_5xx = int(config.get("LLM_5XX_MAX_RETRIES", 1))
    backoff_base = float(config.get("LLM_BACKOFF_BASE", 2.0))
    backoff_max = float(config.get("LLM_BACKOFF_MAX", 60.0))
    timeout_val = int(config.get("LLM_TIMEOUT", 900))

    last_exception = None
    for attempt in range(max_retries + 1):
        is_last = attempt == max_retries
        _llm_cb_wait(provider)
        try:
            # Concurrency gating happens one layer up now (call_llm holds a
            # per-CATEGORY semaphore + this slot's lock for the whole
            # _try_provider call) — this is just the bare HTTP attempt.
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout_val, stream=stream)

            # Credit exhaustion check — happens before any retry logic so we don't
            # waste retries against a billing wall.
            if resp.status_code in (400, 402, 403, 429):
                body_preview = ""
                try:
                    body_preview = resp.text[:2000]
                except Exception:
                    pass
                if _is_credit_exhaustion(resp.status_code, body_preview, provider):
                    resp.close()
                    raise LLMCreditExhausted(resp.status_code, body_preview, provider)

            if resp.status_code == 429:
                wait_time = _parse_retry_after(resp.headers.get("Retry-After"), backoff_base, backoff_max, attempt)
                if is_last:
                    logger.error(f"LLM 429 at {endpoint} after {max_retries+1} attempts.")
                    resp.close()
                    resp.raise_for_status()
                logger.warning(f"LLM 429 at {endpoint}. Backing off {wait_time:.1f}s (attempt {attempt+1}/{max_retries+1}).")
                resp.close()
                time.sleep(wait_time)
                continue

            if resp.status_code == 401:
                logger.error(f"LLM 401 Unauthorized at {endpoint}. Check API key in settings.")
                resp.raise_for_status()

            if resp.status_code == 400:
                err_body = ""
                try:
                    err_body = resp.text[:1000]
                except Exception:
                    pass
                logger.error(f"LLM 400 Bad Request at {endpoint}. Body: {err_body!r}")
                resp.close()
                resp.raise_for_status()

            if 500 <= resp.status_code < 600:
                # Give up after max_5xx same-provider attempts so call_llm fails over
                # to the next provider (a 503 "high demand" needs a different model,
                # not more retries against the overloaded one).
                give_up = is_last or attempt >= max_5xx
                wait_time = min(backoff_base ** attempt, backoff_max) * random.uniform(0.5, 1.5)
                _llm_cb_trip(wait_time, f"{resp.status_code} attempt {attempt+1}/{max_retries+1}", provider=provider)
                err_body = ""
                try:
                    err_body = resp.text[:1000]
                except Exception:
                    pass
                if give_up:
                    logger.warning(f"LLM {resp.status_code} at {endpoint} after {attempt+1} attempt(s) — failing over to the next provider. body={err_body!r}")
                    resp.close()
                    resp.raise_for_status()
                logger.warning(f"LLM {resp.status_code} at {endpoint}. Backing off {wait_time:.1f}s. body={err_body!r}")
                resp.close()
                time.sleep(wait_time)
                continue

            # 4xx: attach the provider's own explanation. raise_for_status()
            # produces only "403 Client Error: Forbidden for url: ...", which says
            # nothing actionable -- Google's body carries the actual reason
            # (SERVICE_DISABLED, API_KEY_HTTP_REFERRER_BLOCKED, a project/billing
            # problem, a model the key cannot use), and discarding it is why a 403
            # here has been indistinguishable from a 404. 5xx already logs its
            # body above; this is the same courtesy for the errors an operator can
            # actually fix.
            if 400 <= resp.status_code < 500:
                err_body = ""
                try:
                    err_body = _redact_secrets(resp.text[:600], headers)
                except Exception:  # noqa: BLE001 — body is best-effort
                    pass
                if err_body:
                    logger.warning(
                        f"LLM {resp.status_code} at {endpoint}. body={err_body!r}")
                    resp.close()
                    raise requests.exceptions.HTTPError(
                        f"{resp.status_code} Client Error: {resp.reason} for url: "
                        f"{endpoint} — {err_body}", response=resp)
            resp.raise_for_status()
            _llm_cb_reset()
            return resp

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            _5xx_retry_ok = 500 <= (status or 0) < 600 and attempt < max_5xx
            if not is_last and status and (status == 429 or _5xx_retry_ok):
                wait_time = min(backoff_base ** attempt, backoff_max) * random.uniform(0.5, 1.5)
                _llm_cb_trip(wait_time, f"HTTPError {status}", provider=provider)
                logger.warning(f"LLM HTTPError {status} at {endpoint}. Backing off {wait_time:.1f}s.")
                last_exception = e
                time.sleep(wait_time)
                continue
            raise
        except LLMCreditExhausted:
            raise  # never retry a billing wall — propagate immediately
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            last_exception = e
            if not is_last:
                wait_time = min(backoff_base ** attempt, backoff_max) * random.uniform(0.5, 1.5)
                _llm_cb_trip(wait_time, f"transient {type(e).__name__}", kind="transient", provider=provider)
                logger.warning(f"LLM transient error at {endpoint} (attempt {attempt+1}): {e}. Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue
            raise
        except Exception as e:
            last_exception = e
            if not is_last:
                wait_time = min(backoff_base ** attempt, backoff_max) * random.uniform(0.5, 1.5)
                logger.warning(f"LLM error at {endpoint} (attempt {attempt+1}): {e}. Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue
            raise

    if last_exception:
        raise last_exception
    raise Exception(f"LLM request to {endpoint} exhausted all {max_retries+1} attempts")


def _request_openai(model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                    provider_name="openai", extra_headers=None, usage_out=None):
    """Call an OpenAI-compatible endpoint. Returns text string or tool-call dict.

    ``provider_name``/``extra_headers`` let an OpenAI-compatible-but-distinct
    provider (currently OpenRouter) reuse this function while still getting its
    own circuit-breaker/credit-exhaustion key (instead of silently sharing
    "openai"'s) and its own attribution headers. Every existing call site omits
    both and is unaffected."""
    base = (base_url or OPENAI_BASE_URL).rstrip("/")
    endpoint = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)

    msgs = _to_openai_messages(messages)
    use_stream = False if tools else effective_stream
    payload = {"model": model, "messages": msgs, "stream": use_stream}
    # Explicit max_tokens gives the model room to return a complete JSON object
    # (matches the anthropic path's 8192). Without it, some OpenAI-compatible
    # backends (ollama) truncate mid-object → "Response ended prematurely" and
    # the fix JSON then fails to parse ("unmatched '}'").
    try:
        out_tok = int((config or {}).get("FIX_MAX_OUTPUT_TOKENS", _chat_defaults()["FIX_MAX_OUTPUT_TOKENS"]) or _chat_defaults()["FIX_MAX_OUTPUT_TOKENS"])
    except Exception:
        out_tok = _chat_defaults()["FIX_MAX_OUTPUT_TOKENS"]
    if out_tok > 0:
        payload["max_tokens"] = out_tok
    if tools:
        payload["tools"] = _tools_to_openai(tools)

    resp = _llm_retry_post(endpoint, payload, headers, config, stream=use_stream, provider=provider_name)

    if not use_stream:
        # A non-streamed request (tools always forces this, but a caller can also
        # pass effective_stream=False directly — the provider-diagnostics probe
        # does, for a fast bounded check) gets back ONE JSON object with
        # choices[].message.content, never choices[].delta.content. The SSE loop
        # below only understands delta chunks — parsing a non-streamed response
        # with it silently produced an empty string (surfaced as "provider
        # returned an empty response" in the diag probe for any provider hitting
        # this path with streaming off, e.g. openrouter/groq/lmstudio).
        data = resp.json()
        _usage_from_openai_json(data, usage_out)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if not tools:
            return text
        raw_tcs = msg.get("tool_calls") or None
        tool_calls = None
        if raw_tcs:
            tool_calls = [
                {"id": tc.get("id") or f"call_{i}", "function": {
                    "name": (tc.get("function") or {}).get("name") or "",
                    "arguments": (tc.get("function") or {}).get("arguments") or "{}",
                }}
                for i, tc in enumerate(raw_tcs)
            ]
        return {"text": text, "tool_calls": tool_calls}

    # Streaming without tools: usage is not observed here (would need
    # stream_options={"include_usage":true} in the payload, which some
    # OpenAI-compatible servers 400 on as an unrecognized key — left as a
    # follow-up rather than risking every streaming chat call in this pass).
    # usage_out simply stays unpopulated; a latency sample is still recorded
    # by the caller regardless, so this only costs tok/s coverage, not the
    # exhaustion/ranking signal.
    full_response = ""
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if line.startswith("data: "):
            line = line[6:]
        if line.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(line)
            for ch in chunk.get("choices", []):
                full_response += (ch.get("delta") or {}).get("content") or ""
            main.state["llm_stream"] = full_response
            if task_id and task_id in main.state.get("active_tasks", {}):
                main.state["active_tasks"][task_id]["stream"] = full_response
        except json.JSONDecodeError:
            pass
    return full_response


def _usage_from_openai_json(data, usage_out):
    """Shared by _request_openai and _request_copilot (identical response
    shape). Best-effort: an absent/malformed usage block leaves usage_out
    untouched rather than raising — telemetry must never fail an LLM call."""
    if usage_out is None:
        return
    try:
        usage = data.get("usage") or {}
        out_tok = usage.get("completion_tokens")
        in_tok = usage.get("prompt_tokens")
        if out_tok is not None or in_tok is not None:
            usage_out.update({"output_tokens": out_tok, "input_tokens": in_tok, "source": "api"})
    except Exception:  # noqa: BLE001 — telemetry is never fatal
        pass


def _request_anthropic(model, api_key, base_url, messages, tools, effective_stream, task_id, config, usage_out=None):
    """Call the Anthropic Messages API. Returns text string or tool-call dict."""
    base = (base_url or ANTHROPIC_BASE_URL).rstrip("/")
    endpoint = f"{base}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key or "",
        "anthropic-version": ANTHROPIC_API_VERSION,
    }

    system, msgs = _to_anthropic_messages(messages)
    use_stream = False if tools else effective_stream
    payload = {"model": model, "max_tokens": 8192, "stream": use_stream}
    # ── Prompt caching ────────────────────────────────────────────────────────
    # Mark the stable prefix (system instruction + the leading user context) with
    # cache_control so repeat calls — retries, same-model re-reviews, a reused big
    # fix/diff/log context — bill those cached tokens at ~10%. GA feature (no beta
    # header). SAFE: a breakpoint on a prefix below the model's min cacheable size
    # is silently ignored. Toggle: prompt_caching_enabled (default on).
    if config.get("prompt_caching_enabled", True):
        if system:
            payload["system"] = [{"type": "text", "text": system,
                                  "cache_control": {"type": "ephemeral"}}]
        cached_msgs = [dict(m) for m in msgs]
        for _m in cached_msgs:
            if _m.get("role") == "user":
                _c = _m.get("content")
                if isinstance(_c, str) and _c:
                    _m["content"] = [{"type": "text", "text": _c,
                                      "cache_control": {"type": "ephemeral"}}]
                elif isinstance(_c, list):
                    for _blk in reversed(_c):
                        if isinstance(_blk, dict) and _blk.get("type") == "text":
                            _blk["cache_control"] = {"type": "ephemeral"}
                            break
                break  # cache through the FIRST user message (the stable context)
        payload["messages"] = cached_msgs
    else:
        payload["messages"] = msgs
        if system:
            payload["system"] = system
    if tools:
        payload["tools"] = _tools_to_anthropic(tools)

    resp = _llm_retry_post(endpoint, payload, headers, config, stream=use_stream, provider="anthropic")

    if tools or not use_stream:
        data = resp.json()
        try:
            usage = data.get("usage") or {}
            out_tok = usage.get("output_tokens")
            in_tok = usage.get("input_tokens")
            if usage_out is not None and (out_tok is not None or in_tok is not None):
                usage_out.update({"output_tokens": out_tok, "input_tokens": in_tok, "source": "api"})
        except Exception:  # noqa: BLE001 — telemetry is never fatal
            pass
        content_blocks = data.get("content") or []
        text = ""
        tool_calls = []
        for block in content_blocks:
            if block.get("type") == "text":
                text += block.get("text") or ""
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
                    "function": {"name": block.get("name") or "", "arguments": block.get("input") or {}},
                })
        if tools:
            return {"text": text, "tool_calls": tool_calls or None}
        main.state["llm_stream"] = text
        if task_id and task_id in main.state.get("active_tasks", {}):
            main.state["active_tasks"][task_id]["stream"] = text
        return text

    full_response = ""
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if not line.startswith("data: "):
            continue
        try:
            chunk = json.loads(line[6:])
            full_response += (chunk.get("delta") or {}).get("text") or ""
            # Usage arrives on two different event types, never with the text
            # deltas: message_start carries input_tokens (the prompt side);
            # message_delta carries the running output_tokens total (the
            # generation side) — read both rather than the text-delta events,
            # which never contain a "usage" key at all.
            if usage_out is not None:
                try:
                    ctype = chunk.get("type")
                    if ctype == "message_start":
                        in_tok = ((chunk.get("message") or {}).get("usage") or {}).get("input_tokens")
                        if in_tok is not None:
                            usage_out["input_tokens"] = in_tok
                            usage_out["source"] = "api"
                    elif ctype == "message_delta":
                        out_tok = (chunk.get("usage") or {}).get("output_tokens")
                        if out_tok is not None:
                            usage_out["output_tokens"] = out_tok
                            usage_out["source"] = "api"
                except Exception:  # noqa: BLE001 — telemetry is never fatal
                    pass
            main.state["llm_stream"] = full_response
            if task_id and task_id in main.state.get("active_tasks", {}):
                main.state["active_tasks"][task_id]["stream"] = full_response
        except json.JSONDecodeError:
            pass
    return full_response


def _request_google(model, api_key, base_url, messages, tools, effective_stream, task_id, config, usage_out=None):
    """Call the Google Gemini API. Returns text string or tool-call dict."""
    if not model:
        model = "gemini-1.5-pro"
    base = (base_url or GOOGLE_BASE_URL).rstrip("/")
    endpoint = f"{base}/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    system_text, contents = _to_google_contents(messages)
    payload = {"contents": contents}
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    if tools:
        payload["tools"] = [{"function_declarations": [
            {"name": (fn := _tool_spec(t)).get("name", ""), "description": fn.get("description", ""),
             "parameters": fn.get("parameters", {})}
            for t in tools
        ]}]

    resp = _llm_retry_post(endpoint, payload, headers, config, stream=False, provider="google")
    data = resp.json()
    try:
        usage = data.get("usageMetadata") or {}
        out_tok = usage.get("candidatesTokenCount")
        in_tok = usage.get("promptTokenCount")
        if usage_out is not None and (out_tok is not None or in_tok is not None):
            usage_out.update({"output_tokens": out_tok, "input_tokens": in_tok, "source": "api"})
    except Exception:  # noqa: BLE001 — telemetry is never fatal
        pass
    candidates = data.get("candidates") or []
    parts = ((candidates[0].get("content") or {}) if candidates else {}).get("parts") or []

    text = ""
    tool_calls = []
    for part in parts:
        if "text" in part:
            text += part["text"]
        elif "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append({
                "id": f"call_{fc.get('name', 'fn')}_{uuid.uuid4().hex[:8]}",
                "function": {"name": fc.get("name") or "", "arguments": fc.get("args") or {}},
            })

    main.state["llm_stream"] = text
    if task_id and task_id in main.state.get("active_tasks", {}):
        main.state["active_tasks"][task_id]["stream"] = text

    if tools:
        return {"text": text, "tool_calls": tool_calls or None}
    return text


#: How many recent generations feed the rolling tok/s average, per model. Small
#: enough to track a real change in box load, large enough that one unlucky
#: generation does not swing the number an operator is watching.
_TPS_WINDOW = 20
#: Minimum seconds between cache writes (see _record_ollama_tps).
_TPS_SAVE_INTERVAL = 60
_tps_last_save = 0.0


def _record_ollama_tps(model, payload):
    """Record one generation's tokens/sec from an Ollama response.

    Ollama returns ``eval_count`` (tokens generated) and ``eval_duration``
    (nanoseconds spent generating) on the final object of both the streaming and
    non-streaming paths, so this is free -- no extra call, no timing of our own,
    and no clock skew from network or queueing because the duration covers
    GENERATION ONLY. That is what makes the average "when busy": idle time is
    never part of it.

    Never raises: throughput telemetry must not be able to fail an LLM call.
    """
    try:
        if not isinstance(payload, dict):
            return
        n = payload.get("eval_count")
        ns = payload.get("eval_duration")
        if not n or not ns or ns <= 0:
            return
        tps = float(n) / (float(ns) / 1e9)
        # A nonsensical value means the fields were not what we assumed; drop it
        # rather than poison the average.
        if not (0 < tps < 100000):
            return
        import main
        store = main.state.setdefault("llm_tps", {})
        key = str(model or "unknown")
        samples = store.setdefault(key, [])
        samples.append(round(tps, 2))
        if len(samples) > _TPS_WINDOW:
            del samples[:-_TPS_WINDOW]
        # Persist so the panel is warm after a restart. DEBOUNCED: this runs on
        # every completed generation, and rewriting the file each time would be
        # pointless disk churn for a number that barely moves. At most one write
        # per _TPS_SAVE_INTERVAL means the cache can lag by a few samples, which
        # costs nothing -- it is a warm-start hint, not an audit trail.
        global _tps_last_save
        now = time.time()
        if now - _tps_last_save >= _TPS_SAVE_INTERVAL:
            _tps_last_save = now
            try:
                import config_store
                config_store.save_llm_tps(store)
            except Exception:  # noqa: BLE001 — persistence is best-effort
                pass
    except Exception:  # noqa: BLE001 — telemetry is never fatal
        pass

def _usage_from_ollama_payload(payload, usage_out):
    """Shared by both _request_ollama return paths. Ollama's eval_count
    (tokens generated) / eval_duration (nanoseconds spent generating) is
    server-measured — no network/queue skew, unlike a wall-clock figure —
    the same data _record_ollama_tps already reads for the legacy panel;
    this is the new store's copy of that same read, not a second call."""
    if usage_out is None or not isinstance(payload, dict):
        return
    try:
        eval_count = payload.get("eval_count")
        eval_duration_ns = payload.get("eval_duration")
        if eval_count is not None:
            usage_out["output_tokens"] = eval_count
            usage_out["source"] = "server"
        if eval_duration_ns:
            usage_out["gen_duration_ms"] = eval_duration_ns / 1e6
            usage_out["source"] = "server"
        prompt_eval_count = payload.get("prompt_eval_count")
        if prompt_eval_count is not None:
            usage_out["input_tokens"] = prompt_eval_count
    except Exception:  # noqa: BLE001 — telemetry is never fatal
        pass


def _request_ollama(model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                    provider="ollama", usage_out=None):
    """Call an Ollama-compatible API (local or Ollama Cloud). Uses /api/chat natively.

    ``provider`` only selects the DEFAULT endpoint when no per-entry base_url is
    set (ollama.com for the cloud slot, localhost otherwise); the wire protocol is
    identical either way."""
    base = _ollama_base_url(provider, base_url).rstrip("/")
    endpoint = f"{base}/api/chat"
    headers = {}
    if api_key:
        clean_key = api_key.strip().strip('"').strip("'").replace("Bearer ", "").strip()
        headers["Authorization"] = f"Bearer {clean_key}"

    use_stream = False if tools else effective_stream
    # Ollama defaults num_ctx to ~2048, which the fix prompt (windowed file + issue body
    # + context) blows past → 400 "prompt is longer than the context length". These
    # coder models support 32k+, so raise the window. Configurable via ollama_num_ctx
    # (default 16384 — comfortably fits fix/log prompts; raise for very large ones).
    try:
        _num_ctx = int(config.get("ollama_num_ctx", 32768) or 32768)
    except (TypeError, ValueError):
        _num_ctx = 32768
    _options = {"num_ctx": _num_ctx}
    # CPU thread count. On a CPU box the big models (e.g. 32b) are slow; give ollama more
    # threads (up to physical cores) to speed inference. 0 = let ollama decide (its
    # default is ~physical core count). Configurable via ollama_num_thread.
    try:
        _num_thread = int(config.get("ollama_num_thread", 0) or 0)
    except (TypeError, ValueError):
        _num_thread = 0
    if _num_thread > 0:
        _options["num_thread"] = _num_thread
    payload = {
        "model": model,
        "messages": messages if messages else [{"role": "user", "content": ""}],
        "stream": use_stream,
        "options": _options,
    }
    # Keep the model resident so the ensemble's constant model-switching doesn't
    # reload 10-20GB from disk each swap. -1 = keep loaded forever (default); a Go
    # duration string ("2h") also works. Pair with OLLAMA_MAX_LOADED_MODELS>=3 on the
    # ollama SERVER so all ensemble models can stay in memory at once.
    _ka = str(config.get("ollama_keep_alive", "-1") or "-1").strip()
    payload["keep_alive"] = int(_ka) if _ka.lstrip("-").isdigit() else _ka
    if tools:
        payload["tools"] = tools

    resp = _llm_retry_post(endpoint, payload, headers, config, stream=use_stream, provider="ollama")

    if tools:
        try:
            data = resp.json()
        except Exception as je:
            logger.warning(f"Ollama tool-turn response not JSON: {je}")
            data = {}
        msg = (data.get("message") or {}) if isinstance(data, dict) else {}
        text = msg.get("content") or ""
        raw_tcs = msg.get("tool_calls") or None
        tool_calls = raw_tcs if isinstance(raw_tcs, list) and raw_tcs else None
        _record_ollama_tps(model, data)
        _usage_from_ollama_payload(data, usage_out)
        return {"text": text, "tool_calls": tool_calls}

    full_response = ""
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
            content = chunk.get("message", {}).get("content") or chunk.get("response") or ""
            # The last object of a stream carries done=true plus the eval_*
            # counters for the whole generation; earlier chunks have neither.
            if chunk.get("done"):
                _record_ollama_tps(model, chunk)
                _usage_from_ollama_payload(chunk, usage_out)
            full_response += content
            main.state["llm_stream"] = full_response
            if task_id and task_id in main.state.get("active_tasks", {}):
                main.state["active_tasks"][task_id]["stream"] = full_response
        except json.JSONDecodeError:
            pass
    return full_response


def _request_claude_cli(model, messages, task_id, config, repo_checkout_path=None,
                        json_schema=None, enable_native_tools=False, search_model=None,
                        profile="readonly", extra_add_dirs=None, usage_out=None):
    """Call the local `claude` CLI in non-interactive print mode.

    Uses the Claude Code session auth — no API key required. The claude binary
    must be in PATH.

    By default tool calling is NOT supported (unchanged legacy behavior): the
    full conversation is serialised into a single prompt string, same as
    always — the caller's `tools=` (the generic OpenAI-style function-schema
    parameter every OTHER provider consumes) is simply never wired here,
    because claude_cli has no such API param.

    ``enable_native_tools=True`` opts into a DIFFERENT, claude_cli-specific
    capability instead: the CLI's own REAL built-in tools (Read/Grep/Glob, plus
    a narrow git-history Bash allowlist), restricted to read-only exploration
    (see claude_cli_native_tools.ALLOWED_TOOLS/DISALLOWED_TOOLS) and scoped to
    ``repo_checkout_path`` via --add-dir (a real checkout the CLI can actually
    read — pass one or this degrades to no useful file access). A cheap-model
    search subagent (--agents, default haiku) handles the mechanical grep/
    file-hunting legwork so the (usually pricier) top-level `model` only
    spends tokens on judgment, not searching — mirrors how this session
    itself delegates exploration to lightweight agents.
    ``profile`` (default "readonly") selects WHICH allow/deny lists back
    ``enable_native_tools`` — see claude_cli_native_tools's module docstring.
    "build" adds Edit/Write/Skill and is used by exactly one caller,
    feature_build.py's agentic feature builder; every other caller must
    leave this at its default so the read-only reviewer guarantee holds.
    --permission-mode bypassPermissions is required for headless operation
    (no human to answer a tool-approval prompt); the allow/deny lists above
    are what actually keeps this safe, not the permission mode.

    ``json_schema`` (a dict or pre-serialized JSON string), when given, is
    passed as --json-schema — the CLI validates + returns a pre-parsed
    ``structured_output`` object, which this function then re-serializes as
    the returned string instead of the freeform ``result`` text. This is
    ALSO the fix for claude_cli's "JSON parse failed (Extra data: ...)"
    class of error: the caller's downstream `json.loads()` was tripping over
    stray prose/markdown fences around a freeform response; a schema-
    validated result has none of that by construction.
    """
    import subprocess

    # Build a plain-text conversation from the messages list.
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    conv_lines = []
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        prefix = "Human" if role == "user" else "Assistant"
        conv_lines.append(f"{prefix}: {content}")

    prompt = "\n\n".join(conv_lines)
    if system_parts:
        prompt = system_parts[0] + "\n\n" + prompt

    # Pass the prompt via stdin to avoid OS ARG_MAX limits on large conversations.
    cmd = claude_cli_native_tools.build_command(
        claude_bin_or_raise(config), model=model, repo_checkout_path=repo_checkout_path,
        json_schema=json_schema, enable_native_tools=enable_native_tools,
        search_model=search_model, profile=profile, extra_add_dirs=extra_add_dirs)

    timeout_val = int(config.get("LLM_TIMEOUT", 900))
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout_val,
            cwd=repo_checkout_path if (enable_native_tools and repo_checkout_path) else None,
        )
        output = proc.stdout.strip()
        stderr = proc.stderr.strip()

        # Parse JSON response if possible.
        try:
            data = json.loads(output)
            # A schema-validated call returns structured_output pre-parsed —
            # re-serialize IT (guaranteed clean JSON) rather than trusting
            # the freeform `result` text, which is where "Extra data" parse
            # failures came from (stray prose/markdown around the JSON).
            text = claude_cli_native_tools.extract_text(data, json_schema=json_schema) or output
            # The CLI's --output-format json envelope carries its own
            # server-measured usage/timing (usage.input_tokens/output_tokens,
            # duration_ms covering the whole invocation, duration_api_ms the
            # API portion alone) — never read before this. Best-effort: an
            # envelope shape without these keys (or from a claude CLI
            # version that changed field names) just leaves usage_out empty.
            if usage_out is not None:
                try:
                    _usage = data.get("usage") or {}
                    out_tok = _usage.get("output_tokens")
                    in_tok = _usage.get("input_tokens")
                    if out_tok is not None or in_tok is not None:
                        usage_out.update({"output_tokens": out_tok, "input_tokens": in_tok, "source": "server"})
                    _dur = data.get("duration_api_ms", data.get("duration_ms"))
                    if _dur is not None:
                        usage_out["gen_duration_ms"] = _dur
                except Exception:  # noqa: BLE001 — telemetry is never fatal
                    pass
            combined_text = (text or "") + " " + stderr
            # Session-limit is NOT an auth failure — the CLI is authenticated but
            # has exhausted its per-session quota.  Raise a rate-limit style error
            # so _try_provider applies a short cooldown rather than demanding re-login.
            if "session limit" in combined_text.lower() or "resets " in combined_text.lower():
                import re as _re
                resets_match = _re.search(r"resets (.+?)[\s·]", combined_text, _re.IGNORECASE)
                resets_at = resets_match.group(1).strip() if resets_match else "soon"
                raise Exception(f"claude_cli_rate_limit:Session limit reached (resets {resets_at})")
            # Detect auth failure from the JSON payload.
            if data.get("is_error") or ("Not logged in" in (text or "") or "/login" in (text or "")):
                raise Exception(
                    f"Claude CLI not authenticated on this server. "
                    f"Go to Settings → LLM Vault → claude_cli → Get Auth URL to log in. "
                    f"Raw: {text[:200]}"
                )
        except json.JSONDecodeError:
            text = output or stderr
            if "session limit" in text.lower():
                raise Exception(f"claude_cli_rate_limit:Session limit reached")

        if proc.returncode != 0:
            if "Not logged in" in (output + stderr) or "/login" in (output + stderr):
                raise Exception(
                    "Claude CLI not authenticated. Go to Settings → LLM Vault → claude_cli → Get Auth URL."
                )
            raise Exception(f"claude CLI exited {proc.returncode}: {stderr[:300]}")

        main.state["llm_stream"] = text
        if task_id and task_id in main.state.get("active_tasks", {}):
            main.state["active_tasks"][task_id]["stream"] = text
        return text
    except subprocess.TimeoutExpired:
        raise Exception(f"claude CLI timed out after {timeout_val}s")
    except FileNotFoundError:
        # Reached only if the resolved path vanished between resolve and exec.
        raise Exception("'claude' binary disappeared after resolution — reinstall Claude Code "
                        "on this server, or set 'claude_binary' in Settings.")


def _request_copilot(model, api_key, base_url, messages, tools, effective_stream, task_id, config, usage_out=None):
    """Call the GitHub Copilot chat API (OpenAI-compatible). api_key is the stored GitHub
    OAuth token; we exchange it for a short-lived Copilot token and add the editor headers
    Copilot requires. Mirrors _request_openai's response handling."""
    bearer = _copilot_api_token(api_key)  # gh_token -> copilot token (cached)
    base = (base_url or COPILOT_API_BASE).rstrip("/")
    endpoint = f"{base}/chat/completions"
    headers = _copilot_headers(bearer)
    msgs = _to_openai_messages(messages)
    use_stream = False if tools else effective_stream
    payload = {"model": model, "messages": msgs, "stream": use_stream}
    try:
        out_tok = int((config or {}).get("FIX_MAX_OUTPUT_TOKENS", _chat_defaults()["FIX_MAX_OUTPUT_TOKENS"]) or _chat_defaults()["FIX_MAX_OUTPUT_TOKENS"])
    except Exception:
        out_tok = _chat_defaults()["FIX_MAX_OUTPUT_TOKENS"]
    if out_tok > 0:
        payload["max_tokens"] = out_tok
    if tools:
        payload["tools"] = _tools_to_openai(tools)
    resp = _llm_retry_post(endpoint, payload, headers, config, stream=use_stream, provider="copilot")
    if not use_stream:
        # See _request_openai's matching branch: a non-streamed response is ONE
        # JSON object with choices[].message.content, not delta chunks — the SSE
        # loop below silently returned "" for any tools=None + effective_stream=
        # False call (e.g. the provider diagnostics probe).
        data = resp.json()
        _usage_from_openai_json(data, usage_out)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        if not tools:
            return text
        raw_tcs = msg.get("tool_calls") or None
        tool_calls = None
        if raw_tcs:
            tool_calls = [
                {"id": tc.get("id") or f"call_{i}", "function": {
                    "name": (tc.get("function") or {}).get("name") or "",
                    "arguments": (tc.get("function") or {}).get("arguments") or "{}",
                }}
                for i, tc in enumerate(raw_tcs)
            ]
        return {"text": text, "tool_calls": tool_calls}
    full_response = ""
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8") if isinstance(line, bytes) else line
        if line.startswith("data: "):
            line = line[6:]
        if line.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(line)
            for ch in chunk.get("choices", []):
                full_response += (ch.get("delta") or {}).get("content") or ""
            main.state["llm_stream"] = full_response
            if task_id and task_id in main.state.get("active_tasks", {}):
                main.state["active_tasks"][task_id]["stream"] = full_response
        except json.JSONDecodeError:
            pass
    return full_response


def _call_provider(provider, model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                   repo_checkout_path=None, json_schema=None, enable_native_tools=False, search_model=None,
                   profile="readonly", extra_add_dirs=None, usage_out=None):
    """Dispatch to the correct provider implementation. The last 6 kwargs
    (before usage_out) are claude_cli-specific (see _request_claude_cli's
    docstring) — every other provider ignores them; they are not the generic
    `tools=` param.

    ``usage_out``, when given a dict, is populated IN PLACE by whichever
    `_request_*` function runs — {"output_tokens", "input_tokens", "source":
    "server"|"api", "gen_duration_ms"} — never via a return-value change (a
    uniform envelope return would ripple into every one of the 18 call
    sites' unpacking logic; a mutable out-param does not). Unlike a
    contextvar, a plain dict argument survives `asyncio.to_thread` (which
    copies context but not object references), so hub_agent.py's proxy path
    is covered too. Population is always best-effort — a provider whose
    response doesn't carry the fields it expects leaves usage_out untouched
    rather than raising; see each _request_*'s usage-capture comment."""
    p = (provider or "openai").lower().strip()
    if _is_copilot(p):
        return _request_copilot(model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                                usage_out=usage_out)
    if p == "anthropic":
        return _request_anthropic(model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                                  usage_out=usage_out)
    if p == "google":
        return _request_google(model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                               usage_out=usage_out)
    if _is_ollama(p):
        return _request_ollama(model, api_key, base_url, messages, tools, effective_stream, task_id,
                               config, provider=p, usage_out=usage_out)
    if p == "groq":
        effective_url = base_url or "https://api.groq.com/openai/v1"
        return _request_openai(model, api_key, effective_url, messages, tools, effective_stream, task_id, config,
                               usage_out=usage_out)
    if p == "openrouter":
        effective_url = base_url or OPENROUTER_BASE_URL
        return _request_openai(model, api_key, effective_url, messages, tools, effective_stream, task_id, config,
                               provider_name="openrouter", extra_headers=OPENROUTER_HEADERS, usage_out=usage_out)
    if _is_lmstudio(p):
        # LM Studio exposes an OpenAI-compatible API; no auth key required.
        effective_url = _normalize_lmstudio_url(base_url)
        return _request_openai(model, api_key, effective_url, messages, tools, effective_stream, task_id, config,
                               usage_out=usage_out)
    if p == "claude_cli":
        return _request_claude_cli(model, messages, task_id, config,
                                   repo_checkout_path=repo_checkout_path, json_schema=json_schema,
                                   enable_native_tools=enable_native_tools, search_model=search_model,
                                   profile=profile, extra_add_dirs=extra_add_dirs, usage_out=usage_out)
    return _request_openai(model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                           usage_out=usage_out)


# ============================================================================
# Performance telemetry (llm_perf.py-backed) — the future model picker's
# ranking/exhaustion signal. Recorded here, at the SAME choke point every
# provider already funnels through (_call_provider), so claude_cli
# (subprocess, never touches the HTTP layer) is covered exactly like every
# HTTP-based provider — instrumenting any lower layer would leave the single
# most expensive provider permanently unmeasured.
# ============================================================================
_LLM_PERF_STORE = None
_LLM_PERF_LOCK = threading.Lock()
_llm_perf_last_save = 0.0
_LLM_PERF_SAVE_INTERVAL = 60  # debounced, same rationale as the legacy _TPS_SAVE_INTERVAL


def _get_llm_perf_store():
    global _LLM_PERF_STORE
    if _LLM_PERF_STORE is None:
        with _LLM_PERF_LOCK:
            if _LLM_PERF_STORE is None:
                _LLM_PERF_STORE = llm_perf.load(config_store.LLM_PERF_FILE)
    return _LLM_PERF_STORE


def get_llm_perf_snapshot():
    """{ModelKey: {"n", "tps", "latency_ms"}} — the read side model_selection's
    select_model() consumes as its `perf` argument (wired in a later phase)."""
    return llm_perf.snapshot(_get_llm_perf_store())


def _model_key(provider, base_url, model):
    return ((provider or "").lower().strip(), (base_url or "").strip().rstrip("/"), model or "")


def _call_provider_timed(provider, model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                         **kwargs):
    """Wraps _call_provider with wire-latency timing + usage capture, then
    records one llm_perf sample on success. Every _try_provider call site
    (main attempt, tools-retry, routed-404-retry) routes through here rather
    than calling _call_provider directly, so a sample lands regardless of
    which retry branch actually wins.

    On failure this re-raises unchanged and records nothing — a failed call
    has no latency signal worth ranking on, and _try_provider's existing
    credit/rate-limit bookkeeping already covers that case."""
    key = _model_key(provider, base_url, model)
    usage_out = {}
    t0 = time.monotonic()
    result = _call_provider(provider, model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                            usage_out=usage_out, **kwargs)
    latency_ms_wire = (time.monotonic() - t0) * 1000.0
    try:
        out_tok = usage_out.get("output_tokens")
        gen_ms = usage_out.get("gen_duration_ms")
        # Prefer the server-measured generation-only duration (ollama/
        # claude_cli) when available — it excludes network/queue time, so
        # it's the more honest tok/s denominator; wall-clock wire latency is
        # the fallback for every provider that only reports total tokens.
        duration_ms = gen_ms if gen_ms else latency_ms_wire
        tps = (out_tok / (duration_ms / 1000.0)) if (out_tok and duration_ms) else None
        source = usage_out.get("source", "api")
        store = _get_llm_perf_store()  # its own lock guards only the lazy-load, released before here
        with _LLM_PERF_LOCK:
            llm_perf.record(store, key, latency_ms_wire, tps=tps, source=source)
            global _llm_perf_last_save
            now = time.time()
            if now - _llm_perf_last_save >= _LLM_PERF_SAVE_INTERVAL:
                _llm_perf_last_save = now
                llm_perf.save(config_store.LLM_PERF_FILE, store)
    except Exception:  # noqa: BLE001 — telemetry must never fail an LLM call
        pass
    return result


# ============================================================================
# requirements= path (LLM Selection Redesign, Phase 4) — model_selection's
# select_model() picks ONE candidate; this section is the impure boundary
# around it (config reads, live env vars, capability resolution, credit/rate
# cooldowns) plus the identity-keyed circuit breakers/locks that replace the
# slot-keyed ones for this path specifically. Coexists with the slot/
# task_kind machinery below during the migration — see the plan's build
# sequencing: every one of the 18 call sites still uses task_kind today;
# they convert to requirements= one at a time in a later phase, and only
# once none remain does the old machinery get deleted.
# ============================================================================

def _endpoint_key(provider, base_url):
    return ((provider or "").lower().strip(), (base_url or "").strip().rstrip("/"))


# Two identity-keyed cooldown maps, split by scope per the plan: credit
# exhaustion is ACCOUNT-wide (running out of Anthropic credit kills every
# Anthropic model on that account — a per-model key would retry 5 models
# against the same wall), so it's keyed by (provider, base_url). A rate limit
# can be per-model on some providers, so it's keyed by the full ModelKey.
# Unlike the slot-keyed _PROVIDER_CREDIT_CB, neither needs "reassignment"
# invalidation logic — the key IS the identity here, it can't be reassigned.
_ENDPOINT_CB_LOCK = threading.Lock()
_ENDPOINT_CREDIT_CB = {}
_MODEL_RATE_CB = {}


def _cb_trip(cb_dict, cb_key, reason, duration_s, cause, provider):
    if cause == "credit" and provider and _provider_is_nokey(provider):
        logger.warning(
            "Ignoring CREDIT cooldown for %s (%s): a local/no-key provider has no "
            "billing to exhaust. Reason was: %s", cb_key, provider, str(reason)[:160])
        return
    secs = duration_s if duration_s is not None else _CREDIT_COOLDOWN_SECONDS
    cd = time.time() + secs
    with _ENDPOINT_CB_LOCK:
        cb_dict[cb_key] = {"cooldown_until": cd, "tripped_at": datetime.now().isoformat(),
                           "reason": reason, "cause": cause}
    until_str = datetime.fromtimestamp(cd).strftime("%H:%M:%S")
    label = "RATE-LIMITED" if cause == "rate_limit" else "CREDIT EXHAUSTED"
    logger.warning("%s %s — pausing for %s min (until ~%s). Reason: %s",
                   cb_key, label, secs // 60, until_str, reason)


def _cb_remaining(cb_dict, cb_key):
    with _ENDPOINT_CB_LOCK:
        entry = cb_dict.get(cb_key)
        if not entry:
            return 0.0
        return max(0.0, entry["cooldown_until"] - time.time())


_MODEL_LOCKS_LOCK = threading.Lock()
_MODEL_LOCKS = {}


def _model_lock(key):
    """Lazily-created per-ModelKey lock — layer 3 of the plan's 3-layer
    concurrency design (global/per-endpoint semaphore is layer 1/2, reused
    from the existing per-category semaphore under a dedicated "PICKER"
    category below rather than building new untested sizing logic for a
    path with zero real traffic yet)."""
    with _MODEL_LOCKS_LOCK:
        lock = _MODEL_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _MODEL_LOCKS[key] = lock
        return lock


def _iter_configured_endpoints(config):
    """Every endpoint BugFixer knows about, read LIVE on every call (never
    frozen/persisted): config["llm_entries"] (the modern vault) plus the
    legacy LLM_PROVIDER_N/LLM_API_KEY_N/LLM_MODEL_N/LLM_BASE_URL_N/LLM_RPM_N
    env-var slots — re-reading live (rather than one-shot converting) means
    a container configured purely via env vars keeps working after every
    restart, not just until the first one (confirmed with the user during
    planning). Yields (entry_id, provider, api_key, model, base_url, rpm)
    regardless of whether the endpoint is actually usable — callers decide
    what "usable" means for their purpose (_enumerate_candidates filters to
    configured+available; _configured_entries filters to configured only,
    for the safety floor)."""
    credentials = config.get("llm_credentials") or {}
    for entry in (config.get("llm_entries") or []):
        if entry.get("enabled") is False:
            continue
        provider = (entry.get("provider") or "openai").lower().strip()
        model = (entry.get("model") or "").strip()
        cred = credentials.get(provider) or {}
        api_key = (entry.get("api_key") or cred.get("api_key") or "").strip()
        base_url = (entry.get("base_url") or cred.get("base_url") or "").strip()
        rpm = int(entry.get("rpm") or 0)
        yield entry.get("id"), provider, api_key, model, base_url, rpm

    for n in _ALL_SLOTS:
        provider = (config.get(f"LLM_PROVIDER_{n}") or os.getenv(f"LLM_PROVIDER_{n}", "")).lower().strip()
        if not provider:
            continue
        api_key = (config.get(f"LLM_API_KEY_{n}") or os.getenv(f"LLM_API_KEY_{n}", "")).strip()
        model = (config.get(f"LLM_MODEL_{n}") or os.getenv(f"LLM_MODEL_{n}", "")).strip()
        base_url = (config.get(f"LLM_BASE_URL_{n}") or os.getenv(f"LLM_BASE_URL_{n}", "")).strip()
        rpm = int(config.get(f"LLM_RPM_{n}") or os.getenv(f"LLM_RPM_{n}", "0") or 0)
        yield f"legacy_{n}", provider, api_key, model, base_url, rpm


def _enumerate_candidates(config):
    """Every USABLE endpoint as plain dicts for model_selection.select_model
    — the impure boundary the redesign plan calls for: config reads, live
    env vars, capability resolution (model_registry.resolve), and credit/
    rate/dead-model checks all happen HERE, before the pure selection logic
    (model_selection.py, no I/O at all) ever runs. Deduplicated by ModelKey
    — the same (provider, base_url, model) reachable via both an llm_entries
    row and a legacy env var is one candidate, not two."""
    candidates = []
    seen_keys = set()
    for entry_id, provider, api_key, model, base_url, rpm in _iter_configured_endpoints(config):
        if not _provider_configured(provider, api_key, model):
            continue
        key = _model_key(provider, base_url, model)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        caps = model_registry.resolve(provider, model, config)
        available, unavailable_reason = True, None
        if _cb_remaining(_ENDPOINT_CREDIT_CB, _endpoint_key(provider, base_url)) > 0:
            available, unavailable_reason = False, "credit_cooldown"
        elif _cb_remaining(_MODEL_RATE_CB, key) > 0:
            available, unavailable_reason = False, "rate_limited"
        elif _routed_model_dead(provider, model):
            available, unavailable_reason = False, "dead_model"
        candidates.append({
            "key": key, "provider": provider, "model": model, "base_url": base_url,
            "api_key": api_key, "rpm": rpm, "caps": caps,
            "available": available, "unavailable_reason": unavailable_reason,
        })
    return candidates


def _configured_entries(config):
    """Every configured endpoint, NOT gated by cooldown/capability — feeds
    model_selection.safety_floor(), the deliberate last resort that's tried
    even when everything select_model considered has been ruled out (a rule,
    never a hardcoded model ID — see model_registry.py's module docstring
    for the incident that makes a pinned ID the wrong answer here)."""
    return [
        {"id": entry_id, "provider": provider, "model": model, "api_key": api_key,
         "base_url": base_url, "rpm": rpm, "_configured": _provider_configured(provider, api_key, model)}
        for entry_id, provider, api_key, model, base_url, rpm in _iter_configured_endpoints(config)
    ]


def _try_candidate(candidate, messages, tools, effective_stream, task_id, config, **kwargs):
    """The requirements= path's per-candidate attempt — the identity-keyed
    counterpart to _try_provider below. Deliberately a conservative subset
    of _try_provider's error handling for this first phase: credit
    exhaustion, rate-limit cooldown (incl. claude_cli session limits) are
    covered (nothing here can leave a dead endpoint retried forever); the
    provider-specific message-rewriting branches (ollama 404/403 detail,
    tool-calling-400 retry-without-tools, routed-model retry) stay on the
    slot-based path for now and can be ported here as a later phase converts
    call sites that actually need them."""
    provider, model, base_url, api_key = (candidate["provider"], candidate["model"],
                                          candidate["base_url"], candidate["api_key"])
    mk = candidate["key"]
    with _model_lock(mk):
        try:
            main.state["active_llm"] = model
            main.state["active_llm_slot"] = "picker"
            main.state["active_llm_provider"] = provider
            main.state["active_llm_at"] = time.time()
            result = _call_provider_timed(provider, model, api_key, base_url, messages, tools, effective_stream,
                                          task_id, config, **kwargs)
            return result, None
        except LLMCreditExhausted as ce:
            _cb_trip(_ENDPOINT_CREDIT_CB, _endpoint_key(provider, base_url), str(ce), None, "credit", provider)
            return None, "credit_exhausted"
        except Exception as e:
            err_str = str(e)
            if err_str.startswith("claude_cli_rate_limit:"):
                reason = err_str[len("claude_cli_rate_limit:"):]
                _cb_trip(_MODEL_RATE_CB, mk, reason, 900, "rate_limit", provider)
                return None, "rate_limited"
            if "429" in err_str:
                _cb_trip(_MODEL_RATE_CB, mk, f"Rate-limited: {err_str[:120]}",
                         _RATELIMIT_COOLDOWN_SECONDS, "rate_limit", provider)
                return None, "rate_limited"
            return None, e


def _call_llm_with_requirements(reqs, prompt, system_prompt, messages, tools, stream, task_id, config,
                                repo_checkout_path=None, json_schema=None, enable_native_tools=False,
                                search_model=None, profile="readonly", extra_add_dirs=None,
                                batch_kind=None, batch_context=None, used_model_out=None):
    """call_llm's requirements= branch. select_model() ranks every candidate;
    on failure the caller walks the Selection's `alternatives` (the rest of
    that ranked list — no re-invoking the picker mid-failover), then falls
    to the rule-based safety floor, before giving up. Fully independent of
    the slot/task_kind machinery in call_llm proper.

    batch_kind/batch_context: consumed here (not by model_selection —
    LlmRequirements.batch_ok is deliberately just a hint field, per its own
    docstring) when reqs.batch_ok is set. Routes the winning candidate through
    batch.py's fire-and-forget enqueue()/register_handler() path instead of a
    synchronous call — the caller gets "" back immediately (same contract as
    "the LLM yielded nothing"), and whatever register_handler(batch_kind, fn)
    was registered runs later, whenever the batch worker's poll picks up the
    result (minutes to hours). Only pr_summary (the one call site with
    batch_ok=True today) is genuinely fire-and-forget/discardable; every other
    batch-ELIGIBLE call site stays synchronous on purpose (parking a worker
    thread for up to batch_sync_max_wait_s, today's OTHER batch integration in
    _try_provider via batch.run_batched, is a real availability cost most
    callers can't absorb).

    used_model_out: an optional dict, populated IN PLACE (mirrors the
    usage_out= convention above) with the winning candidate's identity
    ({"key", "provider", "model", "base_url"}) once a candidate actually
    succeeds. Lets a caller that needs to know WHICH model built THIS
    particular result — e.g. fix_engine's escalation ladder excluding the
    just-used model from both the next attempt and the reviewer panel —
    learn that without call_llm's return-value contract changing for every
    other caller."""
    effective_stream = True if stream is None else bool(stream)
    _explicit_messages = messages is not None
    if messages is None:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    candidates = _enumerate_candidates(config)
    perf = get_llm_perf_snapshot()
    tuning = {}
    if config.get("model_slow_factor") is not None:
        tuning["slow_factor"] = config.get("model_slow_factor")
    if config.get("model_min_samples") is not None:
        tuning["min_samples"] = config.get("model_min_samples")
    selection = model_selection.select_model(reqs, candidates, perf, tuning)

    if selection is not None:
        winning = next((c for c in candidates if c["key"] == selection.key), None)
        chain = ([winning] if winning else []) + list(selection.alternatives or [])
    else:
        if reqs.must_escalate_to_human:
            raise LlmHumanEscalationNeeded(
                f"No candidate satisfies requirements (reqs={reqs!r}) — the caller opted "
                "into must_escalate_to_human instead of the rule-based safety floor.")
        floor_entry = model_selection.safety_floor(_configured_entries(config))
        if floor_entry is None:
            raise Exception("No LLM providers configured")
        logger.warning("select_model resolved nothing for this call (reqs=%r) — falling to the safety "
                       "floor: %s / %s", reqs, floor_entry["provider"], floor_entry["model"])
        chain = [{
            "key": _model_key(floor_entry["provider"], floor_entry.get("base_url", ""), floor_entry["model"]),
            "provider": floor_entry["provider"], "model": floor_entry["model"],
            "api_key": floor_entry.get("api_key", ""), "base_url": floor_entry.get("base_url", ""),
            "rpm": floor_entry.get("rpm", 0),
        }]

    if not chain:
        raise Exception("No LLM providers configured")

    # Fire-and-forget batch routing (see docstring). Only attempted when the
    # caller both asked for it (reqs.batch_ok) and gave us somewhere to send
    # the eventual result (batch_kind) — never for a bare messages=[...] call,
    # since batch.enqueue needs a plain system/user string pair, not an
    # arbitrary message list.
    if (reqs.batch_ok and batch_kind and not _explicit_messages and not tools
            and config.get("batch_enabled", False)):
        top = chain[0]
        if (top.get("provider") or "").lower().strip() in ("anthropic", "google", "gemini"):
            try:
                from batch import enqueue as _batch_enqueue
                _batch_enqueue(batch_kind, batch_context or {}, top["provider"], top["model"],
                               system_prompt, prompt)
                logger.info("call_llm: queued %s via batch API (provider=%s model=%s)",
                           batch_kind, top["provider"], top["model"])
                return ""
            except Exception as e:  # noqa: BLE001
                logger.debug("call_llm: batch enqueue failed (%s) — falling back to a synchronous call", e)

    kwargs = dict(repo_checkout_path=repo_checkout_path, json_schema=json_schema,
                  enable_native_tools=enable_native_tools, search_model=search_model,
                  profile=profile, extra_add_dirs=extra_add_dirs)

    sem = _get_category_semaphore("PICKER")
    sem.acquire()
    try:
        last_err = None
        for candidate in chain:
            result, err = _try_candidate(candidate, messages, tools, effective_stream, task_id, config, **kwargs)
            if err is None:
                if used_model_out is not None:
                    used_model_out.update({"key": candidate["key"], "provider": candidate["provider"],
                                           "model": candidate["model"], "base_url": candidate["base_url"]})
                return result
            last_err = err
        raise Exception(f"All LLM candidates failed. Last error: {last_err}")
    finally:
        sem.release()


# Shared LLM Utility
def call_llm(prompt, system_prompt="You are a helpful AI assistant.", task_id=None, messages=None, tools=None, stream=None,
             repo_checkout_path=None, json_schema=None, enable_native_tools=False, search_model=None, profile="readonly",
             extra_add_dirs=None, requirements=None, batch_kind=None, batch_context=None, used_model_out=None):
    """Generic LLM caller. Routing is capability/cost-aware: the required
    ``requirements=<LlmRequirements>`` describes what the call needs (complexity,
    context size, structured output, restrict/exclude, escalation) and
    model_selection.select_model picks the cheapest capable candidate, with
    per-endpoint credit / per-model rate-limit awareness and identity-keyed
    failover — see _call_llm_with_requirements.

      batch_kind=/batch_context=      — only consulted when requirements.batch_ok
                                        is True (see _call_llm_with_requirements):
                                        routes the call through batch.py's
                                        fire-and-forget enqueue()/
                                        register_handler(batch_kind, fn) path
                                        instead of a synchronous call, returning
                                        "" immediately.
      used_model_out=                — when given a dict, is populated in place
                                        with the winning candidate's identity.

    ``repo_checkout_path``/``json_schema``/``enable_native_tools``/``search_model``
    are claude_cli-specific (see _request_claude_cli's docstring) — every other
    provider silently ignores them. Distinct from the generic ``tools=`` param
    (an OpenAI-style function-schema every OTHER provider consumes; claude_cli
    has no such API param).

    Endpoints in a 1-hour credit-exhaustion cooldown are skipped automatically.
    Concurrency: LLM_MAX_CONCURRENT gates per selection category; a per-model
    lock serialises jobs against the same model.
    """
    config = load_config()
    if requirements is None:
        raise ValueError("call_llm requires a requirements=LlmRequirements (the "
                         "capability/cost-aware picker path); legacy slot routing "
                         "was retired in the LLM Selection Redesign.")
    return _call_llm_with_requirements(requirements, prompt, system_prompt, messages, tools, stream, task_id,
                                       config, repo_checkout_path=repo_checkout_path, json_schema=json_schema,
                                       enable_native_tools=enable_native_tools, search_model=search_model,
                                       profile=profile, extra_add_dirs=extra_add_dirs,
                                       batch_kind=batch_kind, batch_context=batch_context,
                                       used_model_out=used_model_out)


def is_llm_cooldown_error(e) -> bool:
    """True if this exception is the 'every LLM provider is cooling down / rate-
    limited / out of credit' transient (raised by call_llm) — an EXPECTED deferral,
    not a fault. Callers that wrap call_llm use it to log at WARNING instead of
    ERROR so a routine billing cooldown doesn't read as a real failure."""
    s = str(e).lower()
    return any(k in s for k in ("credit_cooldown", "credit_exhausted",
                                "rate_limited", "providers cooling down"))


LOG_ANALYSIS_SYSTEM_PROMPT = (
    "You are a senior SRE reading application logs for the user. Be concise, "
    "specific, and calm. Do not invent problems that aren't in the logs."
)
_LOG_ANALYSIS_MAX_CHARS = 16000


def analyze_logs(log_text, title="logs", task_id=None, requirements=None):
    """Ask the configured LLM whether anything is wrong in `log_text`, what it means,
    and what to check — the shared brain behind BugFixer's Log Analysis panel AND the
    LM hub's delegated ANALYZE_LOGS request. Returns the analysis string, which BEGINS
    with a machine-parseable `VERDICT: none|watch|escalate` line (see parse_log_verdict).
    Raises on LLM failure so callers can classify cooldown vs. error. Char-caps the tail.

    requirements=<LlmRequirements> is caller-supplied (LLM Selection Redesign Phase 5,
    site #17) rather than a single baked-in profile, because this function's two
    callers have opposite latency needs: routes.py's Log Analysis panel has a human
    watching (latency_sensitive=True), while hub_agent.py's delegated ANALYZE_LOGS
    request runs off the event loop with no one blocking on it. Defaults to a plain
    complexity="small" profile (latency_sensitive=False) if the caller doesn't pass
    one, so this never regresses to raising on a missing param."""
    text = log_text or ""
    if len(text) > _LOG_ANALYSIS_MAX_CHARS:
        text = text[-_LOG_ANALYSIS_MAX_CHARS:]
    prompt = (
        f"These are the most recent {title}. Analyze them.\n\n"
        "Begin your reply with EXACTLY one of these lines (nothing before it):\n"
        "  VERDICT: none      — logs look healthy, no action needed\n"
        "  VERDICT: watch     — a minor issue worth noting, but no fix needed yet\n"
        "  VERDICT: escalate  — a real problem (ERROR-class, repeated, or novel) that "
        "should be investigated/fixed\n\n"
        "Then, on the following lines, answer:\n"
        "1. **Is there a problem?** yes/no up front.\n"
        "2. **If yes:** what is going wrong, in plain language — what it most likely "
        "means / what to check next. Quote the key log line(s) verbatim.\n"
        "3. **If healthy:** say so in one line and note anything minor worth watching.\n\n"
        "Prefer WARNING/ERROR/traceback lines. Reserve `escalate` for genuine faults, "
        "not routine warnings. Keep it under ~250 words.\n\n"
        f"----- LOGS -----\n{text}\n----- END LOGS -----"
    )
    if requirements is None:
        requirements = model_selection.LlmRequirements(complexity="small",
                                                        min_context_tokens=len(prompt) // 4)
    result = call_llm(prompt, system_prompt=LOG_ANALYSIS_SYSTEM_PROMPT, task_id=task_id,
                      requirements=requirements)
    return (result or "").strip() or "VERDICT: none\n(the LLM returned an empty analysis)"


_LOG_VERDICT_RE = re.compile(r"VERDICT:\s*(none|watch|escalate)\b", re.IGNORECASE)


def parse_log_verdict(text):
    """Split an analyze_logs result into (verdict, cleaned_text). verdict is one of
    'none'|'watch'|'escalate' (defaults to 'none' if the model omitted the line).
    cleaned_text has the VERDICT line stripped for display."""
    s = text or ""
    m = _LOG_VERDICT_RE.search(s)
    verdict = m.group(1).lower() if m else "none"
    cleaned = _LOG_VERDICT_RE.sub("", s, count=1).strip() if m else s.strip()
    return verdict, cleaned


# Named requirement presets for the dry-run picker — grounded in the real
# LlmRequirements built at the pipeline's call sites (feature_build, fix_engine,
# chat, log_scan, github_ops, pr_review) so the diagnostic audits the routing
# operators actually get, not a synthetic one. Each is (label, description, kwargs).
_DIAG_PRESETS = [
    ("triage", "Issue triage / file identification (trivial, structured JSON)",
     dict(complexity="trivial", needs_structured_output=True)),
    ("log_scan", "Log / hub-log analysis (small, structured JSON)",
     dict(complexity="small", needs_structured_output=True)),
    ("fix_small", "Build a small fix (small, structured JSON)",
     dict(complexity="small", needs_structured_output=True)),
    ("fix_large", "Build a hard fix (large, structured JSON)",
     dict(complexity="large", needs_structured_output=True)),
    ("review", "Skeptical fix-review panel (medium, structured JSON)",
     dict(complexity="medium", needs_structured_output=True)),
    ("chat", "Dashboard chat reply (small, latency-sensitive)",
     dict(complexity="small", latency_sensitive=True)),
    ("chat_tools", "Chat with tool calling (medium, tools, latency-sensitive)",
     dict(complexity="medium", needs_tools=True, latency_sensitive=True)),
    ("feature_build", "Feature auto-build (large, mutating agent)",
     dict(complexity="large", needs_mutating_agent=True)),
    ("batch_summary", "PR-summary batch route (trivial, batch-eligible)",
     dict(complexity="trivial", batch_ok=True)),
]


def _diag_reqs(kwargs, overrides=None):
    """Build an LlmRequirements from a preset's kwargs, applying any UI
    overrides (only known field names, coerced to the field's type)."""
    merged = dict(kwargs)
    for k, v in (overrides or {}).items():
        if not hasattr(model_selection.LlmRequirements, k):
            continue
        if k in ("complexity", "restrict"):
            merged[k] = v or None if k == "restrict" else (v or "small")
        elif k in ("min_context_tokens",):
            try:
                merged[k] = int(v)
            except (TypeError, ValueError):
                pass
        else:  # boolean capability flags
            merged[k] = bool(v)
    return model_selection.LlmRequirements(**merged)


def llm_diag(preset=None, overrides=None, config=None):
    """Dry-run the model picker for one or every requirement preset and report
    the ranked resolution — WITHOUT spending a token.

    The old llm_diag sent a real prompt to each of the 8 provider *slots*. Slots
    are gone: routing is now capability/cost-aware over the enumerated endpoint
    set (model_selection.select_model). So the useful diagnostic is no longer
    "can slot N answer" but "for requirement set X, which endpoint does the
    picker choose, and why is every other one an alternative or excluded". That
    is exactly what model_selection.explain_selection computes — purely, over the
    same _enumerate_candidates()/get_llm_perf_snapshot() inputs the live path
    uses — so this makes REAL routing legible and auditable at zero cost.

    Args:
        preset:     restrict the report to a single preset label; None = all.
        overrides:  optional dict of LlmRequirements field overrides applied to
                    every preset run (lets the UI build a custom requirement set).
        config:     config override (defaults to load_config()).

    Returns ``{"candidate_count": int, "presets": [{label, description,
    selected, permissive, rows:[...]}, ...]}``. Never raises: a preset that
    blows up is reported with an ``error`` field instead of a resolution.
    """
    config = config or load_config()
    candidates = _enumerate_candidates(config)
    perf = get_llm_perf_snapshot()
    wanted = [p for p in _DIAG_PRESETS if not preset or p[0] == preset]
    out = []
    for label, description, kwargs in wanted:
        entry = {"label": label, "description": description}
        try:
            reqs = _diag_reqs(kwargs, overrides)
            res = model_selection.explain_selection(reqs, candidates, perf)
            entry.update(res)
        except Exception as ex:  # noqa: BLE001 — one bad preset never sinks the report
            entry["error"] = str(ex)[:400]
            entry["selected"] = None
            entry["rows"] = []
        out.append(entry)
    return {"candidate_count": len(candidates), "presets": out}


# Re-export every name this module defines (public + underscore) so
# ``from llm_client import *`` in main preserves the full `from main import ...`
# surface, including underscore helpers a bare star-import would otherwise skip.
__EXCLUDE = {"collections", "json", "os", "random", "re", "threading", "time",
             "datetime", "requests",
             "main", "logger", "load_config"}
__all__ = [__n for __n in dir() if not __n.startswith("__") and __n not in __EXCLUDE]
