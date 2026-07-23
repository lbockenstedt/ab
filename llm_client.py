"""Multi-provider LLM routing + circuit breakers for BugFixer.

Extracted verbatim from main.py (highest-value split): the provider constants,
message converters, per-provider config/rpm/reviewer helpers, credit-exhaustion
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
import threading
import time
from datetime import datetime

import requests

import main  # lazy access to the shared app_state dict via main.state
from main import logger, load_config

# ============================================================================
# Multi-Provider LLM Routing
# ============================================================================

OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com"
LMSTUDIO_BASE_URL = "http://localhost:1234/v1"  # LM Studio local OpenAI-compatible server
LMSTUDIO_DEFAULT_PORT = 1234
ANTHROPIC_API_VERSION = "2023-06-01"


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


def _provider_configured(provider, key, model):
    """A provider is usable when it has a model, and either an API key or is a no-key
    provider (claude_cli session auth, LM Studio / Ollama local servers). Centralizes
    the no-key exception so every configured-check site agrees on what "configured"
    means."""
    return bool(model and (key or provider == "claude_cli" or _is_lmstudio(provider) or _is_ollama(provider)))


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


def _get_reviewer_model(n, config):
    """Return reviewer model override for slot n, from vault entry or legacy key."""
    slots = config.get("llm_slots") or {}
    entry_id = slots.get(str(n))
    if entry_id:
        for e in (config.get("llm_entries") or []):
            if e.get("id") == entry_id:
                return (e.get("reviewer_model") or "").strip()
    return (config.get(f"REVIEWER_MODEL_{n}") or "").strip()


def _get_escalation_models(n, config):
    """Ordered list of models the BUILDER escalates through ON slot n before the run
    moves to the next slot — e.g. a CPU slot that ratchets 7b -> 14b -> 32b. Read
    from the entry's ``escalation_models`` (comma-string or list); falls back to the
    slot's single model. Returns at least one element (the model, or None = slot
    default)."""
    _p, _k, model, _u = _get_provider_config(n, config)
    em = None
    entry_id = (config.get("llm_slots") or {}).get(str(n))
    if entry_id:
        for e in (config.get("llm_entries") or []):
            if e.get("id") == entry_id:
                em = e.get("escalation_models")
                break
    lst = []
    if isinstance(em, str):
        lst = [x.strip() for x in em.split(",") if x.strip()]
    elif isinstance(em, list):
        lst = [str(x).strip() for x in em if str(x).strip()]
    return lst or ([model] if model else [None])


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


def _tools_to_openai(tools):
    """Convert Ollama-style tool list to OpenAI function-calling format."""
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
        for t in (tools or [])
    ]


def _tools_to_anthropic(tools):
    """Convert Ollama-style tool list to Anthropic tool format."""
    return [
        {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("parameters", {"type": "object", "properties": {}, "required": []})}
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
        # claude_cli, lmstudio and ollama don't need an API key (session auth / local server).
        if model and (api_key or provider == "claude_cli" or _is_lmstudio(provider) or _is_ollama(provider)):
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

    if p in ("openai", "ollama"):
        if err.get("code") in {"insufficient_quota", "billing_hard_limit_reached"}:
            return True
        if err.get("type") in {"insufficient_quota", "billing_not_active"}:
            return True
        # OpenAI-compat providers sometimes put it in the message too
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
_PROVIDER_CREDIT_CB = {
    1: {"cooldown_until": 0.0, "tripped_at": None, "reason": None, "cause": None},
    2: {"cooldown_until": 0.0, "tripped_at": None, "reason": None, "cause": None},
    3: {"cooldown_until": 0.0, "tripped_at": None, "reason": None, "cause": None},
    4: {"cooldown_until": 0.0, "tripped_at": None, "reason": None, "cause": None},
}
_CREDIT_COOLDOWN_SECONDS = 3600   # 1 hour for credit exhaustion
_RATELIMIT_COOLDOWN_SECONDS = 600  # 10 minutes for sustained 429 storms


def _provider_credit_cb_trip(n, reason, duration_s=None, cause="credit"):
    secs = duration_s if duration_s is not None else _CREDIT_COOLDOWN_SECONDS
    cd = time.time() + secs
    with _PROVIDER_CREDIT_CB_LOCK:
        _PROVIDER_CREDIT_CB[n]["cooldown_until"] = cd
        _PROVIDER_CREDIT_CB[n]["tripped_at"] = datetime.now().isoformat()
        _PROVIDER_CREDIT_CB[n]["reason"] = reason
        _PROVIDER_CREDIT_CB[n]["cause"] = cause
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


def _provider_credit_cb_remaining(n):
    """Seconds remaining on credit/rate-limit cooldown for provider n (0 if clear)."""
    with _PROVIDER_CREDIT_CB_LOCK:
        return max(0.0, _PROVIDER_CREDIT_CB[n]["cooldown_until"] - time.time())


def _provider_credit_cb_snapshot():
    result = {}
    with _PROVIDER_CREDIT_CB_LOCK:
        for n in (1, 2, 3, 4):
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


_PROVIDER_RL_LOCK = threading.Lock()
_PROVIDER_REQUEST_TIMES = {n: collections.deque() for n in (1, 2, 3, 4)}
# Tracks when each provider slot last emitted an RPM-throttle log line.
# Prevents duplicate log pairs when two threads throttle on the same provider.
_PROVIDER_RPM_LAST_LOG: dict = {}

def _provider_rate_limit_wait(n, rpm, provider_name):
    """Block the calling thread until provider n is under its RPM limit.

    Uses a 60-second sliding window. rpm=0 means unlimited.
    Releases the lock before sleeping so other providers are never blocked.
    """
    if not rpm or rpm <= 0:
        return
    window = 60.0
    while True:
        now = time.time()
        with _PROVIDER_RL_LOCK:
            dq = _PROVIDER_REQUEST_TIMES[n]
            while dq and now - dq[0] >= window:
                dq.popleft()
            if len(dq) < rpm:
                dq.append(now)
                return  # Under limit — stamp and proceed.
            wait_s = dq[0] + window - now
        # Sleep outside the lock so other providers are not blocked.
        # Log at most once per 30 s per provider so concurrent threads sharing
        # the same slot don't each emit their own "waiting Xs" line.
        if wait_s > 0:
            now2 = time.time()
            last_log = _PROVIDER_RPM_LAST_LOG.get(n, 0)
            if now2 - last_log >= 30:
                logger.info(
                    f"Provider {n} ({provider_name}) RPM throttle ({rpm}/min) — "
                    f"waiting {wait_s:.1f}s before next request."
                )
                _PROVIDER_RPM_LAST_LOG[n] = now2
            elapsed = 0.0
            chunk = 5.0
            while elapsed < wait_s:
                time.sleep(min(chunk, wait_s - elapsed))
                elapsed += chunk


def _any_provider_available(config):
    """Return (available: bool, soonest_free_s: float).

    available=True  → at least one configured provider is not in cooldown.
    soonest_free_s  → seconds until the soonest cooldown expires (0 if available).
    """
    soonest = float("inf")
    any_free = False
    for n in (1, 2, 3, 4):
        provider, key, model, _ = _get_provider_config(n, config)
        configured = model and (key or provider == "claude_cli" or _is_lmstudio(provider) or _is_ollama(provider))
        if not configured:
            continue  # not configured
        rem = _provider_credit_cb_remaining(n)
        if rem <= 0:
            any_free = True
            soonest = 0.0
        else:
            soonest = min(soonest, rem)
    if soonest == float("inf"):
        soonest = 0.0  # no providers configured at all
    return any_free, soonest


_LLM_CB_LOCK = threading.Lock()
_LLM_CB = {
    "cooldown_until": 0.0,
    "consecutive_429s": 0,
    "total_429s": 0,
    "last_trip_reason": None,
    "last_trip_time": None,
}

def _llm_cb_wait():
    while True:
        with _LLM_CB_LOCK:
            cd = _LLM_CB["cooldown_until"]
        remaining = cd - time.time()
        if remaining <= 0:
            time.sleep(random.uniform(0, 1.5))
            return
        sleep_chunk = min(remaining, 5.0)
        logger.warning(
            f"LLM circuit breaker active — pausing {sleep_chunk:.1f}s "
            f"({remaining:.1f}s remaining) to respect rate-limit cooldown."
        )
        time.sleep(sleep_chunk)

def _llm_cb_trip(wait_time, reason="429"):
    wait_time = max(0.5, min(wait_time, 3600.0))
    with _LLM_CB_LOCK:
        new_cd = max(_LLM_CB["cooldown_until"], time.time() + wait_time)
        _LLM_CB["cooldown_until"] = new_cd
        _LLM_CB["consecutive_429s"] += 1
        _LLM_CB["total_429s"] += 1
        _LLM_CB["last_trip_reason"] = reason
        _LLM_CB["last_trip_time"] = datetime.now().isoformat()
        consecutive = _LLM_CB["consecutive_429s"]
        total = _LLM_CB["total_429s"]
    logger.warning(
        f"LLM circuit breaker TRIPPED for {wait_time:.1f}s (reason={reason}). "
        f"consecutive_429s={consecutive}, total_429s={total}. "
        f"All LLM threads will pause."
    )

def _llm_cb_reset():
    with _LLM_CB_LOCK:
        if _LLM_CB["consecutive_429s"] > 0:
            logger.info(
                f"LLM circuit breaker reset after successful request "
                f"(was consecutive_429s={_LLM_CB['consecutive_429s']}, "
                f"total_429s={_LLM_CB['total_429s']})."
            )
            _LLM_CB["consecutive_429s"] = 0

def _llm_cb_snapshot():
    with _LLM_CB_LOCK:
        cd = _LLM_CB["cooldown_until"]
        return {
            "active": cd > time.time(),
            "cooldown_remaining_s": max(0, cd - time.time()),
            "consecutive_429s": _LLM_CB["consecutive_429s"],
            "total_429s": _LLM_CB["total_429s"],
            "last_trip_reason": _LLM_CB["last_trip_reason"],
            "last_trip_time": _LLM_CB["last_trip_time"],
        }

_LLM_SEMAPHORE = None
_LLM_SEM_LOCK = threading.Lock()

def _get_llm_semaphore():
    global _LLM_SEMAPHORE
    with _LLM_SEM_LOCK:
        if _LLM_SEMAPHORE is None:
            try:
                cfg = load_config()
                max_conc = int(cfg.get("LLM_MAX_CONCURRENT", 1))
            except Exception:
                max_conc = 1
            _LLM_SEMAPHORE = threading.Semaphore(max(1, max_conc))
            logger.info(f"LLM global concurrency limiter initialised: max_concurrent={max(1, max_conc)}")
        return _LLM_SEMAPHORE

def _reset_llm_semaphore():
    """Drop the cached global LLM concurrency semaphore so it is rebuilt from
    the (possibly changed) LLM_MAX_CONCURRENT setting on next use. Kept in main
    so the rebind targets main's module global even when called from routes.py."""
    global _LLM_SEMAPHORE
    with _LLM_SEM_LOCK:
        _LLM_SEMAPHORE = None

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
        _llm_cb_wait()
        try:
            sem = _get_llm_semaphore()
            sem.acquire()
            try:
                resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout_val, stream=stream)
            finally:
                sem.release()

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
                _llm_cb_trip(wait_time, f"{resp.status_code} attempt {attempt+1}/{max_retries+1}")
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

            resp.raise_for_status()
            _llm_cb_reset()
            return resp

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            _5xx_retry_ok = 500 <= (status or 0) < 600 and attempt < max_5xx
            if not is_last and status and (status == 429 or _5xx_retry_ok):
                wait_time = min(backoff_base ** attempt, backoff_max) * random.uniform(0.5, 1.5)
                _llm_cb_trip(wait_time, f"HTTPError {status}")
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
                _llm_cb_trip(wait_time, f"transient {type(e).__name__}")
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


def _request_openai(model, api_key, base_url, messages, tools, effective_stream, task_id, config):
    """Call an OpenAI-compatible endpoint. Returns text string or tool-call dict."""
    base = (base_url or OPENAI_BASE_URL).rstrip("/")
    endpoint = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    msgs = _to_openai_messages(messages)
    use_stream = False if tools else effective_stream
    payload = {"model": model, "messages": msgs, "stream": use_stream}
    # Explicit max_tokens gives the model room to return a complete JSON object
    # (matches the anthropic path's 8192). Without it, some OpenAI-compatible
    # backends (ollama) truncate mid-object → "Response ended prematurely" and
    # the fix JSON then fails to parse ("unmatched '}'").
    try:
        out_tok = int((config or {}).get("FIX_MAX_OUTPUT_TOKENS", CHAT_CONFIG_DEFAULTS["FIX_MAX_OUTPUT_TOKENS"]) or CHAT_CONFIG_DEFAULTS["FIX_MAX_OUTPUT_TOKENS"])
    except Exception:
        out_tok = CHAT_CONFIG_DEFAULTS["FIX_MAX_OUTPUT_TOKENS"]
    if out_tok > 0:
        payload["max_tokens"] = out_tok
    if tools:
        payload["tools"] = _tools_to_openai(tools)

    resp = _llm_retry_post(endpoint, payload, headers, config, stream=use_stream, provider="openai")

    if tools:
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
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


def _request_anthropic(model, api_key, base_url, messages, tools, effective_stream, task_id, config):
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
    payload = {"model": model, "messages": msgs, "max_tokens": 8192, "stream": use_stream}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = _tools_to_anthropic(tools)

    resp = _llm_retry_post(endpoint, payload, headers, config, stream=use_stream, provider="anthropic")

    if tools or not use_stream:
        data = resp.json()
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
            main.state["llm_stream"] = full_response
            if task_id and task_id in main.state.get("active_tasks", {}):
                main.state["active_tasks"][task_id]["stream"] = full_response
        except json.JSONDecodeError:
            pass
    return full_response


def _request_google(model, api_key, base_url, messages, tools, effective_stream, task_id, config):
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
            {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}
            for t in tools
        ]}]

    resp = _llm_retry_post(endpoint, payload, headers, config, stream=False, provider="google")
    data = resp.json()
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


def _request_ollama(model, api_key, base_url, messages, tools, effective_stream, task_id, config):
    """Call an Ollama-compatible API (local or Ollama Cloud). Uses /api/chat natively."""
    base = (base_url or "http://localhost:11434").rstrip("/")
    endpoint = f"{base}/api/chat"
    headers = {}
    if api_key:
        clean_key = api_key.strip().strip('"').strip("'").replace("Bearer ", "").strip()
        headers["Authorization"] = f"Bearer {clean_key}"

    use_stream = False if tools else effective_stream
    payload = {
        "model": model,
        "messages": messages if messages else [{"role": "user", "content": ""}],
        "stream": use_stream,
    }
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
        return {"text": text, "tool_calls": tool_calls}

    full_response = ""
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
            content = chunk.get("message", {}).get("content") or chunk.get("response") or ""
            full_response += content
            main.state["llm_stream"] = full_response
            if task_id and task_id in main.state.get("active_tasks", {}):
                main.state["active_tasks"][task_id]["stream"] = full_response
        except json.JSONDecodeError:
            pass
    return full_response


def _request_claude_cli(model, messages, task_id, config):
    """Call the local `claude` CLI in non-interactive print mode.

    Uses the Claude Code session auth — no API key required. The claude binary
    must be in PATH. Tool calling is not supported; the full conversation is
    serialised into a single prompt string.
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
    cmd = ["claude", "--output-format", "json"]
    if model:
        cmd += ["--model", model]

    timeout_val = int(config.get("LLM_TIMEOUT", 900))
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout_val
        )
        output = proc.stdout.strip()
        stderr = proc.stderr.strip()

        # Parse JSON response if possible.
        try:
            data = json.loads(output)
            text = data.get("result") or data.get("text") or output
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
        raise Exception("'claude' binary not found in PATH — install Claude Code on this server.")


def _call_provider(provider, model, api_key, base_url, messages, tools, effective_stream, task_id, config):
    """Dispatch to the correct provider implementation."""
    p = (provider or "openai").lower().strip()
    if p == "anthropic":
        return _request_anthropic(model, api_key, base_url, messages, tools, effective_stream, task_id, config)
    if p == "google":
        return _request_google(model, api_key, base_url, messages, tools, effective_stream, task_id, config)
    if _is_ollama(p):
        return _request_ollama(model, api_key, base_url, messages, tools, effective_stream, task_id, config)
    if p == "groq":
        effective_url = base_url or "https://api.groq.com/openai/v1"
        return _request_openai(model, api_key, effective_url, messages, tools, effective_stream, task_id, config)
    if _is_lmstudio(p):
        # LM Studio exposes an OpenAI-compatible API; no auth key required.
        effective_url = _normalize_lmstudio_url(base_url)
        return _request_openai(model, api_key, effective_url, messages, tools, effective_stream, task_id, config)
    if p == "claude_cli":
        return _request_claude_cli(model, messages, task_id, config)
    return _request_openai(model, api_key, base_url, messages, tools, effective_stream, task_id, config)


# Shared LLM Utility
def call_llm(prompt, system_prompt="You are a helpful AI assistant.", force_cloud=None, task_id=None, model_override=None, url_override=None, messages=None, tools=None, stream=None, force_provider=None):
    """Generic LLM caller with Provider 1 → 2 → 3 failover and credit-exhaustion awareness.

    Routing priority:
      force_provider=N (int 1/2/3/4) — use that provider slot directly (no failover).
      force_cloud=True               — start at Provider 2, fall to 1 then 3 then 4.
      force_cloud=False              — Provider 1 only, no failover.
      force_cloud=None (default)     — Provider 1 → 2 → 3 → 4 in order.

    Providers in a 1-hour credit-exhaustion cooldown are skipped automatically.
    """
    config = load_config()
    p1_provider, p1_key, p1_model, p1_url = _get_provider_config(1, config)
    p2_provider, p2_key, p2_model, p2_url = _get_provider_config(2, config)
    p3_provider, p3_key, p3_model, p3_url = _get_provider_config(3, config)
    p4_provider, p4_key, p4_model, p4_url = _get_provider_config(4, config)

    if model_override:
        p1_model = p2_model = p3_model = p4_model = model_override
    if url_override:
        p1_url = p2_url = p3_url = p4_url = url_override

    effective_stream = True if stream is None else bool(stream)

    if messages is None:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    # Build an ordered list of providers to try.
    all_providers = [
        (1, p1_provider, p1_model, p1_key, p1_url),
        (2, p2_provider, p2_model, p2_key, p2_url),
        (3, p3_provider, p3_model, p3_key, p3_url),
        (4, p4_provider, p4_model, p4_key, p4_url),
    ]

    def _try_provider(n, provider, model, key, url):
        # claude_cli (Claude Code session), LM Studio and Ollama (local/remote
        # self-hosted servers) need no API key — only a model must be configured
        # for them to be usable. (Ollama Cloud takes a key, but it's optional at
        # this gate so a no-key local ollama is actually tried in failover.)
        if provider == "claude_cli" or _is_lmstudio(provider) or _is_ollama(provider):
            if not model:
                return None, "not_configured"
        elif not (key and model):
            return None, "not_configured"
        rem = _provider_credit_cb_remaining(n)
        if rem > 0:
            with _PROVIDER_CREDIT_CB_LOCK:
                cause = _PROVIDER_CREDIT_CB[n].get("cause", "credit")
            label = "rate-limit" if cause == "rate_limit" else "credit"
            trip_reason = (_PROVIDER_CREDIT_CB[n].get("reason") or "")[:120]
            logger.warning(
                f"Provider {n} ({provider}) skipped — {label} cooldown "
                f"{rem/60:.0f} min remaining"
                + (f" (tripped by: {trip_reason})" if trip_reason else "") + "."
            )
            return None, "credit_cooldown"
        try:
            # Honour per-provider RPM cap (0 = unlimited).
            _provider_rate_limit_wait(n, _get_provider_rpm(n, config), provider)
            main.state["active_llm"] = model
            result = _call_provider(provider, model, key, url, messages, tools, effective_stream, task_id, config)
            # Successful call: clear any rate-limit cooldown for this provider.
            with _PROVIDER_CREDIT_CB_LOCK:
                if _PROVIDER_CREDIT_CB[n].get("cause") == "rate_limit":
                    _PROVIDER_CREDIT_CB[n].update({"cooldown_until": 0.0, "tripped_at": None, "reason": None, "cause": None})
            return result, None
        except LLMCreditExhausted as ce:
            _provider_credit_cb_trip(n, str(ce), cause="credit")
            main.state["provider_credit_cb"] = _provider_credit_cb_snapshot()
            return None, "credit_exhausted"
        except Exception as e:
            err_str = str(e)
            # claude_cli session limit — authenticated but per-session quota hit.
            # Apply a short rate-limit cooldown (15 min) and fall through so the
            # next provider is tried without logging a spurious auth warning.
            if err_str.startswith("claude_cli_rate_limit:"):
                reason = err_str[len("claude_cli_rate_limit:"):]
                logger.warning(f"Provider {n} ({provider}) session limit — applying 15-min cooldown. ({reason})")
                _provider_credit_cb_trip(n, reason, duration_s=900, cause="rate_limit")
                main.state["provider_credit_cb"] = _provider_credit_cb_snapshot()
                return None, "rate_limited"
            # Some models (e.g. certain Groq models) reject tool-calling with a 400.
            # Retry the same provider without tools before giving up.
            if tools and "400" in err_str and "tool" in err_str.lower() and "not supported" in err_str.lower():
                logger.warning(
                    f"Provider {n} ({provider}) model does not support tool calling — retrying without tools."
                )
                try:
                    _provider_rate_limit_wait(n, _get_provider_rpm(n, config), provider)
                    result = _call_provider(provider, model, key, url, messages, None, effective_stream, task_id, config)
                    return result, None
                except Exception as retry_e:
                    err_str = str(retry_e)
                    e = retry_e
            # If the provider exhausted all retries with 429s, apply a short
            # rate-limit cooldown so it stops poisoning the global circuit breaker.
            if "429" in err_str:
                _provider_credit_cb_trip(n, f"Rate-limited: {err_str[:120]}",
                                         duration_s=_RATELIMIT_COOLDOWN_SECONDS, cause="rate_limit")
                main.state["provider_credit_cb"] = _provider_credit_cb_snapshot()
                return None, "rate_limited"
            return None, e

    try:
        # Bind for the except handler on EVERY path: the force_provider (reviewer)
        # and force_cloud=False paths return/raise before the failover loop assigns
        # last_err, so without this the handler hit "cannot access local variable
        # 'last_err'" (UnboundLocalError), which crashed reviewers and masked the
        # real provider error.
        last_err = None
        # force_provider=N: use that slot only, no failover.
        if force_provider in (1, 2, 3, 4):
            row = all_providers[force_provider - 1]
            result, err = _try_provider(*row)
            if result is not None:
                _record_provider_result(force_provider, "ok")
                return result
            _record_provider_result(force_provider, err if isinstance(err, str) else "failed", err)
            raise Exception(f"Provider {force_provider} unavailable: {err}")

        # force_cloud=False: Provider 1 only, no fallover.
        if force_cloud is False:
            result, err = _try_provider(1, p1_provider, p1_model, p1_key, p1_url)
            if result is not None:
                _record_provider_result(1, "ok")
                return result
            _record_provider_result(1, err if isinstance(err, str) else "failed", err)
            raise Exception(f"Provider 1 (force-only) unavailable: {err}")

        # Determine starting provider.
        use_p2_first = force_cloud is True
        order = [2, 1, 3, 4] if use_p2_first else [1, 2, 3, 4]
        pmap = {n: row for n, *row in [(r[0], *r[1:]) for r in all_providers]}

        last_err = None
        for n in order:
            provider, model, key, url = pmap[n]
            result, err = _try_provider(n, provider, model, key, url)
            if result is not None and str(result).strip():
                _record_provider_result(n, "ok")
                return result
            if result is not None and not str(result).strip():
                # Provider returned an empty body — treat as a transient failure so we
                # fall through to the next provider instead of passing "" to the caller.
                logger.warning(f"Provider {n} ({provider}) returned empty response. Trying next provider...")
                _record_provider_result(n, "empty_response", "provider returned an empty body")
                last_err = "empty_response"
                continue
            if err == "not_configured":
                # Previously a silent continue — log it so a skipped provider (e.g. a
                # no-key LM Studio with no model, or a missing API key) is visible.
                reason = "no model configured" if not model else "no API key configured"
                logger.info(f"Provider {n} ({provider}) skipped — not configured ({reason}).")
                _record_provider_result(n, "not_configured", reason)
                continue
            if err in ("credit_cooldown", "credit_exhausted", "rate_limited"):
                _record_provider_result(n, err, "cooldown active")
                last_err = err
                continue
            # Real failure — log and keep trying remaining providers.
            logger.warning(f"Provider {n} ({provider}) failed: {err}. Trying next provider...")
            _record_provider_result(n, "failed", err)
            last_err = err

        raise Exception(
            f"All configured LLM providers failed or are unavailable. "
            f"Last status: {last_err}. Check billing and API keys in settings."
        )
    except Exception as e:
        # A cooldown/rate-limit on every provider is an EXPECTED transient state
        # (credits will refill / the window resets), not a fault to alarm on — log
        # it as a warning. A genuine failure (bad key, real error) stays ERROR.
        if last_err in ("credit_cooldown", "credit_exhausted", "rate_limited"):
            logger.warning(f"LLM request deferred — all providers cooling down: {e}")
        else:
            logger.error(f"LLM request failed after all providers: {e}")
        raise


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


def analyze_logs(log_text, title="logs", task_id=None):
    """Ask the configured LLM whether anything is wrong in `log_text`, what it means,
    and what to check — the shared brain behind BugFixer's Log Analysis panel AND the
    LM hub's delegated ANALYZE_LOGS request. Returns the analysis string, which BEGINS
    with a machine-parseable `VERDICT: none|watch|escalate` line (see parse_log_verdict).
    Raises on LLM failure so callers can classify cooldown vs. error. Char-caps the tail."""
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
    result = call_llm(prompt, system_prompt=LOG_ANALYSIS_SYSTEM_PROMPT, task_id=task_id)
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



# Re-export every name this module defines (public + underscore) so
# ``from llm_client import *`` in main preserves the full `from main import ...`
# surface, including underscore helpers a bare star-import would otherwise skip.
__EXCLUDE = {"collections", "json", "os", "random", "re", "threading", "time",
             "datetime", "requests",
             "main", "logger", "load_config"}
__all__ = [__n for __n in dir() if not __n.startswith("__") and __n not in __EXCLUDE]
