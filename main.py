import os
import sys

# ── bytecode self-heal ───────────────────────────────────────────────────────
# If the venv's site-packages is not writable by the user running us, CPython
# fails on the FIRST import that has no cached .pyc:
#   [Errno 13] Permission denied: .../site-packages/__pycache__/<mod>.pyc.<pid>
# The real cause is install-time: install.sh chowns the tree to the service user
# at step 2 but builds the venv as root at step 5, so site-packages is left
# root-owned. That has been fixed in install.sh -- but the self-update path only
# does a git pull and NEVER re-runs the installer, so an already-broken box would
# stay broken forever and keep reporting it as "Self-update check failed",
# a message that names neither permissions nor the venv.
#
# Repairing ownership needs root, which this process does not have. What it CAN
# do is stop needing the write: disabling bytecode caching removes the failure
# entirely. Costs a little import time and nothing else -- and only when the
# directory is genuinely unwritable, so a healthy install keeps its cache.
#
# Deliberately does NOT silence the underlying problem: it logs, once, with the
# exact command to fix it properly.
def _bf_bytecode_self_heal():
    try:
        import sysconfig
        target = sysconfig.get_paths().get("purelib")
        if not target or not os.path.isdir(target):
            return
        if os.access(target, os.W_OK):
            return          # healthy: keep bytecode caching
        sys.dont_write_bytecode = True
        owner = ""
        try:
            import pwd
            owner = pwd.getpwuid(os.stat(target).st_uid).pw_name
        except Exception:  # noqa: BLE001
            pass
        print(
            f"[ab] {target} is not writable by this user"
            + (f" (owned by {owner})" if owner else "")
            + " — disabling bytecode caching so imports do not fail. "
              f"Repair with: sudo chown -R $(id -un) {os.path.dirname(os.path.dirname(os.path.dirname(target)))}",
            file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — self-heal must never block startup
        pass


_bf_bytecode_self_heal()

import asyncio
import contextlib
import os, json, time, tempfile, threading, requests, logging, traceback, py_compile, random, re, uuid, collections
import sys
from urllib.parse import quote as _urlquote
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
# PYTHONPATH, else an inline equivalent (ab is self-contained and does
# NOT import lm/core, so the inline fallback is the normal path here). Either
# way LOG_LEVEL env is honored at boot and the standard format (with %(name)s)
# is applied so the AppBuilder logger name carries identity without a literal tag.
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
    path = os.getenv("LOG_FILE_PATH", "/var/log/ab.log")
    log_dir = os.path.dirname(path) or "."
    if not os.access(log_dir, os.W_OK):
        return os.path.join(os.getcwd(), "ab.log")
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
logger = logging.getLogger("AppBuilder")
logger.info(f"AppBuilder started. Logging level: {logging.getLevelName(_resolved_level)}. Logging to: {log_file}")

# main.py is launched directly (systemd `ExecStart=python3 main.py`), so it loads
# as the module __main__. The sibling modules below do `from main import ...`
# (logger, load_config, CONFIG_DIR, …). Without this alias Python RE-IMPORTS
# main.py a second time under the name `main`, and that copy races the circular
# `from config_store import *` on the next line — it sees a half-loaded
# config_store (load_config not yet defined) and dies with
# "ImportError: cannot import name 'load_config' from 'main'". Aliasing `main` to
# THIS already-running module makes every `from main import X` resolve to it, so
# there is only ever one main module. No-op when imported normally (name==main).
import sys as _sys
_sys.modules.setdefault("main", _sys.modules[__name__])

# Persistent config paths, chat-config defaults, and the config / processed /
# update-state / version / startup-stamp helpers live in config_store.py.
# Imported right after `logger` (which config_store depends on) so main's own
# module-level code and the sibling modules keep resolving `from main import
# load_config / save_processed / CONFIG_DIR` etc. unchanged.
from config_store import *  # noqa: E402,F401,F403

# ============================================================================
# Multi-provider LLM routing + circuit breakers live in llm_client.py.
# Imported here (after config_store) and re-exported so main's own module-level
# code and the sibling modules keep resolving `from main import call_llm /
# _get_provider_config / validate_llm_config_on_startup / _llm_cb_snapshot` etc.
# ============================================================================
from llm_client import *  # noqa: E402,F401,F403

load_dotenv(ENV_FILE)
app = FastAPI()

# ── Web-server bind (unified-443: HTTPS on :443 by default) ──────────────────
# Overridable via env (set in the systemd unit or for local dev). install.sh
# generates a self-signed cert at SSL_CERT/SSL_KEY; when both exist the server
# serves HTTPS, otherwise it falls back to plain HTTP on the SAME port so the
# UI still comes up. The internal restart/health check and watchdog.py derive
# their probe URL from the same settings so they never drift from the bind.
SERVER_HOST = os.environ.get("AB_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("AB_PORT", "443") or "443")
SSL_CERT = (os.environ.get("AB_SSL_CERT", "/etc/ab/cert.pem") or "").strip()
SSL_KEY  = (os.environ.get("AB_SSL_KEY", "/etc/ab/key.pem") or "").strip()

def _tls_enabled() -> bool:
    return bool(SSL_CERT and SSL_KEY and os.path.exists(SSL_CERT) and os.path.exists(SSL_KEY))


def _sync_webui_cert_from_mtls():
    """One LE cert for BOTH roles: use the deployed mTLS/LE cert as the WebUI server
    cert too. If the mTLS client cert (hub-client-cert.pem) is CA-issued and the
    WebUI cert (cert.pem) is still the install-time SELF-SIGNED one, copy the LE
    cert+key into the WebUI paths so uvicorn serves the trusted cert (browsers +
    GitHub webhooks). INSTALL_CERT writes both going forward; this heals a
    deployment whose WebUI cert was left self-signed by an older handler. Runs at
    startup, before uvicorn binds. Never clobbers an already-CA-issued WebUI cert."""
    client_cert = os.environ.get("AB_HUB_CLIENT_CERT", "/etc/ab/hub-client-cert.pem")
    client_key = os.environ.get("AB_HUB_CLIENT_KEY", "/etc/ab/hub-client-key.pem")
    if not (SSL_CERT and SSL_KEY and os.path.exists(client_cert) and os.path.exists(client_key)):
        return

    def _self_signed(path):
        try:
            from cryptography import x509
            with open(path, "rb") as f:
                c = x509.load_pem_x509_certificate(f.read())
            return c.subject == c.issuer
        except Exception:  # noqa: BLE001
            return None

    if _self_signed(client_cert) is not False:
        return  # mTLS cert isn't a CA-issued cert — nothing to promote
    # The hub mTLS client cert is now ALWAYS a Hub-Local-CA clientAuth cert
    # (issuer e.g. "LM Hub mTLS Client CA"), delivered by SPOKE_SET_MTLS_CLIENT_CERT
    # — NOT a publicly-trusted WebUI cert. Never promote it onto the WebUI (browsers
    # / GitHub webhooks wouldn't trust the private CA). The WebUI cert comes straight
    # from INSTALL_CERT (LE) instead.
    def _issued_by_hub_mtls_ca(path):
        try:
            from cryptography import x509
            with open(path, "rb") as f:
                c = x509.load_pem_x509_certificate(f.read())
            return "mtls" in c.issuer.rfc4514_string().lower()
        except Exception:  # noqa: BLE001
            return False
    if _issued_by_hub_mtls_ca(client_cert):
        return
    web_self_signed = _self_signed(SSL_CERT) if os.path.exists(SSL_CERT) else True
    if web_self_signed is False:
        return  # WebUI already has a CA-issued cert — don't clobber it
    try:
        import shutil
        shutil.copyfile(client_cert, SSL_CERT)
        shutil.copyfile(client_key, SSL_KEY)
        try:
            os.chmod(SSL_KEY, 0o600)
        except OSError:
            pass
        logger.info("WebUI cert synced from the mTLS/LE cert (%s -> %s) — serving "
                    "the trusted cert instead of the self-signed one", client_cert, SSL_CERT)
    except Exception as e:  # noqa: BLE001
        logger.warning("WebUI cert sync from mTLS cert failed: %s", e)

def _local_health_url() -> str:
    scheme = "https" if _tls_enabled() else "http"
    return f"{scheme}://127.0.0.1:{SERVER_PORT}/api/health"

template_path = os.path.join(os.getcwd(), "templates")
templates = Jinja2Templates(directory=template_path)


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




# ── Authentication ────────────────────────────────────────────────────────────
# Everyone who can log in is an admin; the point is that an unauthenticated
# browser on the LAN must not be able to drive a service that holds a GitHub
# token, pushes commits, and can restart itself.
import auth as _auth  # noqa: E402 — after logger/app so its logging is configured

#: Paths reachable WITHOUT a session.
#:  /api/health is load-bearing: ab-watchdog polls it at 127.0.0.1 to verify
#:  a restart succeeded, and restart_worker does the same. Gate it and every
#:  restart looks like a failed start, which triggers the watchdog's ROLLBACK.
_AUTH_EXEMPT_EXACT = {"/api/health", "/login", "/logout", "/setup-admin",
                      "/auth/oidc/enabled", "/auth/oidc/login", "/auth/oidc/callback",
                      "/favicon.ico", "/apple-touch-icon.png",
                      "/apple-touch-icon-precomposed.png"}
_AUTH_EXEMPT_PREFIX = ("/static/", "/assets/", "/v1/")


def _auth_exempt(path: str) -> bool:
    return path in _AUTH_EXEMPT_EXACT or path.startswith(_AUTH_EXEMPT_PREFIX)


def _wants_json(request: Request) -> bool:
    """API callers get 401 JSON; browsers get a redirect to the login page."""
    if request.url.path.startswith("/api/"):
        return True
    return "application/json" in (request.headers.get("accept") or "")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if _auth_exempt(path):
        return await call_next(request)
    # First run: no accounts yet — funnel everything to one-shot setup so the
    # service is never left silently open while an operator finds the page.
    if not _auth.any_users():
        if _wants_json(request):
            return JSONResponse(status_code=401,
                                content={"error": "setup_required",
                                         "message": "No accounts exist. Open /setup-admin."})
        return RedirectResponse("/setup-admin", status_code=303)
    user = _auth.verify_session(request.cookies.get(_auth.SESSION_COOKIE) or "")
    if not user:
        if _wants_json(request):
            return JSONResponse(status_code=401,
                                content={"error": "unauthenticated", "message": "Log in first."})
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login?next={_urlquote(nxt, safe='')}", status_code=303)
    request.state.user = user
    return await call_next(request)


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
            content={"message": "Internal Server Error. Check ab.log for details.", "error": str(e)}
        )

# Shared mutable application state + task-state locks + update_task_state live in
# app_state.py. Imported here (after config_store + llm_client names are already
# re-exported into main's namespace, which app_state builds `state` from) and
# re-exported so the `from main import state / update_task_state / _chat_lock`
# surface used by routes.py and the sibling modules resolves unchanged.
from app_state import *  # noqa: E402,F401,F403

# Stamp which commit this process booted on, before any worker starts, so the watchdog
# can detect stale-running code and the Diagnostics panel can show running vs disk versions.
write_startup_stamp()

try:
    import llm_migrate
    _cfg, _migrated = llm_migrate.migrate(load_config())
    if _migrated:
        save_config(_cfg)
        logger.info("LLM config migrated to schema v2 and persisted.")
except Exception as me:
    logger.warning(f"LLM config migration skipped (non-fatal): {me}")

try:
    validate_llm_config_on_startup()
except Exception as ve:
    logger.warning(f"Startup LLM validation failed (non-fatal): {ve}")



# --- Chat system-prompt context index (cached, TTL-bounded) -----------------
# A compact markdown snapshot of AppBuilder's repos, their open monitored-label
# issues, processed-issue status totals, and recent Hub error count. Prepended
# to the chat system prompt every turn so the assistant has the lay of the land
# without a tool round-trip. Tool calls drill deeper on demand.








# Default colors for labels ab creates when missing. PyGithub's
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

































# =============================================================================
# Chat agent: tool schemas, executors, secret sanitizer, and stream helpers.
#
# The chat agent gives the assistant awareness of AppBuilder's repos, GitHub
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
# workers.py first: the sibling modules below import worker names at import time
# (resolve_self_diagnosis_repo / _get_hub_agent_client / _trigger_spoke_updates /
# _wait_for_spokes_online / _is_triage_only). workers in turn calls back into
# those siblings lazily (main.<fn>) at run time, so the cycle is broken.
from workers import *  # noqa: E402,F401,F403
from ollama_setup import *  # noqa: E402,F401,F403
from github_ops import *  # noqa: E402,F401,F403
from log_scan import *  # noqa: E402,F401,F403
from chat import *  # noqa: E402,F401,F403
from fix_engine import *  # noqa: E402,F401,F403
from routes import *  # noqa: E402,F401,F403
app.include_router(router)
# Anthropic-compatible LLM-router proxy (/v1/*). Does its own api-key auth
# (see llm_proxy), so it's exempt from the WebUI session middleware above.
from llm_proxy import router as _llm_proxy_router  # noqa: E402
app.include_router(_llm_proxy_router)

# ── Single-instance guard for the scan workers ────────────────────────────────
# These threads start at IMPORT time, before uvicorn binds the port. So a second
# ab process that loses the race for :443 has already started all seven
# workers — every scan, LLM call and GitHub request runs twice while only one web
# server exists. Observed live: two PIDs 3s apart, duplicate SelfScan/Discovery/
# RepoScan cycles, doubled API and LLM load, and a max_concurrent=1 limiter that
# bounds each process separately and so bounds nothing.
#
# An flock is deliberately cause-agnostic: whatever spawns the extra process (a
# racing restart, a manual launch, a supervisor overlap), only the lock holder
# scans. The fd is kept open for the process lifetime — the kernel releases it on
# exit, so a crashed holder frees the lock with no stale-PID cleanup.
#
# The loser is NOT killed: it still serves whatever it can and, more importantly,
# exiting here would fight systemd's Restart=always and produce a restart loop.
# It just stays silent instead of doubling the work.
_WORKER_LOCK_PATH = os.path.join(CONFIG_DIR, "ab.workers.lock")
_worker_lock_fh = None   # module-global: holds the flock for the process lifetime


def _acquire_worker_singleton():
    """True if this process may run the scan workers (i.e. it won the flock)."""
    global _worker_lock_fh
    try:
        import fcntl
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fh = open(_WORKER_LOCK_PATH, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            try:
                fh.seek(0)
                holder = (fh.read() or "").strip() or "unknown"
            except Exception:  # noqa: BLE001
                holder = "unknown"
            fh.close()
            logger.error(
                "Scan workers NOT started: another ab process (pid %s) already holds "
                "%s. Running two sets would double every scan, LLM call and GitHub request. "
                "This process will serve requests only.", holder, _WORKER_LOCK_PATH)
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        _worker_lock_fh = fh          # keep the fd open or the lock is released
        return True
    except Exception as e:  # noqa: BLE001
        # Fail OPEN: if locking itself is unavailable, preserve the previous
        # behaviour rather than silently running with no workers at all.
        logger.warning("Worker single-instance lock unavailable (%s) — starting workers "
                       "without it.", e)
        return True


if _acquire_worker_singleton():
    threading.Thread(target=connectivity_worker, daemon=True).start()
    threading.Thread(target=heartbeat_worker, daemon=True).start()
    threading.Thread(target=poller_worker, daemon=True).start()
    threading.Thread(target=updater_worker, daemon=True).start()
    threading.Thread(target=restart_worker, daemon=True).start()
    threading.Thread(target=log_health_worker, daemon=True).start()
    threading.Thread(target=model_preload_worker, daemon=True).start()
    # Batch worker (async cloud batch processing). Gated by batch_enabled (default
    # off). Defensive import/start so a batch issue can never crash AppBuilder startup.
    try:
        from batch import batch_worker as _batch_worker
        threading.Thread(target=_batch_worker, daemon=True).start()
    except Exception as _be:  # noqa: BLE001
        logger.warning(f"batch worker not started: {_be}")

# Start the Hub WebSocket agent (zero-touch onboarding → admin approval →
# signed requests for logs + update triggers). No-op if HUB_WS_URL is unset.
try:
    _start_hub_agent()
except Exception as _e:
    logger.warning(f"Hub agent startup failed (non-fatal): {_e}")

if __name__ == "__main__":
    import uvicorn
    # Promote the deployed LE/mTLS cert to the WebUI cert if the WebUI is still
    # self-signed (before uvicorn reads SSL_CERT/SSL_KEY).
    try:
        _sync_webui_cert_from_mtls()
    except Exception as _e:  # noqa: BLE001
        logger.warning("WebUI cert sync skipped: %s", _e)
    if _tls_enabled():
        logger.info("Serving AppBuilder UI on https://%s:%s (TLS)", SERVER_HOST, SERVER_PORT)
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT,
                    ssl_certfile=SSL_CERT, ssl_keyfile=SSL_KEY)
    else:
        logger.warning("No TLS cert at %s / %s — serving PLAIN HTTP on :%s "
                       "(re-run install.sh to generate a self-signed cert).",
                       SSL_CERT, SSL_KEY, SERVER_PORT)
        uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)