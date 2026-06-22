import asyncio
import os, json, time, tempfile, threading, requests, logging, traceback, py_compile, random, re, uuid, collections
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
# Pure, stdlib-only duplicate-detection helpers. Importable standalone for tests
# (unlike this module, which initializes FastAPI/logging at import time). The
# script dir is prepended to sys.path so the import resolves regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup import (
    _normalize_for_dedup as _normalize_for_dedup_impl,
    _token_set as _token_set_impl,
    _jaccard as _jaccard_impl,
    _is_duplicate_match as _is_duplicate_match_impl,
    MODULE_ALIASES,
    strip_boilerplate,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from github import Github, GithubException
import git
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Setup Logging
DEFAULT_LOG_FILE = "/var/log/bugfixer.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

def get_log_path():
    path = os.getenv("LOG_FILE_PATH", "/var/log/bugfixer.log")
    log_dir = os.path.dirname(path) or "."
    if not os.access(log_dir, os.W_OK):
        return os.path.join(os.getcwd(), "bugfixer.log")
    return path

log_file = get_log_path()

# Ensure log directory exists
log_dir = os.path.dirname(log_file)
if not os.path.exists(log_dir):
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:
        print(f"Error creating log directory {log_dir}: {e}")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BugFixer")
logger.info(f"BugFixer started. Logging level: {LOG_LEVEL}. Logging to: {log_file}")

# Persistent Configuration Paths
CONFIG_DIR = "/etc/bugfixer"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
STATE_FILE = os.path.join(CONFIG_DIR, "processed_issues.json")
UPDATE_STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
SELF_SCAN_OFFSET_FILE = os.path.join(CONFIG_DIR, "self_scan_offset.json")
CHAT_HISTORY_FILE = os.path.join(CONFIG_DIR, "chat_history.json")
VERSION_FILE = os.path.join(os.getcwd(), "VERSION")

# Chat-agent configuration defaults. Applied (without overriding user values) by
# load_config() so every code path sees a fully-populated config, and persisted by
# save_settings when the user edits them on the Settings page.
CHAT_CONFIG_DEFAULTS = {
    "CHAT_TOOLS_ENABLED": True,        # Master switch; False -> chat runs the legacy single-turn path.
    "CHAT_TOOL_MAX_ITERATIONS": 6,     # Cap on tool-call <-> LLM round trips per turn.
    "CHAT_TOOL_MAX_TOKENS": 12000,     # Soft cap on cumulative tool-result text appended in a turn.
    "CHAT_INDEX_ISSUE_LIMIT": 8,       # Max open issues listed per repo in the system-prompt index.
    "CHAT_INDEX_CACHE_TTL": 60,        # Seconds to cache the GitHub issue index across turns.
    "CHAT_FIX_PROPOSAL_TTL": 600,      # Seconds a fix-proposal confirmation token stays valid.
}

class QueueLocalException(Exception):
    """Kept for backward compatibility with persisted 'awaiting_local' issue states."""
    pass

def save_config(config):
    """Saves configuration to persistent storage, falling back to local if needed."""
    try:
        if os.path.exists(CONFIG_DIR) or os.access(CONFIG_DIR, os.W_OK):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except Exception:
                pass
            logger.info(f"Config saved to persistent storage: {CONFIG_FILE}")
        else:
            raise IOError("Persistent config directory not writable")
    except Exception as e:
        logger.warning(f"Could not save to persistent storage ({e}), falling back to local config.json")
        try:
            with open("config.json", "w") as f:
                json.dump(config, f, indent=2)
        except Exception as fe:
            logger.error(f"Critical failure saving config: {fe}")

def load_config():
    # Try persistent config first
    config = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                # Ensure enabled_models exists
                if "enabled_models" not in config:
                    config["enabled_models"] = []
        except Exception as e:
            logger.error(f"Error reading persistent config {CONFIG_FILE}: {e}")
            config = None
    # Fallback to local config
    if config is None:
        try:
            with open("config.json", "r") as f:
                config = json.load(f)
                if "enabled_models" not in config:
                    config["enabled_models"] = []
        except Exception:
            config = {
                "monitored_repos": [],
                "trusted_repos": [],
                "default_branch": "main",
                "direct_push_enabled": False,
                "dev_branch": "dev",
                "repo_tests": {},
                "GITHUB_TOKEN": "",
                "monitored_labels": ["automated-fix"],
                "enabled_models": [],
                "self_diagnosis_repo": ""
            }
    # Apply chat-agent defaults for any keys the stored config does not set, so
    # every caller (chat agent loop, index builder, settings form) sees a complete
    # config regardless of how old the persisted config.json is.
    for _k, _v in CHAT_CONFIG_DEFAULTS.items():
        if _k not in config:
            config[_k] = _v
    return config

def load_processed():
    # Try persistent state first
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()} for issue_id in data}
                return data
        except: pass
    # Fallback to local state
    if os.path.exists("processed_issues.json"):
        try:
            with open("processed_issues.json", "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()} for issue_id in data}
                return data
        except: pass
    return {}

def save_processed(processed):
    """Saves processed issues to persistent storage, with fallback to local file."""
    try:
        # Primary: Persistent storage
        with open(STATE_FILE, "w") as f:
            json.dump(processed, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving to persistent state file {STATE_FILE}: {e}")
        try:
            # Fallback: Local directory
            with open("processed_issues.json", "w") as f:
                json.dump(processed, f, indent=2)
        except Exception as fe:
            logger.error(f"Critical failure saving processed history to both locations: {fe}")

def load_update_state():
    """Loads the update state for recovery."""
    if os.path.exists(UPDATE_STATE_FILE):
        try:
            with open(UPDATE_STATE_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {"last_known_good_commit": None, "failed_commits": []}

def save_update_state(state):
    """Saves the update state for recovery."""
    try:
        with open(UPDATE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving update state: {e}")


def get_version():
    try:
        with open(VERSION_FILE, "r") as f: return f.read().strip()
    except: return "Unknown"

# ============================================================================
# Multi-Provider LLM Routing
# ============================================================================

OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com"
ANTHROPIC_API_VERSION = "2023-06-01"


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
            api_key = (cred.get("api_key") or "").strip()
            base_url = (cred.get("base_url") or entry.get("base_url") or "").strip()
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
        if api_key and model:
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
            "  1. Open the BugFixer dashboard: http://localhost:8000/settings\n"
            "  2. Set LLM_PROVIDER_1, LLM_API_KEY_1, and LLM_MODEL_1\n"
            "  3. Optionally set LLM_PROVIDER_2, LLM_API_KEY_2, LLM_MODEL_2 for failover.\n"
            + "=" * 78
        )
    return ok

load_dotenv(ENV_FILE)
app = FastAPI()

template_path = os.path.join(os.getcwd(), "templates")
templates = Jinja2Templates(directory=template_path)

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

# ---------------------------------------------------------------------------
# Post-update cooldown — suppresses issue filing for a configurable window
# after hub/spoke updates fire, so transient "service offline" errors that
# occur during restarts don't flood GitHub with spurious issues.
# ---------------------------------------------------------------------------
_UPDATE_COOLDOWN_LOCK = threading.Lock()
_update_cooldown_until: float = 0.0


def _set_update_cooldown(config):
    global _update_cooldown_until
    minutes = float(config.get("POST_UPDATE_COOLDOWN_MINUTES") or 10)
    deadline = time.time() + minutes * 60
    with _UPDATE_COOLDOWN_LOCK:
        _update_cooldown_until = deadline
    logger.info(
        f"Post-update cooldown active for {minutes:.0f} min — "
        f"issue filing suppressed until {time.strftime('%H:%M:%S', time.localtime(deadline))}"
    )


def _in_update_cooldown():
    """Return (in_cooldown: bool, remaining_seconds: float)."""
    with _UPDATE_COOLDOWN_LOCK:
        remaining = _update_cooldown_until - time.time()
    if remaining > 0:
        return True, remaining
    return False, 0.0


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
        configured = model and (key or provider == "claude_cli")
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

def _llm_retry_post(endpoint, payload, headers, config, stream=False, provider="openai"):
    """POST to endpoint with retry/backoff. Returns the response object on success.

    Raises LLMCreditExhausted immediately (no retry) when the provider signals
    billing/credit exhaustion so call_llm() can trip the per-provider 1-hour CB.
    """
    max_retries = int(config.get("LLM_MAX_RETRIES", 5))
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
                wait_time = min(backoff_base ** attempt, backoff_max) * random.uniform(0.5, 1.5)
                _llm_cb_trip(wait_time, f"{resp.status_code} attempt {attempt+1}/{max_retries+1}")
                err_body = ""
                try:
                    err_body = resp.text[:1000]
                except Exception:
                    pass
                if is_last:
                    logger.error(f"LLM {resp.status_code} at {endpoint} after {max_retries+1} attempts. body={err_body!r}")
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
            if not is_last and status and (status == 429 or 500 <= status < 600):
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
            state["llm_stream"] = full_response
            if task_id and task_id in state.get("active_tasks", {}):
                state["active_tasks"][task_id]["stream"] = full_response
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
        state["llm_stream"] = text
        if task_id and task_id in state.get("active_tasks", {}):
            state["active_tasks"][task_id]["stream"] = text
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
            state["llm_stream"] = full_response
            if task_id and task_id in state.get("active_tasks", {}):
                state["active_tasks"][task_id]["stream"] = full_response
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

    state["llm_stream"] = text
    if task_id and task_id in state.get("active_tasks", {}):
        state["active_tasks"][task_id]["stream"] = text

    if tools:
        return {"text": text, "tool_calls": tool_calls or None}
    return text


def _request_ollama(model, api_key, base_url, messages, tools, effective_stream, task_id, config):
    """Call an Ollama-compatible API (local or Ollama Cloud). Uses /api/chat natively."""
    base = (base_url or "https://ollama.com").rstrip("/")
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
            state["llm_stream"] = full_response
            if task_id and task_id in state.get("active_tasks", {}):
                state["active_tasks"][task_id]["stream"] = full_response
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

        state["llm_stream"] = text
        if task_id and task_id in state.get("active_tasks", {}):
            state["active_tasks"][task_id]["stream"] = text
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
    if p == "ollama":
        return _request_ollama(model, api_key, base_url, messages, tools, effective_stream, task_id, config)
    if p == "groq":
        effective_url = base_url or "https://api.groq.com/openai/v1"
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
    global state
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
        # claude_cli authenticates via the Claude Code session, not an API key.
        if provider == "claude_cli":
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
            state["active_llm"] = model
            result = _call_provider(provider, model, key, url, messages, tools, effective_stream, task_id, config)
            # Successful call: clear any rate-limit cooldown for this provider.
            with _PROVIDER_CREDIT_CB_LOCK:
                if _PROVIDER_CREDIT_CB[n].get("cause") == "rate_limit":
                    _PROVIDER_CREDIT_CB[n].update({"cooldown_until": 0.0, "tripped_at": None, "reason": None, "cause": None})
            return result, None
        except LLMCreditExhausted as ce:
            _provider_credit_cb_trip(n, str(ce), cause="credit")
            state["provider_credit_cb"] = _provider_credit_cb_snapshot()
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
                state["provider_credit_cb"] = _provider_credit_cb_snapshot()
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
                state["provider_credit_cb"] = _provider_credit_cb_snapshot()
                return None, "rate_limited"
            return None, e

    try:
        # force_provider=N: use that slot only, no failover.
        if force_provider in (1, 2, 3, 4):
            row = all_providers[force_provider - 1]
            result, err = _try_provider(*row)
            if result is not None:
                return result
            raise Exception(f"Provider {force_provider} unavailable: {err}")

        # force_cloud=False: Provider 1 only, no fallover.
        if force_cloud is False:
            result, err = _try_provider(1, p1_provider, p1_model, p1_key, p1_url)
            if result is not None:
                return result
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
                return result
            if result is not None and not str(result).strip():
                # Provider returned an empty body — treat as a transient failure so we
                # fall through to the next provider instead of passing "" to the caller.
                logger.warning(f"Provider {n} ({provider}) returned empty response. Trying next provider...")
                last_err = "empty_response"
                continue
            if err == "not_configured":
                continue
            if err in ("credit_cooldown", "credit_exhausted", "rate_limited"):
                last_err = err
                continue
            # Real failure — log and keep trying remaining providers.
            logger.warning(f"Provider {n} ({provider}) failed: {err}. Trying next provider...")
            last_err = err

        raise Exception(
            f"All configured LLM providers failed or are unavailable. "
            f"Last status: {last_err}. Check billing and API keys in settings."
        )
    except Exception as e:
        logger.error(f"LLM request failed after all providers: {e}")
        raise

def run_sandboxed_command(command, cwd):
    """Executes a command in a Docker sandbox. Fails closed (returns an error result)
    if Docker is unavailable — NEVER runs untrusted repository code on the host as root."""
    import subprocess
    from dataclasses import dataclass

    @dataclass
    class MockResult:
        stdout: str
        stderr: str
        returncode: int

    docker_available = False
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True, timeout=15)
        docker_available = True
    except Exception:
        pass

    if not docker_available:
        msg = ("Docker is not available; refusing to run untrusted repository commands on the host "
               "(fail-closed). Install Docker and retry.")
        logger.error("⚠️ " + msg)
        return MockResult("", msg, 127)

    image = "ubuntu:latest"
    try:
        files = os.listdir(cwd)
    except Exception as e:
        logger.error(f"Cannot read sandbox working directory {cwd}: {e}")
        return MockResult("", str(e), 1)
    if "package.json" in files: image = "node:18-slim"
    elif "requirements.txt" in files or "pyproject.toml" in files: image = "python:3.9-slim"
    elif "go.mod" in files: image = "golang:1.21-slim"

    logger.info(f"Running sandboxed command in Docker image {image}...")

    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{cwd}:/app",
        "-w", "/app",
        image,
        "sh", "-c", command
    ]

    try:
        result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=300)
        return MockResult(result.stdout, result.stderr, result.returncode)
    except Exception as e:
        logger.error(f"Docker execution error: {e}")
        return MockResult("", f"Docker execution error: {e}", 1)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.debug(f"Incoming request: {request.method} {request.url}")
    try:
        response = await call_next(request)
        logger.debug(f"Response status: {response.status_code} for {request.url}")
        return response
    except Exception as e:
        logger.exception(f"Request failed: {e}")
        raise e

@app.middleware("http")
async def catch_exceptions_mid(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"UNCAUGHT EXCEPTION: {e}\n{tb}")
        return JSONResponse(
            status_code=500,
            content={"message": "Internal Server Error. Check bugfixer.log for details.", "error": str(e)}
        )

_task_state_lock = threading.Lock()
_chat_lock = threading.RLock()

def update_task_state(task_id, task_name="Unknown Task", action="start"):
    """Manages active tasks and their start times. action can be 'start' or 'end'."""
    global state
    if not task_id:
        logger.debug("update_task_state called with no task_id; ignoring.")
        return
    try:
        if action == "start":
            with _task_state_lock:
                state["active_tasks"][task_id] = {
                    "name": task_name,
                    "start_time": datetime.now(),
                    "stream": ""
                }
            logger.info(f"Task started: {task_id} - {task_name}")
        elif action == "end":
            with _task_state_lock:
                if task_id in state["active_tasks"]:
                    del state["active_tasks"][task_id]
            logger.info(f"Task completed: {task_id}")
    except Exception as e:
        logger.error(f"update_task_state failed for task_id={task_id!r} action={action!r}: {e}")

config_on_start = load_config()
processed_init = load_processed()
success_count = sum(1 for info in processed_init.values() if info.get("status") in ["fixed", "verified", "awaiting_prod_verification"])
failure_count = sum(1 for info in processed_init.values() if info.get("status") == "failed")

state = {
    "status": "Idle", "active_llm": "Unknown",
    "provider_1_online": False, "provider_2_online": False, "provider_3_online": False, "provider_4_online": False,
    "provider_1_configured": False, "provider_2_configured": False, "provider_3_configured": False, "provider_4_configured": False,
    "local_online": False, "cloud_online": False,
    "last_run": "Never", "api_status": "Not Triggered",
    "processed": processed_init,
    "version": get_version(), "llm_stream": "",
    "active_tasks": {}, "qa_enabled": config_on_start.get("qa_enabled", True),
    "success_count": success_count, "failure_count": failure_count,
    "llm_circuit_breaker": _llm_cb_snapshot(),
    "provider_credit_cb": _provider_credit_cb_snapshot(),
    "paused": False,
    "blackout": False,
    "chat_streams": {}, "chat_fix_proposals": {},
    "daily_fixes_count": 0,
    "daily_budget_date": "",
    "scheduler_mode": "full",
    "claude_auth_proc": None,    # background subprocess running `claude auth login`
    "claude_auth_url": "",       # OAuth URL captured from that process
    "claude_auth_done": False,   # True once the process exits 0
    "restart_pending": False,    # True when an update was pulled; restart deferred until cycle end
    "refresh_status_seconds": config_on_start.get("refresh_status_seconds", 30),
    "refresh_logs_seconds": config_on_start.get("refresh_logs_seconds", 10),
    "cpu_count": os.cpu_count() or 4,  # detected core count, surfaced in the Local LLM setup UI
    "local_llm_setup": {},             # last-run summary for the one-click Local LLM setup
}

try:
    validate_llm_config_on_startup()
except Exception as ve:
    logger.warning(f"Startup LLM validation failed (non-fatal): {ve}")

def clean_repo_name(name):
    """Converts a full GitHub URL or a 'user/repo' string into 'user/repo' format."""
    name = name.strip()
    if name.startswith("http"):
        name = name.replace("https://", "").replace("http://", "")
        name = name.replace("github.com/", "")
        if name.endswith(".git"):
            name = name[:-4]
        name = name.rstrip("/")
    return name

def get_monitored_repos(config):
    """Extracts and normalizes a list of monitored repositories from config,
    always including the self-diagnosis repository if it can be resolved.
    """
    raw_repos = config.get("monitored_repos", [])
    monitored_repos = []
    if isinstance(raw_repos, list):
        for r in raw_repos:
            for split_r in r.replace("\\n", ",").split(","):
                cleaned = clean_repo_name(split_r)
                if cleaned: monitored_repos.append(cleaned)
    elif isinstance(raw_repos, str):
        for split_r in raw_repos.replace("\\n", ",").split(","):
            cleaned = clean_repo_name(split_r)
            if cleaned: monitored_repos.append(cleaned)

    sd_repo = resolve_self_diagnosis_repo(config)
    if sd_repo:
        monitored_repos.append(sd_repo)

    return list(set(monitored_repos))

# --- Chat system-prompt context index (cached, TTL-bounded) -----------------
# A compact markdown snapshot of BugFixer's repos, their open monitored-label
# issues, processed-issue status totals, and recent Hub error count. Prepended
# to the chat system prompt every turn so the assistant has the lay of the land
# without a tool round-trip. Tool calls drill deeper on demand.
_CHAT_INDEX_CACHE = {}            # key("gh"|"notoken") -> {"ts": float, "text": str}
_CHAT_INDEX_LOCK = threading.Lock()


def _build_chat_context_index_uncached(config, gh=None):
    lines = ["## BugFixer Context (snapshot — may be up to ~60s stale; use tools for live detail)"]
    monitored = get_monitored_repos(config)
    trusted = list(config.get("trusted_repos", []) or [])
    sd = resolve_self_diagnosis_repo(config)
    labels = config.get("monitored_labels") or ["automated-fix"]
    issue_limit = int(config.get("CHAT_INDEX_ISSUE_LIMIT", 8) or 8)

    all_repos = list(dict.fromkeys(monitored + trusted))
    lines.append("")
    lines.append("Repositories (owner/repo):")
    for repo_name in all_repos:
        tags = []
        if repo_name in trusted:
            tags.append("trusted")
        if repo_name == sd:
            tags.append("self-diagnosis")
        tagstr = f" [{', '.join(tags)}]" if tags else ""
        if gh is None:
            lines.append(f"- {repo_name}{tagstr} (open monitored issues: unknown — no token)")
            continue
        try:
            issues = gh.get_repo(repo_name).get_issues(state="open", labels=list(labels))
            titles = []
            count = 0
            for it in issues:
                count += 1
                if len(titles) < issue_limit:
                    titles.append(f"#{it.number} {_trunc(it.title, 70)}")
            more = "" if count <= issue_limit else f"  (+{count - issue_limit} more)"
            if titles:
                lines.append(f"- {repo_name}{tagstr} — {count} open: " + "; ".join(titles) + more)
            else:
                lines.append(f"- {repo_name}{tagstr} — 0 open")
        except Exception as e:
            lines.append(f"- {repo_name}{tagstr} (unavailable: {_trunc(type(e).__name__, 40)})")

    # Processed-issue status totals.
    try:
        processed = load_processed()
        counts = {}
        for info in processed.values():
            if isinstance(info, dict):
                st = info.get("status", "unknown")
                counts[st] = counts.get(st, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
        lines.append("")
        lines.append(f"Processed-issue totals (n={len(processed)}): {summary}")
    except Exception:
        lines.append("")
        lines.append("Processed-issue totals: (unavailable)")

    # Recent Hub error count (best-effort; may be None if Hub not configured).
    try:
        hub_url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
        if hub_url and "your-netbox" not in str(hub_url):
            logs = get_hub_logs()
            n = len(filter_error_logs(logs)) if logs else 0
            lines.append(f"Recent Hub errors: {n} (use get_recent_errors for detail)")
    except Exception:
        pass

    lines.append("")
    lines.append(f"Monitored labels: {', '.join(labels)}")
    lines.append("You have tools: list_repos, list_issues, get_issue, list_repo_files, "
                 "read_file, get_processed_issues, get_recent_errors, propose_fix. "
                 "Use them to answer precisely; do not guess issue/file contents.")
    text = "\n".join(lines)
    # Defense-in-depth: never let a leaked token in an issue title reach the model.
    return _redact_text(text, _secret_denylist(config))


def build_chat_context_index(config, gh=None):
    """Returns the cached chat context-index text, rebuilding if older than
    CHAT_INDEX_CACHE_TTL. Cached per token-availability key so a no-token turn's
    sparse index is not served to a later token-enabled turn within the TTL."""
    ttl = int(config.get("CHAT_INDEX_CACHE_TTL", 60) or 60)
    key = "gh" if gh is not None else "notoken"
    with _CHAT_INDEX_LOCK:
        cached = _CHAT_INDEX_CACHE.get(key)
        if cached and (time.time() - cached["ts"]) < ttl:
            return cached["text"]
        text = _build_chat_context_index_uncached(config, gh)
        _CHAT_INDEX_CACHE[key] = {"ts": time.time(), "text": text}
        return text

def resolve_module_repo(module, monitored_repos, config):
    """Maps a Hub log module name to the GitHub repo its issues should be filed in.

    Routing precedence (first match wins):
      1. Explicit 'module_repo_map' config key: {module_name: "owner/repo"}.
         Case-insensitive module lookup; lets the user override auto-matching
         for aliases or modules with no name-matching repo (e.g. "hub" -> "owner/lm").
      2. Auto-match: a monitored repo whose basename (the segment after the final
         '/') equals the module name, case-insensitive. e.g. module "pxmx" ->
         "lbockenstedt/pxmx".
      3. None if nothing matches — the caller should skip filing (NOT dump into
         the self-diagnosis repo, which is the behaviour the user explicitly
         wants to avoid).

    The returned repo is always a member of monitored_repos (auto-match) or a
    user-declared repo (explicit map); it is never invented.
    """
    if not module:
        return None
    mod_key = str(module).strip().lower()
    if not mod_key:
        return None

    # 1. Explicit user-provided mapping.
    module_map = config.get("module_repo_map") or {}
    if isinstance(module_map, dict):
        for k, v in module_map.items():
            if str(k).strip().lower() == mod_key and v and str(v).strip():
                resolved = clean_repo_name(str(v).strip())
                if resolved:
                    return resolved

    # 2. Auto-match against monitored repo basenames.
    for repo_name in monitored_repos:
        basename = str(repo_name).strip().split('/')[-1].lower()
        if basename == mod_key:
            return repo_name

    return None

def parse_module_repo_map(value):
    """Normalises a module_repo_map setting into {module: "owner/repo"}.

    Accepts a dict, a JSON object string, or a newline/comma-separated list of
    'module=owner/repo' pairs, so the Settings form can send any of these shapes.
    Values are cleaned via clean_repo_name; entries with empty module or repo are
    dropped. Module keys are stored as-is (case-insensitive lookup happens in
    resolve_module_repo), so callers see the original casing.
    """
    result = {}
    if value is None:
        return result
    if isinstance(value, dict):
        pairs = value.items()
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return result
        # Try JSON object first; fall back to line/separated 'module=repo' pairs.
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                pairs = obj.items()
            else:
                return result
        except Exception:
            pairs = []
            for part in s.replace(",", "\n").split("\n"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                mod, _, repo = part.partition("=")
                pairs.append((mod.strip(), repo.strip()))
    else:
        return result

    for mod, repo in pairs:
        mod_s = str(mod).strip()
        repo_s = clean_repo_name(str(repo).strip()) if repo else ""
        if mod_s and repo_s:
            result[mod_s] = repo_s
    return result

def discover_labels(gh_current, monitored_repos):
    """Fetches all unique labels from all monitored repositories, including built-in defaults."""
    all_labels = {"automated-fix", "bug", "critical", "high-priority"}
    for repo_name in monitored_repos:
        try:
            repo = gh_current.get_repo(repo_name)
            labels = repo.get_labels()
            for label in labels:
                all_labels.add(label.name)
        except Exception as e:
            logger.error(f"Error discovering labels for {repo_name}: {e}")
    return sorted(list(all_labels))

def bump_repo_version(repo_path):
    """Increments the version in the VERSION file of the target repository."""
    version_file = os.path.join(repo_path, "VERSION")
    current_version = "V.00"
    if os.path.exists(version_file):
        try:
            with open(version_file, "r") as f:
                current_version = f.read().strip()
        except Exception as e:
            logger.error(f"Error reading version file: {e}")

    if current_version.startswith("V."):
        try:
            ver_num = int(current_version[2:])
            new_version = f"V.{ver_num + 1:02d}"
        except ValueError:
            new_version = "V.01"
    else:
        new_version = "V.01"

    try:
        with open(version_file, "w") as f:
            f.write(new_version)
        return new_version
    except Exception as e:
        logger.error(f"Error writing version file: {e}")
        return None

def trigger_infrastructure_update():
    url = os.getenv("UPDATE_API_URL")
    if not url or "your-netbox" in url: return "URL not configured"
    try:
        resp = requests.post(url, json={}, timeout=10)
        return "SUCCESS: Sync Triggered" if resp.status_code == 200 else f"FAILED: {resp.status_code}"
    except Exception as e: return f"ERROR: {str(e)}"

def _normalize_for_dedup(text):
    """Aggressively normalize text for duplicate comparison.

    Thin wrapper around the strengthened implementation in ``dedup.py`` (which
    additionally strips the automated-issue boilerplate wrapper and applies
    module aliases such as ``opns`` -> ``opnsense``). Kept here as a shim so
    existing call sites that import it from main continue to work.
    """
    return _normalize_for_dedup_impl(text)

def _token_set(text):
    return _token_set_impl(text)

def _jaccard(a, b):
    return _jaccard_impl(a, b)

def _is_duplicate_match(new_title, new_body, ex_title, ex_body):
    """Returns True if a new error matches an existing issue, using normalized +
    fuzzy comparison so LLM rephrasing, timestamp drift, boilerplate wrapper,
    and module-name variants (opns/opnsense) don't defeat dedup."""
    return _is_duplicate_match_impl(new_title, new_body, ex_title, ex_body)

# --- Duplicate issue detection configuration ---------------------------------
# How far back to look at CLOSED issues when searching for a recurrence. The bot
# previously only searched OPEN issues, so once a "fix" was merged and the issue
# closed, the next cycle's identical error was filed as a brand-new issue +
# spawned a new ai-fix-issue-* branch — the #25 -> #55 -> #78 -> #90 storm.
# Searching recently-closed issues lets us REOPEN the original instead.
DEDUP_CLOSED_WINDOW_DAYS = 60
# When the target repo has no match and we fall back to searching the OTHER
# monitored repos globally, require a stricter title-level signal to avoid
# cross-module false positives (e.g. an opnsense error matching a pxmx issue on
# incidental wording overlap).
GLOBAL_FALLBACK_JACCARD = 0.8

def find_global_duplicate_issue(gh_current, monitored_repos, error_data):
    """Searches across monitored repositories for an existing issue matching the error.

    Searches OPEN issues AND recently-CLOSED issues (within
    DEDUP_CLOSED_WINDOW_DAYS), because a recurring error whose prior issue was
    closed (the bot merged a "fix") must still be recognised so it can be
    REOPENED rather than re-filed — this is what previously caused the
    opnsense 'time' import storm (#25 -> #55 -> #78 -> #90).

    The target repository (error_data['repo']) is searched first; other
    monitored repos are searched as a fallback with a stricter title-level
    threshold (GLOBAL_FALLBACK_JACCARD) to avoid cross-module false positives.

    Returns a tuple (issue, repo_name, was_closed). ``was_closed`` is True when
    the matched issue is currently closed, signalling the caller to reopen it
    rather than treat it as an open duplicate. Returns (None, None, False) when
    no duplicate is found.

    Safely handles error_data payloads that may be missing the 'title' or 'body'
    keys (the LLM may omit them). Missing fields are treated as empty strings so
    that the deduplication search degrades gracefully instead of raising a
    KeyError.
    """
    # Defensive: ensure error_data is a dict before calling .get()
    if not isinstance(error_data, dict):
        logger.warning(f"find_global_duplicate_issue received non-dict error_data: {type(error_data)}")
        return None, None, False

    new_title = error_data.get('title') or ''
    new_body = error_data.get('body') or ''

    if not str(new_title).strip() and not str(new_body).strip():
        return None, None, False

    target_repo = error_data.get('repo')

    def _search_repo(repo_name, require_strict_global=False, is_self_diag=False):
        try:
            repo = gh_current.get_repo(repo_name)
            # state='all' so we see recently-closed recurrences too; newest-first
            # so the most relevant (recently updated) issues are scanned first.
            issues = repo.get_issues(state='all', sort='updated', direction='desc')
            now = datetime.utcnow()
            for issue in issues:
                # Skip closed issues older than the recurrence window — they are
                # unlikely to be the same recurrence and would risk stale matches.
                if issue.state == 'closed':
                    closed_at = getattr(issue, 'closed_at', None) or issue.updated_at
                    if closed_at and (now - closed_at).days > DEDUP_CLOSED_WINDOW_DAYS:
                        continue
                issue_body = issue.body or ""

                # Special case: Self-Diagnosis. Relax the match to rely primarily on title
                # since JSON error messages often vary by exactly one character (line number).
                if is_self_diag:
                    nt = _normalize_for_dedup(new_title)
                    et = _normalize_for_dedup(issue.title or "")
                    if nt and et and _jaccard(set(nt.split()), set(et.split())) >= 0.7:
                        return issue, repo_name, (issue.state == 'closed')

                if _is_duplicate_match(new_title, new_body, issue.title or "", issue_body):
                    # Global fallback (non-target repo): require a strong
                    # title-level signal so we don't cross-match unrelated
                    # modules on incidental body-wording overlap.
                    if require_strict_global:
                        nt = _normalize_for_dedup(new_title)
                        et = _normalize_for_dedup(issue.title or "")
                        if not (nt and et and
                                _jaccard(set(nt.split()), set(et.split())) >= GLOBAL_FALLBACK_JACCARD):
                            continue
                    return issue, repo_name, (issue.state == 'closed')
        except Exception as e:
            logger.debug(f"Could not search for duplicates in {repo_name}: {e}")
        return None

    # 1. Target repo first — the recurrence almost always lands in the same repo.
    if target_repo:
        config = load_config()
        self_diag_repo = config.get("self_diagnosis_repo")
        is_self_diag = (target_repo == self_diag_repo)

        if target_repo in monitored_repos:
            hit = _search_repo(target_repo, is_self_diag=is_self_diag)
            if hit:
                return hit
        elif is_self_diag:
            # If it's the self-diagnosis repo, search it even if it's not explicitly
            # in the monitored_repos list (though it usually is).
            hit = _search_repo(target_repo, is_self_diag=True)
            if hit:
                return hit


    # 2. Global fallback across the other monitored repos, stricter threshold.
    for repo_name in monitored_repos:
        if repo_name == target_repo:
            continue
        hit = _search_repo(repo_name, require_strict_global=True)
        if hit:
            return hit

    return None, None, False

def create_automated_issue(gh_current, monitored_repos, gh_repo, error_data):
    """Creates a GitHub issue for a log-detected error, deduplicating globally across monitored repos.

    The 'body' field is required to create a meaningful issue. If it is missing or
    empty, the function logs a warning and returns None instead of raising a
    KeyError, which previously crashed automated issue creation with: 'body'.

    Additionally validates that error_data is a dict and that both 'title' and
    'body' are present and non-empty strings before any GitHub API call is made.
    """
    try:
        in_cooldown, remaining = _in_update_cooldown()
        if in_cooldown:
            logger.info(
                f"Post-update cooldown active ({remaining / 60:.1f} min remaining) — "
                f"suppressing issue: {error_data.get('title', 'unknown') if isinstance(error_data, dict) else repr(error_data)}"
            )
            return None

        # Defensive: ensure error_data is a dict; if the LLM returned a malformed
        # payload (e.g., a string or None), .get() would itself raise AttributeError.
        if not isinstance(error_data, dict):
            logger.warning(
                f"Skipping automated issue creation: error_data is not a dict "
                f"(type={type(error_data).__name__}). Value: {error_data!r}"
            )
            return None

        title_text = error_data.get('title')
        body_text = error_data.get('body')

        # Validate body FIRST — this is the field that was causing the KeyError crash.
        # We explicitly check for None, empty string, or whitespace-only strings.
        if body_text is None or not str(body_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'body' field is missing or empty. "
                f"Title was: {title_text!r}. Full error_data: {error_data}"
            )
            return None

        # Validate title as well — a GitHub issue cannot be created without a title.
        if title_text is None or not str(title_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'title' field is missing or empty. "
                f"Body was: {str(body_text)[:120]!r}"
            )
            return None

        # Normalise to strings (the LLM might return non-string types).
        title_text = str(title_text)
        body_text = str(body_text)

        current_repo_name = error_data.get('repo') or gh_repo.full_name

        existing_issue, duplicate_repo_name, was_closed = find_global_duplicate_issue(
            gh_current, monitored_repos, error_data
        )

        if existing_issue:
            duplicate_repo_display = duplicate_repo_name or current_repo_name

            # If the matching issue still carries the 'bugfixer-dismissed' label,
            # it was intentionally marked as not a real issue — skip entirely.
            # A human removing the label is the signal to resume normal processing.
            existing_labels = [lbl.name for lbl in (existing_issue.labels or [])]
            if "bugfixer-dismissed" in existing_labels:
                logger.info(
                    f"Suppressing new issue for #{existing_issue.number} in "
                    f"{duplicate_repo_display} — 'bugfixer-dismissed' label is still present."
                )
                return existing_issue

            if was_closed:
                # The matching issue was closed (typically the bot merged a "fix"
                # for it). Reopen it and record the recurrence instead of filing a
                # brand-new issue + spawning another ai-fix-issue-* branch. This
                # is the core fix for the recurring-error storm.
                logger.info(
                    f"Recurring CLOSED issue #{existing_issue.number} in "
                    f"{duplicate_repo_display} matched; reopening instead of filing a duplicate."
                )
                try:
                    existing_issue.edit(state='open')
                except Exception as reopen_err:
                    logger.warning(f"Could not reopen issue #{existing_issue.number}: {reopen_err}")
                try:
                    existing_issue.create_comment(
                        f"🔁 **Recurrence detected — reopening instead of filing a duplicate**\n\n"
                        f"BugFixer re-detected this error in **{current_repo_name}** after the "
                        f"issue was closed.\n\n"
                        f"```\n{body_text}\n```"
                    )
                    logger.info(f"Reopened issue #{existing_issue.number} for {current_repo_name}")
                except Exception as comment_err:
                    logger.warning(f"Could not add recurrence comment to #{existing_issue.number}: {comment_err}")
                return existing_issue

            # OPEN duplicate — keep the existing evidence-comment behavior.
            logger.info(f"Global duplicate issue detected: #{existing_issue.number} in {duplicate_repo_display}. Adding info.")

            existing_body = existing_issue.body or ""
            if body_text.lower() not in existing_body.lower():
                existing_issue.create_comment(
                    f"🤖 **BugFixer Update**\n\nAdditional instance of this error detected in repository **{current_repo_name}:**\n\n"
                    f"```\n{body_text}\n```"
                )
                logger.info(f"Added additional evidence from {current_repo_name} to issue #{existing_issue.number}")

            return existing_issue

        full_title = f"🤖 Log Alert: {title_text}"
        full_body = (
            f"**Automated Error Detection**\n\n"
            f"The BugFixer Hub analysis detected a potential issue in the logs:\n\n"
            f"### Log Evidence:\n```\n{body_text}\n```\n\n"
            f"This issue has been automatically created for fixing."
        )
        issue = gh_repo.create_issue(
            title=full_title,
            body=full_body,
            labels=["automated-fix", "log-detected"]
        )
        logger.info(f"Created automated issue #{issue.number} for {current_repo_name}")
        return issue
    except Exception as e:
        logger.error(f"Failed to handle automated issue creation: {e}")
        logger.debug(f"create_automated_issue error_data was: {error_data!r}")
        return None

def get_hub_logs():
    """Fetches recent logs from the Hub for all modules. Returns a list of log entries.

    Robustly handles non-JSON 200 responses (e.g., HTML login pages or error pages
    served by reverse proxies). The Hub endpoint may return HTTP 200 with an HTML
    body when an authentication redirect, maintenance page, or upstream error
    page is served. In such cases we detect the mismatch via the Content-Type
    header (and as a fallback by inspecting the body for HTML markers) and return
    None gracefully — logging a single WARNING instead of an ERROR — so we do
    not generate recurring error-log noise that itself triggers automated issue
    creation in a feedback loop.
    """
    config = load_config()
    url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
    if not url or "your-netbox" in url:
        logger.debug("Hub Query URL not configured. Skipping log fetch.")
        return None
    try:
        log_url = url.rstrip('/') + "/setup/logs/all"
        logger.debug(f"Fetching Hub logs from: {log_url}")
        resp = requests.get(log_url, timeout=15)
        if resp.status_code == 200:
            body = resp.text

            # Empty body — nothing to parse.
            if not body or not body.strip():
                logger.warning(
                    f"Hub returned 200 OK but empty response body for {log_url}. "
                    f"Skipping JSON parse to avoid json.decode error."
                )
                return None

            # --- Content-Type guard for non-JSON 200 responses ---
            # The Hub endpoint may serve an HTML error page or login redirect with
            # HTTP 200 (e.g., behind a reverse proxy like nginx/traefik/caddy that
            # intercepts the request, or when an upstream app serves a custom
            # error page). We check the Content-Type header first, and as a
            # fallback inspect the body for HTML markers. This prevents the
            # recurring "Hub returned 200 OK but body was not valid JSON" ERROR
            # log entries that previously spammed the logs and triggered
            # automated issue creation in a noisy feedback loop.
            content_type = (resp.headers.get("Content-Type") or "").lower()
            stripped_body = body.lstrip()
            looks_like_html = (
                "text/html" in content_type
                or "application/xhtml" in content_type
                or stripped_body.startswith("<!DOCTYPE")
                or stripped_body.startswith("<html")
                or stripped_body.startswith("<?xml")
                or (stripped_body.startswith("<") and "<head" in stripped_body[:512].lower())
            )

            if looks_like_html:
                # The endpoint is serving an HTML page instead of JSON. This is
                # typically a login redirect, a maintenance page, or an upstream
                # error page. Log a single WARNING (not ERROR) so we do not
                # generate recurring error-log noise that itself triggers
                # automated issue creation. Return None so callers skip this
                # cycle gracefully. We also include a short content preview to
                # aid debugging without flooding the logs.
                logger.warning(
                    f"Hub returned 200 OK but received non-JSON content "
                    f"(Content-Type={content_type or 'unknown'}) for {log_url}. "
                    f"The endpoint may be serving an error page or login redirect. "
                    f"Skipping this cycle. First 200 chars: {body[:200]!r}"
                )
                return None

            # Content-Type looks JSON-compatible — attempt to parse.
            try:
                data = resp.json()
                raw_logs = []
                if isinstance(data, dict):
                    raw_logs = data.get('logs', [])
                    if not isinstance(raw_logs, list):
                        raw_logs = []
                elif isinstance(data, list):
                    raw_logs = data

                # Try to parse a timestamp from each log line so we can sort
                # newest-first.  Typical format: "2024-01-15 10:30:00 [INFO] …"
                _TS_PAT = re.compile(r'^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})')
                def _log_ts(entry):
                    m = _TS_PAT.match((entry.get('log') or '').strip())
                    return m.group(1) if m else ''

                # Sort descending so newest entries are first.
                return sorted(raw_logs, key=_log_ts, reverse=True)
            except Exception as e:
                # Even with a JSON-ish Content-Type, parsing could fail (truncated
                # body, BOM, etc.). Treat this as a soft failure (WARNING) and
                # return None so we don't crash the scan cycle or generate noise.
                logger.warning(
                    f"Hub returned 200 OK but failed to parse JSON: {e}. "
                    f"Content-Type={content_type}. Content: {body[:200]!r}"
                )
                return None

        logger.warning(f"Hub returned unexpected status code {resp.status_code} for {log_url}")
        return None
    except Exception as e:
        logger.error(f"Hub Log Fetch Error: {e}")
        return None

def get_hub_state():
    """Fetches the current state of the hub for verification."""
    config = load_config()
    url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
    if not url or "your-netbox" in url: return None
    try:
        resp = requests.get(url.rstrip('/'), timeout=15)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception as e:
        logger.error(f"Hub State Fetch Error: {e}")
        return None

def filter_error_logs(logs):
    """Scrubs raw logs down to error-relevant entries before sending to the LLM.

    Why: HubScan previously JSON-dumped the *entire* Hub log set (every INFO line,
    last-500-lines-per-module file logs, recurring duplicates) into the LLM
    prompt. That both bloated the prompt toward the model's context limit
    (a likely cause of upstream HTTP 500s) and buried actionable errors in noise.

    This keeps only entries whose 'log' text carries an error signature
    ([ERROR]/[CRITICAL]/Traceback/Exception/Error/Failed), dedupes identical
    lines per module (recurring errors appear dozens/hundreds of times in file
    logs), and caps the total to bounded entry/character budgets so the prompt
    can never overflow context regardless of log volume.

    Schema-agnostic: handles the Hub shape {"module":..., "log":...} and the
    SelfScan shape {"module":..., "timestamp":..., "log":...} equally, since it
    only inspects the 'log' field (falling back to the stringified entry).
    """
    import re
    if not logs:
        return []

    # ERROR/CRITICAL level tags plus common error signatures (tracebacks,
    # raised exceptions, explicit "Error:"/"Failed"). WARNINGs are excluded:
    # the LLM task is to find actionable *errors*, not routine warnings.
    error_pattern = re.compile(
        r'\[(ERROR|CRITICAL)\]|Traceback|Exception|Error[: ]|Failed|Traceback \(most recent call last\)',
        re.IGNORECASE
    )

    cfg = load_config()
    max_entries = int(cfg.get("LLM_LOG_MAX_ENTRIES", 200))
    max_chars = int(cfg.get("LLM_LOG_MAX_CHARS", 60000))

    seen = set()
    kept = []
    total_chars = 0
    for entry in logs:
        if isinstance(entry, dict):
            module = str(entry.get('module', '') or '')
            text = entry.get('log')
            text = str(text) if text is not None else json.dumps(entry)
        else:
            module = ''
            text = str(entry)

        if not error_pattern.search(text):
            continue

        key = (module, text.strip())
        if key in seen:
            continue
        seen.add(key)

        line_len = len(text) + len(module) + 16
        if total_chars + line_len > max_chars:
            logger.info(
                f"filter_error_logs: reached {max_chars}-char budget after "
                f"{len(kept)} entries; stopping."
            )
            break
        kept.append(entry if isinstance(entry, dict) else {"module": "", "log": text})
        total_chars += line_len
        if len(kept) >= max_entries:
            logger.info(f"filter_error_logs: reached {max_entries}-entry cap; stopping.")
            break

    return kept

def analyze_logs_for_errors(logs):
    """Uses LLM to identify actionable errors in aggregated logs.

    Robustly validates the LLM's JSON response: every entry must be a dict with
    non-empty 'module', 'title', and 'body' fields. Malformed entries are dropped
    so they never reach create_automated_issue(), preventing the 'body' KeyError.

    The 'module' field (carried through from the source log entry) is the
    authoritative key for routing an issue to the correct repository — see
    resolve_module_repo(). The LLM may also suggest a 'repo', but it is treated
    as a hint only and is not required.
    """
    if not logs: return []

    log_text = json.dumps(logs, indent=2)
    prompt = (
        f"Logs from Hub:\n{log_text}\n\n"
        "Analyze these logs for critical, recurring, or actionable errors that can be fixed in code. "
        "Ignore heartbeat messages or routine status updates. "
        "For each actionable error found, provide: \n"
        "1. The exact 'module' value from the source log entry the error came from.\n"
        "2. A concise summary of the bug ('title').\n"
        "3. The specific log snippet that proves the error ('body').\n\n"
        "Return ONLY a JSON array of objects: [{\"module\": \"module-name\", \"title\": \"Error Summary\", \"body\": \"Log snippet and description\"}]. "
        "Every object MUST include non-empty 'module', 'title', and 'body' fields. "
        "The 'module' MUST be copied verbatim from the source log entry's module field."
    )
    try:
        res = call_llm(prompt, system_prompt="You are a log analysis expert. Return only a JSON array.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            # Defensive: the LLM might return a single object instead of an array.
            if isinstance(parsed, dict):
                logger.warning(f"LLM returned a single JSON object instead of an array for log analysis. Wrapping in list.")
                parsed = [parsed]
            if not isinstance(parsed, list):
                logger.warning(f"LLM returned non-array JSON for log analysis: {type(parsed).__name__}. Discarding.")
                return []
            cleaned = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    logger.debug(f"Dropping malformed log-analysis entry (not a dict): {entry}")
                    continue
                module_val = entry.get('module')
                title_val = entry.get('title')
                body_val = entry.get('body')
                if not module_val or not str(module_val).strip():
                    logger.warning(f"Hub log analysis found an actionable error but it's missing a module identifier. Log snippet: {body_val[:200]!r}")
                    continue
                if not title_val or not str(title_val).strip():
                    logger.debug(f"Dropping malformed log-analysis entry (missing/empty title): {entry}")
                    continue
                if not body_val or not str(body_val).strip():
                    logger.debug(f"Dropping malformed log-analysis entry (missing/empty body): {entry}")
                    continue

                # Try to find the original source entry to preserve full context (host, path, etc.)
                source_entry = next((log for log in logs if isinstance(log, dict)
                                   and str(log.get('module')) == str(module_val)
                                   and str(body_val) in str(log.get('log', ''))), {})

                # Normalise all fields to strings so downstream code never receives None.
                cleaned.append({
                    'module': str(module_val),
                    'title': str(title_val),
                    'body': str(body_val),
                    'repo': str(entry.get('repo')) if entry.get('repo') and str(entry.get('repo')).strip() else '',
                    'source_data': source_entry
                })
            return cleaned
        return []
    except Exception as e:
        logger.error(f"Error analyzing logs: {e}")
        return []

def _check_provider_online(n, config):
    """Ping provider n and return True if reachable."""
    provider, api_key, model, base_url = _get_provider_config(n, config)
    p = (provider or "openai").lower().strip()
    # claude_cli authenticates via Claude Code session — just check the binary exists.
    if p == "claude_cli":
        if not model:
            return False
        try:
            import subprocess
            r = subprocess.run(["claude", "--version"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False
    if not api_key or not model:
        return False
    try:
        if p == "anthropic":
            base = (base_url or ANTHROPIC_BASE_URL).rstrip("/")
            url = f"{base}/models"
            headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_API_VERSION}
            resp = requests.get(url, headers=headers, timeout=10)
        elif p == "google":
            base = (base_url or GOOGLE_BASE_URL).rstrip("/")
            url = f"{base}/v1beta/models"
            resp = requests.get(url, headers={"x-goog-api-key": api_key}, timeout=10)
        elif p == "ollama":
            base = (base_url or "https://ollama.com").rstrip("/")
            url = f"{base}/api/tags"
            headers = {}
            if api_key:
                clean = api_key.strip().replace("Bearer ", "").strip()
                headers["Authorization"] = f"Bearer {clean}"
            resp = requests.get(url, headers=headers, timeout=10)
        elif p == "groq":
            base = (base_url or "https://api.groq.com/openai/v1").rstrip("/")
            url = f"{base}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            base = (base_url or OPENAI_BASE_URL).rstrip("/")
            url = f"{base}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 401:
            logger.warning(f"Provider {n} ({provider}) connectivity check: 401 — API key invalid or missing.")
            return False
        if resp.status_code == 429:
            logger.warning(f"Provider {n} ({provider}) connectivity check: 429 — rate-limited (not tripping CB).")
            return False
        return resp.status_code < 300
    except Exception as e:
        logger.debug(f"Provider {n} ({provider}) connectivity check error: {e}")
        return False


def connectivity_worker():
    """Periodic check to verify all configured LLM providers are reachable."""
    while True:
        try:
            config = load_config()
            p1_online = _check_provider_online(1, config)
            p2_online = _check_provider_online(2, config)
            p3_online = _check_provider_online(3, config)
            p4_online = _check_provider_online(4, config)
            state["provider_1_online"] = p1_online
            state["provider_2_online"] = p2_online
            state["provider_3_online"] = p3_online
            state["provider_4_online"] = p4_online
            logger.info(f"Connectivity Check: P1={p1_online}, P2={p2_online}, P3={p3_online}, P4={p4_online}")
        except Exception as e:
            logger.error(f"Connectivity worker error: {e}")
        time.sleep(900)


def heartbeat_worker():
    while True:
        try:
            config = load_config()
            p1_provider, p1_key, p1_model, _ = _get_provider_config(1, config)
            p2_provider, p2_key, p2_model, _ = _get_provider_config(2, config)
            p3_provider, p3_key, p3_model, _ = _get_provider_config(3, config)
            p4_provider, p4_key, p4_model, _ = _get_provider_config(4, config)

            p1_configured = bool(p1_key and p1_model)
            p2_configured = bool(p2_key and p2_model)
            p3_configured = bool(p3_key and p3_model)
            p4_configured = bool(p4_key and p4_model)

            state["provider_1_configured"] = p1_configured
            state["provider_2_configured"] = p2_configured
            state["provider_3_configured"] = p3_configured
            state["provider_4_configured"] = p4_configured
            state["llm_circuit_breaker"] = _llm_cb_snapshot()
            state["provider_credit_cb"] = _provider_credit_cb_snapshot()

            if p1_configured:
                state["active_llm"] = p1_model
            elif p2_configured:
                state["active_llm"] = p2_model
            else:
                state["active_llm"] = "Not configured"
        except Exception as e:
            logger.error(f"Heartbeat worker error: {e}")
        time.sleep(5)

def analyze_issue(issue):
    full_context = f"Issue Title: {issue.title}\nIssue Body: {issue.body}\n\n"
    comments = issue.get_comments()
    for i, comment in enumerate(comments, 1):
        full_context += f"Comment {i}: {comment.body}\n"

    config = load_config()
    strictness = config.get("TRIAGE_STRICTNESS", "Moderate")

    if strictness == "Strict":
        strictness_instruction = "Specifically, for UI or runtime errors, you MUST have full console logs or stack traces. If these are missing, it is non-actionable."
    elif strictness == "Lenient":
        strictness_instruction = "Be generous. If the issue describes a bug and the repository is accessible, mark it as actionable even if full logs are missing, provided there is a plausible lead."
    else:
        strictness_instruction = "Specifically, for UI or runtime errors, prefer console logs or stack traces, but if the description is detailed enough for a senior engineer to hypothesize the bug accurately, mark it as actionable."

    prompt = (
        f"{full_context}\n\n"
        f"Determine if this issue contains enough information to provide a code fix. \n"
        f"{strictness_instruction}\n"
        f"Note: If this is an automated log alert, the provided log snippet is the primary evidence. Do not request a stack trace if a clear error is already present in the logs.\n"
        f"If information is missing, specify exactly what is needed (e.g., 'Please provide the browser console output').\n\n"
        "Return ONLY a JSON object: {\"actionable\": boolean, \"request\": \"message if not actionable\"}"
    )
    try:
        res = call_llm(prompt, system_prompt="You are a triage bot. Only return a JSON object.")
        import re
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data.get("actionable", False), data.get("request", "More information is needed to proceed with a fix.")
        return False, "Information provided is not in a usable format. Please provide more details."
    except Exception as e:
        logger.error(f"Error analyzing issue: {e}")
        return True, ""

def identify_files_to_fix(repo_path, issue_body):
    logger.info("Identifying relevant files for fix...")
    all_files = []
    for root, _, files in os.walk(repo_path):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, repo_path)
            if any(x in rel_path for x in [".git", "node_modules", "__pycache__", "venv", ".env"]):
                continue
            all_files.append(rel_path)
    file_list_str = "\n".join(all_files)
    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Repository File List:\n{file_list_str}\n\n"
        "Identify which files are most likely relevant to fixing this issue. "
        "Return ONLY a JSON array of file paths: [\"path/to/file1\", \"path/to/file2\"]"
    )
    try:
        res = call_llm(prompt, system_prompt="You are a repository analyzer. Only return a JSON array of paths.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []
    except Exception as e:
        logger.error(f"Error identifying files: {e}")
        return []

def prepare_environment(repo_path):
    logger.info("Preparing environment (installing dependencies)...")
    files = os.listdir(repo_path)
    if "package.json" in files:
        logger.info("Detected Node.js project. Running npm install...")
        run_sandboxed_command("npm install", repo_path)
    elif "requirements.txt" in files:
        logger.info("Detected Python project with requirements.txt. Running pip install...")
        run_sandboxed_command("pip install -r requirements.txt", repo_path)
    elif "pyproject.toml" in files:
        logger.info("Detected Python project with pyproject.toml. Running pip install .")
        run_sandboxed_command("pip install .", repo_path)
    elif "go.mod" in files:
        logger.info("Detected Go project. Running go mod download...")
        run_sandboxed_command("go mod download", repo_path)
    elif "Makefile" in files:
        logger.info("Detected Makefile. Attempting 'make install'...")
        run_sandboxed_command("make install", repo_path)
    else:
        logger.info("No known dependency file detected. Skipping installation.")

def review_fix(repo_path, issue_body, proposed_fixes, force_cloud=None, task_id=None, builder_n=None):
    """Run a cross-provider reviewer panel on a proposed fix.

    builder_n: which provider slot (1/2/3) generated the fix being reviewed.
    Reviewers are all OTHER configured providers — the builder is never asked
    to review its own work.  If builder_n is None, it's inferred from force_cloud.

    If a reviewer provider is unavailable (offline, credit-exhausted, or errored):
      - If surviving reviewers reach confidence >= 0.80 with Approve: skip missing reviewer, proceed.
      - Otherwise: return {"status": "pending_review", "reason": ...} so the caller
        can queue the issue for manual approval or retry once providers come back.
    """
    SKIP_CONFIDENCE_THRESHOLD = 0.80  # skip missing reviewer only above this confidence

    logger.info("Running Reviewer Panel pass...")
    config = load_config()

    # Determine which provider built the fix.
    if builder_n is None:
        builder_n = 2 if force_cloud is True else 1

    # Build reviewer panel from all providers EXCEPT the builder.
    reviewers = []
    for n in (1, 2, 3, 4):
        if n == builder_n:
            continue
        provider, key, model, _ = _get_provider_config(n, config)
        if not (key and model):
            continue
        r_model = _get_reviewer_model(n, config) or model
        reviewers.append({"name": f"Reviewer {n} ({provider})", "model": r_model, "provider_n": n})

    if not reviewers:
        logger.warning("No reviewers configured. Falling back to default LLM review.")
        reviewers = [{"name": "Default Reviewer", "model": None, "provider_n": None}]

    # Check if any provider is online at all.
    any_provider_online = any(
        state.get(f"provider_{n}_online", True) for n in (1, 2, 3, 4) if n != builder_n
    )
    if not any_provider_online:
        logger.warning("All reviewer LLM providers appear offline. Signaling retry queue.")
        return {"status": "queue_for_retry", "reason": "all_reviewers_offline"}

    fix_details = ""
    for path, code in proposed_fixes.items():
        fix_details += f"\n--- FILE: {path} ---\n{code}\n"

    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Proposed Fixes:\n{fix_details}\n\n"
        "You are a Skeptical Senior Engineer. Your job is to review this proposed fix. "
        "Check for: \n"
        "1. Does it actually fix the described issue?\n"
        "2. Does it introduce new bugs or regressions?\n"
        "3. Is the code quality acceptable?\n"
        "4. Are there any obvious edge cases missed?\n\n"
        "Return ONLY a JSON object: {\"confidence\": float, \"verdict\": \"Approve\"|\"Reject\", \"critique\": \"detailed explanation\"}"
    )

    votes = []
    failed_reviewers = []
    for r in reviewers:
        try:
            logger.info(f"{r['name']} analyzing fix...")
            res = call_llm(
                prompt,
                system_prompt="You are a skeptical senior engineer. Be critical. Only return JSON.",
                force_provider=r["provider_n"],
                task_id=task_id,
                model_override=r.get("model"),
            )
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if match:
                votes.append({**json.loads(match.group()), "reviewer": r["name"]})
        except Exception as e:
            logger.error(f"{r['name']} failed: {e}")
            failed_reviewers.append(r["name"])

    if not votes:
        return {"confidence": 0.0, "verdict": "Reject", "critique": "All reviewers failed."}

    avg_conf = sum(v.get("confidence", 0.0) for v in votes) / len(votes)
    approvals = [v for v in votes if v.get("verdict") == "Approve"]
    critiques = " | ".join(
        f"[{v.get('reviewer', '?')}] {v.get('critique', '')}" for v in votes
    )

    # If some reviewers were skipped, decide whether to proceed or queue.
    if failed_reviewers:
        if avg_conf >= SKIP_CONFIDENCE_THRESHOLD and len(approvals) == len(votes):
            logger.warning(
                f"Skipped unavailable reviewers {failed_reviewers} — "
                f"surviving panel approved with confidence {avg_conf:.2f} (>= {SKIP_CONFIDENCE_THRESHOLD}). Proceeding."
            )
        else:
            reason = (
                f"Reviewers {failed_reviewers} unavailable and surviving panel confidence "
                f"{avg_conf:.2f} < {SKIP_CONFIDENCE_THRESHOLD} or not unanimous."
            )
            logger.warning(f"Queuing for manual approval: {reason}")
            return {
                "status": "queue_for_retry",
                "reason": reason,
                "partial_confidence": avg_conf,
                "partial_votes": votes,
                "critique": critiques,
            }

    final_verdict = "Approve" if len(approvals) >= (len(votes) / 2 + 0.5) else "Reject"
    return {"confidence": avg_conf, "verdict": final_verdict, "critique": critiques}

def apply_ai_fix(repo_path, issue_body, error_context=None, force_cloud=None, task_id=None, force_provider=None):
    relevant_files = identify_files_to_fix(repo_path, issue_body)
    if not relevant_files:
        logger.warning(f"No specific files identified for issue. Attempting general fix.")
    context_code = ""
    for f_path in relevant_files:
        full_p = os.path.join(repo_path, f_path)
        if os.path.exists(full_p):
            try:
                with open(full_p, 'r') as f:
                    context_code += f"\n--- FILE: {f_path} ---\n{f.read()}\n"
            except Exception as e:
                logger.error(f"Could not read file {f_path}: {e}")
    if error_context:
        prompt = (
            f"Issue: {issue_body}\n\n"
            f"Current Relevant Code:\n{context_code}\n\n"
            f"Previous attempt failed with error:\n{error_context}\n\n"
            "Provide a corrected version of the code. Return ONLY a JSON object with two keys: 'confidence' (a float from 0.0 to 1.0) and 'fixes' (another object where keys are file paths and values are the full new file content). "
            "Example: {\"confidence\": 0.98, \"fixes\": {\"src/main.py\": \"full code here\"}}"
        )
    else:
        prompt = (
            f"Issue: {issue_body}\n\n"
            f"Current Relevant Code:\n{context_code}\n\n"
            "Provide a corrected version of the code. Return ONLY a JSON object with two keys: 'confidence' (a float from 0.0 to 1.0) and 'fixes' (another object where keys are file paths and values are the full new file content). "
            "Example: {\"confidence\": 0.98, \"fixes\": {\"src/main.py\": \"full code here\"}}"
        )
    try:
        return call_llm(prompt, system_prompt="You are a master coder. Only return a JSON object.", force_cloud=force_cloud, task_id=task_id, force_provider=force_provider)
    except Exception as e:
        raise Exception(f"Fix generation failed: {e}")

def parse_and_apply(content, repo_path):
    import re as _re, ast as _ast
    # --- Locate a JSON / Python-dict object in the LLM response ---
    if not content or not content.strip():
        logger.debug("parse_and_apply: empty content — expected retry case.")
        return False, {}, 0.0

    try:
        match = _re.search(r'\{.*\}', content, _re.DOTALL)
        if not match:
            # LLM returned prose / "None" / refusal — no JSON object present.
            # This is a non-error transient failure; caller will retry.
            logger.debug(f"parse_and_apply: no JSON object in response (first 120 chars: {content[:120]!r})")
            return False, {}, 0.0

        raw = match.group()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: some LLMs (Gemini Flash) return Python-style dicts with
            # single quotes instead of JSON double quotes.  ast.literal_eval is
            # safe (only evaluates literals) and handles those cleanly.
            try:
                parsed = _ast.literal_eval(raw)
                if not isinstance(parsed, dict):
                    raise ValueError(f"Expected dict, got {type(parsed).__name__}")
                data = parsed
            except Exception as ast_err:
                logger.error(f"Error parsing or applying JSON fix: {ast_err}")
                logger.debug(f"Failed content: {content[:500]}")
                return False, {}, 0.0

        fixes = data.get("fixes", {})
        confidence = data.get("confidence", 0.0)
        repo_root = os.path.abspath(repo_path)
        applied = {}
        for filepath, code in fixes.items():
            # Confine writes to the cloned repo: reject absolute paths, traversal,
            # and symlinks that escape the repo root (prevents arbitrary file write).
            if not isinstance(filepath, str) or os.path.isabs(filepath) or ".." in filepath.replace("\\", "/").split("/"):
                logger.error(f"Refusing to apply fix with unsafe path: {filepath!r}")
                continue
            full_path = os.path.abspath(os.path.join(repo_root, filepath))
            try:
                if os.path.commonpath([repo_root, full_path]) != repo_root:
                    logger.error(f"Refusing to apply fix escaping repo root: {filepath!r}")
                    continue
            except ValueError:
                logger.error(f"Refusing to apply fix with unresolvable path: {filepath!r}")
                continue
            if os.path.islink(full_path):
                try:
                    link_target = os.path.abspath(os.readlink(full_path))
                    if os.path.commonpath([repo_root, link_target]) != repo_root:
                        logger.error(f"Refusing to write through symlink escaping repo: {filepath!r}")
                        continue
                except Exception:
                    logger.error(f"Refusing to write through unresolvable symlink: {filepath!r}")
                    continue
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(code.strip())
            applied[filepath] = code
            logger.info(f"Applied fix to file: {filepath}")
        if not applied:
            logger.error("No fixes could be applied (all rejected as unsafe or out-of-repo).")
            return False, {}, 0.0
        return True, applied, confidence
    except Exception as e:
        logger.error(f"Error parsing or applying JSON fix: {e}")
        logger.debug(f"Failed content: {content[:500]}")
        return False, {}, 0.0

def _trigger_spoke_updates(config):
    """Trigger a self-update on the Hub, then fan out SPOKE_UPDATE to every approved spoke.

    Called immediately after a fix is pushed to GitHub so services are running the
    new code before the QA service verifies the fix.  Both calls are fire-and-forget;
    actual restarts are asynchronous.  A post-update cooldown is started so transient
    "service offline" errors during restarts don't produce spurious GitHub issues.
    """
    hub_url = (config.get("HUB_QUERY_URL") or "").rstrip("/")
    if not hub_url:
        logger.debug("_trigger_spoke_updates: HUB_QUERY_URL not configured, skipping")
        return
    admin_token = config.get("LM_ADMIN_TOKEN") or os.getenv("LM_ADMIN_TOKEN", "")
    headers = {"X-Admin-Token": admin_token} if admin_token else {}
    # Update the Hub itself first so it is on the latest code before spokes reconnect.
    try:
        r = requests.post(f"{hub_url}/setup/update", headers=headers, timeout=30)
        if r.status_code == 200:
            logger.info("Hub self-update triggered successfully")
        elif r.status_code == 401:
            logger.warning("Hub /setup/update returned 401 — set LM_ADMIN_TOKEN in bugfixer config")
        else:
            logger.warning(f"Hub /setup/update returned HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Could not trigger Hub self-update: {e}")
    # Fan out to all approved spokes (including local lm-dns / lm-dhcp restart).
    try:
        r = requests.post(f"{hub_url}/setup/update/spokes", headers=headers, timeout=30)
        if r.status_code == 200:
            data = r.json()
            logger.info(f"Hub spoke update queued: {data.get('message', '')}")
        elif r.status_code == 401:
            logger.warning("Hub /setup/update/spokes returned 401 — set LM_ADMIN_TOKEN in bugfixer config")
        else:
            logger.warning(f"Hub /setup/update/spokes returned HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Could not trigger Hub spoke update: {e}")
    # Suppress issue filing while services are restarting.
    _set_update_cooldown(config)


def _wait_for_spokes_online(config, min_count=1, timeout=90):
    """Poll Hub GET /status until at least min_count spokes appear in active_connections.

    Spokes reconnect after their systemd unit restarts (~10–20 s).  Returns the list
    of connected spoke IDs, or an empty list on timeout.
    """
    hub_url = (config.get("HUB_QUERY_URL") or "").rstrip("/")
    if not hub_url:
        return []
    deadline = time.time() + timeout
    logger.info(f"Waiting for ≥{min_count} spoke(s) to reconnect (timeout {timeout}s)…")
    while time.time() < deadline:
        try:
            r = requests.get(f"{hub_url}/status", timeout=10)
            if r.status_code == 200:
                conns = r.json().get("active_connections", [])
                if len(conns) >= min_count:
                    logger.info(f"Spokes online: {conns}")
                    return conns
        except Exception:
            pass
        time.sleep(5)
    logger.warning(f"Timed out after {timeout}s waiting for spokes to come back online")
    return []


def _qa_service_verify(repo_name, config, timeout=120):
    """Call the QA service API to run targeted tests for a repo/module.

    Calls POST /api/run?module=<repo_name> and polls GET /api/status until
    COMPLETED or FAILED (or timeout).  Returns (passed: bool, summary: str).
    """
    qa_url = (config.get("QA_API_URL") or "").rstrip("/")
    if not qa_url:
        return None, "QA_API_URL not configured"

    # Map full repo name (owner/name) to just the module name the QA service knows.
    module = repo_name.split("/")[-1] if "/" in repo_name else repo_name

    try:
        # Trigger a targeted test run for this module.
        trigger = requests.post(
            f"{qa_url}/api/run",
            json={"module": module},
            timeout=15,
        )
        if trigger.status_code not in (200, 202):
            return None, f"QA service returned HTTP {trigger.status_code} on trigger"

        # Poll for completion.
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            status_resp = requests.get(f"{qa_url}/api/status", timeout=10)
            if status_resp.status_code != 200:
                continue
            data = status_resp.json()
            status = data.get("status", "")
            if status in ("COMPLETED", "FAILED", "IDLE"):
                results = data.get("results", [])
                passed = sum(1 for r in results if r.get("status") == "PASS")
                total = len(results)
                failed_names = [r["name"] for r in results if r.get("status") != "PASS"]
                summary = f"QA: {passed}/{total} passed"
                if failed_names:
                    summary += f" — failed: {', '.join(failed_names[:5])}"
                return status == "COMPLETED" and passed == total, summary

        return None, f"QA service timed out after {timeout}s"
    except Exception as e:
        return None, f"QA service error: {e}"


def verify_fix(repo_path, repo_name, config):
    logger.info(f"Verifying fix in {repo_path}...")

    # Priority 1: QA service API (when QA_API_URL is configured).
    if config.get("QA_API_URL"):
        passed, summary = _qa_service_verify(repo_name, config)
        if passed is not None:
            if passed:
                logger.info(f"QA service verification passed — {summary}")
                return True, None
            else:
                logger.warning(f"QA service verification failed — {summary}")
                return False, summary
        else:
            logger.warning(f"QA service unreachable ({summary}), falling back to local tests")

    # Priority 2: per-repo explicit test command from config.
    repo_tests = config.get("repo_tests", {})
    test_cmd = repo_tests.get(repo_name)
    if test_cmd:
        logger.info(f"Using per-repo test command for {repo_name}: {test_cmd}")
    else:
        qa_repo = os.getenv("QA_REPO")
        test_cmd = os.getenv("QA_TEST_COMMAND", "pytest")
        if qa_repo:
            logger.info(f"Using external QA repository: {qa_repo}")
            token = os.getenv("GITHUB_TOKEN")
            qa_path = os.path.join(os.path.dirname(repo_path), "qa_suite")
            if not os.path.exists(qa_path):
                url = f"https://{token}@github.com/{qa_repo}.git"
                git.Repo.clone_from(url, qa_path)
                logger.info(f"Cloned QA repository to {qa_path}")
            logger.info(f"Executing QA command: {test_cmd}")
            full_cmd = f"{test_cmd} {repo_path}" if " " not in test_cmd else test_cmd
            result = run_sandboxed_command(full_cmd, qa_path)
            if result.returncode == 0:
                logger.info("External QA tests passed!")
                return True, None
            else:
                error_msg = result.stdout + result.stderr
                logger.error(f"External QA tests failed:\n{error_msg}")
                return False, error_msg
        else:
            if not test_cmd or test_cmd == "pytest":
                files = os.listdir(repo_path)
                if "package.json" in files: test_cmd = "npm test"
                elif "requirements.txt" in files or "pyproject.toml" in files: test_cmd = "python3 -m pytest"
                elif "go.mod" in files: test_cmd = "go test ./..."
                elif "Makefile" in files: test_cmd = "make test"
            if not test_cmd:
                logger.info("No standard test framework detected. Assuming success (blind apply).")
                return True, "No tests found, assuming success"
            logger.info(f"Executing test command: {test_cmd}")
            result = run_sandboxed_command(test_cmd, repo_path)
            if result.returncode == 0:
                logger.info("Tests passed successfully!")
                return True, None
            else:
                error_msg = result.stdout + result.stderr
                logger.error(f"Tests failed:\n{error_msg}")
                return False, error_msg
    result = run_sandboxed_command(test_cmd, repo_path)
    if result.returncode == 0:
        logger.info(f"Per-repo tests for {repo_name} passed!")
        return True, None
    else:
        error_msg = result.stdout + result.stderr
        logger.error(f"Per-repo tests for {repo_name} failed:\n{error_msg}")
        return False, error_msg

def check_for_updates():
    """Checks GitHub for new versions, performs pre-flight syntax checks, and signals a restart if safe."""
    try:
        self_repo = git.Repo(os.getcwd())
        old_commit = self_repo.head.commit.hexsha

        update_state = load_update_state()
        update_state["last_known_good_commit"] = old_commit
        save_update_state(update_state)

        self_repo.remotes.origin.pull()
        new_commit = self_repo.head.commit.hexsha

        if old_commit != new_commit:
            cur_version = get_version()
            logger.info(f"New version detected ({cur_version})! {old_commit[:7]} -> {new_commit[:7]}. Validating...")

            syntax_error = False
            for root, _, files in os.walk(os.getcwd()):
                for file in files:
                    if file.endswith(".py"):
                        full_path = os.path.join(root, file)
                        try:
                            py_compile.compile(full_path, doraise=True)
                        except py_compile.PyCompileError as e:
                            logger.error(f"Syntax error in {full_path}: {e}")
                            syntax_error = True
                            break
                if syntax_error: break

            if syntax_error:
                logger.error(f"Syntax check failed for commit {new_commit[:7]}. Rolling back to {old_commit[:7]}...")
                self_repo.git.reset("--hard", old_commit)
                update_state = load_update_state()
                if new_commit not in update_state["failed_commits"]:
                    update_state["failed_commits"].append(new_commit)
                    save_update_state(update_state)
                return False, f"Update failed: Syntax error detected. Rolled back to {old_commit[:7]}."

            try:
                with open(os.path.join(CONFIG_DIR, "update_pending"), "w") as f:
                    f.write(new_commit)
                logger.info("Update pending signal created. Triggering restart...")
            except Exception as e:
                logger.warning(f"Could not create update_pending signal: {e}")

            # Signal the main loop to restart once the current scan cycle completes
            # rather than killing the process immediately.  Immediate SIGTERM kills
            # in-flight git clone / fix operations and generates spurious self-diagnosis issues.
            state["restart_pending"] = True
            logger.info("Restart deferred — will apply after current scan cycle completes.")
            return True, f"Update found: {cur_version} ({old_commit[:7]} -> {new_commit[:7]}). Restarting..."

        return False, "No updates available."
    except Exception as e:
        logger.warning(f"Self-update check failed: {e}")
        return False, f"Update check failed: {e}"

def updater_worker():
    """Dedicated worker to check for updates every hour."""
    while True:
        try:
            logger.info("Checking for self-updates...")
            check_for_updates()
        except Exception as e:
            logger.error(f"Updater worker error: {e}")
        time.sleep(3600)

def find_existing_pull_request(repo_obj, target_branch, base_branch):
    """Checks whether an open pull request already exists for the given head/base pair."""
    existing_pr = None

    owner = repo_obj.owner.login
    head_param = f"{owner}:{target_branch}"

    try:
        existing_prs = repo_obj.get_pulls(state='open', head=head_param, base=base_branch)
        for pr_item in existing_prs:
            existing_pr = pr_item
            break
    except Exception as e:
        logger.warning(f"Filtered PR check failed for {target_branch} -> {base_branch}: {e}")

    if not existing_pr:
        try:
            all_open_prs = repo_obj.get_pulls(state='open')
            for pr_item in all_open_prs:
                if pr_item.head.ref == target_branch and pr_item.base.ref == base_branch:
                    existing_pr = pr_item
                    break
        except Exception as e:
            logger.warning(f"Manual PR scan failed for {target_branch} -> {base_branch}: {e}")

    return existing_pr

def process_single_issue(repo_name, issue_num, llm_preference=None):
    """Core logic to fix a single issue. Used by poller and manual triggers."""
    global state
    issue_id = f"{repo_name}:{issue_num}"
    try:
        config = load_config()
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            logger.error(f"Manual trigger failed: No GitHub Token configured.")
            return False, "No GitHub Token configured"

        gh_current = Github(token)
        try:
            repo_obj = gh_current.get_repo(repo_name)
            issue = repo_obj.get_issue(int(issue_num))
        except GithubException as ge:
            if ge.status == 410 or ge.status == 404:
                logger.warning(f"Issue {repo_name}:{issue_num} was deleted or not found. Removing from history.")
                processed = load_processed()
                if issue_id in processed:
                    del processed[issue_id]
                    save_processed(processed)
                    state["processed"] = processed
                return False, "Issue deleted"
            raise ge

        # --- Resume from awaiting_review ---
        processed = load_processed()
        issue_info = processed.get(issue_id, {})
        if issue_info.get("status") == "awaiting_review":
            last_attempt = issue_info.get("timestamp")
            if last_attempt:
                try:
                    ts = datetime.fromisoformat(last_attempt)
                    if (datetime.now() - ts).total_seconds() < 3600:
                        logger.info(f"Issue {issue_id} is awaiting review. Next retry in 1 hour.")
                        return False, "Review queued: Cloud LLM offline (retrying in 1 hour)"
                except:
                    pass
            logger.info(f"Resuming review for {issue_id} after 1 hour timeout.")
            # We will use the saved fixes later in the loop.

        update_task_state(task_id=issue_id, task_name=f"Triaging {issue_id}", action="start")
        actionable, request_msg = analyze_issue(issue)

        if not actionable:
            logger.info(f"Issue {repo_name}:{issue_num} is non-actionable: {request_msg}")
            try:
                issue.create_comment(f"🤖 **BugFixer Triage**\n\nThis issue is currently non-actionable. To help me fix this, please provide: {request_msg}")
            except Exception as ce:
                logger.warning(f"Could not post non-actionable comment to {issue_id}: {ce}")
            processed = load_processed()
            processed[f"{repo_name}:{issue_num}"] = {
                "status": "non-actionable",
                "timestamp": datetime.now().isoformat(),
                "reason": request_msg,
                "original_body": issue.body.strip() if issue.body else ""
            }
            save_processed(processed)
            state["processed"] = processed
            update_task_state(task_id=issue_id, action="end")
            return False, f"Non-actionable: {request_msg}"

        force_cloud = None
        force_provider = None
        if llm_preference == "cloud":
            force_cloud = True
        elif llm_preference == "local":
            force_cloud = False
        elif llm_preference == "claude":
            slot = _find_claude_cli_slot(config)
            if slot is None:
                logger.error("Claude CLI fix requested but no claude_cli provider is configured.")
                update_task_state(task_id=issue_id, action="end")
                return False, "Claude CLI is not configured in the LLM Vault."
            force_provider = slot
            logger.info(f"Claude CLI fix requested for {issue_id} — using provider slot {slot}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "repo")
            url = repo_obj.clone_url.replace("https://", f"https://{token}@")
            logger.info(f"Cloning {repo_name} for manual fix...")
            repo_git = git.Repo.clone_from(url, path)

            max_attempts = 3
            success = False
            error_context = None
            final_verdict = "Reject"
            final_confidence = 0.0
            base_branch = config.get("default_branch", "main")

            for attempt in range(1, max_attempts + 1):
                try:
                    update_task_state(task_id=issue_id, task_name=f"Fix Attempt {attempt}/{max_attempts} for {issue_id}", action="start")
                    logger.info(f"AI Fix Attempt {attempt}/{max_attempts} for {repo_name}:{issue_num}...")

                    # --- Resume from awaiting_review ---
                    pending_fix = issue_info.get("pending_fix") if attempt == 1 and issue_info.get("status") == "awaiting_review" else None
                    if pending_fix:
                        logger.info(f"Resuming from queued review for {issue_id} using saved fix.")
                        success_applied, fixes, confidence = parse_and_apply(json.dumps(pending_fix), path)
                        # Clear the pending status now that we're processing it
                        processed = load_processed()
                        if issue_id in processed:
                            processed[issue_id]["status"] = "processing"
                            save_processed(processed)
                    elif not pending_fix:
                        fix_code = apply_ai_fix(path, issue.body or "", error_context, force_cloud=force_cloud, task_id=issue_id, force_provider=force_provider)
                        success_applied, fixes, confidence = parse_and_apply(fix_code, path)

                    if not success_applied:
                        verified = False
                        failure_msg = "AI generated invalid JSON format"
                    else:
                        if config.get("skip_review", False):
                            logger.info("Skeptical Reviewer bypassed by configuration.")
                            review_conf = confidence
                            review_verdict = "Approve"
                        else:
                            update_task_state(task_id=issue_id, task_name=f"Reviewing {issue_id}", action="start")
                            review = review_fix(path, issue.body or "", fixes, force_cloud=force_cloud, task_id=issue_id, builder_n=force_provider)

                            # --- Handle Queue for Retry ---
                            if isinstance(review, dict) and review.get("status") == "queue_for_retry":
                                logger.info(f"Review queued for {issue_id}: Cloud LLM offline. Saving fix for retry in 1 hour.")
                                processed = load_processed()
                                processed[issue_id] = {
                                    "status": "awaiting_review",
                                    "timestamp": datetime.now().isoformat(),
                                    "pending_fix": {"confidence": confidence, "fixes": fixes},
                                    "original_body": issue.body.strip() if issue.body else ""
                                }
                                save_processed(processed)
                                state["processed"] = processed
                                update_task_state(task_id=issue_id, action="end")
                                return False, "Cloud offline: Review queued for retry in 1 hour."

                            review_conf = review.get("confidence", 0.0)
                            review_verdict = review.get("verdict", "Reject")

                        if config.get("qa_enabled", True):
                            prepare_environment(path)
                            update_task_state(task_id=issue_id, task_name=f"Verifying {issue_id}", action="start")
                            verified, failure_msg = verify_fix(path, repo_name, config)
                        else:
                            logger.info("QA Testing disabled. Assuming verified.")
                            verified, failure_msg = True, "QA disabled"

                        if verified:
                            success = True
                            state["success_count"] += 1
                            final_confidence = (confidence + review_conf) / 2
                            final_verdict = review_verdict
                            break
                        else:
                            error_context = failure_msg
                except Exception as inner_e:
                    if "No LLM providers" in str(inner_e):
                        logger.error(f"No LLM providers configured for issue {issue_id}: {inner_e}")
                        update_task_state(task_id=issue_id, action="end")
                        return False, "No LLM providers configured"
                    raise

            if not success:
                state["failure_count"] += 1
                failure_reason = "AI failed to find a verified fix after max attempts."
                if error_context:
                    failure_reason += f" Last attempt error: {error_context}"

                try:
                    issue.create_comment(f"🤖 **BugFixer Failure**\n\nI attempted to fix this issue {max_attempts} times, but I could not find a solution that passed verification.\n\n**Final Error:** `{failure_reason}`")
                except Exception as ce:
                    logger.warning(f"Could not post failure comment to {issue_id}: {ce}")

                processed = load_processed()
                processed[f"{repo_name}:{issue_num}"] = {
                    "status": "failed",
                    "timestamp": datetime.now().isoformat(),
                    "error": failure_reason,
                    "original_body": issue.body
                }
                save_processed(processed)
                state["processed"] = processed
                update_task_state(task_id=issue_id, action="end")
                return False, failure_reason

            # Triage-only mode: discard the generated changes, post a comment, defer fix.
            if _is_triage_only():
                try:
                    repo_git.git.reset("--hard", "HEAD")
                    repo_git.git.clean("-fd")
                except Exception:
                    pass
                try:
                    mode_reason = "Blackout active" if state.get("blackout") else "Triage-only mode enabled"
                    issue.create_comment(
                        f"🔍 **BugFixer Triage** — A fix has been identified for this issue.\n\n"
                        f"Fix commit is being held back ({mode_reason}). "
                        f"BugFixer will apply the fix automatically once restrictions are lifted."
                    )
                    issue.add_to_labels("bugfixer-triaged")
                except Exception as ce:
                    logger.warning(f"Could not post triage comment to {issue_id}: {ce}")
                processed = load_processed()
                processed[f"{repo_name}:{issue_num}"] = {
                    "status": "triaged",
                    "timestamp": datetime.now().isoformat(),
                    "reason": "Fix identified; commit deferred (triage-only mode)",
                    "original_body": issue.body or "",
                }
                save_processed(processed)
                state["processed"] = processed
                update_task_state(task_id=issue_id, action="end")
                return True, "Triaged — fix identified, commit deferred"

            repo_git.git.add(A=True)

            confidence_threshold = 0.95
            is_trusted = (repo_name in config["trusted_repos"]) or (repo_name == resolve_self_diagnosis_repo(config))
            bot_user = gh_current.get_user().login
            is_owner = repo_obj.owner.login == bot_user
            direct_push_setting = config.get("direct_push_enabled")
            can_direct_push = direct_push_setting and is_trusted and is_owner

            logger.info(f"Deployment decision for {repo_name}: DirectPushSetting={direct_push_setting}, IsTrusted={is_trusted}, IsOwner={is_owner} -> can_direct_push={can_direct_push}")


            version_bumped = False
            new_v = None
            can_actually_direct_push = False
            if can_direct_push and final_verdict == "Approve":
                new_v = bump_repo_version(path)
                if new_v:
                    version_bumped = True
                    logger.info(f"Bumped target repository {repo_name} version to {new_v}")

                try:
                    repo_git.remotes.origin.push(f"HEAD:{base_branch}")
                    can_actually_direct_push = True
                    decision_reason = "Trusted repo & approved"
                except Exception as pe:
                    logger.warning(f"Direct push failed for {repo_name} ({pe}). Attempting rebase...")
                    try:
                        repo_git.remotes.origin.pull(base_branch, rebase=True)
                        repo_git.remotes.origin.push(f"HEAD:{base_branch}")
                        can_actually_direct_push = True
                        decision_reason = "Trusted repo & approved (after rebase)"
                        logger.info(f"Push successful after rebase for {repo_name}")
                    except Exception as re_err:
                        logger.warning(f"Direct push failed even after rebase: {re_err}. Falling back to PR.")
                        decision_reason = f"Direct push failed: {re_err}"
                        can_actually_direct_push = False

            commit_msg = f"AI Fix #{issue.number}: {issue.title[:50]}..."
            if version_bumped:
                commit_msg += f" (Version Bump to {new_v})"
            repo_git.index.commit(commit_msg)

            if can_actually_direct_push:
                logger.info(f"Decision: Direct Commit to {base_branch}. Reason: {decision_reason}")
                commit_type = "Direct Commit"
                detail_msg = f"The fix was verified and pushed directly to the {base_branch} branch. Avg Confidence: {final_confidence:.2%}"
                # Fix is live on main — tell every spoke to pull and restart, then let
                # the QA service verify against the updated code.
                _trigger_spoke_updates(config)
                _wait_for_spokes_online(config, min_count=1, timeout=90)
            else:
                reason = "Skeptical Reviewer rejected" if final_verdict != "Approve" else (decision_reason if not can_direct_push or "Direct push failed" in decision_reason else "Trust/Ownership requirements not met")
                decision_reason = reason
                logger.info(f"Decision: Pull Request. Reason: {reason}.")
                target_branch = config.get("dev_branch", "dev") if final_confidence < confidence_threshold else f"ai-fix-issue-{issue.number}"
                try:
                    repo_git.git.checkout(target_branch)
                except:
                    repo_git.create_head(target_branch).checkout()
                repo_git.remotes.origin.push(target_branch, force=True)
                base_branch = config.get("default_branch", "main")

                existing_pr = find_existing_pull_request(repo_obj, target_branch, base_branch)

                if existing_pr:
                    pr = existing_pr
                    logger.info(f"Found existing open PR for {target_branch} -> {base_branch}: {pr.html_url}")
                else:
                    try:
                        pr = repo_obj.create_pull(
                            title=f"AI Fix #{issue.number}",
                            body=f"Automated fix for issue #{issue.number}. Avg Confidence: {final_confidence:.2%}",
                            head=target_branch,
                            base=base_branch
                        )
                        logger.info(f"Created new PR for {target_branch} -> {base_branch}: {pr.html_url}")
                    except GithubException as ge:
                        if ge.status == 422:
                            logger.warning(
                                f"PR creation returned 422 (likely already exists for "
                                f"{target_branch} -> {base_branch}). Re-checking for existing PR..."
                            )
                            time.sleep(2)
                            existing_pr = find_existing_pull_request(repo_obj, target_branch, base_branch)
                            if existing_pr:
                                pr = existing_pr
                                logger.info(
                                    f"Found existing open PR after 422 error: {pr.html_url}"
                                )
                            else:
                                logger.error(
                                    f"Could not find existing PR after 422 error for "
                                    f"{target_branch} -> {base_branch}. Re-raising."
                                )
                                raise ge
                        else:
                            raise ge

                commit_type = "Pull Request"
                detail_msg = f"The fix was verified and a Pull Request has been created on branch {target_branch}: {pr.html_url}"


            files_list = ", ".join(fixes.keys()) if fixes else "No files changed"
            commit_hash = repo_git.head.commit.hexsha

            comment_body = (
                f"🤖 **BugFixer AI Update**\n\n"
                f"The issue has been successfully resolved via {commit_type}.\n"
                f"{detail_msg}\n\n"
                f"**Changes:**\n- Files modified: `{files_list}`\n- Commit: `{commit_hash[:7]}`\n\n"
                f"Verification: ✅ Tests passed successfully."
            )
            try:
                issue.create_comment(comment_body)
            except Exception as ce:
                logger.warning(f"Could not post success comment to {issue_id}: {ce}")

            is_log_detected = "log-detected" in [lbl.name for lbl in issue.get_labels()]
            if not is_log_detected:
                issue.edit(state='closed')

            processed = load_processed()
            processed[f"{repo_name}:{issue_num}"] = {
                "status": "fixed" if not is_log_detected else "awaiting_prod_verification",
                "timestamp": datetime.now().isoformat(),
                "commit": commit_hash,
                "commit_msg": commit_msg,
                "files": list(fixes.keys()),
                "commit_type": commit_type,
                "decision_reason": decision_reason,
                "original_body": issue.body
            }

            save_processed(processed)
            state["processed"] = processed
            state["daily_fixes_count"] = state.get("daily_fixes_count", 0) + 1

            update_task_state(task_id=issue_id, action="end")
            return True, f"Fixed via {commit_type}"

    except Exception as e:
        logger.exception(f"Error in process_single_issue: {e}")
        try:
            update_task_state(task_id=issue_id, action="end")
        except Exception as cleanup_err:
            logger.error(f"Failed to clean up task state for {issue_id}: {cleanup_err}")
        return False, str(e)

def verify_production_fixes(gh_current, processed):
    """Verify issues that were 'fixed' but are awaiting log confirmation.
    Now implements a configurable 'cooling period' (PROD_VERIFICATION_DAYS).
    The issue is only closed if the error snippet has been absent for the full period.
    """
    config = load_config()
    days_required = int(config.get("PROD_VERIFICATION_DAYS", 7))

    # Fetch hub logs once for the whole verification pass.
    hub_logs_cache = get_hub_logs()

    for issue_id, info in list(processed.items()):
        if info.get("status") == "awaiting_prod_verification":
            repo_name, issue_num = issue_id.split(":")
            logger.info(f"Verifying production fix for {issue_id} (Required clean period: {days_required} days)...")
            try:
                repo_obj = gh_current.get_repo(repo_name)
                issue = repo_obj.get_issue(int(issue_num))

                logs = hub_logs_cache
                if logs:
                    module_name = repo_name.split('/')[-1]
                    relevant_logs = [l['log'] for l in logs if l.get('module') == module_name]
                    full_log_text = "\n".join(relevant_logs)

                    import re
                    match = re.search(r"### Log Evidence:\n```\n(.*?)\n```", issue.body, re.DOTALL)
                    if match:
                        snippet = match.group(1).strip()
                        if snippet.lower() not in full_log_text.lower():
                            # Snippet is gone. Check if we've been clean long enough.
                            clean_since = info.get("clean_since")
                            now = datetime.now()

                            if not clean_since:
                                logger.info(f"Issue {issue_id} is clean. Starting {days_required}-day cooling period.")
                                info["clean_since"] = now.isoformat()
                                processed[issue_id] = info
                                save_processed(processed)
                            else:
                                first_clean_ts = datetime.fromisoformat(clean_since)
                                days_clean = (now - first_clean_ts).days
                                if days_clean >= days_required:
                                    logger.info(f"Verified: Issue {issue_id} has been clean for {days_clean} days. Closing issue.")
                                    try:
                                        issue.create_comment(f"🤖 **BugFixer AI Verification**\n\nProduction logs have been scanned and the error is no longer detected. The issue has remained clean for {days_required} days. Closing issue.")
                                    except Exception as ce:
                                        logger.warning(f"Could not post verification comment to {issue_id}: {ce}")
                                    issue.edit(state='closed')
                                    processed[issue_id]["status"] = "verified"
                                    state["success_count"] += 1
                                    save_processed(processed)
                                else:
                                    logger.info(f"Issue {issue_id} is clean, but only for {days_clean}/{days_required} days. Waiting...")
                        else:
                            # Error reappeared. Reset the clean timer.
                            if info.get("clean_since"):
                                logger.warning(f"Issue {issue_id} error reappeared in logs. Resetting cooling period.")
                                info["clean_since"] = None
                                processed[issue_id] = info
                                save_processed(processed)
                            logger.info(f"Issue {issue_id} still failing in production logs.")
            except Exception as e:
                logger.error(f"Error verifying {issue_id}: {e}")

def scan_hub_logs(gh_current, config):
    """Phase: Scan Hub for new errors and create GitHub issues."""
    global state
    update_task_state(task_id="HubScan", task_name="Scanning Hub Logs", action="start")
    logger.info("Scanning Hub for new errors...")
    try:
        hub_logs = get_hub_logs()
        if hub_logs:
            # Scrub to error-relevant entries only before paying for an LLM
            # call: keeps the prompt small (avoids context-overflow 500s) and
            # focuses the model on actionable errors instead of INFO noise.
            error_logs = filter_error_logs(hub_logs)
            logger.info(
                f"Hub logs scrubbed: {len(hub_logs)} entries -> {len(error_logs)} "
                f"error-relevant entries for LLM analysis."
            )
            actionable_errors = []
            if not error_logs:
                logger.info("No error-level Hub log entries this cycle. Skipping LLM analysis.")
            else:
                actionable_errors = analyze_logs_for_errors(error_logs)
            monitored_repos = get_monitored_repos(config)
            for error in actionable_errors:
                # Defensive: ensure error is a dict (analyze_logs_for_errors already
                # guarantees this, but we double-check to be absolutely safe).
                if not isinstance(error, dict):
                    logger.warning(f"Skipping non-dict actionable error: {error!r}")
                    continue
                if not error.get('body') or not str(error.get('body')).strip():
                    logger.warning(f"Skipping actionable error with no body specified: {error.get('title')}")
                    continue

                # Route the issue to the module's own repo rather than relying on
                # the LLM's repo guess (which previously dumped everything into the
                # self-diagnosis repo). The module is authoritative.
                module = error.get('module')
                repo_name = resolve_module_repo(module, monitored_repos, config)
                if not repo_name:
                    # Fall back to the LLM's repo hint only if it is itself a
                    # monitored repo (so we never file into an arbitrary repo).
                    llm_repo = error.get('repo') or ''
                    if llm_repo and llm_repo in monitored_repos:
                        repo_name = llm_repo
                    else:
                        source_info = error.get('source_data', {})
                        host_info = source_info.get('host', 'unknown host') if isinstance(source_info, dict) else 'unknown source'
                        logger.warning(
                            f"Skipping actionable error for module={module!r} (source host: {host_info}): no monitored repo "
                            f"maps to this module (LLM repo hint={llm_repo!r}). Add a "
                            f"'module_repo_map' entry in Settings if this module should be tracked."
                        )
                        continue
                # Make the resolved repo authoritative for downstream code.
                error['repo'] = repo_name
                try:
                    repo_obj = gh_current.get_repo(repo_name)
                    create_automated_issue(gh_current, monitored_repos, repo_obj, error)
                    logger.info(f"Handled automated issue for log error in {repo_name} (module={module})")
                except GithubException as ge:
                    if ge.status == 404:
                        logger.error(
                            f"Cannot create automated issue for '{repo_name}': repository not found (404). "
                            f"Verify that '{repo_name}' exists and the configured GITHUB_TOKEN has access. "
                            f"Skipping this error."
                        )
                    else:
                        logger.error(f"Failed to create auto-issue for {repo_name}: {ge}")
                except Exception as e:
                    logger.error(f"Failed to create auto-issue for {repo_name}: {e}")
    except Exception as e:
        logger.error(f"Hub log scan failed: {e}")
    finally:
        update_task_state(task_id="HubScan", action="end")

def scan_repo_issues(gh_current, config, processed):
    """Phase: Scan monitored repos for issues and attempt fixes concurrently."""
    global state
    bot_user = gh_current.get_user().login

    monitored_repos = get_monitored_repos(config)
    max_workers = int(config.get("MAX_CONCURRENT_FIXES", 5))

    try:
        for repo_name in monitored_repos:
            update_task_state(task_id="RepoScan", task_name=f"Checking {repo_name}", action="start")
            logger.info(f"Scanning repository: {repo_name}")
            try:
                repo_obj = gh_current.get_repo(repo_name)
                is_owner = repo_obj.owner.login == bot_user
                labels = config.get("monitored_labels", ["automated-fix"])
                if "NONE" in labels:
                    continue
                if "ANY" in labels:
                    issues = repo_obj.get_issues(state="open")
                else:
                    issues = repo_obj.get_issues(labels=labels, state="open")

                sched = _schedule_check(config)
                critical_label = (config.get("SCHEDULER_CRITICAL_LABEL") or "").strip()

                to_fix = []
                critical_to_fix = []
                for issue in issues:
                    try:
                        if issue.state != 'open' or issue.pull_request:
                            continue

                        # Skip issues carrying the 'bugfixer-dismissed' label — they were
                        # intentionally marked as not real. Remove the label to resume processing.
                        if any(lbl.name == "bugfixer-dismissed" for lbl in issue.labels):
                            logger.debug(f"Skipping {repo_name}#{issue.number} — 'bugfixer-dismissed' label present.")
                            continue

                        issue_id = f"{repo_name}:{issue.number}"
                        if issue_id in processed:
                            status = processed[issue_id].get("status")
                            if status in ["fixed", "non-actionable", "failed", "awaiting_prod_verification"]:
                                if status != "awaiting_review": # Allow resuming reviews
                                    continue
                            if status == "awaiting_local":
                                pass
                            # For awaiting_review, we let it proceed to check the 1-hour timer in process_single_issue

                        issue_label_names = {lbl.name for lbl in issue.labels}
                        if critical_label and critical_label in issue_label_names:
                            critical_to_fix.append((repo_name, issue.number))
                        else:
                            to_fix.append((repo_name, issue.number))
                    except Exception as e:
                        logger.exception(f"Failed to triage issue {issue_id}: {e}")

                # Normal issues respect the scheduler; critical issues always run.
                if not sched["allowed"] and to_fix:
                    logger.info(
                        f"Scheduler: deferring {len(to_fix)} issue(s) in {repo_name} — {sched['reason']}"
                    )
                    to_fix = []
                if critical_to_fix:
                    logger.info(
                        f"Critical-label override: processing {len(critical_to_fix)} critical issue(s) "
                        f"in {repo_name} regardless of schedule."
                    )
                all_to_fix = critical_to_fix + to_fix

                if all_to_fix:
                    available, soonest_s = _any_provider_available(config)
                    if not available:
                        eta_min = round(soonest_s / 60, 1)
                        logger.warning(
                            f"All LLM providers are in cooldown — skipping {len(all_to_fix)} issue(s) "
                            f"in {repo_name}. Soonest provider available in ~{eta_min} min. "
                            f"Will retry on next scan cycle."
                        )
                    else:
                        max_per_cycle = int(config.get("MAX_ISSUES_PER_CYCLE") or 15)
                        # Cap applies to normal issues only; critical issues are always included.
                        capped_normal = to_fix[:max_per_cycle]
                        deferred = len(to_fix) - len(capped_normal)
                        if deferred > 0:
                            logger.info(
                                f"Found {len(all_to_fix)} issues in {repo_name} — processing first {max_per_cycle} "
                                f"normal + {len(critical_to_fix)} critical this cycle, {deferred} deferred."
                            )
                        else:
                            logger.info(f"Found {len(all_to_fix)} issues to process in {repo_name} (max workers={max_workers}).")
                        batch = critical_to_fix + capped_normal
                        with ThreadPoolExecutor(max_workers=max_workers) as executor:
                            futures = [executor.submit(process_single_issue, r, n) for r, n in batch]
                            for future in futures:
                                future.result()

            except GithubException as ge:
                if ge.status == 404:
                    logger.error(
                        f"Monitored repository '{repo_name}' not found or inaccessible (404). "
                        f"Remove it from monitored_repos or verify GITHUB_TOKEN access. Skipping."
                    )
                else:
                    logger.exception(f"GitHub API error while processing {repo_name}: {ge}")
            except Exception as e:
                logger.exception(f"Unexpected error while processing {repo_name}: {e}")
    except Exception as e:
        logger.exception(f"scan_repo_issues failed: {e}")
    finally:
        update_task_state(task_id="RepoScan", action="end")

def resolve_self_diagnosis_repo(config):
    """Resolves the target repository for self-diagnosis issues.

    Priority:
      1. Explicit 'self_diagnosis_repo' config key (preferred, user-configurable).
      2. Git remote origin URL of the running BugFixer checkout (best-effort).

    Returns the normalized 'owner/repo' string, or None if no valid target could
    be determined. Callers MUST handle a None return by skipping self-diagnosis
    instead of attempting to create issues against a hardcoded fallback that may
    not exist (which previously caused 404 errors every scan cycle).
    """
    self_repo_name = (config.get("self_diagnosis_repo") or "").strip()

    if not self_repo_name:
        try:
            repo = git.Repo(os.getcwd())
            remote_url = repo.remotes.origin.url
            import re
            match = re.search(r'github\.com[:/]([^/]+/[^./]+)', remote_url)
            if match:
                self_repo_name = match.group(1).replace('.git', '')
        except Exception as e:
            logger.debug(f"Could not determine self-repo name from git remote: {e}")

    if not self_repo_name:
        return None

    return clean_repo_name(self_repo_name)

def _file_inode(path):
    """Returns the inode of a file, or None if it cannot be stat'd."""
    try:
        return os.stat(path).st_ino
    except Exception:
        return None

def load_self_scan_offset():
    """Returns (offset, inode) of the last self-scan read position, or (None, None)."""
    try:
        with open(SELF_SCAN_OFFSET_FILE, "r") as f:
            data = json.load(f)
        return data.get("offset"), data.get("inode")
    except Exception:
        return None, None

def save_self_scan_offset(offset, inode):
    """Persists the last-read byte offset and inode for incremental self-scans."""
    try:
        with open(SELF_SCAN_OFFSET_FILE, "w") as f:
            json.dump({"offset": offset, "inode": inode}, f)
    except Exception as e:
        logger.debug(f"Could not save self-scan offset: {e}")

def _empty_chats_store():
    """Returns a fresh multi-conversation store with one untitled, active chat."""
    conv = {"id": "c1", "title": "", "created": datetime.now().isoformat(), "messages": []}
    return {"next_id": 2, "active_id": "c1", "conversations": [conv]}

def _title_from_message(content):
    """Derives a short conversation title from the first user message."""
    title = (content or "").strip().splitlines()[0] if content else ""
    return title[:60]

def load_chats():
    """Returns the persisted multi-conversation store.

    Schema: {"next_id": int, "active_id": str|None, "conversations": [
        {"id": str, "title": str, "created": str, "messages": [{role,content,ts}]}
    ]}. Transparently migrates a legacy flat message list (pre-V.48 single-thread
    chat_history.json) into the first conversation so no history is lost.
    """
    with _chat_lock:
        try:
            with open(CHAT_HISTORY_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            return _empty_chats_store()

        if isinstance(data, list):
            # Legacy flat single-thread history -> wrap into one conversation.
            first_user = next((m.get("content", "") for m in data
                               if isinstance(m, dict) and m.get("role") == "user"), "")
            store = {
                "next_id": 2,
                "active_id": "c1",
                "conversations": [{
                    "id": "c1",
                    "title": _title_from_message(first_user) or "Chat 1",
                    "created": datetime.now().isoformat(),
                    "messages": [m for m in data if isinstance(m, dict)],
                }],
            }
            save_chats(store)
            return store

        if not isinstance(data, dict) or not isinstance(data.get("conversations"), list):
            return _empty_chats_store()

        store = {
            "next_id": int(data.get("next_id", 1) or 1),
            "active_id": data.get("active_id"),
            "conversations": [c for c in data["conversations"] if isinstance(c, dict)],
        }
        if not store["conversations"]:
            return _empty_chats_store()
        if not store["active_id"] or not get_conversation(store, store["active_id"]):
            store["active_id"] = store["conversations"][-1]["id"]
        return store

def save_chats(store):
    """Persists the whole multi-conversation store under _chat_lock."""
    with _chat_lock:
        try:
            with open(CHAT_HISTORY_FILE, "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save chats to {CHAT_HISTORY_FILE}: {e}")

def get_conversation(store, chat_id):
    """Returns the conversation dict for chat_id, or None if not found."""
    for c in store.get("conversations", []):
        if c.get("id") == chat_id:
            return c
    return None

def append_chat_message(chat_id, msg):
    """Atomically appends a message to a conversation and persists the store.

    Auto-titles the conversation from the first user message if it is untitled.
    Sets the conversation as active. Returns the message dict, or None if the
    conversation does not exist.
    """
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            return None
        conv["messages"].append(msg)
        if msg.get("role") == "user" and not conv.get("title"):
            conv["title"] = _title_from_message(msg.get("content", ""))
        store["active_id"] = chat_id
        save_chats(store)
        return msg

def create_conversation():
    """Creates a new empty conversation, makes it active, and persists. Returns its id."""
    with _chat_lock:
        store = load_chats()
        cid = f"c{store['next_id']}"
        store["next_id"] += 1
        store["conversations"].append({
            "id": cid,
            "title": "",
            "created": datetime.now().isoformat(),
            "messages": [],
        })
        store["active_id"] = cid
        save_chats(store)
        return cid

def rename_conversation(chat_id, title):
    """Renames a conversation. Returns True if found and updated."""
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            return False
        conv["title"] = (title or "").strip()[:120]
        save_chats(store)
        return True

def delete_conversation(chat_id):
    """Deletes a conversation and selects a new active one. Returns the new active id."""
    with _chat_lock:
        store = load_chats()
        store["conversations"] = [c for c in store["conversations"] if c.get("id") != chat_id]
        if not store["conversations"]:
            store = _empty_chats_store()
        else:
            store["active_id"] = store["conversations"][-1]["id"]
        save_chats(store)
        return store["active_id"]

def set_active_chat(chat_id):
    """Sets the active conversation if chat_id exists. Returns True on success."""
    with _chat_lock:
        store = load_chats()
        if not get_conversation(store, chat_id):
            return False
        store["active_id"] = chat_id
        save_chats(store)
        return True

def scan_self_logs(gh_current, config):
    """Scans BugFixer's own logs and creates GitHub issues for internal errors.

    The target repository for self-diagnosis issues is resolved via
    resolve_self_diagnosis_repo(), which honors the 'self_diagnosis_repo' config
    key. If no valid repository can be determined, or if the resolved repository
    is not accessible (e.g., 404), self-diagnosis is skipped gracefully rather
    than crashing or spamming the logs with 404 errors every cycle.
    """
    global state
    update_task_state(task_id="SelfScan", task_name="Scanning Self Logs", action="start")
    logger.info("Scanning internal BugFixer logs for errors...")

    # Resolve and validate the target repository for self-diagnosis issues.
    self_repo_name = resolve_self_diagnosis_repo(config)

    if not self_repo_name:
        logger.warning(
            "Self-diagnosis repository is not configured. Set 'self_diagnosis_repo' in the "
            "BugFixer settings (http://localhost:8000/settings) to a valid, accessible "
            "'owner/repo' GitHub repository where self-diagnosis issues should be filed. "
            "Skipping self-log scan until configured."
        )
        update_task_state(task_id="SelfScan", action="end")
        return

    # Pre-validate that the target repository exists and is accessible with the
    # configured token. We catch 404 (and other GitHubExceptions) explicitly so a
    # misconfigured or inaccessible repo does not produce recurring 404 errors
    # in the logs every scan cycle.
    try:
        repo_obj = gh_current.get_repo(self_repo_name)
    except GithubException as ge:
        if ge.status == 404:
            logger.error(
                f"Self-diagnosis target repository '{self_repo_name}' was not found or is "
                f"inaccessible (404 Not Found). The configured GITHUB_TOKEN may lack access, "
                f"or the repository does not exist. Update 'self_diagnosis_repo' in the "
                f"BugFixer settings (http://localhost:8000/settings) to point at a valid, "
                f"accessible repository. Skipping self-log scan."
            )
        else:
            logger.error(
                f"Cannot access self-diagnosis repository '{self_repo_name}' "
                f"(GitHub API status {ge.status}): {ge}. Skipping self-log scan."
            )
        update_task_state(task_id="SelfScan", action="end")
        return
    except Exception as e:
        logger.error(
            f"Cannot access self-diagnosis repository '{self_repo_name}': {e}. "
            f"Skipping self-log scan."
        )
        update_task_state(task_id="SelfScan", action="end")
        return

    log_path = get_log_path()
    if not os.path.exists(log_path):
        logger.warning(f"BugFixer log file not found at {log_path}")
        update_task_state(task_id="SelfScan", action="end")
        return

    try:
        # Only analyze log lines appended SINCE the last self-scan, not the
        # entire historical log. Previously this read the whole file every
        # cycle, so stale errors (old 500s/401s, already-fixed exceptions) were
        # re-analyzed and re-filed as new GitHub issues every single cycle — the
        # "self-diagnosis issue storm" (#400-#409 etc.). We persist a byte offset
        # + inode; on first run or log rotation we skip straight to the current
        # end so historical content is never reported.
        current_size = os.path.getsize(log_path)
        current_inode = _file_inode(log_path)
        last_offset, last_inode = load_self_scan_offset()

        if last_inode is None or last_offset is None or last_inode != current_inode:
            # First ever scan, or the log was rotated/recreated: start at the
            # current end so we only capture errors logged from now on.
            start_offset = current_size
        elif last_offset > current_size:
            # Same file but it shrank (truncated in place): skip to the new end.
            start_offset = current_size
        else:
            start_offset = last_offset

        with open(log_path, "r") as f:
            f.seek(start_offset)
            new_text = f.read()

        # Persist the new read position. Saved immediately after reading so a
        # crash or filing failure never causes the same lines to be re-read.
        save_self_scan_offset(os.path.getsize(log_path), current_inode)

        # Patterns that are transient/expected and should never become GitHub issues.
        _SELF_SCAN_SKIP = (
            "exit code(-15)",   # SIGTERM from a planned restart — not a real bug
            "exit code(-9)",    # SIGKILL (watchdog kill) — symptom not cause
            "GitCommandError",  # git killed by restart signal (covered by -15 above)
            "systemctl restart",
            "Update pending signal",
            "Triggering restart",
        )

        formatted_logs = []
        for line in new_text.splitlines():
            if "[ERROR]" in line or "[CRITICAL]" in line:
                if any(skip in line for skip in _SELF_SCAN_SKIP):
                    continue
                ts = line[:23] if len(line) > 23 else "Unknown"
                formatted_logs.append({
                    "module": "bugfixer-core",
                    "timestamp": ts,
                    "log": line.strip()
                })

        logger.info(
            f"Self-scan read {len(new_text)} new byte(s) from offset {start_offset} "
            f"(file size {current_size}, inode {current_inode}); "
            f"{len(formatted_logs)} new error line(s) this cycle."
        )

        if not formatted_logs:
            update_task_state(task_id="SelfScan", action="end")
            return

        # Dedupe + cap recurring self-errors before LLM analysis: the same
        # error is logged many times per cycle, and sending every copy bloats
        # the prompt and yields duplicate issues.
        scrubbed_self_logs = filter_error_logs(formatted_logs)
        logger.info(
            f"Self logs scrubbed: {len(formatted_logs)} -> {len(scrubbed_self_logs)} "
            f"unique error entries for LLM analysis."
        )
        actionable_errors = analyze_logs_for_errors(scrubbed_self_logs)
        if not actionable_errors:
            update_task_state(task_id="SelfScan", action="end")
            return

        monitored_repos = get_monitored_repos(config)
        for error in actionable_errors:
            # Defensive: ensure error is a dict before mutation and access.
            if not isinstance(error, dict):
                logger.warning(f"Skipping non-dict self-diagnosis error: {error!r}")
                continue
            error['repo'] = self_repo_name
            if not error.get('body') or not str(error.get('body')).strip():
                logger.warning(f"Skipping self-diagnosis error with no body specified: {error.get('title')}")
                continue
            try:
                create_automated_issue(gh_current, monitored_repos, repo_obj, error)
                logger.info(f"Handled self-diagnosis issue for BugFixer: {error.get('title')}")
            except GithubException as ge:
                if ge.status == 404:
                    logger.error(
                        f"Self-diagnosis repository '{self_repo_name}' returned 404 while "
                        f"creating issue for '{error.get('title')}'. Repository may have been "
                        f"deleted or token access revoked. Skipping."
                    )
                else:
                    logger.error(f"Failed to create self-diagnosis issue: {ge}")
            except Exception as e:
                logger.error(f"Failed to create self-diagnosis issue: {e}")

    except Exception as e:
        logger.error(f"Error during self-log scan: {e}")
    finally:
        update_task_state(task_id="SelfScan", action="end")

def _is_triage_only():
    """Return True when BugFixer should analyse issues but not push any fix commits."""
    if state.get("blackout"):
        return True
    config = load_config()
    return bool(config.get("TRIAGE_ONLY_MODE"))


def _schedule_check(config):
    """Return the current scheduler status.

    Returns a dict:
      allowed      – bool: whether a full scan cycle should run
      mode         – "full" | "restricted" | "paused_work_cap" | "paused_budget"
      reason       – human-readable explanation
      is_work_hours– bool
      is_weekend   – bool
      daily_used   – int: issues fixed today
      daily_cap    – int: effective cap right now (work-cap or full budget)
      daily_budget – int: total daily budget
    """
    if not config.get("SCHEDULER_ENABLED"):
        return {
            "allowed": True, "mode": "full", "reason": "Scheduler disabled",
            "is_work_hours": False, "is_weekend": False,
            "daily_used": state.get("daily_fixes_count", 0),
            "daily_cap": 0, "daily_budget": 0,
        }

    now = datetime.now()
    is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6
    weekend_full = config.get("SCHEDULER_WEEKEND_FULL", True)

    work_start = int(config.get("SCHEDULER_WORK_START_HOUR", 7))
    work_end   = int(config.get("SCHEDULER_WORK_END_HOUR", 18))
    # Work-hours restriction is lifted on weekends when weekend_full is on.
    is_work_hours = (work_start <= now.hour < work_end) and not (is_weekend and weekend_full)

    daily_budget  = max(1, int(config.get("SCHEDULER_DAILY_BUDGET", 50) or 50))
    work_cap_pct  = max(1, min(100, int(config.get("SCHEDULER_WORK_CAP_PCT", 25) or 25)))
    work_cap      = max(1, int(daily_budget * work_cap_pct / 100))

    # Reset daily counter if it's a new day.
    today_str = now.strftime("%Y-%m-%d")
    if state.get("daily_budget_date") != today_str:
        state["daily_budget_date"] = today_str
        state["daily_fixes_count"] = 0

    daily_used  = state.get("daily_fixes_count", 0)
    current_cap = work_cap if is_work_hours else daily_budget

    if daily_used >= daily_budget:
        mode = "paused_budget"
        state["scheduler_mode"] = mode
        return {
            "allowed": False, "mode": mode,
            "reason": f"Daily budget reached ({daily_used}/{daily_budget} issues today)",
            "is_work_hours": is_work_hours, "is_weekend": is_weekend,
            "daily_used": daily_used, "daily_cap": current_cap, "daily_budget": daily_budget,
        }

    if is_work_hours and daily_used >= work_cap:
        mode = "paused_work_cap"
        state["scheduler_mode"] = mode
        return {
            "allowed": False, "mode": mode,
            "reason": f"Work-hours cap reached ({daily_used}/{work_cap} — {work_cap_pct}% of {daily_budget} daily budget)",
            "is_work_hours": is_work_hours, "is_weekend": is_weekend,
            "daily_used": daily_used, "daily_cap": current_cap, "daily_budget": daily_budget,
        }

    mode = "restricted" if is_work_hours else "full"
    state["scheduler_mode"] = mode
    return {
        "allowed": True, "mode": mode, "reason": "OK",
        "is_work_hours": is_work_hours, "is_weekend": is_weekend,
        "daily_used": daily_used, "daily_cap": current_cap, "daily_budget": daily_budget,
    }


def run_scan_cycle():
    """Performs a single complete cycle of: Auth -> Label Discovery -> Prod Verification -> Scanning."""
    global state
    try:
        load_dotenv(override=True)
        config = load_config()

        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["status"] = "Scanning"
        processed = load_processed()

        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            raise Exception("Configuration Pending: Please enter your GitHub Token in Settings")
        gh_current = Github(token)
        try:
            logger.info("Attempting to authenticate with GitHub API...")
            bot_user = gh_current.get_user().login
            logger.info(f"Authenticated as GitHub user: {bot_user}")
        except GithubException as ge:
            if ge.status == 401:
                logger.error("GitHub Authentication Failed: 401 Unauthorized. Please check your token.")
                raise Exception("Invalid GitHub Token (401 Unauthorized)")
            logger.error(f"GitHub API Error: {ge.status} - {ge.data}")
            raise ge
        except Exception as e:
            logger.exception(f"Unexpected error during GitHub authentication: {e}")
            raise e

        monitored_repos = get_monitored_repos(config)
        if not monitored_repos:
            logger.warning("No monitored repositories configured. Skipping scan.")

        update_task_state(task_id="Discovery", task_name="Discovering Labels", action="start")
        state["available_labels"] = discover_labels(gh_current, monitored_repos)
        logger.info(f"Discovered {len(state['available_labels'])} unique labels across monitored repos.")
        update_task_state(task_id="Discovery", action="end")

        verify_production_fixes(gh_current, processed)

        scan_self_logs(gh_current, config)

        state["status"] = "Scanning"
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(scan_hub_logs, Github(token), config),
                executor.submit(scan_repo_issues, Github(token), config, processed)
            ]
            for future in futures:
                future.result()
        state["status"] = "Idle"
        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.exception(f"Critical poller error: {e}")
        state["status"] = f"Error: {str(e)}"
        for cleanup_id in ("Discovery", "HubScan", "RepoScan", "SelfScan"):
            try:
                update_task_state(task_id=cleanup_id, action="end")
            except Exception as cleanup_err:
                logger.debug(f"Cleanup for {cleanup_id} failed: {cleanup_err}")

def poller_worker():
    global state
    while True:
        cfg = load_config()
        if not state["paused"]:
            # Always run the scan cycle — triage, hub logs, prod verification, label discovery
            # all run regardless of scheduler state.  The schedule/blackout gates are applied
            # per-issue inside scan_repo_issues.
            run_scan_cycle()
            # Apply any deferred restart NOW — the cycle just finished so no tasks are
            # in flight.  This avoids SIGTERM killing git clone / LLM calls mid-operation.
            if state.get("restart_pending"):
                logger.info("Deferred restart: scan cycle complete, no tasks in flight — restarting now.")
                state["restart_pending"] = False
                import subprocess as _sp
                _sp.Popen(["sudo", "systemctl", "restart", "bugfixer"])
                time.sleep(5)  # give systemd time to send SIGTERM cleanly
                return
            sched = _schedule_check(cfg)
            if sched.get("is_work_hours") and cfg.get("SCHEDULER_WORK_POLL_INTERVAL"):
                interval = int(cfg.get("SCHEDULER_WORK_POLL_INTERVAL") or 600)
            else:
                interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 300))
        else:
            logger.debug("Poller worker is paused. Skipping scan cycle.")
            interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 300))
        time.sleep(interval)

@app.get("/api/health")
async def health_check():
    """Heartbeat endpoint for the watchdog service."""
    return {"status": "ok"}

@app.post("/api/toggle-pause")
async def toggle_pause():
    state["paused"] = not state["paused"]
    logger.info(f"BugFixer autonomous operations {'PAUSED' if state['paused'] else 'RESUMED'}")
    return {"status": "success", "paused": state["paused"]}

@app.post("/api/toggle-blackout")
async def toggle_blackout():
    state["blackout"] = not state.get("blackout", False)
    logger.info(f"BugFixer blackout mode {'ON (triage only)' if state['blackout'] else 'OFF (fixes resumed)'}")
    return {"status": "success", "blackout": state["blackout"]}

@app.get("/")
async def dashboard(request: Request):
    recent_processed = {}
    if state["processed"]:
        now = datetime.now()
        for issue_id, info in state["processed"].items():
            try:
                ts = datetime.fromisoformat(info.get("timestamp", "{}"))
                if (now - ts).days < 7:
                    recent_processed[issue_id] = info
            except:
                recent_processed[issue_id] = info

    return templates.TemplateResponse(request=request, name="index.html", context={"view": "status", "state": {**state, "processed": recent_processed}})

@app.get("/api/task-details")
async def get_task_details(task_id: str = None):
    if task_id:
        if task_id not in state["active_tasks"]:
            return JSONResponse(status_code=404, content={"error": "Task not found or no longer active"})

        task = state["active_tasks"][task_id]
        duration = datetime.now() - task["start_time"]
        seconds = int(duration.total_seconds())
        duration_str = f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"

        return {
            "status": state["status"],
            "task": task["name"],
            "duration": duration_str,
            "stream": task["stream"]
        }

    return {
        "active_tasks": state["active_tasks"],
        "count": len(state["active_tasks"])
    }

def _fetch_models_for_provider(provider, api_key, base_url):
    """Fetch available model names from a provider's API using live credentials.
    Returns list of {"name": str, "details": str}.
    """
    p = (provider or "openai").lower().strip()
    # claude_cli needs no API key — return the current Claude model roster.
    if p == "claude_cli":
        return [
            {"name": "claude-sonnet-4-6",         "details": "Claude Sonnet 4.6"},
            {"name": "claude-opus-4-8",            "details": "Claude Opus 4.8"},
            {"name": "claude-haiku-4-5-20251001",  "details": "Claude Haiku 4.5"},
        ]
    if not api_key:
        return []
    models = []
    try:
        if p == "ollama":
            base = (base_url or "https://ollama.com").rstrip("/")
            headers = {}
            if api_key:
                clean = api_key.strip().replace("Bearer ", "").strip()
                headers["Authorization"] = f"Bearer {clean}"
            resp = requests.get(f"{base}/api/tags", headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("models", []):
                name = m.get("name", "")
                if "bf16" in name:
                    name = name[:name.find("bf16") + 4]
                details = m.get("details", "")
                if isinstance(details, dict):
                    details = details.get("family", str(details))
                models.append({"name": name, "details": str(details)})
        elif p == "anthropic":
            base = (base_url or ANTHROPIC_BASE_URL).rstrip("/")
            headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_API_VERSION}
            resp = requests.get(f"{base}/models", headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                models.append({"name": m.get("id", ""), "details": m.get("display_name", "")})
        elif p == "google":
            base = (base_url or GOOGLE_BASE_URL).rstrip("/")
            resp = requests.get(f"{base}/v1beta/models", headers={"x-goog-api-key": api_key}, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                if "gemini" in name or "gemma" in name:
                    models.append({"name": name, "details": m.get("displayName", "")})
        elif p == "groq":
            base = (base_url or "https://api.groq.com/openai/v1").rstrip("/")
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(f"{base}/models", headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                models.append({"name": m.get("id", ""), "details": m.get("owned_by", "")})
        else:  # openai (and openai-compatible)
            base = (base_url or OPENAI_BASE_URL).rstrip("/")
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(f"{base}/models", headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                mid = m.get("id", "")
                if any(k in mid for k in ("gpt", "o1", "o3", "o4")):
                    models.append({"name": mid, "details": m.get("owned_by", "")})
    except Exception as e:
        logger.debug(f"Model fetch for provider {p!r}: {e}")
    return models


@app.get("/api/models")
async def get_models():
    """Fetches available models from both configured LLM providers."""
    config = load_config()
    p1_provider, p1_key, _, p1_url = _get_provider_config(1, config)
    p2_provider, p2_key, _, p2_url = _get_provider_config(2, config)
    return {
        "local_models": _fetch_models_for_provider(p1_provider, p1_key, p1_url),
        "cloud_models": _fetch_models_for_provider(p2_provider, p2_key, p2_url),
        "enabled_models": config.get("enabled_models", []),
    }


@app.post("/api/fetch-models")
async def fetch_models_live(request: Request):
    """Fetch available models for a provider.

    Accepts explicit api_key/base_url (for live testing), or just provider name
    to look up credentials already saved in the vault.
    """
    try:
        data = await request.json()
        provider = (data.get("provider") or "openai").strip()
        api_key = (data.get("api_key") or "").strip()
        base_url = (data.get("base_url") or "").strip()

        # If no key supplied, try the vault.
        if not api_key:
            cfg = load_config()
            cred = (cfg.get("llm_credentials") or {}).get(provider.lower()) or {}
            api_key = (cred.get("api_key") or "").strip()
            base_url = base_url or (cred.get("base_url") or "").strip()

        models = _fetch_models_for_provider(provider, api_key, base_url)
        return {"models": models}
    except Exception as e:
        logger.error(f"fetch-models error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "models": []})

@app.get("/api/scheduler/status")
async def scheduler_status():
    config = load_config()
    return _schedule_check(config)


@app.get("/api/claude-cli/status")
def claude_cli_status():
    """Check whether the local claude CLI is installed and authenticated.
    Runs as a sync handler so FastAPI threads it — avoids blocking the event loop.
    """
    import subprocess
    try:
        probe = subprocess.run(
            ["claude", "--output-format", "json"],
            input="ping", capture_output=True, text=True, timeout=15,
        )
        output = probe.stdout.strip()
        try:
            data = json.loads(output)
            result_text = data.get("result", "")
            if "Not logged in" in result_text or "/login" in result_text:
                return {"status": "needs_auth",
                        "detail": "Claude CLI installed but not authenticated. Use 'Start Login Flow' to log in."}
            if data.get("is_error") and data.get("result"):
                return {"status": "error", "detail": result_text[:300]}
            return {"status": "authenticated", "detail": "Claude CLI authenticated and ready."}
        except (json.JSONDecodeError, KeyError):
            r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return {"status": "ok", "version": r.stdout.strip() or r.stderr.strip()}
            return {"status": "error", "detail": (r.stderr or r.stdout).strip()[:300]}
    except FileNotFoundError:
        return {"status": "not_found", "detail": "'claude' binary not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": "CLI probe timed out — may be authenticating"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/claude-cli/auth/start")
def claude_cli_auth_start():
    """Start 'claude auth login' as a background process and capture the OAuth URL.

    Uses a throwaway shell script as BROWSER so the URL is written to a temp file
    AND printed to claude's stderr — two independent capture paths.  The process
    stays alive (stdin=PIPE) so the approval code can be piped back later.
    """
    import subprocess, select as _select, tempfile, stat

    # Kill any existing auth process first.
    old = state.get("claude_auth_proc")
    if old and old.poll() is None:
        try:
            old.terminate()
        except Exception:
            pass
    state["claude_auth_proc"] = None
    state["claude_auth_url"] = ""
    state["claude_auth_done"] = False

    try:
        # Write a tiny browser-wrapper script.  When claude calls $BROWSER URL it:
        #   1. Writes the URL to a temp file (readable even if pipe buffering delays it)
        #   2. Prints it to stdout (inherited from claude → our pipe)
        url_file = "/tmp/_claude_auth_url.txt"
        browser_script = "/tmp/_claude_browser.sh"
        try:
            with open(url_file, "w") as f:
                f.write("")
            with open(browser_script, "w") as f:
                f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" | tee "{url_file}" >&2\n')
            os.chmod(browser_script, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        except Exception:
            browser_script = "/bin/echo"   # fallback to original approach

        env = os.environ.copy()
        env["BROWSER"] = browser_script
        # Some distributions also check BROWSER_OPENER / OPENER
        env["BROWSER_OPENER"] = browser_script
        # Suppress any DISPLAY so electron-based openers fall back to $BROWSER
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)

        proc = subprocess.Popen(
            ["claude", "auth", "login"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        state["claude_auth_proc"] = proc

        # If claude prompts "Open browser? [y/N]" we answer yes immediately.
        # This is non-blocking — if stdin isn't being read, write() still returns.
        try:
            proc.stdin.write("y\n")
            proc.stdin.flush()
        except Exception:
            pass

        # Read stdout+stderr for up to 25 s, scanning every half-second for a URL.
        # Also poll the temp file as a second capture path.
        lines = []
        url_found = ""
        deadline = time.time() + 25
        while time.time() < deadline:
            ready, _, _ = _select.select([proc.stdout, proc.stderr], [], [], 0.5)
            for stream in ready:
                line = stream.readline()
                if line:
                    lines.append(line)
                    for u in re.findall(r'https?://\S+', line):
                        u = u.rstrip(".,;)\"'")
                        if "claude.ai" in u or "anthropic.com" in u or "oauth" in u.lower() or "auth" in u.lower():
                            url_found = u
                            break
            if url_found:
                break
            # Check the temp file written by the browser script
            try:
                with open(url_file) as f:
                    raw = f.read().strip()
                if raw:
                    url_found = raw.splitlines()[0].strip()
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                break

        # Final pass: scan everything captured for any URL (less targeted)
        combined = "".join(lines)
        if not url_found:
            for u in re.findall(r'https?://\S+', combined):
                u = u.rstrip(".,;)\"'")
                if u:
                    url_found = u
                    break
        # Also try the temp file one more time
        if not url_found:
            try:
                with open(url_file) as f:
                    raw = f.read().strip()
                if raw:
                    url_found = raw.splitlines()[0].strip()
            except Exception:
                pass

        state["claude_auth_url"] = url_found

        if proc.poll() == 0:
            state["claude_auth_done"] = True
            return {"status": "authenticated", "url": "", "output": combined[:3000]}

        return {
            "status": "pending",
            "url": url_found,
            "output": combined[:3000] or "(no output yet — process is running, waiting for claude auth login…)",
        }

    except FileNotFoundError:
        return {"status": "not_found", "detail": "'claude' binary not found in PATH"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/api/claude-cli/auth/poll")
def claude_cli_auth_poll():
    """Check whether the background auth process has completed."""
    proc = state.get("claude_auth_proc")
    url = state.get("claude_auth_url", "")

    if state.get("claude_auth_done"):
        return {"status": "authenticated", "url": url}

    if proc is None:
        # No process — check live whether CLI is authenticated.
        try:
            r = subprocess.run(
                ["claude", "--output-format", "json"],
                input="ping", capture_output=True, text=True, timeout=10,
            )
            try:
                data = json.loads(r.stdout.strip())
                if "Not logged in" in data.get("result", ""):
                    return {"status": "needs_auth", "url": ""}
                return {"status": "authenticated", "url": ""}
            except Exception:
                pass
        except Exception:
            pass
        return {"status": "no_process", "url": ""}

    rc = proc.poll()
    if rc is None:
        # Still running — drain any new output and look for a URL.
        extra = []
        try:
            import select as _select
            while True:
                ready, _, _ = _select.select([proc.stdout, proc.stderr], [], [], 0)
                if not ready:
                    break
                for s in ready:
                    line = s.readline()
                    if line:
                        extra.append(line)
        except Exception:
            pass
        combined = "".join(extra)
        if not url:
            for u in re.findall(r'https?://\S+', combined):
                u = u.rstrip(".,;)\"'")
                if u:
                    url = u
                    state["claude_auth_url"] = url
                    break
        # Also check the temp file written by the browser wrapper script.
        if not url:
            try:
                with open("/tmp/_claude_auth_url.txt") as f:
                    raw = f.read().strip()
                if raw:
                    url = raw.splitlines()[0].strip()
                    state["claude_auth_url"] = url
            except Exception:
                pass
        # Re-send "y\n" in case claude is still prompting for browser confirmation.
        if not url:
            try:
                proc.stdin.write("y\n")
                proc.stdin.flush()
            except Exception:
                pass
        return {"status": "pending", "url": url, "output": combined[:500]}

    # Process exited.
    if rc == 0:
        state["claude_auth_done"] = True
        state["claude_auth_proc"] = None
        return {"status": "authenticated", "url": url}
    # Non-zero exit — collect remaining stderr.
    try:
        remaining, _ = proc.communicate(timeout=2)
    except Exception:
        remaining = ""
    state["claude_auth_proc"] = None
    return {"status": "error", "detail": f"auth login exited {rc}: {remaining[:300]}", "url": url}


@app.post("/api/claude-cli/auth/submit-code")
async def claude_cli_auth_submit_code(request: Request):
    """Send an authorization code to the waiting 'claude auth login' process via stdin.

    After the user visits the OAuth URL, claude.ai shows an approval code.
    The user pastes it here and we forward it to the subprocess's stdin.
    Blocking subprocess I/O runs in a thread via asyncio.to_thread so the
    event loop is never blocked.
    """
    data = await request.json()
    code = (data.get("code") or "").strip()
    if not code:
        return {"status": "error", "detail": "No code provided."}

    proc = state.get("claude_auth_proc")
    if proc is None or proc.poll() is not None:
        return {"status": "error", "detail": "No active auth process — click 'Start Login Flow' first."}

    def _blocking_submit(proc, code):
        import select as _select
        try:
            proc.stdin.write(code + "\n")
            proc.stdin.flush()
        except Exception as e:
            return {"status": "error", "detail": f"Failed to send code to auth process: {e}"}

        lines = []
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                ready, _, _ = _select.select([proc.stdout, proc.stderr], [], [], 0.5)
                for s in ready:
                    line = s.readline()
                    if line:
                        lines.append(line)
            except Exception:
                break
            if proc.poll() is not None:
                break

        rc = proc.poll()
        output = "".join(lines)
        if rc == 0:
            state["claude_auth_done"] = True
            state["claude_auth_proc"] = None
            return {"status": "authenticated", "output": output}
        if rc is not None:
            state["claude_auth_proc"] = None
            return {"status": "error", "detail": f"Auth process exited {rc}: {output[:300]}"}
        return {"status": "pending", "output": output,
                "message": "Code submitted — authentication in progress. Click 'Check Status' in a moment."}

    return await asyncio.to_thread(_blocking_submit, proc, code)


@app.post("/api/toggle-model")
async def toggle_model(request: Request):
    """Toggles a model's enabled status in the configuration."""
    try:
        data = await request.json()
        model_name = data.get("model")
        enabled = data.get("enabled")

        if not model_name or enabled is None:
            return JSONResponse(status_code=400, content={"error": "Missing model or enabled status"})

        config = load_config()
        enabled_list = config.get("enabled_models", [])

        if enabled and model_name not in enabled_list:
            enabled_list.append(model_name)
        elif not enabled and model_name in enabled_list:
            enabled_list.remove(model_name)

        config["enabled_models"] = enabled_list
        save_config(config)

        return {"status": "success", "enabled_models": enabled_list}
    except Exception as e:
        logger.error(f"Error toggling model: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/logs")
async def get_logs(request: Request):
    try:
        current_log = get_log_path()
        with open(current_log, "r") as f:
            lines = f.readlines()
            logs = "".join(reversed(lines[-100:]))
    except Exception as e: logs = f"Error reading logs from {get_log_path()}: {e}"
    return templates.TemplateResponse(request=request, name="index.html", context={"view": "logs", "logs": logs, "state": state})

@app.get("/hub-logs")
async def get_hub_logs_page(request: Request):
    config = load_config()
    hub_url = (config.get("HUB_QUERY_URL") or "").strip()
    fetch_error = None
    fetch_status = None
    logs = None
    try:
        if hub_url and "your-netbox" not in hub_url:
            probe = requests.get(hub_url.rstrip("/") + "/setup/logs/all", timeout=15)
            fetch_status = probe.status_code
            if probe.status_code == 200:
                logs = get_hub_logs()
            else:
                fetch_error = f"Hub returned HTTP {probe.status_code}"
        else:
            fetch_error = "HUB_QUERY_URL not configured"
    except Exception as ex:
        fetch_error = str(ex)
    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"view": "hub-logs", "hub_logs": logs, "state": state,
                 "hub_fetch_time": fetch_time, "hub_fetch_error": fetch_error,
                 "hub_fetch_status": fetch_status, "hub_url": hub_url},
    )


@app.get("/api/hub-logs/raw")
async def hub_logs_raw():
    """Return the raw JSON from the Hub /setup/logs/all endpoint for debugging."""
    config = load_config()
    url = (config.get("HUB_QUERY_URL") or "").strip()
    if not url or "your-netbox" in url:
        return JSONResponse({"error": "HUB_QUERY_URL not configured"}, status_code=400)
    try:
        resp = requests.get(url.rstrip("/") + "/setup/logs/all", timeout=15)
        return JSONResponse({
            "status_code": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "body_preview": resp.text[:5000],
            "body_length": len(resp.text),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

DEFAULT_ENV = {
    "GITHUB_TOKEN": "",
    "LLM_PROVIDER_1": "openai",
    "LLM_API_KEY_1": "",
    "LLM_MODEL_1": "gpt-4o",
    "LLM_BASE_URL_1": "",
    "LLM_PROVIDER_2": "anthropic",
    "LLM_API_KEY_2": "",
    "LLM_MODEL_2": "claude-opus-4-5",
    "LLM_BASE_URL_2": "",
    "LLM_PROVIDER_3": "google",
    "LLM_API_KEY_3": "",
    "LLM_MODEL_3": "gemini-1.5-pro",
    "LLM_BASE_URL_3": "",
    "LLM_PROVIDER_4": "ollama",
    "LLM_API_KEY_4": "",
    "LLM_MODEL_4": "",
    "LLM_BASE_URL_4": "",
    "QA_API_URL": "",
    "QA_REPO": "",
    "QA_TEST_COMMAND": "pytest",
    "POLL_INTERVAL_SECONDS": "300",
    "UPDATE_API_URL": "",
    "HUB_QUERY_URL": "",
    "LM_ADMIN_TOKEN": "",
    "POST_UPDATE_COOLDOWN_MINUTES": "10",
    "LOG_FILE_PATH": "/var/log/bugfixer.log",
    "DEV_BRANCH": "dev",
    "LLM_TIMEOUT": "900",
    "MAX_CONCURRENT_FIXES": "5",
    "TRIAGE_STRICTNESS": "Moderate",
    "REVIEWER_MODEL_1": "",
    "REVIEWER_MODEL_2": "",
    "REVIEWER_MODEL_3": "",
    "REVIEWER_MODEL_4": "",
    "LLM_RPM_1": "0",
    "LLM_RPM_2": "0",
    "LLM_RPM_3": "0",
    "LLM_RPM_4": "0",
    "LLM_MAX_RETRIES": "5",
    "LLM_BACKOFF_BASE": "2.0",
    "LLM_BACKOFF_MAX": "600.0",
    "LLM_MAX_CONCURRENT": "1",
    "PROD_VERIFICATION_DAYS": "7",
    "MAX_ISSUES_PER_CYCLE": "15",
    "POLL_INTERVAL_SECONDS": "300",
    "CHAT_SYSTEM_PROMPT": "",
    "CHAT_HISTORY_WINDOW": "20",
    "LLM_LOG_MAX_ENTRIES": "200",
    "LLM_LOG_MAX_CHARS": "60000",
    "SCHEDULER_WORK_START_HOUR": "7",
    "SCHEDULER_WORK_END_HOUR": "18",
    "SCHEDULER_DAILY_BUDGET": "50",
    "SCHEDULER_WORK_CAP_PCT": "25",
    "SCHEDULER_WORK_POLL_INTERVAL": "600",
    "SCHEDULER_CRITICAL_LABEL": "critical",
}

@app.get("/settings")
async def settings_page(request: Request):
    load_dotenv(override=True)
    settings = DEFAULT_ENV.copy()
    for k in DEFAULT_ENV:
        val = os.getenv(k)
        if val: settings[k] = val
    config = load_config()
    repo_tests = config.get("repo_tests", {})
    repo_tests_str = ", ".join([f"{k}:{v}" for k, v in repo_tests.items()])
    settings["GITHUB_TOKEN"] = config.get("GITHUB_TOKEN") or settings.get("GITHUB_TOKEN", "")
    settings["LM_ADMIN_TOKEN"] = config.get("LM_ADMIN_TOKEN") or settings.get("LM_ADMIN_TOKEN", "")
    settings["LLM_TIMEOUT"] = config.get("LLM_TIMEOUT") or settings.get("LLM_TIMEOUT", "900")
    labels = config.get("monitored_labels", ["automated-fix"])
    settings["monitored_labels_str"] = ", ".join(labels)

    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "settings",
        "settings": {**settings, **config, "repo_tests_str": repo_tests_str, "monitored_labels_str": settings["monitored_labels_str"]},
        "available_labels": state.get("available_labels", []),
        "state": state,
    })

@app.post("/save_settings")
async def save_settings(request: Request):
    form_data = await request.form()
    data = dict(form_data)

    config_data = load_config()

    labels_mode = data.get("label_mode", "SPECIFIC")
    if labels_mode == "ANY":
        labels = ["ANY"]
    elif labels_mode == "NONE":
        labels = ["NONE"]
    else:
        # ------------------------------------------------------------------
        # BUGFIX: 'dict' object has no attribute 'getlist'
        #
        # The previous code called form_data.getlist("monitored_labels")
        # inside a try/except AttributeError. However, if form_data is a
        # plain dict (not a Starlette FormData / MultiDict), calling
        # .getlist() raises an UNCAUGHT AttributeError in some code paths,
        # producing the log error: "'dict' object has no attribute 'getlist'".
        #
        # Fix: Use hasattr() to explicitly check whether the object supports
        # getlist() before calling it. If it does (Starlette FormData), use
        # getlist() to retrieve all checked checkbox values. If it does not
        # (plain dict), fall back to dict.get() with manual list handling so
        # we never raise an AttributeError.
        # ------------------------------------------------------------------
        if hasattr(form_data, "getlist"):
            labels_list = form_data.getlist("monitored_labels")
        else:
            # form_data is a plain dict — use .get() with manual list handling.
            val = form_data.get("monitored_labels", [])
            if isinstance(val, list):
                labels_list = val
            elif isinstance(val, str) and val:
                labels_list = [val]
            else:
                labels_list = []

        custom_labels_raw = data.get("custom_labels", "")
        if custom_labels_raw:
            custom_labels = [x.strip() for x in custom_labels_raw.split(",") if x.strip()]
            labels_list.extend(custom_labels)

        if not labels_list:
            labels = ["automated-fix"]
        else:
            labels = list(set(labels_list))

    if "label_mode" in data or "monitored_labels" in form_data or "custom_labels" in data:
        config_data["monitored_labels"] = labels

    updates = {
        "monitored_repos": lambda v: [clean_repo_name(x.strip()) for x in v.replace("\\n", ",").split(",") if x.strip()],
        "trusted_repos": lambda v: [clean_repo_name(x.strip()) for x in v.replace("\\n", ",").split(",") if x.strip()],
        "default_branch": lambda v: v,
        "dev_branch": lambda v: v,
        "GITHUB_TOKEN": lambda v: v,
        "LLM_PROVIDER_1": lambda v: v,
        "LLM_API_KEY_1": lambda v: v,
        "LLM_MODEL_1": lambda v: v,
        "LLM_BASE_URL_1": lambda v: v,
        "LLM_PROVIDER_2": lambda v: v,
        "LLM_API_KEY_2": lambda v: v,
        "LLM_MODEL_2": lambda v: v,
        "LLM_BASE_URL_2": lambda v: v,
        "LLM_PROVIDER_3": lambda v: v,
        "LLM_API_KEY_3": lambda v: v,
        "LLM_MODEL_3": lambda v: v,
        "LLM_BASE_URL_3": lambda v: v,
        "LLM_PROVIDER_4": lambda v: v,
        "LLM_API_KEY_4": lambda v: v,
        "LLM_MODEL_4": lambda v: v,
        "LLM_BASE_URL_4": lambda v: v,
        "LLM_TIMEOUT": lambda v: v,
        "MAX_CONCURRENT_FIXES": lambda v: v,
        "TRIAGE_STRICTNESS": lambda v: v,
        "REVIEWER_MODEL_1": lambda v: v,
        "REVIEWER_MODEL_2": lambda v: v,
        "REVIEWER_MODEL_3": lambda v: v,
        "REVIEWER_MODEL_4": lambda v: v,
        "LLM_RPM_1": lambda v: v,
        "LLM_RPM_2": lambda v: v,
        "LLM_RPM_3": lambda v: v,
        "LLM_RPM_4": lambda v: v,
        "LLM_MAX_RETRIES": lambda v: v,
        "LLM_BACKOFF_BASE": lambda v: v,
        "LLM_BACKOFF_MAX": lambda v: v,
        "LLM_MAX_CONCURRENT": lambda v: v,
        "PROD_VERIFICATION_DAYS": lambda v: v,
        "MAX_ISSUES_PER_CYCLE": lambda v: v,
        "POLL_INTERVAL_SECONDS": lambda v: v,
        "self_diagnosis_repo": lambda v: clean_repo_name(v.strip()) if v and v.strip() else "",
        "module_repo_map": lambda v: parse_module_repo_map(v),
        # Chat-agent numeric settings (stored as strings by the form; coerce to int).
        "CHAT_TOOL_MAX_ITERATIONS": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_TOOL_MAX_ITERATIONS"],
        "CHAT_TOOL_MAX_TOKENS": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_TOOL_MAX_TOKENS"],
        "CHAT_INDEX_ISSUE_LIMIT": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_INDEX_ISSUE_LIMIT"],
        "CHAT_INDEX_CACHE_TTL": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_INDEX_CACHE_TTL"],
        "CHAT_FIX_PROPOSAL_TTL": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_FIX_PROPOSAL_TTL"],
        "CHAT_SYSTEM_PROMPT": lambda v: v.strip() if v else "",
        "CHAT_HISTORY_WINDOW": lambda v: int(v) if str(v).strip().isdigit() else 20,
        "LLM_LOG_MAX_ENTRIES": lambda v: int(v) if str(v).strip().isdigit() else 200,
        "LLM_LOG_MAX_CHARS": lambda v: int(v) if str(v).strip().isdigit() else 60000,
        "SCHEDULER_WORK_START_HOUR": lambda v: int(v) if str(v).strip().isdigit() else 7,
        "SCHEDULER_WORK_END_HOUR": lambda v: int(v) if str(v).strip().isdigit() else 18,
        "SCHEDULER_DAILY_BUDGET": lambda v: int(v) if str(v).strip().isdigit() else 50,
        "SCHEDULER_WORK_CAP_PCT": lambda v: int(v) if str(v).strip().isdigit() else 25,
        "SCHEDULER_WORK_POLL_INTERVAL": lambda v: int(v) if str(v).strip().isdigit() else 600,
        "SCHEDULER_CRITICAL_LABEL": lambda v: v.strip() if v else "critical",
        "QA_API_URL": lambda v: v.strip() if v else "",
        "QA_REPO": lambda v: v.strip() if v else "",
        "QA_TEST_COMMAND": lambda v: v.strip() if v else "pytest",
        "HUB_QUERY_URL": lambda v: v.strip() if v else "",
        "LM_ADMIN_TOKEN": lambda v: v.strip() if v else "",
        "POST_UPDATE_COOLDOWN_MINUTES": lambda v: max(0, int(v)) if str(v).isdigit() else 10,
    }


    for key, transform in updates.items():
        if key in data:
            val = data[key]
            if "repos" in key and not val:
                config_data[key] = []
            else:
                config_data[key] = transform(val)

    config_data["direct_push_enabled"] = data.get("direct_push_enabled") == "on"
    config_data["qa_enabled"] = data.get("qa_enabled") == "on"
    config_data["skip_review"] = data.get("skip_review") == "on"
    config_data["CHAT_TOOLS_ENABLED"] = data.get("CHAT_TOOLS_ENABLED") == "on"
    config_data["SCHEDULER_ENABLED"] = data.get("SCHEDULER_ENABLED") == "on"
    config_data["SCHEDULER_WEEKEND_FULL"] = data.get("SCHEDULER_WEEKEND_FULL") == "on"
    config_data["TRIAGE_ONLY_MODE"] = data.get("TRIAGE_ONLY_MODE") == "on"

    repo_tests_raw = data.get("repo_tests", "")
    if repo_tests_raw:
        new_tests = {}
        for pair in repo_tests_raw.split(","):
            if ":" in pair:
                repo, cmd = pair.split(":", 1)
                new_tests[repo.strip()] = cmd.strip()
        config_data["repo_tests"] = new_tests

    save_config(config_data)

    env_vars = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env_vars[k] = v

    for k, v in data.items():
        if k in updates:
            env_vars[k] = v

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    global _LLM_SEMAPHORE
    with _LLM_SEM_LOCK:
        _LLM_SEMAPHORE = None

    try:
        validate_llm_config_on_startup()
    except Exception as ve:
        logger.warning(f"Post-save LLM validation failed (non-fatal): {ve}")

    return RedirectResponse(url="/settings", status_code=303)

@app.post("/api/llm/credentials")
async def save_llm_credential(request: Request):
    """Save or update a provider credential in the vault."""
    data = await request.json()
    provider = (data.get("provider") or "").lower().strip()
    if not provider:
        return JSONResponse(status_code=400, content={"error": "provider required"})
    config = load_config()
    creds = config.setdefault("llm_credentials", {})
    creds[provider] = {
        "api_key": (data.get("api_key") or "").strip(),
        "base_url": (data.get("base_url") or "").strip(),
    }
    save_config(config)
    return {"status": "ok", "provider": provider}


@app.post("/api/llm/entries")
async def create_llm_entry(request: Request):
    """Create a new named provider/model entry."""
    data = await request.json()
    entry = {
        "id": str(uuid.uuid4())[:12],
        "label": (data.get("label") or "").strip(),
        "provider": (data.get("provider") or "openai").lower().strip(),
        "model": (data.get("model") or "").strip(),
        "rpm": int(data.get("rpm") or 0),
        "reviewer_model": (data.get("reviewer_model") or "").strip(),
    }
    if not entry["model"]:
        return JSONResponse(status_code=400, content={"error": "model required"})
    config = load_config()
    config.setdefault("llm_entries", []).append(entry)
    save_config(config)
    return {"status": "ok", "entry": entry}


@app.put("/api/llm/entries/{entry_id}")
async def update_llm_entry(entry_id: str, request: Request):
    """Update an existing named provider/model entry."""
    data = await request.json()
    config = load_config()
    entries = config.get("llm_entries") or []
    for e in entries:
        if e.get("id") == entry_id:
            e["label"] = (data.get("label") or e.get("label") or "").strip()
            e["provider"] = (data.get("provider") or e.get("provider") or "openai").lower().strip()
            e["model"] = (data.get("model") or e.get("model") or "").strip()
            e["rpm"] = int(data.get("rpm") or e.get("rpm") or 0)
            e["reviewer_model"] = (data.get("reviewer_model") or "").strip()
            save_config(config)
            return {"status": "ok", "entry": e}
    return JSONResponse(status_code=404, content={"error": "entry not found"})


@app.delete("/api/llm/entries/{entry_id}")
async def delete_llm_entry(entry_id: str):
    """Delete a named entry and clear it from any slot assignments."""
    config = load_config()
    config["llm_entries"] = [e for e in (config.get("llm_entries") or []) if e.get("id") != entry_id]
    slots = config.get("llm_slots") or {}
    for k in list(slots.keys()):
        if slots[k] == entry_id:
            slots[k] = None
    config["llm_slots"] = slots
    save_config(config)
    return {"status": "ok"}


@app.post("/api/llm/slots")
async def update_llm_slots(request: Request):
    """Update the slot→entry_id assignment for P1-P4."""
    data = await request.json()  # {"1": "entry_id_or_null", ...}
    config = load_config()
    config["llm_slots"] = {str(k): (v or None) for k, v in data.items()}
    save_config(config)
    return {"status": "ok"}


@app.get("/api/llm/config")
async def get_llm_config():
    """Return current vault credentials (keys redacted), entries, and slot assignments."""
    config = load_config()
    creds = config.get("llm_credentials") or {}
    safe_creds = {p: {"configured": bool(v.get("api_key")), "base_url": v.get("base_url", "")}
                  for p, v in creds.items()}
    return {
        "credentials": safe_creds,
        "entries": config.get("llm_entries") or [],
        "slots": config.get("llm_slots") or {},
    }


# ---------------------------------------------------------------------------
# One-click local (CPU-only) LLM setup
#
# Installs Ollama (if missing), pulls a model, applies CPU tuning (systemd
# override + a context-tuned derived model), wires it into BugFixer provider
# slot 4, restarts the ollama service, and verifies. Streams staged progress
# into state["active_tasks"]["LocalLLMSetup"] so the UI can poll
# /api/task-details?task_id=LocalLLMSetup. Idempotent: each stage skips when
# its end state is already present, so it is safe to click repeatedly.
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_OVERRIDE_DIR = "/etc/systemd/system/ollama.service.d"
OLLAMA_OVERRIDE_PATH = os.path.join(OLLAMA_OVERRIDE_DIR, "bugfixer-tuning.conf")


def _llm_setup_log(line, replace_last=False):
    """Append a line to the LocalLLMSetup task stream.

    With replace_last=True the previous line is overwritten in place — used for
    `ollama pull` carriage-return progress bars so the bar updates instead of
    scrolling. Mirrors the overwrite-the-stream approach the _request_* handlers
    use, but keeps a growing log for staged steps.
    """
    with _task_state_lock:
        task = state["active_tasks"].get("LocalLLMSetup")
        if task is None:
            return
        s = task["stream"]
        if replace_last and s:
            idx = s.rfind("\n")
            task["stream"] = (s[:idx + 1] if idx != -1 else "") + line + "\n"
        else:
            task["stream"] = s + line + "\n"


def _ollama_reachable(base_url=OLLAMA_BASE_URL, timeout=5):
    """True if the Ollama HTTP API answers at {base_url}/api/tags."""
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def _wait_for_ollama(base_url, timeout=60):
    """Poll {base_url}/api/tags until it answers 200 (or timeout). Returns True if reachable."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ollama_reachable(base_url, timeout=5):
            return True
        time.sleep(2)
    return False


def _ollama_tags(base_url=OLLAMA_BASE_URL):
    """Return the set of model names via GET /api/tags (empty on failure).

    Uses the HTTP API rather than the `ollama` CLI because the bugfixer service
    runs under systemd with a minimal PATH that does not include /usr/local/bin
    (where the Ollama installer places the binary).
    """
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=15)
        resp.raise_for_status()
        return {m.get("name", "") for m in resp.json().get("models", []) if m.get("name")}
    except Exception as e:
        logger.debug(f"ollama /api/tags failed: {e}")
        return set()


def _ollama_bin_path():
    """Locate the ollama binary, checking common install paths (not just PATH).

    Returns the path string if found, else None. Used only to decide whether
    the installer needs to run; pull/create/list go through the HTTP API.
    """
    import shutil
    p = shutil.which("ollama")
    if p:
        return p
    for cand in ("/usr/local/bin/ollama", "/usr/bin/ollama",
                 os.path.expanduser("~/.local/bin/ollama")):
        if os.path.exists(cand):
            return cand
    return None


def _ensure_zstd(log_fn):
    """Ensure the zstd binary is available.

    The official Ollama installer extracts its release tarball with zstd, so a
    box without zstd fails with "This version requires zstd for extraction".
    Installs it via the system package manager when missing; logs clear manual
    instructions if that is not possible. Returns True when zstd is available.
    """
    import subprocess, shutil
    if shutil.which("zstd"):
        log_fn("  zstd already available")
        return True
    log_fn("  zstd not found — attempting to install via apt-get (requires root)…")
    apt = shutil.which("apt-get")
    if not apt:
        log_fn("  ✗ apt-get not found. Install zstd manually, then retry:")
        log_fn("     Debian/Ubuntu: sudo apt-get install -y zstd")
        log_fn("     RHEL/CentOS/Fedora: sudo dnf install -y zstd")
        log_fn("     Arch: sudo pacman -S zstd")
        return False
    try:
        r = subprocess.run([apt, "install", "-y", "zstd"],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        log_fn(f"  ✗ apt-get install zstd raised: {e}")
        return False
    if r.returncode != 0:
        log_fn(f"  ✗ apt-get install zstd failed (exit {r.returncode}): "
               f"{((r.stderr or '') + (r.stdout or '')).strip()[-400:]}")
        return False
    if shutil.which("zstd"):
        log_fn("  ✓ zstd installed")
        return True
    log_fn("  ⚠ apt-get reported success but zstd still not on PATH; installer may fail")
    return True


def _ollama_http_pull(model, log_fn, base_url=OLLAMA_BASE_URL):
    """Pull a model via POST /api/pull, streaming JSON progress into the log.

    Ollama emits newline-delimited JSON: {"status": ..., "total": ..., "completed": ...}
    with a final {"status": "success"}. We render a compact progress line that
    replaces the last log line so the bar updates in place.
    """
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/pull",
                              json={"name": model}, stream=True, timeout=None)
        resp.raise_for_status()
        saw_success = False
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                log_fn("  " + str(raw))
                continue
            status = obj.get("status", "")
            if status == "success":
                log_fn("  " + status)
                saw_success = True
                break
            total = obj.get("total")
            completed = obj.get("completed")
            if total and completed:
                pct = 100.0 * completed / total
                line = f"{status}: {pct:.1f}% ({_gb(completed)}/{_gb(total)})"
            else:
                line = status
            log_fn("  " + line, replace_last=True)
        if not saw_success:
            # stream ended without an explicit success — verify via /api/tags
            if model not in _ollama_tags(base_url):
                raise RuntimeError("pull stream ended without success and model not present")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"ollama /api/pull failed: {e}")


def _ollama_http_create(name, modelfile, log_fn, base_url=OLLAMA_BASE_URL):
    """Create a model via POST /api/create (inline modelfile), streaming status."""
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/create",
                              json={"name": name, "modelfile": modelfile, "stream": True},
                              stream=True, timeout=None)
        resp.raise_for_status()
        saw_success = False
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                log_fn("  " + str(raw))
                continue
            status = obj.get("status", "")
            log_fn("  " + status, replace_last=True)
            if status == "success":
                saw_success = True
                break
        if not saw_success and name not in _ollama_tags(base_url):
            raise RuntimeError("create stream ended without success and model not present")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"ollama /api/create failed: {e}")


def _gb(b):
    """Format a byte count as gigabytes with one decimal."""
    return f"{b / 1024 ** 3:.1f}GB"


def run_local_llm_setup(model, num_ctx, cores):
    """Background pipeline for the one-click local (CPU-only) LLM setup.

    Stages: install Ollama → ensure service → pull model → create context-tuned
    derived model → write systemd override + restart → configure slot 4 →
    verify. Each stage is idempotent. Progress is streamed via
    _llm_setup_log() into state['active_tasks']['LocalLLMSetup'].
    """
    import subprocess
    task_id = "LocalLLMSetup"
    update_task_state(task_id, "Local LLM Setup", action="start")
    base_url = OLLAMA_BASE_URL
    derived_tag = model
    summary = {"state": "failed", "message": "not started"}
    try:
        # ---- Stage 1: detect / install Ollama ----
        # Installed = the HTTP API answers OR the binary exists at a known path.
        # We do NOT rely on `which("ollama")` alone because the bugfixer service
        # runs under systemd with a minimal PATH that omits /usr/local/bin.
        _llm_setup_log("▶ Stage 1/7 — Prerequisites + checking for Ollama…")
        # zstd is required by the Ollama installer to extract its tarball; ensure
        # it is present up front (no-op if already installed) so the install path
        # works when needed.
        if not _ensure_zstd(_llm_setup_log):
            raise RuntimeError("zstd is required by the Ollama installer and could not be "
                               "installed automatically. Install zstd, then click Setup again.")
        already_up = _ollama_reachable(base_url, timeout=5)
        bin_path = _ollama_bin_path()
        if already_up or bin_path:
            _llm_setup_log(f"✓ Ollama already installed (service {'up' if already_up else 'down'}, "
                           f"binary at {bin_path or 'unknown path'})")
        else:
            _llm_setup_log("  Ollama not found — running official installer (requires root)…")
            inst = subprocess.run(["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                                  capture_output=True, text=True, timeout=900)
            if inst.returncode != 0:
                tail = ((inst.stdout or "") + (inst.stderr or "")).strip()[-800:]
                raise RuntimeError(f"installer exited {inst.returncode}: {tail}")
            _llm_setup_log("✓ Ollama installed")

        # ---- Stage 2: ensure the ollama service is up ----
        _llm_setup_log("▶ Stage 2/7 — Ensuring the ollama service is running…")
        active = subprocess.run(["systemctl", "is-active", "ollama"], capture_output=True, text=True, timeout=15)
        if (active.stdout or "").strip() != "active":
            subprocess.run(["systemctl", "start", "ollama"], capture_output=True, text=True, timeout=30)
        if not _wait_for_ollama(base_url):
            raise RuntimeError(f"ollama service did not become reachable at {base_url}")
        _llm_setup_log(f"✓ ollama service reachable at {base_url}")

        # ---- Stage 3: pull the model (skip if already present) ----
        _llm_setup_log(f"▶ Stage 3/7 — Pulling model {model} (skip if present)…")
        present = _ollama_tags(base_url)
        if model in present:
            _llm_setup_log(f"✓ Model {model} already present")
        else:
            _ollama_http_pull(model, _llm_setup_log, base_url)
            _llm_setup_log(f"✓ Model {model} pulled")

        # ---- Stage 4: create a context-tuned derived model ----
        if num_ctx and int(num_ctx) > 0:
            ctx_k = int(num_ctx) // 1024
            derived_tag = f"{model}-{ctx_k}k" if ctx_k else f"{model}-{int(num_ctx)}"
            _llm_setup_log(f"▶ Stage 4/7 — Creating context-tuned model {derived_tag} (num_ctx={int(num_ctx)}, num_thread={int(cores)})…")
            modelfile = f"FROM {model}\nPARAMETER num_ctx {int(num_ctx)}\nPARAMETER num_thread {int(cores)}\n"
            _ollama_http_create(derived_tag, modelfile, _llm_setup_log, base_url)
            _llm_setup_log(f"✓ Derived model {derived_tag} created")
        else:
            _llm_setup_log("▶ Stage 4/7 — num_ctx unset; using base model as-is")
            _llm_setup_log("✓ Skipped derived-model step")

        # ---- Stage 5: write the systemd override + restart ollama ----
        _llm_setup_log("▶ Stage 5/7 — Applying systemd CPU tuning (daemon-reload + restart ollama)…")
        wanted = (
            "[Service]\n"
            'Environment="OLLAMA_NUM_PARALLEL=1"\n'
            'Environment="OLLAMA_KEEP_ALIVE=30m"\n'
            f'Environment="OLLAMA_NUM_THREAD={int(cores)}"\n'
        )
        current = ""
        if os.path.exists(OLLAMA_OVERRIDE_PATH):
            try:
                with open(OLLAMA_OVERRIDE_PATH) as f:
                    current = f.read()
            except Exception:
                current = ""
        if current.strip() == wanted.strip():
            _llm_setup_log("✓ systemd override already matches")
        else:
            os.makedirs(OLLAMA_OVERRIDE_DIR, exist_ok=True)
            with open(OLLAMA_OVERRIDE_PATH, "w") as f:
                f.write(wanted)
            _llm_setup_log(f"  Wrote {OLLAMA_OVERRIDE_PATH}")
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=30)
        rs = subprocess.run(["systemctl", "restart", "ollama"], capture_output=True, text=True, timeout=30)
        if rs.returncode != 0:
            raise RuntimeError(f"systemctl restart ollama failed: {(rs.stderr or '').strip()}")
        if not _wait_for_ollama(base_url):
            raise RuntimeError("ollama did not come back after restart")
        _llm_setup_log("✓ systemd tuned and ollama restarted")

        # ---- Stage 6: configure BugFixer provider slot 4 ----
        _llm_setup_log("▶ Stage 6/7 — Configuring BugFixer provider slot 4 (P4)…")
        config = load_config()
        config.setdefault("llm_credentials", {})["ollama"] = {"api_key": "", "base_url": base_url}
        entries = config.setdefault("llm_entries", [])
        entry = next((e for e in entries if e.get("provider") == "ollama" and e.get("model") == derived_tag), None)
        if entry is None:
            entry = {
                "id": str(uuid.uuid4())[:12],
                "label": "Local Ollama (CPU)",
                "provider": "ollama",
                "model": derived_tag,
                "rpm": 0,
                "reviewer_model": "",
            }
            entries.append(entry)
        else:
            entry["label"] = "Local Ollama (CPU)"
            entry["model"] = derived_tag
        config.setdefault("llm_slots", {})["4"] = entry["id"]
        save_config(config)
        _llm_setup_log(f"✓ Slot 4 → {entry['label']} / {derived_tag}")

        # ---- Stage 7: verify ----
        _llm_setup_log("▶ Stage 7/7 — Verifying Ollama responds with the model…")
        names = _ollama_tags(base_url)
        if derived_tag in names:
            _llm_setup_log(f"✓ Verified — {len(names)} model(s) available, {derived_tag} present")
            _llm_setup_log(f"\n✅ Setup complete. Slot 4 = {derived_tag} @ {base_url}")
            summary = {"state": "complete", "model": derived_tag, "base_url": base_url, "message": "Setup complete"}
        else:
            _llm_setup_log(f"⚠ Verify: {derived_tag} not yet in ollama list ({len(names)} models); slot 4 still configured.")
            _llm_setup_log(f"\n✅ Setup complete with a warning. Slot 4 = {derived_tag} @ {base_url}")
            summary = {"state": "warning", "model": derived_tag, "base_url": base_url, "message": "Configured but model not yet listed"}
    except Exception as e:
        logger.exception("Local LLM setup failed")
        _llm_setup_log(f"\n✗ Setup failed: {e}")
        summary = {"state": "failed", "message": str(e)}
    finally:
        state["local_llm_setup"] = {**summary, "updated": datetime.now().isoformat()}
        update_task_state(task_id, action="end")


@app.post("/api/local-llm/setup")
async def local_llm_setup(request: Request):
    """Kick off the one-click local (CPU-only) LLM setup in the background.

    Body (all optional, defaults applied): {model, num_ctx, cores}.
    Returns immediately with the task_id the UI polls via /api/task-details.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = (data.get("model") or "qwen2.5-coder:32b").strip()
    try:
        num_ctx = int(data.get("num_ctx") or 32768)
    except (TypeError, ValueError):
        num_ctx = 32768
    try:
        cores = int(data.get("cores") or state.get("cpu_count") or os.cpu_count() or 4)
    except (TypeError, ValueError):
        cores = os.cpu_count() or 4
    if "LocalLLMSetup" in state.get("active_tasks", {}):
        return JSONResponse(status_code=409, content={"status": "busy", "message": "A local LLM setup is already running."})
    threading.Thread(target=run_local_llm_setup, args=(model, num_ctx, cores), daemon=True).start()
    return {"status": "started", "task_id": "LocalLLMSetup"}


@app.get("/api/local-llm/status")
async def local_llm_status():
    """Whether a setup is running + the last-run summary + detected core count."""
    return {
        "running": "LocalLLMSetup" in state.get("active_tasks", {}),
        "last": state.get("local_llm_setup") or {},
        "cpu_count": state.get("cpu_count") or os.cpu_count() or 4,
    }


@app.post("/clear_history")
async def clear_history():
    """Clears all processed issues and resets success/failure counters."""
    global state
    logger.info("Clearing all issue history and resetting counters.")

    state["processed"] = {}
    state["success_count"] = 0
    state["failure_count"] = 0

    save_processed({})

    return {"status": "success", "message": "All history and tasks have been cleared."}


@app.post("/delete_issue")
async def delete_issue(request: Request):
    """Remove an issue from local history and close it on GitHub."""
    global state
    data = await request.json()
    issue_id = data.get("issue_id", "").strip()
    if not issue_id or ":" not in issue_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid issue_id"})

    repo_name, issue_num_str = issue_id.rsplit(":", 1)
    try:
        issue_num = int(issue_num_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid issue number"})

    # Remove from local processed history.
    processed = load_processed()
    was_in_history = issue_id in processed
    if was_in_history:
        entry = processed.pop(issue_id)
        if entry.get("status") in ("fixed", "verified", "awaiting_prod_verification"):
            state["success_count"] = max(0, state.get("success_count", 0) - 1)
        elif entry.get("status") == "failed":
            state["failure_count"] = max(0, state.get("failure_count", 0) - 1)
        state["processed"] = processed
        save_processed(processed)

    # Close the issue on GitHub.
    github_msg = ""
    try:
        config = load_config()
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
        if not token:
            raise ValueError("No GitHub token configured")
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(issue_num)

        # Ensure the dismissal label exists in the repo; create it if not.
        label_name = "bugfixer-dismissed"
        try:
            repo.get_label(label_name)
        except Exception:
            try:
                repo.create_label(label_name, "b60205",
                                  "Marked by BugFixer as not a real issue — will not be reopened")
            except Exception as le:
                logger.warning(f"Could not create label '{label_name}': {le}")

        # Apply the label and close with an explanatory comment.
        try:
            issue.add_to_labels(label_name)
        except Exception as le:
            logger.warning(f"Could not apply label '{label_name}' to #{issue_num}: {le}")
        try:
            issue.create_comment(
                "🤖 **BugFixer**: This issue has been marked as **not a real issue** and dismissed. "
                "It will not be automatically reopened or processed again."
            )
        except Exception:
            pass

        if issue.state != "closed":
            issue.edit(state="closed")
            github_msg = f"Issue #{issue_num} labelled '{label_name}' and closed on GitHub."
        else:
            github_msg = f"Issue #{issue_num} labelled '{label_name}' (was already closed)."
        logger.info(f"Dismissed issue {issue_id}: removed from history, {github_msg}")
    except Exception as e:
        github_msg = f"GitHub close failed: {e}"
        logger.warning(f"Could not close {issue_id} on GitHub: {e}")

    return {
        "status": "success",
        "message": f"{'Removed from history. ' if was_in_history else ''}{github_msg}",
    }

@app.post("/update_now")
async def update_now():
    updated, msg = check_for_updates()
    logger.info(f"Manual update check: {msg}")
    return {"status": "success", "message": msg}



@app.post("/api/clear-credit-cooldown/{n}")
async def clear_credit_cooldown(n: int):
    """Manually clear the 1-hour credit-exhaustion cooldown for provider n (1/2/3)."""
    if n not in (1, 2, 3, 4):
        return JSONResponse(status_code=400, content={"error": "n must be 1, 2, 3, or 4"})
    with _PROVIDER_CREDIT_CB_LOCK:
        _PROVIDER_CREDIT_CB[n]["cooldown_until"] = 0.0
        _PROVIDER_CREDIT_CB[n]["tripped_at"] = None
        _PROVIDER_CREDIT_CB[n]["reason"] = None
    state["provider_credit_cb"] = _provider_credit_cb_snapshot()
    logger.info(f"Credit cooldown for Provider {n} manually cleared.")
    return {"status": "cleared", "provider": n}


@app.post("/trigger_fix")
async def trigger_fix(request: Request):
    data = await request.json()
    repo_name = data.get("repo_name")
    issue_num = data.get("issue_num")
    llm_pref = data.get("llm_preference")

    if not repo_name or not issue_num:
        return JSONResponse(status_code=400, content={"message": "Missing repo_name or issue_num"})

    logger.info(f"Manual trigger: Fixing {repo_name}:{issue_num} with preference {llm_pref}")

    def run_fix():
        success, msg = process_single_issue(repo_name, int(issue_num), llm_preference=llm_pref)
        if success:
            logger.info(f"Manual fix successful for {repo_name}:{issue_num}")
        else:
            logger.error(f"Manual fix failed for {repo_name}:{issue_num}: {msg}")

    threading.Thread(target=run_fix, daemon=True).start()
    return {"status": "triggered", "message": f"Fix process started for {repo_name}:{issue_num}"}

@app.post("/scan_now")
async def scan_now():
    def trigger():
        state["status"] = "Manual Scan"
        run_scan_cycle()
    threading.Thread(target=trigger, daemon=True).start()
    return {"status": "triggered", "message": "Manual scan cycle started in background."}

@app.post("/retry_issue")
async def retry_issue(request: Request):
    data = await request.json()
    issue_id = data.get("issue_id")

    if not issue_id or ":" not in issue_id:
        return JSONResponse(status_code=400, content={"message": "Invalid issue_id format. Expected 'repo:num'"})

    repo_name, issue_num = issue_id.split(":")

    logger.info(f"Manual retry: {issue_id}")

    def run_fix():
        success, msg = process_single_issue(repo_name, int(issue_num))
        if success:
            logger.info(f"Manual retry successful for {issue_id}")
        else:
            logger.error(f"Manual retry failed for {issue_id}: {msg}")

    threading.Thread(target=run_fix, daemon=True).start()
    return {"status": "triggered", "message": f"Retry started for {issue_id}"}

@app.post("/retry_all_failed")
async def retry_all_failed(request: Request):
    """Retries all issues that currently have a 'failed' or 'non-actionable' status with a given LLM preference."""
    data = await request.json()
    llm_pref = data.get("llm_preference")

    processed = load_processed()
    to_retry = [issue_id for issue_id, info in processed.items()
                if info.get("status") in ["failed", "non-actionable"]]

    if not to_retry:
        return {"status": "no_issues", "message": "No failed or non-actionable issues found to retry."}

    logger.info(f"Bulk retry triggered for {len(to_retry)} issues with preference {llm_pref}: {to_retry}")

    def bulk_run():
        config = load_config()
        max_w = int(config.get("MAX_CONCURRENT_FIXES", 2))
        with ThreadPoolExecutor(max_workers=max_w) as ex:
            futs = [
                ex.submit(process_single_issue, *issue_id.split(":"), llm_preference=llm_pref)
                for issue_id in to_retry
                if not state.get("paused")
            ]
            for f in futs:
                try:
                    f.result()
                except Exception as e:
                    logger.error(f"Bulk retry error: {e}")

    threading.Thread(target=bulk_run, daemon=True).start()
    return {"status": "triggered", "message": f"Bulk retry started for {len(to_retry)} issues using {llm_pref} LLM."}

@app.post("/restart")
async def restart_service():
    logger.info("Restart request received. Triggering systemctl restart...")
    try:
        import subprocess
        subprocess.Popen(["sudo", "systemctl", "restart", "bugfixer"])
        return {"status": "success", "message": "Restart signal sent successfully."}
    except Exception as e:
        logger.error(f"Restart failed: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/trigger_hub_update")
async def trigger_hub_update():
    """Triggers an update on all spokes and agents via the Hub API."""
    result = trigger_infrastructure_update()
    return {"status": "success" if "SUCCESS" in result else "error", "message": result}

# =============================================================================
# Chat agent: tool schemas, executors, secret sanitizer, and stream helpers.
#
# The chat agent gives the assistant awareness of BugFixer's repos, GitHub
# issues, processed-issue state, and recent Hub/self log errors. The LLM calls
# these tools on demand (Ollama /api/chat `tools`). All tools are READ-ONLY
# except `propose_fix`, which does NOT mutate GitHub either — it only produces a
# confirmation descriptor that the UI renders as a Confirm button; the actual
# fix run (process_single_issue) is launched only after the user clicks Confirm
# and the server validates a single-use token (see /api/chat/confirm_fix).
#
# SECURITY: API keys (GITHUB_TOKEN, LLM_API_KEY_1, LLM_API_KEY_2) live only on
# the server and are used solely to authenticate calls. They never enter chat
# messages, the system-prompt index, tool results, the streamed response, or logs.
# Every tool result is passed through _sanitize_tool_result, which redacts any
# accidental secret before it is appended to the conversation.
# =============================================================================

# GitHub token prefix patterns (classic PATs + fine-grained PATs). Redacted on
# sight in addition to exact-match denylisting of the configured keys.
_GH_TOKEN_RE = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{80,})')

# Skip-list mirroring identify_files_to_fix (main.py) so list_repo_files hides
# the same noise the automated pipeline hides.
_CHAT_FILE_SKIP = (".git", "node_modules", "__pycache__", "venv", ".env")


def _secret_denylist(config):
    """Builds the set of literal secret strings to redact from tool results."""
    dl = set()
    candidates = [
        config.get("GITHUB_TOKEN"), os.getenv("GITHUB_TOKEN"),
        config.get("LLM_API_KEY_1"), os.getenv("LLM_API_KEY_1"),
        config.get("LLM_API_KEY_2"), os.getenv("LLM_API_KEY_2"),
        config.get("LLM_API_KEY_3"), os.getenv("LLM_API_KEY_3"),
        config.get("LLM_API_KEY_4"), os.getenv("LLM_API_KEY_4"),
    ]
    # Also redact vault credentials.
    for cred in (config.get("llm_credentials") or {}).values():
        k = (cred.get("api_key") or "").strip()
        if k:
            candidates.append(k)
    for src in candidates:
        if src and isinstance(src, str):
            s = src.strip().strip('"').strip("'")
            if len(s) >= 8:
                dl.add(s)
    return dl


def _redact_text(text, denylist):
    if not text:
        return text
    t = text if isinstance(text, str) else str(text)
    for s in denylist:
        if s:
            t = t.replace(s, "***REDACTED***")
    return _GH_TOKEN_RE.sub("***REDACTED***", t)


def _sanitize_tool_result(obj, config):
    """Recursively redacts configured secrets + GitHub PAT patterns from a tool
    result (dict/list/str) before it is appended to the conversation or sent to
    the browser. Defense-in-depth: the executors never put keys into results in
    the first place, but an issue body or file may contain a leaked token."""
    deny = _secret_denylist(config)
    def walk(o):
        if isinstance(o, str):
            return _redact_text(o, deny)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return walk(obj)


def _trunc(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + " …[truncated]"


# --- Chat tool schemas (Ollama-compatible format; converted per-provider at call time) ---
CHAT_TOOLS = [
    {
        "name": "list_repos",
        "description": "List all repositories BugFixer monitors (monitored + trusted + self-diagnosis), with the count of open issues matching monitored labels for each. Use this first to learn what repos exist.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_issues",
        "description": "List open issues for a repo, optionally filtered by state/label/limit. Defaults to issues matching the configured monitored labels.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                "label": {"type": "string", "description": "single label filter; omit to use monitored labels"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_issue",
        "description": "Fetch one issue with its body, labels, state, and all comments. Use after list_issues to drill into a specific issue.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
            },
            "required": ["repo", "number"],
        },
    },
    {
        "name": "list_repo_files",
        "description": "List files in a repo's default branch via the git tree API (no clone). Skips .git/node_modules/__pycache__/venv/.env.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 300},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a single file's decoded contents from a repo's default branch. For large files, ask the user to narrow scope. Returns up to max_bytes.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 256, "maximum": 20000, "default": 8000},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "get_processed_issues",
        "description": "Return BugFixer's processed-issue state (statuses: fixed/verified/awaiting_prod_verification/failed/non-actionable/awaiting_review/processing). Optionally filter by repo.",
        "parameters": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_recent_errors",
        "description": "Fetch recent Hub + BugFixer self log errors. Returns deduped, capped error entries.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}},
            "required": [],
        },
    },
    {
        "name": "propose_fix",
        "description": "Propose running a full automated fix on an issue. Does NOT execute the fix. Returns a confirmation descriptor the user must approve in the UI before the fix runs. Pass llm_preference as 'cloud' or 'local', or omit for default.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "llm_preference": {"type": "string", "enum": ["cloud", "local"]},
            },
            "required": ["repo", "number"],
        },
    },
]


def _tool_list_repos(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    monitored = get_monitored_repos(config)
    trusted = config.get("trusted_repos", []) or []
    sd = resolve_self_diagnosis_repo(config)
    seen, out = set(), []
    label_filter = config.get("monitored_labels", ["automated-fix"]) or ["automated-fix"]
    for repo_name in list(dict.fromkeys(monitored + list(trusted))):
        entry = {"repo": repo_name, "is_trusted": repo_name in trusted,
                 "is_self_diagnosis": repo_name == sd, "open_monitored_issues": None}
        try:
            issues = gh.get_repo(repo_name).get_issues(state="open", labels=list(label_filter))
            count = sum(1 for _ in issues)
            entry["open_monitored_issues"] = count
        except Exception as e:
            entry["open_monitored_issues"] = f"(unavailable: {_trunc(type(e).__name__, 40)})"
        out.append(entry)
        seen.add(repo_name)
    return {"repos": out}


def _tool_list_issues(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    if not repo_name:
        return {"error": "repo is required"}
    state = args.get("state") or "open"
    limit = max(1, min(30, int(args.get("limit") or 10)))
    label = args.get("label")
    labels = [label] if label else (config.get("monitored_labels") or ["automated-fix"])
    try:
        issues = gh.get_repo(repo_name).get_issues(state=state, labels=list(labels))
        out = []
        for it in issues:
            if len(out) >= limit:
                break
            out.append({"number": it.number, "title": _trunc(it.title, 200),
                        "state": it.state, "labels": [lb.name for lb in it.labels],
                        "updated_at": str(it.updated_at)})
        return {"repo": repo_name, "state": state, "labels": labels, "issues": out}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_get_issue(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    number = args.get("number")
    if not repo_name or number is None:
        return {"error": "repo and number are required"}
    try:
        issue = gh.get_repo(repo_name).get_issue(int(number))
        comments = []
        for i, c in enumerate(issue.get_comments()):
            if i >= 20:
                comments.append({"author": "...", "body": "[more comments truncated]"})
                break
            try:
                author = c.user.login if c.user else "(unknown)"
            except Exception:
                author = "(unknown)"
            comments.append({"author": author, "body": _trunc(c.body, 1500)})
        return {"number": issue.number, "title": issue.title, "state": issue.state,
                "labels": [lb.name for lb in issue.labels],
                "body": _trunc(issue.body, 4000), "comments": comments}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_list_repo_files(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    if not repo_name:
        return {"error": "repo is required"}
    limit = max(1, min(500, int(args.get("limit") or 300)))
    try:
        repo = gh.get_repo(repo_name)
        tree = repo.get_git_tree(repo.default_branch, recursive=True)
        files = []
        for el in tree.tree:
            if getattr(el, "type", "") != "blob":
                continue
            p = el.path
            if any(seg in p for seg in _CHAT_FILE_SKIP):
                continue
            files.append(p)
            if len(files) >= limit:
                break
        return {"repo": repo_name, "branch": repo.default_branch,
                "files": files, "truncated": len(files) >= limit}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_read_file(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    path = (args.get("path") or "").strip()
    if not repo_name or not path:
        return {"error": "repo and path are required"}
    max_bytes = max(256, min(20000, int(args.get("max_bytes") or 8000)))
    try:
        contents = gh.get_repo(repo_name).get_contents(path)
        if isinstance(contents, list):
            return {"error": f"{path} is a directory, not a file"}
        raw = contents.decoded_content or b""
        text = raw.decode("utf-8", "replace")
        truncated = len(text) > max_bytes
        return {"repo": repo_name, "path": path, "truncated": truncated,
                "content": text[:max_bytes]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_get_processed_issues(gh, config, args):
    repo_filter = (args.get("repo") or "").strip()
    processed = load_processed()
    counts = {}
    sample = []
    matched = 0
    for issue_id, info in processed.items():
        if not isinstance(info, dict):
            continue
        if repo_filter and not issue_id.startswith(repo_filter + ":"):
            continue
        matched += 1
        st = info.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
        if len(sample) < 20:
            sample.append({"issue": issue_id, "status": st,
                           "timestamp": info.get("timestamp", "")})
    return {"filter_repo": repo_filter or None, "counts": counts, "total": matched,
            "total_all": len(processed), "sample": sample}


def _tool_get_recent_errors(gh, config, args):
    limit = max(1, min(50, int(args.get("limit") or 15)))
    hub_errors = []
    logs = get_hub_logs()
    if logs:
        try:
            hub_errors = filter_error_logs(logs)[:limit]
        except Exception as e:
            hub_errors = [{"error": f"filter failed: {type(e).__name__}"}]
    # Self errors: tail the local BugFixer log and keep ERROR/Traceback lines.
    self_errors = []
    try:
        path = get_log_path()
        if path and os.path.exists(path):
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()[-500:]
            for ln in lines:
                if re.search(r'\[ERROR\]|\[CRITICAL\]|Traceback|Exception|Error[: ]', ln):
                    self_errors.append(_trunc(ln.strip(), 300))
                    if len(self_errors) >= limit:
                        break
    except Exception:
        pass
    return {"hub_errors": hub_errors, "self_errors": self_errors,
            "note": "Use get_issue/list_issues for detail on any error filed as a GitHub issue."}


def _tool_propose_fix(gh, config, args):
    repo_name = (args.get("repo") or "").strip()
    number = args.get("number")
    if not repo_name or number is None:
        return {"error": "repo and number are required"}
    pref = args.get("llm_preference")
    if pref not in ("cloud", "local", None):
        pref = None
    # Best-effort issue title for the confirmation descriptor (no mutation).
    title = ""
    if gh is not None:
        try:
            title = gh.get_repo(repo_name).get_issue(int(number)).title or ""
        except Exception:
            title = ""
    token = uuid.uuid4().hex
    descriptor = {"kind": "confirm_fix", "repo": repo_name, "number": int(number),
                  "title": _trunc(title, 200), "llm_preference": pref, "confirm_token": token}
    return descriptor


CHAT_TOOL_EXECUTORS = {
    "list_repos": _tool_list_repos,
    "list_issues": _tool_list_issues,
    "get_issue": _tool_get_issue,
    "list_repo_files": _tool_list_repo_files,
    "read_file": _tool_read_file,
    "get_processed_issues": _tool_get_processed_issues,
    "get_recent_errors": _tool_get_recent_errors,
    "propose_fix": _tool_propose_fix,
}


# --- Chat stream / proposal helpers (all lock-guarded) -----------------------
def _set_chat_stream_status(chat_id, text):
    """Publishes an interim status string (e.g. '[calling tool: list_issues …]')
    so /api/chat/stream shows progress during multi-turn tool resolution. Writes
    to both chat_streams (under _chat_lock) and active_tasks (under
    _task_state_lock), mirroring how chat_stream folds active_tasks in."""
    with _chat_lock:
        entry = state.setdefault("chat_streams", {}).setdefault(chat_id, {})
        entry["stream"] = text
        entry["done"] = False
        entry["error"] = None
    with _task_state_lock:
        task = state.get("active_tasks", {}).get(chat_id)
        if task is not None:
            task["stream"] = text


def _finalize_chat_stream(chat_id, text):
    with _chat_lock:
        state.setdefault("chat_streams", {})[chat_id] = {
            "stream": text or "", "done": True, "error": None,
        }


def _set_chat_stream_error(chat_id, message):
    with _chat_lock:
        state.setdefault("chat_streams", {})[chat_id] = {
            "stream": "", "done": True, "error": message,
        }


def _register_fix_proposal(chat_id, descriptor, config):
    """Stores a fix-proposal confirmation token server-side (under _chat_lock)
    with a creation timestamp so /api/chat/confirm_fix can validate + TTL it."""
    token = descriptor.get("confirm_token")
    if not token:
        return
    ttl = int(config.get("CHAT_FIX_PROPOSAL_TTL", 600) or 600)
    with _chat_lock:
        state.setdefault("chat_fix_proposals", {})[token] = {
            "repo": descriptor.get("repo"),
            "number": descriptor.get("number"),
            "llm_preference": descriptor.get("llm_preference"),
            "chat_id": chat_id,
            "created": time.time(),
            "ttl": ttl,
        }


def _confirm_fix_marker(descriptor):
    """Renders the propose_fix descriptor as a fenced block the chat UI parses
    into a Confirm button. The confirm_token is a server-generated uuid (not a
    secret); the descriptor has already been through _sanitize_tool_result."""
    pref = descriptor.get("llm_preference") or ""
    return (
        f":::confirm_fix repo={descriptor.get('repo')} number={descriptor.get('number')} "
        f"token={descriptor.get('confirm_token')} pref={pref}\n"
        f"Run automated fix on #{descriptor.get('number')} "
        f"\"{descriptor.get('title', '')}\"? Click Confirm to proceed.\n:::"
    )


def _run_chat_reply_simple(chat_id, config):
    """Legacy single-turn chat path: used when CHAT_TOOLS_ENABLED is False (or as
    the graceful-degradation fallback). Streams one call_llm reply with a plain
    system prompt. Preserves the pre-tool chat behavior."""
    try:
        window_size = int(config.get("CHAT_HISTORY_WINDOW", 20) or 20)
        system_prompt = config.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return
        messages = conv.get("messages", [])
        window = [{"role": "system", "content": system_prompt}] + messages[-window_size:]
        reply = call_llm("", messages=window, task_id=chat_id)
        if reply and reply.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": reply,
                "ts": datetime.now().isoformat(),
            })
        _finalize_chat_stream(chat_id, reply or "")
    except Exception as e:
        logger.error(f"_run_chat_reply_simple failed for {chat_id}: {e}\n{traceback.format_exc()}")
        _set_chat_stream_error(chat_id, f"LLM error: {e}")


def run_chat_reply(chat_id):
    """Background worker that produces an LLM reply for one conversation turn.

    With CHAT_TOOLS_ENABLED (default), runs an agent loop: the system prompt
    carries a compact repo/issue index (build_chat_context_index) and the model
    may call read-only tools (CHAT_TOOLS) to drill in. propose_fix does not
    mutate; it emits a :::confirm_fix block the UI renders as a Confirm button,
    and the real fix run only happens via /api/chat/confirm_fix after the user
    clicks. Without a GitHub token, tools are disabled but the index still gives
    the assistant repo/issue awareness. Tool turns are non-streaming so
    message.tool_calls parse cleanly; interim status is written to
    state["chat_streams"][chat_id] / active_tasks so /api/chat/stream shows
    progress. Completion/error is tracked in state["chat_streams"][chat_id].
    """
    try:
        config = load_config()
        if not config.get("CHAT_TOOLS_ENABLED", True):
            return _run_chat_reply_simple(chat_id, config)

        window_size = int(config.get("CHAT_HISTORY_WINDOW", 20) or 20)
        base_system = config.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
        max_iter = int(config.get("CHAT_TOOL_MAX_ITERATIONS", 6) or 6)
        # Token-budget config is in ~tokens; apply a 4x char budget for results.
        max_result_chars = int(config.get("CHAT_TOOL_MAX_TOKENS", 12000) or 12000) * 4

        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return

        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        gh = Github(token) if token else None

        # Index gives awareness even without tools; gh passed so issue titles fill in.
        index_text = build_chat_context_index(config, gh=gh)
        system_prompt = base_system + "\n\n" + index_text
        history = conv.get("messages", [])
        window = history[-window_size:]
        messages = [{"role": "system", "content": system_prompt}] + list(window)

        # No GitHub token -> no tools, but the index still informs the answer.
        if gh is None:
            _set_chat_stream_status(chat_id, "Thinking…")
            reply = call_llm("", messages=messages, task_id=chat_id)  # streaming string
            if reply and reply.strip():
                append_chat_message(chat_id, {
                    "role": "assistant",
                    "content": reply,
                    "ts": datetime.now().isoformat(),
                })
            _finalize_chat_stream(chat_id, reply or "")
            return

        tools = CHAT_TOOLS
        used_chars = 0
        final_text = None
        last_text = ""
        for iteration in range(max_iter):
            _set_chat_stream_status(chat_id, "Thinking…" if iteration == 0 else "Working…")
            try:
                result = call_llm("", messages=messages, task_id=chat_id, tools=tools, stream=False)
            except Exception as e:
                # Tool-capable /api/chat failed (e.g. cloud without /api/chat tool
                # support). Degrade to one streaming index-only turn and finish.
                logger.warning(f"Chat tool turn {iteration} failed ({e}); degrading to index-only answer.")
                try:
                    reply = call_llm("", messages=messages[:], task_id=chat_id)
                    final_text = reply or ""
                except Exception as ee:
                    _set_chat_stream_error(chat_id, f"LLM error: {ee}")
                    return
                break

            if not isinstance(result, dict):
                final_text = str(result)
                break
            text = result.get("text") or ""
            tool_calls = result.get("tool_calls") or []
            last_text = text
            if not tool_calls:
                final_text = text
                break

            # Echo the assistant turn (with tool_calls) back for the next round.
            messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})

            hit_proposal = False
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name")
                args_raw = fn.get("arguments") if fn else tc.get("arguments")
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw) if args_raw else {}
                    except Exception:
                        args = {}
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    args = {}

                if not name or name not in CHAT_TOOL_EXECUTORS:
                    out = {"error": f"unknown tool: {name}"}
                else:
                    _set_chat_stream_status(chat_id, f"[calling tool: {name} …]")
                    try:
                        out = CHAT_TOOL_EXECUTORS[name](gh, config, args)
                    except Exception as ee:
                        out = {"error": f"{type(ee).__name__}: {_trunc(ee, 300)}"}
                out = _sanitize_tool_result(out, config)
                out_str = json.dumps(out)
                if used_chars + len(out_str) > max_result_chars:
                    out_str = json.dumps({"error": "tool result budget exceeded; narrow your query", "truncated": True})
                used_chars += len(out_str)
                tool_call_id = tc.get("id") or f"call_{name}_{iteration}"
                messages.append({"role": "tool", "name": name or "unknown", "content": out_str, "tool_call_id": tool_call_id})

                # propose_fix is non-mutating: surface a Confirm button and stop.
                if name == "propose_fix" and isinstance(out, dict) and out.get("kind") == "confirm_fix":
                    _register_fix_proposal(chat_id, out, config)
                    marker = _confirm_fix_marker(out)
                    messages.append({"role": "system", "content": "A confirmation button has been shown to the user for this fix. Stop calling tools this turn and tell the user to click Confirm to run the fix."})
                    final_text = (text + "\n\n" + marker).strip() if text else marker
                    hit_proposal = True
                    break
            if hit_proposal:
                break
        else:
            # Iteration cap reached without a no-tool_calls turn; return last text.
            final_text = last_text or ""

        if final_text is None:
            final_text = last_text or ""
        if final_text and final_text.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": final_text,
                "ts": datetime.now().isoformat(),
            })
        _finalize_chat_stream(chat_id, final_text or "")
    except Exception as e:
        logger.error(f"run_chat_reply failed for {chat_id}: {e}\n{traceback.format_exc()}")
        _set_chat_stream_error(chat_id, f"LLM error: {e}")
    finally:
        # Remove the chat task from the Dashboard activity feed.
        update_task_state(chat_id, "Chat", action="end")

@app.get("/chat")
async def chat_page(request: Request, chat_id: str = None):
    """Server-rendered Chat view; renders the sidebar + the active conversation."""
    store = load_chats()
    if chat_id and set_active_chat(chat_id):
        store = load_chats()
    active_id = store["active_id"]
    conv = get_conversation(store, active_id) or store["conversations"][0]
    chats_list = [{"id": c["id"], "title": c.get("title", "")} for c in store["conversations"]]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "view": "chat",
            "state": state,
            "chats": chats_list,
            "active_chat_id": conv["id"],
            "active_chat_title": conv.get("title", "") or "New chat",
            "chat_history": conv.get("messages", []),
        },
    )

@app.post("/api/chat/new")
async def chat_new():
    """Creates a new empty conversation and makes it active."""
    cid = create_conversation()
    return {"chat_id": cid}

@app.post("/api/chat")
async def chat_send(request: Request):
    """Accepts a user message for a conversation, persists it, kicks off a reply."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    message = (data.get("message") or "").strip() if isinstance(data, dict) else ""
    if not message:
        return JSONResponse(status_code=400, content={"message": "Message is required"})
    chat_id = (data.get("chat_id") or "").strip() if isinstance(data, dict) else ""
    if not chat_id:
        chat_id = load_chats()["active_id"]

    appended = append_chat_message(chat_id, {
        "role": "user",
        "content": message,
        "ts": datetime.now().isoformat(),
    })
    if appended is None:
        return JSONResponse(status_code=404, content={"message": "Conversation not found"})

    with _chat_lock:
        state["chat_streams"][chat_id] = {"stream": "", "done": False, "error": None}
    update_task_state(chat_id, "Chat", action="start")

    threading.Thread(target=run_chat_reply, args=(chat_id,), daemon=True).start()
    return {"chat_id": chat_id}

@app.get("/api/chat/stream")
async def chat_stream(chat_id: str):
    """Polls the live assistant stream and completion state for a conversation."""
    with _chat_lock:
        entry = state["chat_streams"].get(chat_id)
        if entry is None:
            return {"done": True, "stream": "", "error": "Unknown chat_id"}
        stream_text = entry.get("stream", "")
        done = bool(entry.get("done"))
        error = entry.get("error")
    # Fold in any partial progress call_llm streamed into active_tasks.
    with _task_state_lock:
        task = state["active_tasks"].get(chat_id)
        if task and task.get("stream"):
            stream_text = task["stream"]
    return {"done": done, "stream": stream_text, "error": error}

@app.post("/api/chat/rename")
async def chat_rename(request: Request):
    """Renames a conversation."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    chat_id = (data.get("chat_id") or "").strip() if isinstance(data, dict) else ""
    title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
    if not chat_id:
        return JSONResponse(status_code=400, content={"message": "chat_id is required"})
    ok = rename_conversation(chat_id, title)
    return {"ok": ok}

@app.post("/api/chat/delete")
async def chat_delete(request: Request):
    """Deletes a conversation and selects a new active one."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    chat_id = (data.get("chat_id") or "").strip() if isinstance(data, dict) else ""
    if not chat_id:
        return JSONResponse(status_code=400, content={"message": "chat_id is required"})
    new_active = delete_conversation(chat_id)
    with _chat_lock:
        state["chat_streams"].pop(chat_id, None)
    return {"active_chat_id": new_active}

@app.post("/api/chat/clear")
async def chat_clear():
    """Clears the active conversation's messages (keeps the conversation shell)."""
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, store["active_id"])
        if conv:
            conv["messages"] = []
            conv["title"] = ""
        save_chats(store)
        state["chat_streams"].pop(store["active_id"], None)
    return {"ok": True}

@app.post("/api/chat/confirm_fix")
async def chat_confirm_fix(request: Request):
    """Confirms a chat-proposed automated fix and launches it in the background.

    The chat agent's propose_fix tool does NOT mutate GitHub; it registers a
    single-use, TTL-bounded confirmation token in state["chat_fix_proposals"]
    and emits a :::confirm_fix block the UI renders as a Confirm button. Only
    when the user clicks Confirm does this endpoint run: it validates + consumes
    the token, then launches process_single_issue in a daemon thread (the fix
    run clones, runs tests, and can take minutes — it must NOT block the chat or
    the request). Returns the pipeline's own issue_id task_id so the UI can watch
    progress via /api/task-details. Never returns any API key.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    token = (data.get("token") or "").strip() if isinstance(data, dict) else ""
    if not token:
        return JSONResponse(status_code=400, content={"message": "Missing token"})

    config = load_config()
    ttl = int(config.get("CHAT_FIX_PROPOSAL_TTL", 600) or 600)
    with _chat_lock:
        prop = state.get("chat_fix_proposals", {}).pop(token, None)
    if not prop:
        return JSONResponse(status_code=410, content={"message": "Proposal expired or already used"})
    if time.time() - float(prop.get("created", 0)) > ttl:
        return JSONResponse(status_code=410, content={"message": "Proposal expired"})

    repo_name = prop.get("repo")
    issue_num = prop.get("number")
    pref = prop.get("llm_preference")
    if not repo_name or issue_num is None:
        return JSONResponse(status_code=400, content={"message": "Invalid proposal"})

    # Use the pipeline's own issue_id form so /api/task-details latches onto the
    # update_task_state entries process_single_issue creates internally.
    task_id = f"{repo_name}:{issue_num}"

    def _run():
        try:
            ok, msg = process_single_issue(repo_name, issue_num, llm_preference=pref)
            logger.info(f"Chat-triggered fix {repo_name}:{issue_num} -> ok={ok} msg={msg}")
        except Exception as e:
            logger.error(f"Chat-triggered fix {repo_name}:{issue_num} failed: {e}\n{traceback.format_exc()}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "triggered", "task_id": task_id, "repo": repo_name, "number": issue_num}

threading.Thread(target=connectivity_worker, daemon=True).start()
threading.Thread(target=heartbeat_worker, daemon=True).start()
threading.Thread(target=poller_worker, daemon=True).start()
threading.Thread(target=updater_worker, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)