"""Background workers + scan/update orchestration for AppBuilder.

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
# Standard and third-party modules for git, JSON, files, HTTP, processes, threads and time.
import git
import json
import os
import py_compile
import requests
import subprocess
import sys
import threading
import time
# Thread pool, timestamps, .env loading and the GitHub API.
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from github import Github, GithubException

# main itself, for sibling functions that are only available after this module loads.
import main  # lazy access to sibling functions imported after workers (main.scan_hub_logs, ...)
# Shared config, state, provider helpers and logging re-exported by main.
from main import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_BASE_URL,
    GOOGLE_BASE_URL,
    OPENAI_BASE_URL,
    OPENROUTER_BASE_URL,
    CONFIG_DIR,
    SELF_SCAN_OFFSET_FILE,
    _atomic_write_json,
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
    recompute_issue_counters,
    save_processed,
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
    # No model configured: nothing more to check.
    want = (model or "").strip()
    if not want:
        return True
    # Read the model names from the response.
    try:
        names = [(m.get("name") or "") for m in (resp.json().get("models") or [])]
    except Exception:
        return True  # unparseable — don't mark offline on a parse hiccup
    # Accept an exact match or the same name with ":latest".
    base_want = want.split(":")[0]
    for nm in names:
        if nm == want or nm == f"{want}:latest":
            return True
        # Tolerate the implicit :latest tag: a bare "llama3" matches pulled "llama3:latest".
        if ":" not in want and nm.split(":")[0] == base_want:
            return True
    # Model not pulled.
    return None


def _check_provider_online(n, config):
    """Ping legacy provider slot n and return True if reachable."""
    # Look up the slot's provider settings and probe them.
    provider, api_key, model, base_url = _get_provider_config(n, config)
    return _probe_endpoint_online(provider, api_key, model, base_url, label=f"Provider {n}")


def _probe_endpoint_online(provider, api_key, model, base_url, label="endpoint"):
    """Ping a single (provider, model, base_url) endpoint; return True if
    reachable, else None/False.

    Shared by the legacy per-slot _check_provider_online and the entry-aware
    _check_entries_online, so the header online/offline pills reflect the SAME
    endpoints the capability/cost picker actually routes over (llm_entries) —
    not the retired 8 fixed provider slots.
    """
    # Normalise the provider name (default openai).
    p = (provider or "openai").lower().strip()
    # claude_cli authenticates via Claude Code session — just check the binary exists.
    if p == "claude_cli":
        if not model:
            return None
        try:
            import subprocess
            r = subprocess.run(["claude", "--version"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return None
    if _is_lmstudio(p):
        # LM Studio needs no API key — just confirm the local server is reachable.
        if not model:
            return None
        try:
            base = _normalize_lmstudio_url(base_url).rstrip("/")
            resp = requests.get(f"{base}/models", timeout=10)
            if resp.status_code == 401:
                return None
            return resp.status_code < 300
        except Exception:
            return None
    if _is_ollama(p):
        # Ollama needs no API key (self-hosted; Ollama Cloud sends a Bearer key) —
        # confirm the server is reachable (/api/tags) AND that the configured model
        # is actually pulled. /api/tags is 200 whenever the server is up, but
        # /api/chat 404s if the model is missing, so a bare server ping would
        # falsely show the provider green while every real call fails.
        if not model:
            return None
        try:
            base = _ollama_base_url(p, base_url).rstrip("/")
            headers = {}
            if api_key:
                clean = api_key.strip().replace("Bearer ", "").strip()
                headers["Authorization"] = f"Bearer {clean}"
            resp = requests.get(f"{base}/api/tags", headers=headers, timeout=10)
            if resp.status_code == 401:
                return None
            if resp.status_code >= 300:
                return None
            if not _ollama_model_present(resp, model):
                logger.warning(
                    f"{label} (ollama) server is up but model '{model}' is not pulled on "
                    f"{base} — pull it (Settings → Local LLM Setup, or `ollama pull {model}`)."
                )
                return None
            return True
        except Exception:
            return None
    if p.startswith("copilot"):
        # Copilot: the stored api_key is a GitHub OAuth token; it must be exchanged
        # for a short-lived Copilot API token, and the models call needs editor headers.
        # No token: cannot check.
        if not api_key:
            return None
        try:
            from llm_client import _copilot_api_token, _copilot_headers, COPILOT_API_BASE
            tok = _copilot_api_token(api_key)
            resp = requests.get(f"{COPILOT_API_BASE}/models", headers=_copilot_headers(tok), timeout=15)
            if resp.status_code == 401:
                logger.warning(f"{label} (copilot) connectivity check: 401 — token exchange rejected (re-authorize).")
                return None
            return resp.status_code < 300
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{label} (copilot) connectivity check error: {e}")
            return None
    # Every other provider needs both a key and a model.
    if not api_key or not model:
        return None
    # Call the provider's model-list endpoint with its own auth style.
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
        # 401 (bad key) and 429 (rate-limited) are logged and reported as unknown.
        if resp.status_code == 401:
            logger.warning(f"{label} ({provider}) connectivity check: 401 — API key invalid or missing.")
            return None
        if resp.status_code == 429:
            logger.warning(f"{label} ({provider}) connectivity check: 429 — rate-limited (not tripping CB).")
            return None
        # Any 2xx means online.
        return resp.status_code < 300
    # Network errors: unknown.
    except Exception as e:
        logger.debug(f"{label} ({provider}) connectivity check error: {e}")
        return None


def _check_entries_online(config):
    """Probe every ENABLED routable endpoint (llm_entries + legacy env slots,
    exactly the set model_selection.select_model ranks) and return a compact
    per-endpoint status list for the header pills.

    The legacy provider_N_online pills only reflected the retired 8 fixed slots
    (llm_slots), so once routing moved to the capability/cost picker over
    llm_entries, endpoints that route fine still showed red. This mirrors the
    real routable set instead. Each probe is wrapped so one unreachable endpoint
    can't abort the whole sweep. Deduplicated by (provider, base_url, model).
    """
    # Load the endpoint helpers; without them there is nothing to list.
    try:
        from llm_client import _iter_configured_endpoints, _model_key, get_llm_perf_snapshot
    except Exception as e:  # noqa: BLE001
        logger.debug(f"_check_entries_online: cannot enumerate endpoints: {e}")
        return []
    # "Being used" signal: llm_perf records one sample per completed generation,
    # keyed by ModelKey. n > 0 means this endpoint has actually served a call —
    # the header pills show ONLY these (plus the one serving right now), not
    # every configured endpoint.
    try:
        perf = get_llm_perf_snapshot() or {}
    except Exception:  # noqa: BLE001
        perf = {}
    # The model currently serving, so it counts as "used" even with no samples yet.
    active_model = (state.get("active_llm") or "").strip()
    active_provider = state.get("active_llm_provider")
    # Probe each endpoint once, skipping duplicates.
    out, seen = [], set()
    for entry_id, provider, api_key, model, base_url, rpm in _iter_configured_endpoints(config):
        key = ((provider or "").lower().strip(), (base_url or "").strip(), (model or "").strip())
        if key in seen:
            continue
        seen.add(key)
        label = f"{provider}:{model}" if model else (provider or "endpoint")
        # A failing probe counts as offline rather than stopping the sweep.
        try:
            online = _probe_endpoint_online(provider, api_key, model, base_url, label=label)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"_check_entries_online probe failed for {label}: {e}")
            online = None
        # How many calls this endpoint has served.
        try:
            runs = int((perf.get(_model_key(provider, base_url, model)) or {}).get("n") or 0)
        except Exception:  # noqa: BLE001
            runs = 0
        # Is this the model serving right now?
        is_active = bool(active_model) and (model or "").strip() == active_model and (
            active_provider is None
            or (provider or "").lower().strip() == (active_provider or "").lower().strip())
        # Add the endpoint's status to the list.
        out.append({"id": entry_id, "provider": provider, "model": model,
                    "base_url": base_url, "online": bool(online),
                    "used": bool(runs > 0 or is_active), "runs": runs})
    return out


def refresh_llm_endpoints_async():
    """Re-probe endpoints and refresh ``state["llm_endpoints_online"]`` now.

    connectivity_worker only rebuilds that list every 15 minutes, so a newly
    added LLM entry stayed absent from the header Providers chip until the next
    tick — indistinguishable, to the user, from the new provider never having
    registered at all. Called after any llm_entries create/update/delete so the
    header reflects the change immediately.

    Runs on a daemon thread: probing every endpoint is network I/O and must
    never block the save request. Best-effort — a probe failure must not turn a
    successful save into an error.
    """
    def _run():
        """Probe all endpoints and store the result in state."""
        try:
            config = load_config()
            endpoints = _check_entries_online(config)
            state["llm_endpoints_online"] = endpoints
            state["any_llm_online"] = any(e["online"] for e in endpoints)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"refresh_llm_endpoints_async failed: {e}")

    # Run the probe in the background so the save is not delayed.
    threading.Thread(target=_run, daemon=True).start()


def connectivity_worker():
    """Periodic check to verify all configured LLM providers are reachable."""
    # Every 15 minutes, probe the legacy slots and every endpoint and store the results.
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
            # Entry-aware view: the header pills read this so they reflect the
            # endpoints the picker actually routes over, not the retired slots.
            endpoints = _check_entries_online(config)
            state["llm_endpoints_online"] = endpoints
            state["any_llm_online"] = any(e["online"] for e in endpoints)
            online_n = sum(1 for e in endpoints if e["online"])
            logger.info(f"Connectivity Check: P1={p1_online}, P2={p2_online}, P3={p3_online}, "
                        f"P4={p4_online}; entries {online_n}/{len(endpoints)} online")
        except Exception as e:
            logger.error(f"Connectivity worker error: {e}")
        time.sleep(900)


def heartbeat_worker():
    """Every 5 seconds, record which providers are configured and which model is active."""
    while True:
        try:
            # Read the four legacy slot settings.
            config = load_config()
            p1_provider, p1_key, p1_model, _ = _get_provider_config(1, config)
            p2_provider, p2_key, p2_model, _ = _get_provider_config(2, config)
            p3_provider, p3_key, p3_model, _ = _get_provider_config(3, config)
            p4_provider, p4_key, p4_model, _ = _get_provider_config(4, config)

            # Note which slots are fully configured.
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

            # The active model is slot 1, else slot 2, else the first configured entry.
            if p1_configured:
                state["active_llm"] = p1_model
            elif p2_configured:
                state["active_llm"] = p2_model
            else:
                # The 8 fixed provider slots are retired: an operator may
                # configure ONLY llm_entries, in which case the slots above are
                # all empty and the header would read "Not configured" while
                # routing works fine. Fall back to the first configured entry so
                # the banner reflects the real routable model.
                entry_model = None
                entry_provider = None
                try:
                    from llm_client import _configured_entries
                    for ent in _configured_entries(config):
                        if ent.get("_configured"):
                            entry_model = ent.get("model")
                            entry_provider = ent.get("provider")
                            break
                except Exception:
                    logger.exception("Heartbeat: entry lookup failed")
                if entry_model:
                    state["active_llm"] = entry_model
                    if not state.get("active_llm_provider"):
                        state["active_llm_provider"] = entry_provider
                else:
                    state["active_llm"] = "Not configured"
        except Exception as e:
            logger.error(f"Heartbeat worker error: {e}")
        time.sleep(5)







def _trigger_spoke_updates(config):
    """Trigger updates across the Hub, its spokes, and its agents after a fix push.

    AppBuilder is an authenticated WebSocket agent of the Hub, so it issues a single
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
    # No hub agent: just start the cooldown and report.
    client = _get_hub_agent_client()
    if not client:
        logger.debug("_trigger_spoke_updates: Hub agent not configured/approved, skipping")
        _set_update_cooldown(config)
        return "Hub agent not connected — update not triggered"
    # Ask the hub to update itself, its spokes and its agents.
    result = client.request_sync("TRIGGER_ALL_UPDATES", {}, timeout=60)
    if not isinstance(result, dict):
        logger.warning("Hub agent not approved/connected — skipping update trigger")
        _set_update_cooldown(config)
        return "Hub agent not approved/connected — update not triggered"
    # The hub returns {hub, spokes, agents} on success but an ERROR ENVELOPE
    # ({"status":"error","message":...}) when it rejects the request — most
    # commonly the H1 authz denial (the AppBuilder client cert must be pinned via
    # the LE module AND presented over mTLS). That envelope is also a dict, so a
    # bare isinstance check would report a hollow "hub= | spokes= | agents="
    # SUCCESS for a request the hub never honored. Require all three result keys.
    if not all(k in result for k in ("hub", "spokes", "agents")):
        msg = result.get("message") or result.get("status") or "hub rejected the update request"
        logger.warning(f"Hub update NOT triggered: {msg}")
        # Do NOT start the restart cooldown — nothing is restarting, and
        # suppressing issue filing would hide real errors.
        return f"Hub update NOT triggered: {msg}"
    # Summarise each part of the result and log it.
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
    # Not a dict: show it as-is.
    if not isinstance(d, dict):
        return str(d)
    # Status message plus any triggered names.
    msg = d.get("message") or d.get("status") or ""
    trig = d.get("triggered") or []
    if isinstance(trig, list) and trig:
        return f"{msg} (triggered: {', '.join(map(str, trig))})"
    return msg


# ---------------------------------------------------------------------------
# Hub agent (WebSocket) — AppBuilder authenticates to the LM Hub as an agent.
# See hub_agent.py for the protocol. The helpers below bridge the async agent
# client to AppBuilder's sync state dict + config file.
# ---------------------------------------------------------------------------

def _derive_ws_url(http_url):
    """Derive a Hub WebSocket URL from an HTTP Hub URL.

    The unified hub shares ONE port (:443) for HTTP + WebSocket, with the spoke
    socket on the ``/ws/spoke`` path — the old bare ``:8765`` raw-socket listener
    is gone. So derive ``wss://<host>:443/ws/spoke`` (hub_agent._normalize_hub_ws_url
    would fill the same defaults). TLS is unverified by default (self-signed hub).
    """
    # Build the wss URL from the hub's host name; empty if it cannot be parsed.
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
    # Store the new status, message and time seen.
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
    # Store whether connected; note when it dropped.
    state["hub_agent_connected"] = bool(connected)
    if not connected:
        state["hub_agent_last_disconnect"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _persist_config_key(key, value):
    """Callback helper: upsert a single key into config.json (agent-managed secrets)."""
    # Load, update and save the config; a failure is only a warning.
    try:
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)
    except Exception as e:
        logger.warning(f"Could not persist {key} to config: {e}")


def _hub_agent_on_secret(secret):
    """Callback: the Hub pushed a (possibly empty) session secret."""
    # Save the secret to the config.
    _persist_config_key("HUB_AGENT_SECRET", secret or "")


def _hub_agent_on_hub_secret(hub_secret):
    """Callback: the Hub pushed its (rotated) identity secret for mutual auth."""
    # Save the hub secret to the config.
    _persist_config_key("HUB_SECRET", hub_secret or "")


def _hub_agent_on_recovery_psk(psk):
    """Callback: the Hub pushed its durable recovery PSK (survives secret rotations)."""
    # Save the recovery PSK to the config.
    _persist_config_key("HUB_RECOVERY_PSK", psk or "")


def _get_hub_agent_client():
    """Return the running Hub agent singleton, or None if not started."""
    # Return the client if hub_agent can be imported.
    try:
        import hub_agent
        return hub_agent.hub_agent_client
    except Exception:
        return None


def _start_hub_agent():
    """Start the Hub agent at app startup if a Hub WebSocket URL is configured.

    HUB_WS_URL is authoritative; if it's empty we try to derive it from
    HUB_QUERY_URL (host + :8765). If still empty, the agent stays unstarted and
    state reports "not_registered" — AppBuilder runs fine without Hub integration.
    """
    try:
        # Use HUB_WS_URL, else derive it from HUB_QUERY_URL.
        import hub_agent
        cfg = load_config()
        ws_url = (cfg.get("HUB_WS_URL") or "").strip()
        if not ws_url:
            hq = (cfg.get("HUB_QUERY_URL") or "").strip()
            if hq and "your-netbox" not in hq:
                ws_url = _derive_ws_url(hq)
        # No URL: run without the hub.
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
            on_recovery_psk=_hub_agent_on_recovery_psk,
        )
    # Start-up failure: record it in state and carry on.
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
    # Need either the hub URL or an agent client.
    hub_url = (config.get("HUB_QUERY_URL") or "").rstrip("/")
    client = _get_hub_agent_client()
    if not hub_url and not client:
        return []
    # Check every 5 seconds until the deadline.
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
        # Enough spokes: return them.
        if isinstance(conns, list) and len(conns) >= min_count:
            logger.info(f"Spokes online: {conns}")
            return conns
        time.sleep(5)
    # Timed out.
    logger.warning(f"Timed out after {timeout}s waiting for spokes to come back online")
    return []





def check_for_updates():
    """Checks GitHub for new versions, performs pre-flight syntax checks, and signals a restart if safe."""
    try:
        # Remember the current commit as the last known good one.
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
            return None, f"Update skipped: remote head {remote_head[:7]} is a known-bad commit."

        # Advance to the fetched remote head. /opt/ab is a DEPLOYMENT MIRROR,
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
        # Note the commit we ended up on.
        new_commit = self_repo.head.commit.hexsha

        # New code: check it before restarting.
        if old_commit != new_commit:
            cur_version = get_version()
            logger.info(f"New version detected ({cur_version})! {old_commit[:7]} -> {new_commit[:7]}. Validating...")

            # Compile every .py file; stop at the first syntax error.
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

            # Syntax error: roll back and blocklist the commit.
            if syntax_error:
                logger.error(f"Syntax check failed for commit {new_commit[:7]}. Rolling back to {old_commit[:7]}...")
                self_repo.git.reset("--hard", old_commit)
                update_state = load_update_state()
                if new_commit not in update_state["failed_commits"]:
                    update_state["failed_commits"].append(new_commit)
                    save_update_state(update_state)
                return None, f"Update failed: Syntax error detected. Rolled back to {old_commit[:7]}."

            # Dependency sync. install.sh and update.sh both run
            # `pip install -r requirements.txt`, but self-update previously went
            # straight from `git pull` to a restart — so a commit that ADDS a
            # dependency restarted into a process that had never installed it,
            # producing a silent degradation (or a crash loop) on every
            # self-updating host. Only runs when requirements.txt actually
            # changed, so the normal update path stays fast.
            if _requirements_changed(self_repo, old_commit, new_commit):
                ok, pip_msg = _sync_dependencies()
                if not ok:
                    # Roll back to the last known good commit so the service keeps
                    # running on code whose dependencies are satisfied. Deliberately
                    # NOT added to failed_commits: a pip failure is often transient
                    # (network/index), and permanently blacklisting the commit would
                    # wedge a perfectly good update forever. The next hourly cycle
                    # retries it.
                    logger.error("Self-update: dependency sync failed (%s). Rolling back to %s.",
                                 pip_msg, old_commit[:7])
                    self_repo.git.reset("--hard", old_commit)
                    return None, f"Update failed: dependency install failed ({pip_msg}). Rolled back to {old_commit[:7]}."

            # Write the update_pending marker with the new commit.
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

        # Already up to date.
        return None, "No updates available."
    # Any other failure: report it.
    except Exception as e:
        logger.warning(f"Self-update check failed: {e}")
        return None, f"Update check failed: {e}"

def _requirements_changed(self_repo, old_commit, new_commit):
    """True if requirements.txt differs between the two commits (best-effort:
    on any git error assume it DID change, so a dependency sync is attempted
    rather than silently skipped)."""
    try:
        # List the changed files and look for requirements.txt.
        changed = self_repo.git.diff("--name-only", old_commit, new_commit)
        return any(line.strip().endswith("requirements.txt")
                   for line in (changed or "").splitlines())
    except Exception as e:  # noqa: BLE001
        logger.warning("Self-update: could not diff requirements.txt (%s); syncing anyway.", e)
        return True


def _sync_dependencies(timeout=600):
    """Install requirements.txt into the venv this process is running from.

    Uses ``sys.executable -m pip`` so it always targets AppBuilder's own
    interpreter rather than whatever "pip" happens to be on PATH (the systemd
    unit runs with a minimal PATH that excludes the venv's bin dir).
    Returns (ok, message).
    """
    # No requirements file: nothing to install.
    req = os.path.join(os.getcwd(), "requirements.txt")
    if not os.path.isfile(req):
        return True, "no requirements.txt"
    logger.info("Self-update: requirements.txt changed — syncing dependencies...")
    # Run pip with this interpreter; timeouts and launch errors are failures.
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req, "-q"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "pip install timed out"
    except Exception as e:  # noqa: BLE001
        return False, f"pip install could not run: {e}"
    # pip failed: return its last few output lines.
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, "; ".join(detail[-3:]) or f"pip exited {proc.returncode}"
    logger.info("Self-update: dependencies synced.")
    return True, "ok"


# How often the updater checks for new code.
UPDATER_INTERVAL_SECONDS = 300  # 5 minutes — was 3600 (hourly), which let the
                                 # fleet drift stale on `main` for up to an hour
                                 # after a merge before anyone noticed.

# Repo labels only feed the Settings label picker, so re-listing them (one
# GitHub call per repo) every poll cycle is wasted API quota.
LABEL_DISCOVERY_TTL_S = 6 * 3600
# Cached repo labels and when they were fetched.
_label_cache = {"at": 0.0, "repos": None}

def updater_worker():
    """Dedicated worker to check for updates every 5 minutes.

    Stamps ``last_update_check_ts`` in update_state.json on every cycle
    (success or failure) so watchdog.py can detect a wedged/dead thread —
    an uncaught exception here would otherwise silently stop checks forever
    with no external signal.
    """
    # Check for updates forever, logging the outcome of each check.
    while True:
        try:
            logger.info("Checking for self-updates...")
            updated, msg = check_for_updates()
            _log_restart_event("auto_update", msg, ok=bool(updated))
        except Exception as e:
            logger.error(f"Updater worker error: {e}")
        # Always record the check time so the watchdog can see the thread is alive.
        finally:
            try:
                update_state = load_update_state()
                update_state["last_update_check_ts"] = datetime.now().isoformat()
                save_update_state(update_state)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Updater worker: could not stamp last_update_check_ts: {e}")
        time.sleep(UPDATER_INTERVAL_SECONDS)

def _log_restart_event(kind, message, ok=True):
    """Append a bounded entry to state["restart_log"] for the Diagnostics panel."""
    # Build the log entry and append it to the restart log.
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

# File the watchdog reads to restart the service for us.
RESTART_REQUEST_FILE = os.path.join(CONFIG_DIR, "restart_request")


def _watchdog_active():
    """True if ab-watchdog.service is active (so a delegated restart request
    will actually be consumed). None/False on error → caller falls back to a
    hard exit. Mirrors ollama_setup._watchdog_active."""
    # Ask systemd whether the watchdog is running.
    import subprocess as _sp
    try:
        r = _sp.run(["systemctl", "is-active", "--quiet", "ab-watchdog"], timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return None


def _request_watchdog_restart():
    """Ask ab-watchdog (the unrestricted privileged arm) to restart us, by
    writing a request file it consumes. Returns True if the request was queued to
    an ACTIVE watchdog; False otherwise (caller hard-exits instead). Mirrors the
    ollama-setup delegation — ab.service is cap-locked and can't restart
    itself, but the watchdog can."""
    # A stopped watchdog would never see the request.
    if _watchdog_active() is False:
        return None
    # Write the request file.
    try:
        with open(RESTART_REQUEST_FILE, "w") as f:
            json.dump({"requested_at": time.time()}, f)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("restart: could not write watchdog restart request (%s)", e)
        return None


def _spawn_restart():
    """Restart ab, robust to the cap-locked service.

    As root (dev/standalone): a direct detached `systemctl restart ab`.

    As the svc_bg service user, ab.service is locked to
    CAP_NET_BIND_SERVICE, so sudo can't setgid(0) ("unable to change to root gid:
    Operation not permitted") and the old `sudo -n /usr/local/bin/ab-self-restart`
    path fails every time (noisy ERROR + only recovered via a hard exit). Instead we
    DELEGATE to ab-watchdog — the unrestricted privileged arm that already runs
    ollama-setup on our behalf — via a request file it consumes to run the restart.
    If the watchdog isn't active to pick it up, hard-exit so systemd Restart=always
    revives us regardless (the manual Restart button can never silently no-op)."""
    import subprocess as _sp
    # Root: restart the service directly, hard-exiting if that fails.
    if os.geteuid() == 0:
        try:
            _sp.Popen(["systemctl", "restart", "ab"], start_new_session=True,
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, close_fds=True)
        except Exception as e:  # noqa: BLE001
            logger.error("restart: root systemctl restart failed (%s) — hard-exiting", e)
            _exit_for_systemd_restart()
        return
    # Non-root: delegate to the watchdog, else hard-exit.
    if _request_watchdog_restart():
        logger.info("restart: delegated to ab-watchdog (privileged arm); "
                    "it will `systemctl restart ab` shortly.")
        return
    logger.warning("restart: ab-watchdog not active — hard-exiting so systemd "
                   "Restart=always revives us.")
    _exit_for_systemd_restart()


def _exit_for_systemd_restart():
    """Last-resort restart: flush logs, then hard-exit. ab.service is
    Restart=always / RestartSec=10, so systemd revives the process — no sudo, no
    cgroup race. Used when the detached restart path fails so the manual Restart
    button (and cert-install restarts) can never silently no-op."""
    # Flush every log handler, ignoring errors, then exit.
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
    then spawns a detached `systemctl restart ab`. Best-effort verifies health
    post-restart and retries the spawn once. The authoritative post-restart verification
    is the watchdog's restart-then-verify flow; this worker is the fast path and the
    on-disk update_pending file is the durable backstop."""
    # Wait time, health-check limits and the local health URL.
    GRACE_SECONDS = 15
    VERIFY_TIMEOUT = 60
    VERIFY_INTERVAL = 3
    HEALTH_URL = _local_health_url()
    # Check every 2 seconds for a pending restart.
    while True:
        try:
            if state.get("restart_pending"):
                # Clear the flag, wait the grace period, then restart.
                state["restart_pending"] = False
                logger.info(f"restart_worker: applying restart (grace window {GRACE_SECONDS}s).")
                time.sleep(GRACE_SECONDS)
                _spawn_restart()
                _log_restart_event("restart", "restart spawned", ok=True)
                # The current process will receive SIGTERM from systemd within ~1s. If by
                # some reason we are still alive after VERIFY_TIMEOUT, retry the spawn once.
                # Poll the health URL until it answers ok or the time runs out.
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
                # Still not healthy: log it and try once more.
                if not healthy:
                    logger.error("restart_worker: post-restart health not observed; retrying spawn.")
                    _log_restart_event("restart", "post-restart health failed; retrying", ok=False)
                    _spawn_restart()
            else:
                time.sleep(2)
        # Errors: log and wait before checking again.
        except Exception as e:
            logger.error(f"restart_worker error: {e}")
            time.sleep(5)




# Static module_type -> repo map for heartbeat triage. Each spoke advertises a
# logical module_type (e.g. "firewall", "hypervisor") at Hub auth time; this maps
# that type to the repo whose issues a missing/stale heartbeat should land in.
# "hub" covers the Hub itself. Falls back to module_repo_map / resolve_module_repo
# / resolve_self_diagnosis_repo at runtime so every module gets triaged somewhere.










# Stored statuses that wait on GitHub. RepoScan only reads OPEN issues, so
# without this sync an issue closed on GitHub stays failed/non-actionable in
# the AB feed forever. pending_verification is left out: those issues are
# closed on GitHub by design while a person confirms the fix.
_GITHUB_SYNC_STATUSES = ("failed", "non-actionable", "ops-alert")
GITHUB_SYNC_PER_CYCLE = 30


def _github_issue_status(gh_current, issue_id, repos):
    """"closed", "ops-alert" or "open" for a "owner/repo:number" id. A 404
    (deleted, or the number is a PR the token cannot see) counts as closed."""
    repo_name, _, number = issue_id.rpartition(":")
    try:
        if repo_name not in repos:
            repos[repo_name] = gh_current.get_repo(repo_name)
        issue = repos[repo_name].get_issue(int(number))
    except GithubException as e:
        if e.status == 404:
            return "closed"
        raise
    if issue.state == "closed":
        return "closed"
    if any(lbl.name == "ops-alert" for lbl in issue.labels):
        return "ops-alert"
    return "open"


def sync_github_issue_state(gh_current, limit=GITHUB_SYNC_PER_CYCLE, shared=None):
    """Check up to ``limit`` waiting records against GitHub, least recently
    checked first. Closed or gone -> "closed"; open with the ops-alert label ->
    "ops-alert", which takes it out of the Failed/Non-Actionable buckets.
    ``shared`` is the cycle's in-memory store; it gets the same updates, so a
    later save of it does not undo them."""
    processed = load_processed()
    waiting = [k for k, v in processed.items() if v.get("status") in _GITHUB_SYNC_STATUSES]
    waiting.sort(key=lambda k: processed[k].get("github_checked_at") or processed[k].get("timestamp") or "")
    if not waiting:
        return []
    repos = {}
    found = {}
    for issue_id in waiting[:limit]:
        try:
            found[issue_id] = _github_issue_status(gh_current, issue_id, repos)
        except Exception as e:  # noqa: BLE001 - one bad record must not stop the sync
            logger.debug(f"GitHub state sync skipped {issue_id}: {e}")

    # Reload: other workers may have saved while we were asking GitHub.
    processed = load_processed()
    now = datetime.now().isoformat()
    changed = []
    for issue_id, gh_status in found.items():
        rec = processed.get(issue_id)
        if not rec or rec.get("status") not in _GITHUB_SYNC_STATUSES:
            continue
        rec["github_checked_at"] = now
        if gh_status == "closed":
            rec["status_before_close"] = rec["status"]
            rec["status"] = "closed"
            rec["closed_at"] = now
            changed.append(issue_id)
        elif gh_status == "ops-alert" and rec["status"] != "ops-alert":
            rec["status_before_ops_alert"] = rec["status"]
            rec["status"] = "ops-alert"
            changed.append(issue_id)
        if shared is not None:
            shared[issue_id] = rec
    save_processed(processed)
    if changed:
        logger.info(f"GitHub state sync: updated {len(changed)} record(s): {', '.join(changed)}")
        recompute_issue_counters(processed)
    return changed


def scan_repo_issues(gh_current, config, processed):
    """Phase: Scan monitored repos for issues and attempt fixes concurrently."""
    global state
    # Our bot account, the repos to scan and the fix concurrency limit.
    bot_user = gh_current.get_user().login

    monitored_repos = main.get_monitored_repos(config)
    max_workers = int(config.get("MAX_CONCURRENT_FIXES", 5))

    # Scan each monitored repo in turn.
    try:
        for repo_name in monitored_repos:
            update_task_state(task_id="RepoScan", task_name=f"Checking {repo_name}", action="start")
            logger.info(f"Scanning repository: {repo_name}")
            try:
                # Fetch the open issues with the monitored labels ("NONE" skips, "ANY" means all).
                repo_obj = gh_current.get_repo(repo_name)
                is_owner = repo_obj.owner.login == bot_user
                labels = config.get("monitored_labels", ["automated-fix"])
                if "NONE" in labels:
                    continue
                if "ANY" in labels:
                    issues = repo_obj.get_issues(state="open")
                else:
                    issues = repo_obj.get_issues(labels=labels, state="open")

                # Is the scheduler window open, and which label marks critical issues?
                sched = _schedule_check(config)
                critical_label = (config.get("SCHEDULER_CRITICAL_LABEL") or "").strip()

                # Sort the issues into critical, bug and normal lists.
                to_fix = []           # log-detected / generic automated-fix issues
                critical_to_fix = []  # SCHEDULER_CRITICAL_LABEL — always run, first, uncapped
                bug_to_fix = []       # bug-label (human-filed Bug) — run before log-detected
                                      # and bypass the scheduler window (processed now, not at
                                      # the next allowed slot). Subject to the per-cycle cap,
                                      # but takes cap slots ahead of log-detected.
                bug_label = (config.get("SCHEDULER_BUG_LABEL") or "bug").strip()
                # Triage each open issue; one bad issue must not stop the rest.
                for issue in issues:
                    try:
                        # Skip closed issues and pull requests.
                        if issue.state != 'open' or issue.pull_request:
                            continue

                        # Skip issues carrying the 'ab-dismissed' label — they were
                        # intentionally marked as not real. Remove the label to resume processing.
                        if any(lbl.name == "ab-dismissed" for lbl in issue.labels):
                            logger.debug(f"Skipping {repo_name}#{issue.number} — 'ab-dismissed' label present.")
                            continue
                        # Operational faults (log_scan.OPS_ALERT_LABEL) need a person or a
                        # device fixed, not a code edit; the fixer only failed on them.
                        if any(lbl.name == "ops-alert" for lbl in issue.labels):
                            continue

                        # Skip LM-filed feature requests — feature_drive.py owns these via its
                        # own single-label query (see that module's docstring for why it can't
                        # just be added to monitored_labels). Both pipelines key `processed` by
                        # the same "repo:number" id, so without this guard a feature request
                        # that also happens to carry a monitored label (e.g. an operator using
                        # monitored_labels=["ANY"]) could be picked up by BOTH triage loops.
                        if "<!-- report-type: feature -->" in (issue.body or ""):
                            continue

                        # Skip issues already finished (awaiting_review may resume).
                        issue_id = f"{repo_name}:{issue.number}"
                        if issue_id in processed:
                            status = processed[issue_id].get("status")
                            if status in ["fixed", "non-actionable", "failed", "awaiting_prod_verification"]:
                                if status != "awaiting_review": # Allow resuming reviews
                                    continue
                            if status == "awaiting_local":
                                pass
                            # For awaiting_review, we let it proceed to check the 1-hour timer in process_single_issue

                        # Case-INSENSITIVE label match: AppBuilder files with "Bug",
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
                    # Log triage errors and carry on.
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
                # Critical first, then bugs, then normal issues.
                all_to_fix = critical_to_fix + bug_to_fix + to_fix

                # Fix them if any LLM provider is free.
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
                        # Apply the per-cycle cap, logging how many were deferred.
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
                        # Fix the batch in parallel and wait for all of them.
                        batch = critical_to_fix + capped_bugs + capped_normal
                        with ThreadPoolExecutor(max_workers=max_workers) as executor:
                            futures = [executor.submit(main.process_single_issue, r, n) for r, n in batch]
                            for future in futures:
                                future.result()

            # 404: the repo is missing or we lack access; other errors are just logged.
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
        try:
            sync_github_issue_state(gh_current, shared=processed)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"GitHub state sync failed: {e}")
    except Exception as e:
        logger.exception(f"scan_repo_issues failed: {e}")
    # Always mark the scan task as finished.
    finally:
        update_task_state(task_id="RepoScan", action="end")

def resolve_self_diagnosis_repo(config):
    """Resolves the target repository for self-diagnosis issues.

    Priority:
      1. Explicit 'self_diagnosis_repo' config key (preferred, user-configurable).
      2. Git remote origin URL of the running AppBuilder checkout (best-effort).

    Returns the normalized 'owner/repo' string, or None if no valid target could
    be determined. Callers MUST handle a None return by skipping self-diagnosis
    instead of attempting to create issues against a hardcoded fallback that may
    not exist (which previously caused 404 errors every scan cycle).
    """
    # Use the configured repo name first.
    self_repo_name = (config.get("self_diagnosis_repo") or "").strip()

    # Else read it from this checkout's origin remote.
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

    # No repo found.
    if not self_repo_name:
        return None

    # Return it in owner/repo form.
    return main.clean_repo_name(self_repo_name)

def _file_inode(path):
    """Returns the inode of a file, or None if it cannot be stat'd."""
    # Stat the file.
    try:
        return os.stat(path).st_ino
    except Exception:
        return None

def load_self_scan_offset():
    """Returns (offset, inode) of the last self-scan read position, or (None, None)."""
    # Read the saved offset and inode; missing or bad file means none.
    try:
        with open(SELF_SCAN_OFFSET_FILE, "r") as f:
            data = json.load(f)
        return data.get("offset"), data.get("inode")
    except Exception:
        return None, None

def save_self_scan_offset(offset, inode):
    """Persists the last-read byte offset and inode for incremental self-scans."""
    # Save atomically; failures only matter for the next scan.
    try:
        _atomic_write_json(SELF_SCAN_OFFSET_FILE, {"offset": offset, "inode": inode}, indent=None)
    except Exception as e:
        logger.debug(f"Could not save self-scan offset: {e}")











def scan_self_logs(gh_current, config):
    """Scans AppBuilder's own logs and creates GitHub issues for internal errors.

    The target repository for self-diagnosis issues is resolved via
    resolve_self_diagnosis_repo(), which honors the 'self_diagnosis_repo' config
    key. If no valid repository can be determined, or if the resolved repository
    is not accessible (e.g., 404), self-diagnosis is skipped gracefully rather
    than crashing or spamming the logs with 404 errors every cycle.
    """
    global state
    # Mark the self-scan task as started.
    update_task_state(task_id="SelfScan", task_name="Scanning Self Logs", action="start")
    logger.info("Scanning internal AppBuilder logs for errors...")

    # Resolve and validate the target repository for self-diagnosis issues.
    self_repo_name = resolve_self_diagnosis_repo(config)

    # No repo configured: warn and stop.
    if not self_repo_name:
        logger.warning(
            "Self-diagnosis repository is not configured. Set 'self_diagnosis_repo' in the "
            "AppBuilder settings (https://<this-host>/settings) to a valid, accessible "
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
                f"AppBuilder settings (https://<this-host>/settings) to point at a valid, "
                f"accessible repository. Skipping self-log scan."
            )
        else:
            logger.error(
                f"Cannot access self-diagnosis repository '{self_repo_name}' "
                f"(GitHub API status {ge.status}): {ge}. Skipping self-log scan."
            )
        update_task_state(task_id="SelfScan", action="end")
        return
    # Any other access error: log it and stop.
    except Exception as e:
        logger.error(
            f"Cannot access self-diagnosis repository '{self_repo_name}': {e}. "
            f"Skipping self-log scan."
        )
        update_task_state(task_id="SelfScan", action="end")
        return

    # No log file yet: nothing to scan.
    log_path = get_log_path()
    if not os.path.exists(log_path):
        logger.warning(f"AppBuilder log file not found at {log_path}")
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

        # Pick where to start reading this time.
        if last_inode is None or last_offset is None or last_inode != current_inode:
            # First ever scan, or the log was rotated/recreated: start at the
            # current end so we only capture errors logged from now on.
            start_offset = current_size
        elif last_offset > current_size:
            # Same file but it shrank (truncated in place): skip to the new end.
            start_offset = current_size
        else:
            start_offset = last_offset

        # Read everything new since that point.
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
            # not a AppBuilder bug.
            "Hub agent",
            "APPROVAL_REQUIRED",
            "pending admin approval",
            "Hub rejected secret",
            "zero-touch",
            "reconnect",
            "Hub connection",
        )

        # Keep only new ERROR/CRITICAL lines that are not on the skip list.
        formatted_logs = []
        for line in new_text.splitlines():
            # Match AppBuilder's actual log format ("<ts> - AppBuilder - ERROR - ...")
            # AND the legacy "[ERROR]" bracket form. The bracket-only check missed
            # every real error (the format uses " - ERROR - "), so AppBuilder never
            # surfaced its own errors as bugs — including 500s logged as
            # "UNCAUGHT EXCEPTION". Now it does (→ self-diagnosis repo).
            if (" - ERROR - " in line or " - CRITICAL - " in line
                    or "[ERROR]" in line or "[CRITICAL]" in line):
                if any(skip in line for skip in _SELF_SCAN_SKIP):
                    continue
                ts = line[:23] if len(line) > 23 else "Unknown"
                formatted_logs.append({
                    "module": "ab-core",
                    "timestamp": ts,
                    "log": line.strip()
                })

        # Log how much was read and how many errors were found.
        logger.info(
            f"Self-scan read {len(new_text)} new byte(s) from offset {start_offset} "
            f"(file size {current_size}, inode {current_inode}); "
            f"{len(formatted_logs)} new error line(s) this cycle."
        )

        # No new errors: done.
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
        # Ask the LLM which errors need an issue; stop if none do.
        actionable_errors = main.analyze_logs_for_errors(scrubbed_self_logs)
        if not actionable_errors:
            update_task_state(task_id="SelfScan", action="end")
            return

        # File an issue for each actionable error in the self-diagnosis repo.
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
            # File the issue; a 404 means the repo vanished or access was lost.
            try:
                main.create_automated_issue(gh_current, monitored_repos, repo_obj, error)
                logger.info(f"Handled self-diagnosis issue for AppBuilder: {error.get('title')}")
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

    # Log any scan failure.
    except Exception as e:
        logger.error(f"Error during self-log scan: {e}")
    # Always mark the self-scan task as finished.
    finally:
        update_task_state(task_id="SelfScan", action="end")

def _is_triage_only():
    """Return True when AppBuilder should analyse issues but not push any fix commits."""
    # Blackout always means triage only.
    if state.get("blackout"):
        return True
    # Otherwise follow the config setting.
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
    # Scheduler off: always allowed.
    if not config.get("SCHEDULER_ENABLED"):
        return {
            "allowed": True, "mode": "full", "reason": "Scheduler disabled",
            "is_work_hours": False, "is_weekend": False,
            "daily_used": state.get("daily_fixes_count", 0),
            "daily_cap": 0, "daily_budget": 0,
        }

    # Is it a weekend, and does the weekend run at full rate?
    now = datetime.now()
    is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6
    weekend_full = config.get("SCHEDULER_WEEKEND_FULL", True)

    # Work hours (not counted on full weekends).
    work_start = int(config.get("SCHEDULER_WORK_START_HOUR", 7))
    work_end   = int(config.get("SCHEDULER_WORK_END_HOUR", 18))
    # Work-hours restriction is lifted on weekends when weekend_full is on.
    is_work_hours = (work_start <= now.hour < work_end) and not (is_weekend and weekend_full)

    # Daily budget and the smaller work-hours cap.
    daily_budget  = max(1, int(config.get("SCHEDULER_DAILY_BUDGET", 50) or 50))
    work_cap_pct  = max(1, min(100, int(config.get("SCHEDULER_WORK_CAP_PCT", 25) or 25)))
    work_cap      = max(1, int(daily_budget * work_cap_pct / 100))

    # Reset daily counter if it's a new day.
    today_str = now.strftime("%Y-%m-%d")
    if state.get("daily_budget_date") != today_str:
        state["daily_budget_date"] = today_str
        state["daily_fixes_count"] = 0

    # Today's fix count and the cap that applies now.
    daily_used  = state.get("daily_fixes_count", 0)
    current_cap = work_cap if is_work_hours else daily_budget

    # Daily budget used up: pause.
    if daily_used >= daily_budget:
        mode = "paused_budget"
        state["scheduler_mode"] = mode
        return {
            "allowed": False, "mode": mode,
            "reason": f"Daily budget reached ({daily_used}/{daily_budget} issues today)",
            "is_work_hours": is_work_hours, "is_weekend": is_weekend,
            "daily_used": daily_used, "daily_cap": current_cap, "daily_budget": daily_budget,
        }

    # Work-hours cap used up: pause until after hours.
    if is_work_hours and daily_used >= work_cap:
        mode = "paused_work_cap"
        state["scheduler_mode"] = mode
        return {
            "allowed": False, "mode": mode,
            "reason": f"Work-hours cap reached ({daily_used}/{work_cap} — {work_cap_pct}% of {daily_budget} daily budget)",
            "is_work_hours": is_work_hours, "is_weekend": is_weekend,
            "daily_used": daily_used, "daily_cap": current_cap, "daily_budget": daily_budget,
        }

    # Otherwise allowed, restricted during work hours.
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
    # Reload .env and config, mark the cycle as started and load processed issues.
    try:
        load_dotenv(override=True)
        config = load_config()

        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["status"] = "Scanning"
        processed = load_processed()

        # A GitHub token is required.
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            raise Exception("Configuration Pending: Please enter your GitHub Token in Settings")
        # Log in to GitHub; a 401 means the token is bad.
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

        # The repos to scan.
        monitored_repos = main.get_monitored_repos(config)
        if not monitored_repos:
            logger.warning("No monitored repositories configured. Skipping scan.")

        # Re-discover repo labels when missing, stale or the repo list changed.
        if (not state.get("available_labels")
                or _label_cache["repos"] != sorted(monitored_repos)
                or time.time() - _label_cache["at"] >= LABEL_DISCOVERY_TTL_S):
            update_task_state(task_id="Discovery", task_name="Discovering Labels", action="start")
            state["available_labels"] = main.discover_labels(gh_current, monitored_repos)
            _label_cache.update(at=time.time(), repos=sorted(monitored_repos))
            logger.info(f"Discovered {len(state['available_labels'])} unique labels across monitored repos.")
            update_task_state(task_id="Discovery", action="end")

        # Check whether fixes already shipped work in production.
        main.verify_production_fixes(gh_current, processed)

        # Self-log scan is ON by default (self-diagnosis: scan AppBuilder's own
        # logs for errors + file them in self_diagnosis_repo). The Settings
        # "Self-monitor AppBuilder logs" toggle turns it OFF so AppBuilder stops
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

        # Scan hub logs, repo issues, open PRs and feature requests in parallel.
        state["status"] = "Scanning"
        from pr_review import scan_open_prs  # PR pre-review — gated by pr_review_enabled (default off)
        from feature_drive import scan_feature_requests  # feature auto-drive — gated by feature_drive_enabled (default off)
        # During a GitHub Actions outage every push lands a PR whose checks can
        # never run, so back off the work that commits, opens PRs or merges.
        # Hub-log scanning (files issues only) keeps going.
        import gh_actions_health
        gh_down, gh_why = gh_actions_health.outage(config)
        state["gh_backoff"] = gh_why if gh_down else ""
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(main.scan_hub_logs, Github(token), config)]
            if not gh_down:
                futures += [
                    executor.submit(scan_repo_issues, Github(token), config, processed),
                    executor.submit(scan_open_prs, Github(token), config),
                    executor.submit(scan_feature_requests, Github(token), config),
                ]
            for future in futures:
                future.result()
        # Performance/health review of the hub (interval-gated, default on).
        try:
            from health_check import scan_health
            scan_health(config)
        except Exception as _he:  # noqa: BLE001
            logger.debug(f"health check skipped: {_he}")
        # Cycle done.
        state["status"] = ("Backing off: %s" % gh_why) if gh_down else "Idle"
        state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Failure: log it, show it, and close any task still marked running.
    except Exception as e:
        logger.exception(f"Critical poller error: {e}")
        state["status"] = f"Error: {str(e)}"
        for cleanup_id in ("Discovery", "HubScan", "RepoScan", "SelfScan"):
            try:
                update_task_state(task_id=cleanup_id, action="end")
            except Exception as cleanup_err:
                logger.debug(f"Cleanup for {cleanup_id} failed: {cleanup_err}")

# Captured at import (≈ process boot / post-restart). Used for the startup grace so
# AppBuilder doesn't hammer a not-yet-ready ollama (404 /api/chat) right after a reboot.
_BOOT_TIME = time.time()


def _startup_grace_remaining():
    """Seconds left in the post-boot grace window (0 once elapsed / disabled). During
    it, LLM-dependent work is deferred so ollama + services can finish coming up.
    Configurable via startup_grace_seconds (default 300); 0 disables."""
    # Read the grace length (default 300s); 0 or less disables it.
    try:
        grace = int(load_config().get("startup_grace_seconds", 300))
    except (TypeError, ValueError):
        grace = 300
    if grace <= 0:
        return 0
    # Time left since boot.
    remaining = grace - (time.time() - _BOOT_TIME)
    return int(remaining) if remaining > 0 else 0


def _ollama_answers(base, timeout=4):
    """True if the ollama server responds on /api/tags right now."""
    try:
        # Any answer under 300 counts as up.
        return requests.get(f"{base}/api/tags", timeout=timeout).status_code < 300
    except Exception:  # noqa: BLE001 — not up yet
        return None


def preload_ollama_models(config):
    """Load each local ensemble model into ollama memory (with keep_alive) so the first
    fix doesn't pay the cold-load cost and mid-ensemble switches don't reload from disk.
    Loads the SAME set the ensemble uses (P1 local, min-size filtered) at the configured
    num_ctx. Best-effort. Needs OLLAMA_MAX_LOADED_MODELS>=N on the ollama server for all
    N to stay resident at once."""
    # Imported here to avoid import cycles at load time.
    from llm_client import _get_provider_config, _is_ollama
    from ollama_setup import _ollama_models_detailed  # lives in ollama_setup, not llm_client
    import model_registry
    # Only applies when slot 1 is an ollama provider.
    provider, _k, _m, url = _get_provider_config(1, config)
    if not _is_ollama(provider):
        return
    # The models to preload; none means nothing to do.
    base = _ollama_base_url(provider, url).rstrip("/")
    names = model_registry.local_models_for_preload(_ollama_models_detailed(base), config)
    if not names:
        return
    # Context size and keep-alive to load them with.
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
    # Load each model with one generate call, logging how long it took.
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
        # Not ollama: just wait out the grace.
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
    """Run scan cycles forever, waiting for start-up grace and model preload first."""
    global state
    _gate_t0 = [None]   # first time we started waiting on model residency
    # Each loop: wait out the grace, then the model gate, then scan and sleep.
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
            # How long to wait for preload at most.
            try:
                _cap = max(60, int(cfg.get("model_gate_max_wait_s",
                                           cfg.get("ollama_preload_timeout_s", 3600)) or 3600))
            except (TypeError, ValueError):
                _cap = 3600
            # Start the clock on the first wait; keep waiting while under the cap.
            if _gate_t0[0] is None:
                _gate_t0[0] = time.time()
            _waited = time.time() - _gate_t0[0]
            if _waited < _cap:
                logger.info("Holding scans: waiting for ensemble model(s) to become resident "
                            "(%.0fs elapsed, cap %ds).", _waited, _cap)
                time.sleep(10)
                continue
            # Waited too long: carry on anyway.
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
            # Poll interval: the work-hours setting if set, else the normal one.
            if sched.get("is_work_hours") and cfg.get("SCHEDULER_WORK_POLL_INTERVAL"):
                interval = int(cfg.get("SCHEDULER_WORK_POLL_INTERVAL") or 3600)
            else:
                interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 3600))
        # Paused: skip the scan.
        else:
            logger.debug("Poller worker is paused. Skipping scan cycle.")
            interval = int(cfg.get("POLL_INTERVAL_SECONDS") or os.getenv("POLL_INTERVAL_SECONDS", 3600))
        time.sleep(interval)


def promote_scheduler_worker():
    """Open promotion PRs on a fixed cadence (dev->qa and, if enabled, qa->main).

    Off by default. When ``promote_schedule_enabled`` is set, this wakes every
    ``promote_schedule_check_interval_min`` minutes and asks promote_ops which
    routes are due (per their configured interval-in-hours); each due route is
    dispatched exactly like the WebUI promotion buttons — it OPENS a PR into the
    next branch and never merges. AppBuilder's own pre-review then runs on that
    PR, which is the "run the changes through AB" step. See promote_ops for the
    decision logic and the shared dispatch."""
    # Imported here because only this worker needs timezone-aware times.
    from datetime import datetime, timezone
    # Each loop: reload config, open any due promotion PRs, then sleep.
    while True:
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001 — a bad config read must not kill the loop
            logger.error("promote scheduler: could not load config: %s", e)
            time.sleep(1800)
            continue
        # How often to check, in minutes (at least 1).
        interval_min = max(1, int(cfg.get("promote_schedule_check_interval_min", 30) or 30))
        if cfg.get("promote_schedule_enabled"):
            try:
                from promote_ops import run_scheduled_promotions
                summary = run_scheduled_promotions(cfg, now=datetime.now(timezone.utc))
                if summary.get("dispatched"):
                    logger.warning("promote scheduler: opened %d promotion PR dispatch(es) this cycle",
                                   summary["dispatched"])
            except Exception as e:  # noqa: BLE001 — never let a cycle crash the worker
                logger.error("promote scheduler: cycle failed: %s", e)
        time.sleep(interval_min * 60)


def _model_fetch_reason(e):
    """Reduce a requests/HTTP exception to a short, UI-displayable reason."""
    # HTTPError from raise_for_status() carries the response object.
    resp = getattr(e, "response", None)
    if resp is not None and getattr(resp, "status_code", None):
        return f"HTTP {resp.status_code}"
    # Otherwise classify by the error text.
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
    # Normalise the provider name (default openai).
    p = (provider or "openai").lower().strip()
    # claude_cli needs no API key — return the current Claude model roster.
    if p == "claude_cli":
        return {"models": [
            {"name": "claude-opus-5",             "details": "Claude Opus 5"},
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
        # Send the key as a bearer token if one is set.
        headers = {}
        if api_key:
            clean = api_key.strip().replace("Bearer ", "").strip()
            headers["Authorization"] = f"Bearer {clean}"
        # List the pulled models, trimming bf16 names and using the family as details.
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
    # Every other provider needs a key.
    if not api_key:
        return {"models": [], "error": ""}
    # Results, any error, and the URL tried.
    models = []
    error = ""
    attempted = ""
    # Call the provider's model-list endpoint and keep usable models.
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
    # Failure: report the reason with the URL tried.
    except Exception as e:
        error = f"{attempted} — {_model_fetch_reason(e)}" if attempted else _model_fetch_reason(e)
        logger.warning(f"Model fetch failed for provider {p!r}: {error} [{type(e).__name__}]")
    return {"models": models, "error": error}







def _diag_origin_head():
    """Best-effort origin HEAD from locally-cached remote refs (no network fetch)."""
    try:
        # Use the tracked branch name, else main.
        repo = git.Repo(os.getcwd())
        branch = "main"
        try:
            branch = repo.active_branch.tracking_branch().name.split("/")[-1]
        except Exception:
            pass
        # Read origin's commit from local refs.
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
        # Use the tracked branch name, else main.
        repo = git.Repo(os.getcwd())
        branch = "main"
        try:
            branch = repo.active_branch.tracking_branch().name.split("/")[-1]
        except Exception:
            pass
        # Read VERSION at origin's head from local refs.
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
# override + a context-tuned derived model), wires it into AppBuilder provider
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
# Export every name defined here except the ones above.
__all__ = [__n for __n in dir() if not __n.startswith("__") and __n not in __EXCLUDE]
