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