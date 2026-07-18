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
# workers.py first: the sibling modules below import worker names at import time
# (resolve_self_diagnosis_repo / _get_hub_agent_client / _trigger_spoke_updates /
# _wait_for_spokes_online / _is_triage_only). workers in turn calls back into
# those siblings lazily (main.<fn>) at run time, so the cycle is broken.
from workers import *  # noqa: E402,F401,F403
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