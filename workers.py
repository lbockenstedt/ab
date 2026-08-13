"""Background workers + scan/update orchestration for BugFixer.

Extracted verbatim from main.py: the connectivity / heartbeat / updater / restart
/ poller worker loops, run_scan_cycle + scan_repo_issues + scan_self_logs, the
Hub-agent WebSocket lifecycle, check_for_updates, resolve_self_diagnosis_repo,
the self-scan offset store, model fetch, and the git-diag helpers. Pure move, no
behavior change.

Import ordering: main re-exports these via ``from workers import *`` placed
BEFORE github_ops/log_scan/chat/fix_engine, because those sibling modules import
worker names at import time (`from main import resolve_self_diagnosis_repo /
_get_hub_agent_client / _trigger_spoke_updates / _wait_for_spokes_online /
_is_triage_only`). Conversely, a handful of functions here call back into those
siblings at RUN time (run_scan_cycle -> scan_hub_logs, scan_repo_issues ->
process_single_issue, scan_self_logs -> create_automated_issue, etc.). Since
those siblings are imported after workers, workers references them lazily as
``main.<fn>`` (resolved at call time). ``state`` is imported directly because
app_state is re-exported into main before workers.
"""
import git
import json
import os
import py_compile
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from github import Github, GithubException

import main  # lazy access to sibling functions imported after workers (main.scan_hub_logs, ...)
from main import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_BASE_URL,
    GOOGLE_BASE_URL,
    OPENAI_BASE_URL,
    OPENROUTER_BASE_URL,
    CONFIG_DIR,
    SELF_SCAN_OFFSET_FILE,
    _any_provider_available,
    _get_provider_config,
    _is_lmstudio,
    _is_ollama,
    _llm_cb_snapshot,
    _ollama_base_url,
    _local_health_url,
    _normalize_lmstudio_url,
    _provider_configured,
    _provider_credit_cb_snapshot,
    _set_update_cooldown,
    get_log_path,
    get_version,
    load_config,
    load_processed,
    load_update_state,
    logger,
    save_config,
    save_update_state,
    state,
    update_task_state,
)

def _ollama_model_present(resp, model):
    """True if `model` appears in an Ollama /api/tags response.

    Ollama's /api/tags answers 200 whenever the server is up, regardless of which
    models are pulled — but /api/chat 404s ("model not found") if the requested
    model is missing. So a plain server ping falsely reports the provider healthy.
    This narrows the check to the configured model. Lenient (True) when the body
    can't be parsed, so a transient/odd response never false-flags the provider."""
    want = (model or "").strip()
    if not want:
        return True
    try:
        names = [(m.get("name") or "") for m in (resp.json().get("models") or [])]
    except Exception:
        return True  # unparseable — don't mark offline on a parse hiccup
    base_want = want.split(":")[0]
    for nm in names:
        if nm == want or nm == f"{want}:latest":
            return True
        # Tolerate the implicit :latest tag: a bare "llama3" matches pulled "llama3:latest".
        if ":" not in want and nm.split(":")[0] == base_want:
            return True
    return False


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
    if _is_ollama(p):
        # Ollama needs no API key (self-hosted; Ollama Cloud sends a Bearer key) —
        # confirm the server is reachable (/api/tags) AND that the configured model
        # is actually pulled. /api/tags is 200 whenever the server is up, but
        # /api/chat 404s if the model is missing, so a bare server ping would
        # falsely show the provider green while every real call fails.
        if not model:
            return False
        try:
            base = _ollama_base_url(p, base_url).rstrip("/")
            headers = {}
            if api_key:
                clean = api_key.strip().replace("Bearer ", "").strip()
                headers["Authorization"] = f"Bearer {clean}"
            resp = requests.get(f"{base}/api/tags", headers=headers, timeout=10)
            if resp.status_code == 401:
                return False
            if resp.status_code >= 300:
                return False
            if not _ollama_model_present(resp, model):
                logger.warning(
                    f"Provider {n} (ollama) server is up but model '{model}' is not pulled on "
                    f"{base} — pull it (Settings → Local LLM Setup, or `ollama pull {model}`)."
                )
                return False
            return True
        except Exception:
            return False
    if p.startswith("copilot"):
        # Copilot: the stored api_key is a GitHub OAuth token; it must be exchanged
        # for a short-lived Copilot API token, and the models call needs editor headers.
        if not api_key:
            return False
        try:
            from llm_client import _copilot_api_token, _copilot_headers, COPILOT_API_BASE
            tok = _copilot_api_token(api_key)
            resp = requests.get(f"{COPILOT_API_BASE}/models", headers=_copilot_headers(tok), timeout=15)
            if resp.status_code == 401:
                logger.warning(f"Provider {n} (copilot) connectivity check: 401 — token exchange rejected (re-authorize).")
                return False
            return resp.status_code < 300
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Provider {n} (copilot) connectivity check error: {e}")
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
        elif p == "openrouter":
            base = (base_url or OPENROUTER_BASE_URL).rstrip("/")
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

    Returns a human-readable status string so a caller wiring this to a UI button
    (the "Hub Update" action) can surface the outcome. Post-fix callers may ignore
    the return value.
    """
    client = _get_hub_agent_client()
    if not client:
        logger.debug("_trigger_spoke_updates: Hub agent not configured/approved, skipping")
        _set_update_cooldown(config)
        return "Hub agent not connected — update not triggered"
    result = client.request_sync("TRIGGER_ALL_UPDATES", {}, timeout=60)
    if not isinstance(result, dict):
        logger.warning("Hub agent not approved/connected — skipping update trigger")
        _set_update_cooldown(config)
        return "Hub agent not approved/connected — update not triggered"
    # The hub returns {hub, spokes, agents} on success but an ERROR ENVELOPE
    # ({"status":"error","message":...}) when it rejects the request — most
    # commonly the H1 authz denial (the BugFixer client cert must be pinned via
    # the LE module AND presented over mTLS). That envelope is also a dict, so a
    # bare isinstance check would report a hollow "hub= | spokes= | agents="
    # SUCCESS for a request the hub never honored. Require all three result keys.
    if not all(k in result for k in ("hub", "spokes", "agents")):
        msg = result.get("message") or result.get("status") or "hub rejected the update request"
        logger.warning(f"Hub update NOT triggered: {msg}")
        # Do NOT start the restart cooldown — nothing is restarting, and
        # suppressing issue filing would hide real errors.
        return f"Hub update NOT triggered: {msg}"
    hub = result.get("hub") or {}
    spokes = result.get("spokes") or {}
    agents = result.get("agents") or {}
    summary = (
        f"Hub update triggered: hub={_upd_summary(hub)} | "
        f"spokes={_upd_summary(spokes)} | agents={_upd_summary(agents)}"
    )
    logger.info(summary)
    # Suppress issue filing while services are restarting.
    _set_update_cooldown(config)
    return summary


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
    # Record when the agent (re)became approved so the heartbeat triage warm-up
    # gate (log_scan.scan_heartbeats) can avoid filing a false flood of
    # "missing heartbeat" issues while the Hub/telemetry pipeline is still
    # coming up after a reinstall. Reset on every (re)approval.
    if status == "approved":
        state["hub_agent_approved_at"] = state["hub_agent_last_seen"]


def _hub_agent_on_connection(connected):
    """Callback: the LIVE hub socket came up (True) or dropped (False). Distinct
    from approval status — lets the header dot show green only while actually
    connected, so a flapping agent no longer reads as steady-green."""
    state["hub_agent_connected"] = bool(connected)
    if not connected:
        state["hub_agent_last_disconnect"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
            on_connection=_hub_agent_on_connection,
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

        # Advance to the fetched remote head. /opt/bugfixer is a DEPLOYMENT MIRROR,
        # not a dev tree — a plain `git pull` aborts with "local changes would be
        # overwritten" the moment any tracked file is dirtied at runtime, wedging
        # self-update forever. Hard-reset to the fetched head instead (robust to a
        # dirty worktree), logging what gets discarded so the dirtying cause stays
        # visible. Fall back to pull only if we couldn't read the remote head.
        if remote_head:
            try:
                if self_repo.is_dirty(untracked_files=False):
                    dirty = self_repo.git.status("--porcelain")
                    logger.warning("Self-update: worktree has local changes; hard-resetting to "
                                   "origin/%s (deployment mirror). Discarding:\n%s", tracked, dirty)
                self_repo.git.reset("--hard", remote_head)
            except Exception as re:
                logger.warning(f"Self-update: hard-reset to {remote_head[:7]} failed ({re}); falling back to pull.")
                self_repo.remotes.origin.pull()
        else:
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

RESTART_REQUEST_FILE = os.path.join(CONFIG_DIR, "restart_request")


def _watchdog_active():
    """True if bugfixer-watchdog.service is active (so a delegated restart request
    will actually be consumed). None/False on error → caller falls back to a
    hard exit. Mirrors ollama_setup._watchdog_active."""
    import subprocess as _sp
    try:
        r = _sp.run(["systemctl", "is-active", "--quiet", "bugfixer-watchdog"], timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return None


def _request_watchdog_restart():
    """Ask bugfixer-watchdog (the unrestricted privileged arm) to restart us, by
    writing a request file it consumes. Returns True if the request was queued to
    an ACTIVE watchdog; False otherwise (caller hard-exits instead). Mirrors the
    ollama-setup delegation — bugfixer.service is cap-locked and can't restart
    itself, but the watchdog can."""
    if _watchdog_active() is False:
        return False
    try:
        with open(RESTART_REQUEST_FILE, "w") as f:
            json.dump({"requested_at": time.time()}, f)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("restart: could not write watchdog restart request (%s)", e)
        return False


def _spawn_restart():
    """Restart bugfixer, robust to the cap-locked service.

    As root (dev/standalone): a direct detached `systemctl restart bugfixer`.

    As the svc_bg service user, bugfixer.service is locked to
    CAP_NET_BIND_SERVICE, so sudo can't setgid(0) ("unable to change to root gid:
    Operation not permitted") and the old `sudo -n /usr/local/bin/bugfixer-self-restart`
    path fails every time (noisy ERROR + only recovered via a hard exit). Instead we
    DELEGATE to bugfixer-watchdog — the unrestricted privileged arm that already runs
    ollama-setup on our behalf — via a request file it consumes to run the restart.
    If the watchdog isn't active to pick it up, hard-exit so systemd Restart=always
    revives us regardless (the manual Restart button can never silently no-op)."""
    import subprocess as _sp
    if os.geteuid() == 0:
        try:
            _sp.Popen(["systemctl", "restart", "bugfixer"], start_new_session=True,
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, close_fds=True)
        except Exception as e:  # noqa: BLE001
            logger.error("restart: root systemctl restart failed (%s) — hard-exiting", e)
            _exit_for_systemd_restart()
        return
    # Non-root: delegate to the watchdog, else hard-exit.
    if _request_watchdog_restart():
        logger.info("restart: delegated to bugfixer-watchdog (privileged arm); "
                    "it will `systemctl restart bugfixer` shortly.")
        return
    logger.warning("restart: bugfixer-watchdog not active — hard-exiting so systemd "
                   "Restart=always revives us.")
    _exit_for_systemd_restart()


def _exit_for_systemd_restart():
    """Last-resort restart: flush logs, then hard-exit. bugfixer.service is
    Restart=always / RestartSec=10, so systemd revives the process — no sudo, no
    cgroup race. Used when the detached restart path fails so the manual Restart
    button (and cert-install restarts) can never silently no-op."""
    try:
        import logging as _l
        for _h in _l.getLogger().handlers:
            try:
                _h.flush()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)

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

    monitored_repos = main.get_monitored_repos(config)
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

                to_fix = []           # log-detected / generic automated-fix issues
                critical_to_fix = []  # SCHEDULER_CRITICAL_LABEL — always run, first, uncapped
                bug_to_fix = []       # bug-label (human-filed Bug) — run before log-detected
                                      # and bypass the scheduler window (processed now, not at
                                      # the next allowed slot). Subject to the per-cycle cap,
                                      # but takes cap slots ahead of log-detected.
                bug_label = (config.get("SCHEDULER_BUG_LABEL") or "bug").strip()
                for issue in issues:
                    try:
                        if issue.state != 'open' or issue.pull_request:
                            continue

                        # Skip issues carrying the 'bugfixer-dismissed' label — they were
                        # intentionally marked as not real. Remove the label to resume processing.
                        if any(lbl.name == "bugfixer-dismissed" for lbl in issue.labels):
                            logger.debug(f"Skipping {repo_name}#{issue.number} — 'bugfixer-dismissed' label present.")
                            continue

                        # Skip LM-filed feature requests — feature_drive.py owns these via its
                        # own single-label query (see that module's docstring for why it can't
                        # just be added to monitored_labels). Both pipelines key `processed` by
                        # the same "repo:number" id, so without this guard a feature request
                        # that also happens to carry a monitored label (e.g. an operator using
                        # monitored_labels=["ANY"]) could be picked up by BOTH triage loops.
                        if "<!-- report-type: feature -->" in (issue.body or ""):
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

                        # Case-INSENSITIVE label match: BugFixer files with "Bug",
                        # but GitHub applies the repo's EXISTING lowercase "bug"
                        # label, so an exact match misses it and a real LM bug report
                        # drops into the log-detected tier — which fix_logdetected_
                        # enabled can gate OFF. Match lower-cased so LM bug reports
                        # always land in bug_to_fix (bypass scheduler + that gate).
                        issue_label_names = {lbl.name for lbl in issue.labels}
                        _labels_lc = {n.lower() for n in issue_label_names}
                        if critical_label and critical_label.lower() in _labels_lc:
                            critical_to_fix.append((repo_name, issue.number))
                        elif bug_label and bug_label.lower() in _labels_lc:
                            bug_to_fix.append((repo_name, issue.number))
                        elif config.get("fix_logdetected_enabled", False):
                            # Log-detected / automated-fix issues are auto-FIXED only
                            # when enabled (Settings, default OFF) — so the fixer
                            # stops churning on log-scraped issues. Critical + Bug
                            # (incl. LM bug reports) always fix.
                            to_fix.append((repo_name, issue.number))
                    except Exception as e:
                        logger.exception(f"Failed to triage issue {issue_id}: {e}")

                # Normal (log-detected) issues respect the scheduler; critical AND
                # bug-label issues always run — bugs bypass the window so a human-filed
                # bug is processed immediately, not at the next allowed slot.
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
                if bug_to_fix and not sched["allowed"]:
                    logger.info(
                        f"Bug-label override: processing {len(bug_to_fix)} bug(s) in {repo_name} "
                        f"regardless of schedule (bugs bypass the scheduler)."
                    )
                all_to_fix = critical_to_fix + bug_to_fix + to_fix

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
                        # Bugs take the per-cycle cap ahead of log-detected (bugs first when
                        # capacity is limited). Critical is outside the cap entirely.
                        capped_bugs = bug_to_fix[:max_per_cycle]
                        remaining = max(0, max_per_cycle - len(capped_bugs))
                        capped_normal = to_fix[:remaining]
                        deferred = (len(bug_to_fix) - len(capped_bugs)) + (len(to_fix) - len(capped_normal))
                        if deferred > 0:
                            logger.info(
                                f"Found {len(all_to_fix)} issues in {repo_name} — processing "
                                f"{len(critical_to_fix)} critical + {len(capped_bugs)} bug + "
                                f"{len(capped_normal)} normal this cycle, {deferred} deferred."
                            )
                        else:
                            logger.info(f"Found {len(all_to_fix)} issues to process in {repo_name} (max workers={max_workers}).")
                        batch = critical_to_fix + capped_bugs + capped_normal
                        with ThreadPoolExecutor(max_workers=max_workers) as executor:
                            futures = [executor.submit(main.process_single_issue, r, n) for r, n in batch]
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

    return main.clean_repo_name(self_repo_name)

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

        # Persist the END OF WHAT WE ACTUALLY READ, not a fresh getsize(). If we
        # saved getsize() here, any lines appended between the read() above and
        # the getsize() call would be skipped forever (the next scan starts past
        # them). start_offset + bytes-read is exactly the position we consumed to,
        # so nothing written after our read is lost. Saved immediately after
        # reading so a crash or filing failure never re-reads the same lines.
        next_offset = start_offset + len(new_text.encode("utf-8", "replace"))
        save_self_scan_offset(next_offset, current_inode)

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
            # Match BugFixer's actual log format ("<ts> - BugFixer - ERROR - ...")
            # AND the legacy "[ERROR]" bracket form. The bracket-only check missed
            # every real error (the format uses " - ERROR - "), so BugFixer never
            # surfaced its own errors as bugs — including 500s logged as
            # "UNCAUGHT EXCEPTION". Now it does (→ self-diagnosis repo).
            if (" - ERROR - " in line or " - CRITICAL - " in line
                    or "[ERROR]" in line or "[CRITICAL]" in line):
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
        scrubbed_self_logs = main.filter_error_logs(formatted_logs)
        logger.info(
            f"Self logs scrubbed: {len(formatted_logs)} -> {len(scrubbed_self_logs)} "
            f"unique error entries for LLM analysis."
        )
        actionable_errors = main.analyze_logs_for_errors(scrubbed_self_logs)
        if not actionable_errors:
            update_task_state(task_id="SelfScan", action="end")
            return

        monitored_repos = main.get_monitored_repos(config)
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
                main.create_automated_issue(gh_current, monitored_repos, repo_obj, error)
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

        monitored_repos = main.get_monitored_repos(config)
        if not monitored_repos:
            logger.warning("No monitored repositories configured. Skipping scan.")

        update_task_state(task_id="Discovery", task_name="Discovering Labels", action="start")
        state["available_labels"] = main.discover_labels(gh_current, monitored_repos)
        logger.info(f"Discovered {len(state['available_labels'])} unique labels across monitored repos.")
        update_task_state(task_id="Discovery", action="end")

        main.verify_production_fixes(gh_current, processed)

        # Self-log scan is ON by default (self-diagnosis: scan BugFixer's own
        # logs for errors + file them in self_diagnosis_repo). The Settings
        # "Self-monitor BugFixer logs" toggle turns it OFF so BugFixer stops
        # monitoring/filing its own logs. Default True preserves existing
        # behavior for installs that never saved the key.
        if config.get("self_log_scan_enabled", True):
            scan_self_logs(gh_current, config)

        # Load project skills ("agents") from the LM repo (.claude/skills) so fixes
        # follow their recipes + boundaries. Best-effort, cached with a TTL.
        try:
            from skills_loader import load_skills
            state["skills"] = sorted(load_skills(Github(token), config).keys())
        except Exception as _se:  # noqa: BLE001
            logger.debug(f"skills load skipped: {_se}")

        state["status"] = "Scanning"
        from pr_review import scan_open_prs  # PR pre-review — gated by pr_review_enabled (default off)
        from feature_drive import scan_feature_requests  # feature auto-drive — gated by feature_drive_enabled (default off)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(main.scan_hub_logs, Github(token), config),
                executor.submit(scan_repo_issues, Github(token), config, processed),
                executor.submit(scan_open_prs, Github(token), config),
                executor.submit(scan_feature_requests, Github(token), config),
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

# Captured at import (≈ process boot / post-restart). Used for the startup grace so
# BugFixer doesn't hammer a not-yet-ready ollama (404 /api/chat) right after a reboot.
_BOOT_TIME = time.time()


def _startup_grace_remaining():
    """Seconds left in the post-boot grace window (0 once elapsed / disabled). During
    it, LLM-dependent work is deferred so ollama + services can finish coming up.
    Configurable via startup_grace_seconds (default 300); 0 disables."""
    try:
        grace = int(load_config().get("startup_grace_seconds", 300))
    except (TypeError, ValueError):
        grace = 300
    if grace <= 0:
        return 0
    remaining = grace - (time.time() - _BOOT_TIME)
    return int(remaining) if remaining > 0 else 0


def _ollama_answers(base, timeout=4):
    """True if the ollama server responds on /api/tags right now."""
    try:
        return requests.get(f"{base}/api/tags", timeout=timeout).status_code < 300
    except Exception:  # noqa: BLE001 — not up yet
        return False


def preload_ollama_models(config):
    """Load each local ensemble model into ollama memory (with keep_alive) so the first
    fix doesn't pay the cold-load cost and mid-ensemble switches don't reload from disk.
    Loads the SAME set the ensemble uses (P1 local, min-size filtered) at the configured
    num_ctx. Best-effort. Needs OLLAMA_MAX_LOADED_MODELS>=N on the ollama server for all
    N to stay resident at once."""
    from llm_client import _get_provider_config, _is_ollama
    from ollama_setup import _ollama_models_detailed  # lives in ollama_setup, not llm_client
    import model_registry
    provider, _k, _m, url = _get_provider_config(1, config)
    if not _is_ollama(provider):
        return
    base = _ollama_base_url(provider, url).rstrip("/")
    names = model_registry.local_models_for_preload(_ollama_models_detailed(base), config)
    if not names:
        return
    try:
        num_ctx = int(config.get("ollama_num_ctx", 32768) or 32768)
    except (TypeError, ValueError):
        num_ctx = 32768
    _ka = str(config.get("ollama_keep_alive", "-1") or "-1").strip()
    ka_val = int(_ka) if _ka.lstrip("-").isdigit() else _ka
    # Load timeout, separate from LLM_TIMEOUT (which bounds INFERENCE). A cold load
    # is disk -> RAM plus a num_ctx-sized KV allocation, and on a CPU-only box a
    # 14B at 32k context legitimately exceeds the old hardcoded 900s — so the
    # preload "failed" on a model that would have finished, then every later call
    # paid the cold-load cost anyway. Configurable via ollama_preload_timeout_s.
    try:
        load_timeout = max(60, int(config.get("ollama_preload_timeout_s", 3600) or 3600))
    except (TypeError, ValueError):
        load_timeout = 3600
    logger.info(f"Preloading {len(names)} ensemble model(s) into ollama memory "
                f"(load timeout {load_timeout}s each): {names}")
    for name in names:
        _t0 = time.time()
        try:
            logger.info(f"  preloading {name}…")
            requests.post(f"{base}/api/generate",
                          json={"model": name, "keep_alive": ka_val, "options": {"num_ctx": num_ctx}},
                          timeout=load_timeout)
            logger.info(f"  ✓ {name} resident ({time.time() - _t0:.0f}s)")
        except Exception as e:  # noqa: BLE001
            # Report how long it actually took: distinguishes "needs a bigger
            # timeout" from "will never load on this hardware".
            logger.warning(f"  preload {name} failed after {time.time() - _t0:.0f}s "
                           f"(timeout {load_timeout}s): {e}")


def model_preload_worker():
    """One-shot: after the startup grace (so ollama is up), warm all ensemble models into
    memory. Off via ollama_preload_models=false."""
    # Start as soon as ollama ANSWERS, rather than waiting out the whole grace.
    # The grace exists so LLM work doesn't hit a server that is still starting —
    # but ollama does not preload anything on boot (it loads on first request), so
    # sitting out the full startup_grace_seconds (default 300) before even asking
    # just delays residency by however long the grace exceeds ollama's actual boot.
    # With scans now gated on residency, that delay is paid by every scan. The
    # grace remains the ceiling: if ollama never answers we fall through to the old
    # behaviour rather than blocking here.
    try:
        _cfg0 = load_config()
        _p0, _k0, _m0, _u0 = _get_provider_config(1, _cfg0)
        _probe_base = _ollama_base_url(_p0, _u0).rstrip("/") if _is_ollama(_p0) else None
    except Exception:  # noqa: BLE001
        _probe_base = None
    if _probe_base:
        while _startup_grace_remaining():
            if _ollama_answers(_probe_base):
                logger.info("ollama is answering — starting model preload without waiting "
                            "out the remaining ~%ds of startup grace.", _startup_grace_remaining())
                break
            time.sleep(5)
    else:
        while _startup_grace_remaining():
            time.sleep(15)
    # No extra sleep here: this used to wait 5s after the grace expired, which
    # handed the Ollama queue to poller_worker (it resumes the instant the grace
    # ends). The first CPU build then occupied the single OLLAMA_NUM_PARALLEL slot
    # and the preload sat behind it for the whole build — so models became
    # resident long AFTER work started, defeating the point of preloading.
    try:
        cfg = load_config()
        if cfg.get("ollama_preload_models", True):
            preload_ollama_models(cfg)
        else:
            logger.info("model preload disabled (ollama_preload_models=false).")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"model preload worker error: {e}")
    finally:
        # ALWAYS publish completion, including on failure or when disabled: the
        # scan gate below waits on this flag, and a preload that cannot succeed
        # must never hold work forever.
        state["models_preloaded"] = True
        logger.info("Model preload phase complete — scans may proceed.")


def poller_worker():
    global state
    _gate_t0 = [None]   # first time we started waiting on model residency
    while True:
        cfg = load_config()
        _grace = _startup_grace_remaining()
        if _grace:
            logger.info(f"Startup grace: holding scans ~{_grace}s more so ollama/services finish starting.")
            time.sleep(min(_grace, 30))
            continue
        # Gate work on models actually being RESIDENT, not merely on the grace
        # clock expiring. Ollama serialises requests (OLLAMA_NUM_PARALLEL=1), so a
        # scan that starts first puts a multi-minute CPU build in the single slot
        # and the preload queues behind it — models finish loading long after the
        # work that needed them. Bounded so a preload that can never succeed does
        # not wedge the poller; on timeout we proceed and say so.
        if cfg.get("gate_scans_on_model_preload", True) and not state.get("models_preloaded"):
            try:
                _cap = max(60, int(cfg.get("model_gate_max_wait_s",
                                           cfg.get("ollama_preload_timeout_s", 3600)) or 3600))
            except (TypeError, ValueError):
                _cap = 3600
            if _gate_t0[0] is None:
                _gate_t0[0] = time.time()
            _waited = time.time() - _gate_t0[0]
            if _waited < _cap:
                logger.info("Holding scans: waiting for ensemble model(s) to become resident "
                            "(%.0fs elapsed, cap %ds).", _waited, _cap)
                time.sleep(10)
                continue
            logger.warning("Proceeding WITHOUT confirmed model residency after %.0fs (cap %ds) — "
                           "the first LLM call may pay a cold-load cost.", _waited, _cap)
            state["models_preloaded"] = True   # stop re-warning every cycle
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
                interval = int(cfg.get("SCHEDULER_WORK_POLL_INTERVAL") or 3600)
            else:
                interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 3600))
        else:
            logger.debug("Poller worker is paused. Skipping scan cycle.")
            interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 3600))
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
    if (p or "").startswith("copilot"):
        # Copilot: exchange the stored GitHub token for a Copilot token, list its models.
        out, error = [], ""
        try:
            from llm_client import _copilot_api_token, _copilot_headers, COPILOT_API_BASE
            tok = _copilot_api_token(api_key)
            resp = requests.get(f"{COPILOT_API_BASE}/models", headers=_copilot_headers(tok), timeout=15)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                if m.get("id"):
                    out.append({"name": m["id"], "details": "GitHub Copilot"})
        except Exception as e:  # noqa: BLE001
            error = f"Copilot models — {_model_fetch_reason(e)}"
            logger.warning(f"Copilot model fetch failed: {error} [{type(e).__name__}]")
        return {"models": out, "error": error}
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
    if _is_ollama(p):
        # Ollama exposes /api/tags; a self-hosted instance needs no auth key.
        # Put this BEFORE the api_key gate so a local (no-key) ollama shows its
        # models as a selectable option — without this, a no-key ollama fell
        # through to the "api_key required" empty-list branch and never appeared.
        # Ollama Cloud (https://ollama.com) takes a key, sent as a Bearer header
        # when present. Default base_url: ollama.com for the `ollama_cloud` slot,
        # localhost for every self-hosted slot (_ollama_base_url).
        base = _ollama_base_url(p, base_url).rstrip("/")
        headers = {}
        if api_key:
            clean = api_key.strip().replace("Bearer ", "").strip()
            headers["Authorization"] = f"Bearer {clean}"
        attempted = f"{base}/api/tags"
        out = []
        error = ""
        try:
            resp = requests.get(attempted, headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("models", []):
                name = m.get("name", "")
                if "bf16" in name:
                    name = name[:name.find("bf16") + 4]
                details = m.get("details", "")
                if isinstance(details, dict):
                    details = details.get("family", str(details))
                out.append({"name": name, "details": str(details)})
        except Exception as e:
            error = f"{attempted} — {_model_fetch_reason(e)}"
            logger.warning(f"Ollama model fetch failed: {error} [{type(e).__name__}]")
        return {"models": out, "error": error}
    if not api_key:
        return {"models": [], "error": ""}
    models = []
    error = ""
    attempted = ""
    try:
        if p == "anthropic":
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
                # Only models that actually support generateContent are usable
                # here. The endpoint also returns embedding / tuning-only models,
                # and offering one of those yields a 404 at call time that reads
                # as "the model does not exist" rather than "wrong method".
                methods = m.get("supportedGenerationMethods") or []
                if methods and "generateContent" not in methods:
                    continue
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
        elif p == "openrouter":
            # No gpt/o1/o3/o4 substring filter (unlike the generic openai-compat
            # branch below): OpenRouter ids are vendor-prefixed
            # ("anthropic/claude-3.5-sonnet", "meta-llama/llama-3.1-70b-instruct",
            # ...) — that filter would drop nearly every one of them.
            base = (base_url or OPENROUTER_BASE_URL).rstrip("/")
            headers = {"Authorization": f"Bearer {api_key}"}
            attempted = f"{base}/models"
            resp = requests.get(attempted, headers=headers, timeout=10)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                models.append({"name": m.get("id", ""), "details": (m.get("name") or m.get("owned_by") or "")})
            # "openrouter/free" (the Free Models Router — routes each request to a
            # random available :free model, filtered for the features it needs)
            # does NOT appear in OpenRouter's own /models listing (confirmed live:
            # that endpoint returns only "openrouter/auto-beta", a DIFFERENT, paid
            # router) even though it's a valid, documented model id. Inject it and
            # pin it first so it's always selectable and easy to find.
            models = [m for m in models if m.get("name") != "openrouter/free"]
            models.insert(0, {"name": "openrouter/free",
                              "details": "Free Models Router — random available :free model"})
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


def _diag_origin_version():
    """Best-effort VERSION file content at origin HEAD (no network fetch).

    Companion to _diag_origin_head: returns the version string the
    Diagnostics panel shows for "Origin" instead of a commit SHA."""
    try:
        repo = git.Repo(os.getcwd())
        branch = "main"
        try:
            branch = repo.active_branch.tracking_branch().name.split("/")[-1]
        except Exception:
            pass
        try:
            return repo.git.show(f"origin/{branch}:VERSION").strip() or None
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


# Re-export every name this module defines (public + underscore worker helpers)
# so ``from workers import *`` in main preserves the full `from main import ...`
# surface used by routes.py and the sibling modules.
__EXCLUDE = {"git", "json", "os", "py_compile", "requests", "time",
             "ThreadPoolExecutor", "datetime", "load_dotenv", "Github",
             "GithubException", "main",
             "ANTHROPIC_API_VERSION", "ANTHROPIC_BASE_URL", "GOOGLE_BASE_URL",
             "OPENAI_BASE_URL", "CONFIG_DIR", "SELF_SCAN_OFFSET_FILE",
             "_any_provider_available", "_get_provider_config", "_is_lmstudio", "_is_ollama",
             "_llm_cb_snapshot", "_local_health_url", "_normalize_lmstudio_url",
             "_provider_configured", "_provider_credit_cb_snapshot",
             "_set_update_cooldown", "get_log_path", "get_version", "load_config",
             "load_processed", "load_update_state", "logger", "save_config",
             "save_update_state", "state", "update_task_state"}
__all__ = [__n for __n in dir() if not __n.startswith("__") and __n not in __EXCLUDE]
