import asyncio
import contextlib
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
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from github import Github, GithubException
import git
from tenacity import retry

# Setup Logging — uses the shared logging_setup helper when lm/core is on
# PYTHONPATH, else an inline equivalent (bugfixer is self-contained and does
# NOT import lm/core, so the inline fallback is the normal path here). Either
# way LOG_LEVEL env is honored at boot and the standard format (with %(name)s)
# is applied so the BugFixer logger name carries identity without a literal tag.
try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        import logging as _logging
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=_logging.INFO, *, log_file=None, **_):
            handlers = ([_logging.FileHandler(log_file, mode='a'), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)

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

_resolved_level = configure_logging(log_file=log_file)
logger = logging.getLogger("BugFixer")
logger.info(f"BugFixer started. Logging level: {logging.getLevelName(_resolved_level)}. Logging to: {log_file}")

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
    # AI fix-generation context bounds. apply_ai_fix used to concatenate every
    # relevant file in full, which blew past provider limits (groq 413 Payload
    # Too Large, ollama "Response ended prematurely", then truncated JSON →
    # "unmatched '}'"). These bound the prompt so the returned fix JSON stays
    # complete and parseable.
    "FIX_MAX_FILES": 5,                # Max relevant files included in one fix prompt.
    "FIX_MAX_FILE_CHARS": 12000,       # Max chars per file in the prompt (truncated past this).
    "FIX_MAX_CONTEXT_CHARS": 60000,   # Max total chars across all files in one prompt.
    "FIX_MAX_OUTPUT_TOKENS": 8192,     # max_tokens sent on OpenAI-compatible fix requests (output headroom).
    # Per-module heartbeat triage. scan_heartbeats reads the raw Hub logs and
    # files/reopens an issue when an expected module's [heartbeat] line is
    # missing or older than HEARTBEAT_STALE_S. heartbeat_exclude is a list of
    # spoke_ids and/or module_types to never triage (e.g. an undeployed spoke).
    "HEARTBEAT_STALE_S": 300,          # Max age (seconds) of a heartbeat line before triage.
    "heartbeat_exclude": [],           # spoke_ids / module_types to skip (list).
}


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

STARTUP_STAMP_FILE = os.path.join(CONFIG_DIR, "startup_stamp.json")

def write_startup_stamp():
    """Record which commit this process actually booted on, plus start time / pid /
    main.py mtime. The watchdog reads this to detect stale-running code (disk updated
    but the process never restarted) and force a restart to load the pending update.
    Also drives the Diagnostics panel's running-vs-disk version comparison."""
    try:
        commit = "unknown"
        try:
            commit = git.Repo(os.getcwd()).head.commit.hexsha
        except Exception as ge:
            logger.warning(f"Startup stamp: could not read git commit: {ge}")
        stamp = {
            "commit": commit,
            "started_at": datetime.now().isoformat(),
            "pid": os.getpid(),
            "main_mtime": os.path.getmtime(__file__),
        }
        with open(STARTUP_STAMP_FILE, "w") as f:
            json.dump(stamp, f, indent=2)
        logger.info(f"Startup stamp written: commit={commit[:7] if commit != 'unknown' else 'unknown'} pid={os.getpid()}")
    except Exception as e:
        logger.warning(f"Could not write startup stamp: {e}")

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


def _provider_configured(provider, key, model):
    """A provider is usable when it has a model, and either an API key or is a no-key
    provider (claude_cli session auth, LM Studio local server). Centralizes the no-key
    exception so every configured-check site agrees on what "configured" means."""
    return bool(model and (key or provider == "claude_cli" or _is_lmstudio(provider)))


def _record_provider_result(n, status, reason=""):
    """Record the last failover outcome for provider slot n.

    Surfaces silent skips (e.g. ``not_configured``) and per-provider failure reasons in
    the Diagnostics panel so they are visible without reading CLI logs. Best-effort:
    never raises into the failover path.
    """
    try:
        state["provider_last_result"][n] = {
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
        # claude_cli and lmstudio don't need an API key (session auth / local server).
        if model and (api_key or provider == "claude_cli" or _is_lmstudio(provider)):
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

load_dotenv(ENV_FILE)
app = FastAPI()

# ── Web-server bind (unified-443: HTTPS on :443 by default) ──────────────────
# Overridable via env (set in the systemd unit or for local dev). install.sh
# generates a self-signed cert at SSL_CERT/SSL_KEY; when both exist the server
# serves HTTPS, otherwise it falls back to plain HTTP on the SAME port so the
# UI still comes up. The internal restart/health check and watchdog.py derive
# their probe URL from the same settings so they never drift from the bind.
SERVER_HOST = os.environ.get("BUGFIXER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("BUGFIXER_PORT", "443") or "443")
SSL_CERT = (os.environ.get("BUGFIXER_SSL_CERT", "/etc/bugfixer/cert.pem") or "").strip()
SSL_KEY  = (os.environ.get("BUGFIXER_SSL_KEY", "/etc/bugfixer/key.pem") or "").strip()

def _tls_enabled() -> bool:
    return bool(SSL_CERT and SSL_KEY and os.path.exists(SSL_CERT) and os.path.exists(SSL_KEY))

def _local_health_url() -> str:
    scheme = "https" if _tls_enabled() else "http"
    return f"{scheme}://127.0.0.1:{SERVER_PORT}/api/health"

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
        configured = model and (key or provider == "claude_cli" or _is_lmstudio(provider))
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
        # claude_cli (Claude Code session) and LM Studio (local server) need no
        # API key — only a model must be configured for them to be usable.
        if provider == "claude_cli" or _is_lmstudio(provider):
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
        logger.error(f"LLM request failed after all providers: {e}")
        raise



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
# Issues closed on GitHub and recorded locally as `closed` (terminal resolved state).
closed_count = sum(1 for info in processed_init.values() if info.get("status") == "closed")

state = {
    "status": "Idle", "active_llm": "Unknown",
    "provider_1_online": False, "provider_2_online": False, "provider_3_online": False, "provider_4_online": False,
    "provider_1_configured": False, "provider_2_configured": False, "provider_3_configured": False, "provider_4_configured": False,
    # Per-slot last failover outcome (status sentinel + reason + iso8601), surfaced in the
    # Diagnostics panel so silent skips (e.g. "not_configured") are visible without CLI logs.
    "provider_last_result": {1: None, 2: None, 3: None, 4: None},
    # Bounded recent log of self-update / restart events for the Diagnostics panel.
    "restart_log": [],
    "local_online": False, "cloud_online": False,
    "last_run": "Never", "api_status": "Not Triggered",
    "processed": processed_init,
    "version": get_version(), "llm_stream": "",
    "active_tasks": {}, "qa_enabled": config_on_start.get("qa_enabled", True),
    "success_count": success_count, "failure_count": failure_count, "closed_count": closed_count,
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
    # Hub agent (WebSocket) status — BugFixer authenticates to the LM Hub as an
    # agent like any other system, instead of the removed static admin token.
    "hub_agent_status": "not_registered",  # not_registered | pending | approved | error
    "hub_agent_message": "",
    "hub_agent_last_seen": "",
}

# Stamp which commit this process booted on, before any worker starts, so the watchdog
# can detect stale-running code and the Diagnostics panel can show running vs disk versions.
write_startup_stamp()

try:
    validate_llm_config_on_startup()
except Exception as ve:
    logger.warning(f"Startup LLM validation failed (non-fatal): {ve}")



# --- Chat system-prompt context index (cached, TTL-bounded) -----------------
# A compact markdown snapshot of BugFixer's repos, their open monitored-label
# issues, processed-issue status totals, and recent Hub error count. Prepended
# to the chat system prompt every turn so the assistant has the lay of the land
# without a tool round-trip. Tool calls drill deeper on demand.








# Default colors for labels bugfixer creates when missing. PyGithub's
# create_issue(labels=...) raises UnknownObjectException if a label doesn't
# exist on the target repo, so _ensure_label creates it first. The "Bug"
# label is applied to user-filed "File a Bug" reports (see scan_bugs).








# --- Duplicate issue detection configuration ---------------------------------
# How far back to look at CLOSED issues when searching for a recurrence. The bot
# previously only searched OPEN issues, so once a "fix" was merged and the issue
# closed, the next cycle's identical error was filed as a brand-new issue +
# spawned a new ai-fix-issue-* branch — the #25 -> #55 -> #78 -> #90 storm.
# Searching recently-closed issues lets us REOPEN the original instead.
# When the target repo has no match and we fall back to searching the OTHER
# monitored repos globally, require a stricter title-level signal to avoid
# cross-module false positives (e.g. an opnsense error matching a pxmx issue on
# incidental wording overlap).







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
    if _is_lmstudio(p):
        # LM Studio needs no API key — just confirm the local server is reachable.
        if not model:
            return False
        try:
            base = _normalize_lmstudio_url(base_url).rstrip("/")
            resp = requests.get(f"{base}/models", timeout=10)
            if resp.status_code == 401:
                return False
            return resp.status_code < 300
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

            p1_configured = _provider_configured(p1_provider, p1_key, p1_model)
            p2_configured = _provider_configured(p2_provider, p2_key, p2_model)
            p3_configured = _provider_configured(p3_provider, p3_key, p3_model)
            p4_configured = _provider_configured(p4_provider, p4_key, p4_model)

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







def _trigger_spoke_updates(config):
    """Trigger updates across the Hub, its spokes, and its agents after a fix push.

    BugFixer is an authenticated WebSocket agent of the Hub, so it issues a single
    TRIGGER_ALL_UPDATES request rather than the old admin-token HTTP calls (which
    the Hub never actually honored and always returned 401). The Hub self-updates,
    fans SPOKE_UPDATE to every approved spoke, and queues updates for every
    approved agent. Fire-and-forget; actual restarts are asynchronous. A post-update
    cooldown is started so transient "service offline" errors during restarts don't
    produce spurious GitHub issues.
    """
    client = _get_hub_agent_client()
    if not client:
        logger.debug("_trigger_spoke_updates: Hub agent not configured/approved, skipping")
        _set_update_cooldown(config)
        return
    result = client.request_sync("TRIGGER_ALL_UPDATES", {}, timeout=60)
    if not isinstance(result, dict):
        logger.warning("Hub agent not approved/connected — skipping update trigger")
        _set_update_cooldown(config)
        return
    hub = result.get("hub") or {}
    spokes = result.get("spokes") or {}
    agents = result.get("agents") or {}
    logger.info(
        f"Hub update triggered: hub={_upd_summary(hub)} | spokes={_upd_summary(spokes)} | agents={_upd_summary(agents)}"
    )
    # Suppress issue filing while services are restarting.
    _set_update_cooldown(config)


def _upd_summary(d):
    """Compact one-line summary of a Hub update-method result dict."""
    if not isinstance(d, dict):
        return str(d)
    msg = d.get("message") or d.get("status") or ""
    trig = d.get("triggered") or []
    if isinstance(trig, list) and trig:
        return f"{msg} (triggered: {', '.join(map(str, trig))})"
    return msg


# ---------------------------------------------------------------------------
# Hub agent (WebSocket) — BugFixer authenticates to the LM Hub as an agent.
# See hub_agent.py for the protocol. The helpers below bridge the async agent
# client to BugFixer's sync state dict + config file.
# ---------------------------------------------------------------------------

def _derive_ws_url(http_url):
    """Derive a Hub WebSocket URL from an HTTP Hub URL.

    The unified hub shares ONE port (:443) for HTTP + WebSocket, with the spoke
    socket on the ``/ws/spoke`` path — the old bare ``:8765`` raw-socket listener
    is gone. So derive ``wss://<host>:443/ws/spoke`` (hub_agent._normalize_hub_ws_url
    would fill the same defaults). TLS is unverified by default (self-signed hub).
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse((http_url or "").strip())
        host = parsed.hostname
        if not host:
            return ""
        return f"wss://{host}:443/ws/spoke"
    except Exception:
        return ""


def _hub_agent_on_status(status, message):
    """Callback: the agent client updated its connection/approval status."""
    state["hub_agent_status"] = status
    state["hub_agent_message"] = message or ""
    state["hub_agent_last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _persist_config_key(key, value):
    """Callback helper: upsert a single key into config.json (agent-managed secrets)."""
    try:
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)
    except Exception as e:
        logger.warning(f"Could not persist {key} to config: {e}")


def _hub_agent_on_secret(secret):
    """Callback: the Hub pushed a (possibly empty) session secret."""
    _persist_config_key("HUB_AGENT_SECRET", secret or "")


def _hub_agent_on_hub_secret(hub_secret):
    """Callback: the Hub pushed its (rotated) identity secret for mutual auth."""
    _persist_config_key("HUB_SECRET", hub_secret or "")


def _get_hub_agent_client():
    """Return the running Hub agent singleton, or None if not started."""
    try:
        import hub_agent
        return hub_agent.hub_agent_client
    except Exception:
        return None


def _start_hub_agent():
    """Start the Hub agent at app startup if a Hub WebSocket URL is configured.

    HUB_WS_URL is authoritative; if it's empty we try to derive it from
    HUB_QUERY_URL (host + :8765). If still empty, the agent stays unstarted and
    state reports "not_registered" — BugFixer runs fine without Hub integration.
    """
    try:
        import hub_agent
        cfg = load_config()
        ws_url = (cfg.get("HUB_WS_URL") or "").strip()
        if not ws_url:
            hq = (cfg.get("HUB_QUERY_URL") or "").strip()
            if hq and "your-netbox" not in hq:
                ws_url = _derive_ws_url(hq)
        if not ws_url:
            state["hub_agent_status"] = "not_registered"
            state["hub_agent_message"] = "HUB_WS_URL not configured"
            return
        # Pass the resolved WS URL into the config dict the client reads.
        cfg_with_ws = dict(cfg)
        cfg_with_ws["HUB_WS_URL"] = ws_url
        hub_agent.start_agent_from_config(
            cfg_with_ws,
            on_status=_hub_agent_on_status,
            on_secret=_hub_agent_on_secret,
            on_hub_secret=_hub_agent_on_hub_secret,
        )
    except Exception as e:
        logger.warning(f"Could not start Hub agent: {e}")
        state["hub_agent_status"] = "error"
        state["hub_agent_message"] = str(e)






def _wait_for_spokes_online(config, min_count=1, timeout=90):
    """Poll until at least min_count spokes appear online, then return their IDs.

    Prefers the Hub agent's GET_SPOKE_STATUS (signed, authenticated). Falls back
    to the public HTTP GET /status endpoint if the agent isn't approved yet —
    that endpoint is ungated and never 401s. Spokes reconnect after their systemd
    unit restarts (~10–20 s). Returns the list of connected spoke IDs, or an
    empty list on timeout.
    """
    hub_url = (config.get("HUB_QUERY_URL") or "").rstrip("/")
    client = _get_hub_agent_client()
    if not hub_url and not client:
        return []
    deadline = time.time() + timeout
    logger.info(f"Waiting for ≥{min_count} spoke(s) to reconnect (timeout {timeout}s)…")
    while time.time() < deadline:
        conns = None
        # 1. Try the authenticated agent request first.
        if client:
            result = client.request_sync("GET_SPOKE_STATUS", {}, timeout=10)
            if isinstance(result, dict):
                conns = result.get("active_connections") or []
        # 2. Fall back to the public HTTP /status endpoint.
        if conns is None and hub_url:
            try:
                r = requests.get(f"{hub_url}/status", timeout=10)
                if r.status_code == 200:
                    conns = r.json().get("active_connections", [])
            except Exception:
                conns = None
        if isinstance(conns, list) and len(conns) >= min_count:
            logger.info(f"Spokes online: {conns}")
            return conns
        time.sleep(5)
    logger.warning(f"Timed out after {timeout}s waiting for spokes to come back online")
    return []





def check_for_updates():
    """Checks GitHub for new versions, performs pre-flight syntax checks, and signals a restart if safe."""
    try:
        self_repo = git.Repo(os.getcwd())
        old_commit = self_repo.head.commit.hexsha

        update_state = load_update_state()
        update_state["last_known_good_commit"] = old_commit
        save_update_state(update_state)

        # PRE-PULL GATE: don't re-pull a commit we already know is bad (syntax failure
        # or watchdog rollback). Derive the tracked branch instead of hardcoding main.
        try:
            tracked = None
            try:
                tracked = self_repo.active_branch.tracking_branch().name.split("/")[-1]
            except Exception:
                pass
            tracked = tracked or "main"
            self_repo.remotes.origin.fetch()
            remote_head = self_repo.commit(f"origin/{tracked}").hexsha
        except Exception as fe:
            logger.warning(f"Pre-pull gate: could not read remote head: {fe}")
            remote_head = None
        if remote_head and remote_head in update_state.get("failed_commits", []):
            logger.warning(f"Remote head {remote_head[:7]} is in failed_commits blocklist. Skipping pull.")
            return False, f"Update skipped: remote head {remote_head[:7]} is a known-bad commit."

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

            # Signal the dedicated restart_worker to apply the restart. This is decoupled
            # from the scan cycle (and from state["paused"]) so the running process reliably
            # reloads the new code; the watchdog enforces it as a durable backstop.
            state["restart_pending"] = True
            logger.info("Restart scheduled — restart_worker will apply it shortly.")
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
            updated, msg = check_for_updates()
            _log_restart_event("auto_update", msg, ok=bool(updated))
        except Exception as e:
            logger.error(f"Updater worker error: {e}")
        time.sleep(3600)

def _log_restart_event(kind, message, ok=True):
    """Append a bounded entry to state["restart_log"] for the Diagnostics panel."""
    try:
        entry = {"at": datetime.now().isoformat(), "kind": kind,
                 "result": "ok" if ok else "failed", "message": str(message)[:200]}
        log = state.get("restart_log", [])
        log.append(entry)
        # keep only the most recent 20
        del log[:-20]
        state["restart_log"] = log
    except Exception:
        pass

def _spawn_restart():
    """Spawn a detached `systemctl restart bugfixer` that survives this process dying.
    As root (dev/standalone) that's a direct `systemctl restart bugfixer`; as the
    svc_bg service user, it goes through the narrow root helper
    /usr/local/bin/bugfixer-self-restart (granted via /etc/sudoers.d/bugfixer),
    which re-execs into a transient systemd unit OUTSIDE bugfixer.service's cgroup
    — a bare `systemctl restart bugfixer` from inside the unit races the
    stop/start against this process's cgroup and can strand bugfixer inactive
    (same ~min-strand bug lm hit, see lm/install_all.sh lm-self-restart).
    Detaching into a new session + closing fds ensures the restart proceeds
    even after the parent receives SIGTERM."""
    import subprocess as _sp
    if os.geteuid() == 0:
        cmd = ["systemctl", "restart", "bugfixer"]
    else:
        cmd = ["sudo", "-n", "/usr/local/bin/bugfixer-self-restart"]
    _sp.Popen(cmd, start_new_session=True, stdout=_sp.DEVNULL,
              stderr=_sp.DEVNULL, close_fds=True)

def restart_worker():
    """Applies a pending restart independent of scan-cycle completion and paused state.

    Watches state["restart_pending"]; when set, consumes the flag, waits a short grace
    window so in-flight git clone / LLM calls can reach a commit point or fail cleanly
    (SIGTERM mid-op is already classified as a non-bug by the self-diagnosis filter),
    then spawns a detached `systemctl restart bugfixer`. Best-effort verifies health
    post-restart and retries the spawn once. The authoritative post-restart verification
    is the watchdog's restart-then-verify flow; this worker is the fast path and the
    on-disk update_pending file is the durable backstop."""
    GRACE_SECONDS = 15
    VERIFY_TIMEOUT = 60
    VERIFY_INTERVAL = 3
    HEALTH_URL = _local_health_url()
    while True:
        try:
            if state.get("restart_pending"):
                state["restart_pending"] = False
                logger.info(f"restart_worker: applying restart (grace window {GRACE_SECONDS}s).")
                time.sleep(GRACE_SECONDS)
                _spawn_restart()
                _log_restart_event("restart", "restart spawned", ok=True)
                # The current process will receive SIGTERM from systemd within ~1s. If by
                # some reason we are still alive after VERIFY_TIMEOUT, retry the spawn once.
                t0 = time.time()
                healthy = False
                while time.time() - t0 < VERIFY_TIMEOUT:
                    try:
                        r = requests.get(HEALTH_URL, timeout=2, verify=False)
                        if r.status_code == 200 and r.json().get("status") == "ok":
                            healthy = True
                            break
                    except Exception:
                        pass
                    time.sleep(VERIFY_INTERVAL)
                if not healthy:
                    logger.error("restart_worker: post-restart health not observed; retrying spawn.")
                    _log_restart_event("restart", "post-restart health failed; retrying", ok=False)
                    _spawn_restart()
            else:
                time.sleep(2)
        except Exception as e:
            logger.error(f"restart_worker error: {e}")
            time.sleep(5)




# Static module_type -> repo map for heartbeat triage. Each spoke advertises a
# logical module_type (e.g. "firewall", "hypervisor") at Hub auth time; this maps
# that type to the repo whose issues a missing/stale heartbeat should land in.
# "hub" covers the Hub itself. Falls back to module_repo_map / resolve_module_repo
# / resolve_self_diagnosis_repo at runtime so every module gets triaged somewhere.










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
            "BugFixer settings (https://<this-host>/settings) to a valid, accessible "
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
                f"BugFixer settings (https://<this-host>/settings) to point at a valid, "
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
            # Hub agent lifecycle — expected during onboarding/approval/rotation,
            # not a BugFixer bug.
            "Hub agent",
            "APPROVAL_REQUIRED",
            "pending admin approval",
            "Hub rejected secret",
            "zero-touch",
            "reconnect",
            "Hub connection",
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
            # Restart handling moved to the dedicated restart_worker (decoupled from the
            # scan cycle and from state["paused"]), so updates reliably reload the running
            # process even while busy or paused.
            sched = _schedule_check(cfg)
            if sched.get("is_work_hours") and cfg.get("SCHEDULER_WORK_POLL_INTERVAL"):
                interval = int(cfg.get("SCHEDULER_WORK_POLL_INTERVAL") or 600)
            else:
                interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 300))
        else:
            logger.debug("Poller worker is paused. Skipping scan cycle.")
            interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 300))
        time.sleep(interval)






def _model_fetch_reason(e):
    """Reduce a requests/HTTP exception to a short, UI-displayable reason."""
    # HTTPError from raise_for_status() carries the response object.
    resp = getattr(e, "response", None)
    if resp is not None and getattr(resp, "status_code", None):
        return f"HTTP {resp.status_code}"
    name = type(e).__name__
    s = str(e).lower()
    if "refused" in s:
        return "connection refused (wrong host/port, or server not running)"
    if "getaddrinfo" in s or "name or service not known" in s or "nodename nor servname" in s or "name resolution" in s:
        return "host not found (DNS/name resolution failed)"
    if "timed out" in s or "timeout" in name.lower():
        return "timed out"
    if "missing schema" in s or "missing schema" in name.lower():
        return "bad URL (missing http://)"
    return f"{name}: {str(e)[:100]}"


def _fetch_models_for_provider(provider, api_key, base_url):
    """Fetch available model names from a provider's API using live credentials.

    Returns ``{"models": [{"name": str, "details": str}], "error": str}``.
    ``error`` is empty on success; on failure it holds a short, UI-displayable
    reason (including the attempted URL) so the dropdown can show *why* no
    models came back instead of a silent empty list. Every failure is logged
    at WARNING with the attempted URL and exception type.
    """
    p = (provider or "openai").lower().strip()
    # claude_cli needs no API key — return the current Claude model roster.
    if p == "claude_cli":
        return {"models": [
            {"name": "claude-sonnet-4-6",         "details": "Claude Sonnet 4.6"},
            {"name": "claude-opus-4-8",            "details": "Claude Opus 4.8"},
            {"name": "claude-haiku-4-5-20251001",  "details": "Claude Haiku 4.5"},
        ], "error": ""}
    if _is_lmstudio(p):
        # LM Studio exposes /v1/models (OpenAI-compatible); no auth key needed.
        base = _normalize_lmstudio_url(base_url).rstrip("/")
        attempted = f"{base}/models"
        out = []
        error = ""
        try:
            resp = requests.get(attempted, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                out.append({"name": m.get("id", ""), "details": "LM Studio (local)"})
        except Exception as e:
            error = f"{attempted} — {_model_fetch_reason(e)}"
            logger.warning(f"LM Studio model fetch failed: {error} [{type(e).__name__}]")
        return {"models": out, "error": error}
    if not api_key:
        return {"models": [], "error": ""}
    models = []
    error = ""
    attempted = ""
    try:
        if p == "ollama":
            base = (base_url or "https://ollama.com").rstrip("/")
            headers = {}
            if api_key:
                clean = api_key.strip().replace("Bearer ", "").strip()
                headers["Authorization"] = f"Bearer {clean}"
            attempted = f"{base}/api/tags"
            resp = requests.get(attempted, headers=headers, timeout=10)
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
            attempted = f"{base}/models"
            resp = requests.get(attempted, headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                models.append({"name": m.get("id", ""), "details": m.get("display_name", "")})
        elif p == "google":
            base = (base_url or GOOGLE_BASE_URL).rstrip("/")
            attempted = f"{base}/v1beta/models"
            resp = requests.get(attempted, headers={"x-goog-api-key": api_key}, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                if "gemini" in name or "gemma" in name:
                    models.append({"name": name, "details": m.get("displayName", "")})
        elif p == "groq":
            base = (base_url or "https://api.groq.com/openai/v1").rstrip("/")
            headers = {"Authorization": f"Bearer {api_key}"}
            attempted = f"{base}/models"
            resp = requests.get(attempted, headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                models.append({"name": m.get("id", ""), "details": m.get("owned_by", "")})
        else:  # openai (and openai-compatible)
            base = (base_url or OPENAI_BASE_URL).rstrip("/")
            headers = {"Authorization": f"Bearer {api_key}"}
            attempted = f"{base}/models"
            resp = requests.get(attempted, headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                mid = m.get("id", "")
                if any(k in mid for k in ("gpt", "o1", "o3", "o4")):
                    models.append({"name": mid, "details": m.get("owned_by", "")})
    except Exception as e:
        error = f"{attempted} — {_model_fetch_reason(e)}" if attempted else _model_fetch_reason(e)
        logger.warning(f"Model fetch failed for provider {p!r}: {error} [{type(e).__name__}]")
    return {"models": models, "error": error}







def _diag_origin_head():
    """Best-effort origin HEAD from locally-cached remote refs (no network fetch)."""
    try:
        repo = git.Repo(os.getcwd())
        branch = "main"
        try:
            branch = repo.active_branch.tracking_branch().name.split("/")[-1]
        except Exception:
            pass
        try:
            return repo.commit(f"origin/{branch}").hexsha
        except Exception:
            try:
                return repo.remotes.origin.refs[f"{branch}"].commit.hexsha
            except Exception:
                return None
    except Exception:
        return None

































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
        # ---- Stage 1: detect / install Ollama + apply CPU tuning ----
        # Installed = the HTTP API answers OR the binary exists at a known path.
        # We do NOT rely on `which("ollama")` alone because the bugfixer service
        # runs under systemd with a minimal PATH that omits /usr/local/bin.
        # bugfixer runs as svc_bg (no root), so the privileged stages — install
        # ollama (curl|sh), ensure zstd, start the service, write the
        # /etc/systemd/system/ollama.service.d CPU-tuning override, daemon-reload
        # + restart — are delegated to /usr/local/bin/bugfixer-ollama-setup via
        # passwordless sudo (granted in /etc/sudoers.d/bugfixer). The helper is
        # idempotent (installs only if absent, starts only if down, applies the
        # override only if it changed), so calling it unconditionally also
        # covers the former Stage 5 tuning. It prints progress to stdout, which
        # we relay into the setup log. HTTP-API stages (pull/create model,
        # verify) stay here in svc_bg.
        _llm_setup_log("▶ Stage 1/7 — Prerequisites + installing/tuning Ollama (root helper)…")
        already_up = _ollama_reachable(base_url, timeout=5)
        bin_path = _ollama_bin_path()
        _llm_setup_log(f"  pre-check: ollama service {'up' if already_up else 'down'}, "
                       f"binary at {bin_path or 'unknown path'}")
        helper_cmd = ["sudo", "-n", "/usr/local/bin/bugfixer-ollama-setup", str(int(cores))]
        # The helper may restart ollama, so stream its progress synchronously
        # and surface failure before we depend on the API being up.
        proc = subprocess.run(helper_cmd, capture_output=True, text=True, timeout=900)
        for line in (proc.stdout or "").splitlines():
            _llm_setup_log(f"  [helper] {line}")
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
            raise RuntimeError(f"ollama-setup helper exited {proc.returncode}: {tail}")
        _llm_setup_log("✓ Ollama installed/tuned by helper")

        # ---- Stage 2: confirm the ollama service is reachable ----
        _llm_setup_log("▶ Stage 2/7 — Confirming the ollama service is reachable…")
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

        # ---- Stage 5: confirm the systemd override applied by the root helper ----
        # The root helper invoked in Stage 1 already writes the override, runs
        # daemon-reload, and restarts ollama. This stage is now a read-only
        # confirmation so the 7-stage narrative stays intact and the operator can
        # see the tuning landed. The privileged writes/restart are NOT repeated
        # here — that would require systemd access svc_bg no longer has.
        _llm_setup_log("▶ Stage 5/7 — Confirming systemd CPU tuning applied by helper…")
        wanted_line = f"OLLAMA_NUM_THREAD={int(cores)}"
        current = ""
        if os.path.exists(OLLAMA_OVERRIDE_PATH):
            try:
                with open(OLLAMA_OVERRIDE_PATH) as f:
                    current = f.read()
            except Exception:
                current = ""
        if wanted_line in current:
            _llm_setup_log(f"✓ systemd override already applied by helper ({OLLAMA_OVERRIDE_PATH})")
        else:
            # Don't raise — Stage 2 already confirmed ollama is reachable, so
            # a missing/stale override file is a degraded-tuning warning, not a
            # setup failure. The helper is the authority on the override now.
            _llm_setup_log(
                f"⚠ systemd override not found / missing {wanted_line} at {OLLAMA_OVERRIDE_PATH} "
                f"— tuning may not be applied (helper should have written it)"
            )
        _llm_setup_log("✓ systemd override confirmed (read-only)")

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

# Skip-list mirroring identify_files_to_fix (main.py) so list_repo_files hides
# the same noise the automated pipeline hides.










# --- Chat tool schemas (Ollama-compatible format; converted per-provider at call time) ---




















# --- Chat stream / proposal helpers (all lock-guarded) -----------------------






















# ==========================================================================
# Extracted modules. Imported here (after all shared state + core functions
# are defined) so each module's `from main import ...` resolves, and so the
# moved names are re-exported into main's namespace (preserving the public
# `from main import X` surface). Order matters: later modules depend on
# names re-exported by earlier ones.
# ==========================================================================
from github_ops import *  # noqa: E402,F401,F403
from log_scan import *  # noqa: E402,F401,F403
from chat import *  # noqa: E402,F401,F403
from fix_engine import *  # noqa: E402,F401,F403
from routes import *  # noqa: E402,F401,F403
app.include_router(router)

threading.Thread(target=connectivity_worker, daemon=True).start()
threading.Thread(target=heartbeat_worker, daemon=True).start()
threading.Thread(target=poller_worker, daemon=True).start()
threading.Thread(target=updater_worker, daemon=True).start()
threading.Thread(target=restart_worker, daemon=True).start()

# Start the Hub WebSocket agent (zero-touch onboarding → admin approval →
# signed requests for logs + update triggers). No-op if HUB_WS_URL is unset.
try:
    _start_hub_agent()
except Exception as _e:
    logger.warning(f"Hub agent startup failed (non-fatal): {_e}")

if __name__ == "__main__":
    import uvicorn
    if _tls_enabled():
        logger.info("Serving BugFixer UI on https://%s:%s (TLS)", SERVER_HOST, SERVER_PORT)
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT,
                    ssl_certfile=SSL_CERT, ssl_keyfile=SSL_KEY)
    else:
        logger.warning("No TLS cert at %s / %s — serving PLAIN HTTP on :%s "
                       "(re-run install.sh to generate a self-signed cert).",
                       SSL_CERT, SSL_KEY, SERVER_PORT)
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)