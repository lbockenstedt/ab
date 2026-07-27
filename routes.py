"""FastAPI HTTP routes exposed via an APIRouter, included by main.app (extracted from main.py)."""
import asyncio, git, json, os, re, threading, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from github import Github
from app_state import mark_pr_approved
from fastapi import APIRouter

router = APIRouter()

from main import (
    CHAT_CONFIG_DEFAULTS,
    CONFIG_DIR,
    ENV_FILE,
    STARTUP_STAMP_FILE,
    _PROVIDER_CREDIT_CB,
    _PROVIDER_CREDIT_CB_LOCK,
    _apply_closed_label,
    _chat_lock,
    _diag_origin_head,
    _diag_origin_version,
    _fetch_models_for_provider,
    _get_hub_agent_client,
    _get_provider_config,
    _get_provider_rpm,
    _log_restart_event,
    _persist_config_key,
    _provider_configured,
    _provider_credit_cb_snapshot,
    _reset_llm_semaphore,
    _schedule_check,
    _start_hub_agent,
    _task_state_lock,
    _trigger_spoke_updates,
    app,
    append_chat_message,
    check_for_updates,
    clean_repo_name,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_hub_logs,
    get_log_path,
    get_version,
    load_chats,
    load_config,
    load_processed,
    recompute_issue_counters,
    load_update_state,
    logger,
    parse_module_repo_map,
    process_single_issue,
    rename_conversation,
    run_chat_reply,
    run_local_llm_setup,
    run_local_llm_pull,
    _ollama_models_detailed,
    _ollama_delete,
    OLLAMA_BASE_URL,
    run_scan_cycle,
    save_chats,
    save_config,
    save_processed,
    set_active_chat,
    state,
    templates,
    update_task_state,
    validate_llm_config_on_startup,
)


@router.get("/api/hub-agent/status")
async def hub_agent_status():
    """Current Hub agent connection/approval status + config (for the Settings UI badge)."""
    cfg = load_config()
    return JSONResponse({
        "status": state.get("hub_agent_status", "not_registered"),
        "message": state.get("hub_agent_message", ""),
        "last_seen": state.get("hub_agent_last_seen", ""),
        "hub_ws_url": (cfg.get("HUB_WS_URL") or "").strip(),
        "hub_agent_id": (cfg.get("HUB_AGENT_ID") or "bugfixer").strip(),
        "has_secret": bool((cfg.get("HUB_AGENT_SECRET") or "").strip()),
        "has_hub_secret": bool((cfg.get("HUB_SECRET") or "").strip()),
    })


@router.post("/api/hub-agent/reregister")
async def hub_agent_reregister():
    """Force re-onboarding: clear the stored session secret and restart the agent.

    Used after a key revoke/re-approval, or to re-trigger the pending-approval
    flow. Returns immediately; the agent reconnects zero-touch in the background.
    """
    try:
        _persist_config_key("HUB_AGENT_SECRET", "")
        client = _get_hub_agent_client()
        if client:
            try:
                client.stop()
            except Exception:
                pass
        # Brief pause so the old loop closes before we start a fresh one.
        time.sleep(1)
        _start_hub_agent()
        return JSONResponse({"status": "restarted", "message": "Hub agent re-registering (approve bugfixer in the Hub WebUI)"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.get("/api/health")
async def health_check():
    """Heartbeat endpoint for the watchdog service."""
    return {"status": "ok"}


@router.post("/api/toggle-pause")
async def toggle_pause():
    state["paused"] = not state["paused"]
    logger.info(f"BugFixer autonomous operations {'PAUSED' if state['paused'] else 'RESUMED'}")
    return {"status": "success", "paused": state["paused"]}


@router.post("/api/toggle-blackout")
async def toggle_blackout():
    state["blackout"] = not state.get("blackout", False)
    logger.info(f"BugFixer blackout mode {'ON (triage only)' if state['blackout'] else 'OFF (fixes resumed)'}")
    return {"status": "success", "blackout": state["blackout"]}


@router.post("/api/pr-review/approve")
async def pr_review_approve(request: Request):
    """Human 'Approve' for a pre-reviewed PR (from the PRs Reviewed list). Adds a
    'bugfixer-approved' label + an approval comment and flags it in state. Does
    NOT merge — the human merges/pulls after. Only this endpoint (a human click)
    approves; BugFixer never auto-approves."""
    try:
        data = await request.json()
        repo_name = (data.get("repo") or "").strip()
        number = int(data.get("number"))
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "repo + number required"})
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No GitHub token configured"})
    try:
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(number)
        label = "bugfixer-approved"
        try:
            repo.get_label(label)
        except Exception:
            try:
                repo.create_label(label, "0E8A16")
            except Exception:
                pass
        try:
            pr.add_to_labels(label)
        except Exception:
            pass
        try:
            pr.create_issue_comment("✅ **Approved** via BugFixer (human review). Cleared to merge/pull.")
        except Exception:
            pass
        mark_pr_approved(repo_name, number, True)
        logger.info("pr_review: %s #%s APPROVED via UI", repo_name, number)
        return {"status": "success", "repo": repo_name, "number": number}
    except Exception as e:  # noqa: BLE001
        logger.error("pr_review approve failed for %s#%s: %s", repo_name, number, e)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.get("/")
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


@router.get("/api/task-details")
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


@router.get("/api/models")
async def get_models():
    """Fetches available models from both configured LLM providers."""
    config = load_config()
    p1_provider, p1_key, _, p1_url = _get_provider_config(1, config)
    p2_provider, p2_key, _, p2_url = _get_provider_config(2, config)
    p1 = _fetch_models_for_provider(p1_provider, p1_key, p1_url)
    p2 = _fetch_models_for_provider(p2_provider, p2_key, p2_url)
    return {
        "local_models": p1["models"],
        "cloud_models": p2["models"],
        "local_error": p1["error"],
        "cloud_error": p2["error"],
        "enabled_models": config.get("enabled_models", []),
    }


@router.post("/api/fetch-models")
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

        result = _fetch_models_for_provider(provider, api_key, base_url)
        return {"models": result["models"], "error": result["error"]}
    except Exception as e:
        logger.error(f"fetch-models error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "models": []})


@router.get("/api/scheduler/status")
async def scheduler_status():
    config = load_config()
    return _schedule_check(config)


def _hub_connection_diag():
    """Hub-connection + mTLS cert state from the running HubAgentClient (module
    global), so the Diagnostics page can show whether log/update access works and
    whether the WebUI is still self-signed — the cert data we otherwise SSH for."""
    try:
        import hub_agent
        client = hub_agent.hub_agent_client
        if client is None:
            return {"available": False, "reason": "hub agent not started"}
        return client.cert_diagnostics()
    except Exception as e:  # noqa: BLE001 - diagnostics must never 500
        return {"available": False, "error": str(e)}


def _system_stats():
    """Live host + process + LLM telemetry for the Diagnostics page. Never raises —
    every field degrades to None/[] so the panel can render partial data. psutil is
    used when present; load average + core count fall back to os primitives."""
    import time
    out = {"cpu": {}, "memory": {}, "disk": {}, "processes": [], "llm": {}, "uptime": {}}
    try:
        out["cpu"]["cores"] = os.cpu_count()
    except Exception:
        pass
    try:
        la = os.getloadavg()  # 1/5/15 min; unavailable on some platforms
        out["cpu"]["load_avg"] = [round(x, 2) for x in la]
        if out["cpu"].get("cores"):
            out["cpu"]["load_pct_1m"] = round(100.0 * la[0] / out["cpu"]["cores"], 1)
    except Exception:
        pass

    try:
        import psutil
    except Exception:
        psutil = None

    if psutil is not None:
        try:
            out["cpu"]["percent"] = psutil.cpu_percent(interval=0.15)
        except Exception:
            pass
        try:
            vm = psutil.virtual_memory()
            out["memory"] = {
                "total_gb": round(vm.total / 1073741824, 1),
                "used_gb": round((vm.total - vm.available) / 1073741824, 1),
                "available_gb": round(vm.available / 1073741824, 1),
                "percent": vm.percent,
            }
        except Exception:
            pass
        try:
            du = psutil.disk_usage(os.getcwd())
            out["disk"] = {
                "total_gb": round(du.total / 1073741824, 1),
                "used_gb": round(du.used / 1073741824, 1),
                "free_gb": round(du.free / 1073741824, 1),
                "percent": du.percent,
            }
        except Exception:
            pass
        try:
            boot = psutil.boot_time()
            out["uptime"]["host_seconds"] = int(time.time() - boot)
        except Exception:
            pass
        # Processes of interest: this app + any ollama/llm runtimes.
        try:
            me = psutil.Process().pid
            procs = []
            for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_percent", "create_time"]):
                try:
                    nm = (p.info.get("name") or "").lower()
                    cmd = " ".join(p.info.get("cmdline") or []).lower()
                    is_me = p.info["pid"] == me
                    if not (is_me or "ollama" in nm or "ollama" in cmd
                            or "bugfixer" in cmd or "llama" in nm):
                        continue
                    mi = p.info.get("memory_info")
                    procs.append({
                        "pid": p.info["pid"],
                        "name": p.info.get("name"),
                        "label": "bugfixer" if is_me else (p.info.get("name") or "proc"),
                        "rss_mb": round(mi.rss / 1048576, 1) if mi else None,
                        "cpu_percent": p.info.get("cpu_percent"),
                        "self": is_me,
                    })
                except Exception:
                    continue
            out["processes"] = sorted(procs, key=lambda x: (x["rss_mb"] or 0), reverse=True)[:20]
        except Exception:
            pass

    # LLM slots — provider/model + live online + cooldown, compact for the page.
    try:
        cfg = load_config()
        slots = []
        for n in (1, 2, 3, 4):
            provider, key, model, base_url = _get_provider_config(n, cfg)
            cb = (state.get("provider_credit_cb") or {}).get(n) or {}
            slots.append({
                "slot": n,
                "tier": "P1 (CPU)" if n == 1 else f"P{n} (external)",
                "provider": provider,
                "model": model,
                "configured": _provider_configured(provider, key, model),
                "online": state.get(f"provider_{n}_online", False),
                "cooldown_active": bool(cb.get("active")),
                "cooldown_remaining_min": cb.get("cooldown_remaining_min"),
                "rpm": _get_provider_rpm(n, cfg),
                "last_result": (state.get("provider_last_result") or {}).get(n),
            })
        out["llm"] = {
            "slots": slots,
            "circuit_breaker": state.get("llm_circuit_breaker"),
            "active_llm": state.get("active_llm"),
            "daily_fixes_count": state.get("daily_fixes_count"),
            "local_ensemble": bool(cfg.get("local_ensemble")),
            "crosscheck_target": cfg.get("CPU_CROSSCHECK_TARGET"),
        }
    except Exception:
        pass

    # BugFixer process uptime from the startup stamp.
    try:
        with open(STARTUP_STAMP_FILE, "r") as f:
            started = json.load(f).get("started_at")
        if started:
            out["uptime"]["started_at"] = started
    except Exception:
        pass
    return out


@router.get("/api/system-stats")
async def system_stats():
    """Host/process/LLM telemetry for the Diagnostics → System panel."""
    return _system_stats()


@router.get("/api/diagnostics")
async def diagnostics():
    """Surfaces running-vs-disk-vs-origin versions, stale-code state, per-provider
    status (including the previously-silent skip reasons), and update/restart state,
    so the user can see what is wrong from the UI instead of reading CLI logs."""
    config = load_config()

    # Startup stamp — which commit this process booted on.
    running_commit, running_version, started_at, pid, main_mtime = None, None, None, None, None
    try:
        with open(STARTUP_STAMP_FILE, "r") as f:
            stamp = json.load(f)
        running_commit = stamp.get("commit")
        if running_commit == "unknown":
            running_commit = None
        running_version = stamp.get("version")
        started_at = stamp.get("started_at")
        pid = stamp.get("pid")
        main_mtime = stamp.get("main_mtime")
    except Exception:
        pass

    # On-disk HEAD.
    disk_commit = None
    try:
        disk_commit = git.Repo(os.getcwd()).head.commit.hexsha
    except Exception:
        pass

    origin_commit = _diag_origin_head()

    update_state = load_update_state()
    update_pending_exists = os.path.exists(os.path.join(CONFIG_DIR, "update_pending"))

    # Resolve the last-known-good commit's VERSION so the UI can show a
    # version label instead of a raw commit SHA.
    lkg_commit = update_state.get("last_known_good_commit")
    lkg_version = None
    if lkg_commit:
        try:
            lkg_version = git.Repo(os.getcwd()).git.show(f"{lkg_commit}:VERSION").strip() or None
        except Exception:
            lkg_version = None

    providers = []
    for n in (1, 2, 3, 4):
        provider, key, model, _ = _get_provider_config(n, config)
        cb = (state.get("provider_credit_cb") or {}).get(n) or {}
        providers.append({
            "n": n,
            "provider": provider,
            "model": model,
            "configured": _provider_configured(provider, key, model),
            "online": state.get(f"provider_{n}_online", False),
            "cooldown_active": bool(cb.get("active")),
            "cooldown_remaining_min": cb.get("cooldown_remaining_min"),
            "cooldown_cause": cb.get("cause"),
            "last_result": (state.get("provider_last_result") or {}).get(n),
            "rpm": _get_provider_rpm(n, config),
        })

    return {
        "versions": {
            "running": running_commit,
            "disk": disk_commit,
            "origin": origin_commit,
            "label": get_version(),
            "running_version": running_version,
            "disk_version": get_version(),
            "origin_version": _diag_origin_version(),
            "stale": bool(disk_commit and running_commit and disk_commit != running_commit),
        },
        "process": {"pid": pid, "started_at": started_at, "main_mtime": main_mtime},
        "update": {
            "pending": update_pending_exists,
            "restart_pending": bool(state.get("restart_pending")),
            "last_known_good_commit": update_state.get("last_known_good_commit"),
            "last_known_good_version": lkg_version,
            "failed_commits": update_state.get("failed_commits", []),
            "restart_log": state.get("restart_log", []),
        },
        "providers": providers,
        "watchdog_signal": update_pending_exists,
        "hub_connection": _hub_connection_diag(),
        "bug_ingest": state.get("bug_ingest", {}),
        "feature_ingest": state.get("feature_ingest", {}),
        "heartbeat": {
            "agent_status": state.get("hub_agent_status", "not_registered"),
            "approved": state.get("hub_agent_status") == "approved",
            "approved_at": state.get("hub_agent_approved_at", ""),
            "suppressed": bool(state.get("heartbeat_suppression")),
            "suppression_reason": (state.get("heartbeat_suppression") or {}).get("reason"),
            "suppression_at": (state.get("heartbeat_suppression") or {}).get("at"),
        },
    }


@router.get("/api/claude-cli/status")
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


@router.post("/api/claude-cli/auth/start")
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


@router.get("/api/claude-cli/auth/poll")
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


@router.post("/api/claude-cli/auth/submit-code")
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


@router.post("/api/toggle-model")
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


@router.get("/logs")
async def get_logs(request: Request):
    logs = ""
    log_rows = []
    try:
        current_log = get_log_path()
        with open(current_log, "r") as f:
            lines = f.readlines()
        tail = [l.rstrip("\n") for l in lines[-100:]]
        logs = "\n".join(reversed(tail))  # raw string kept for the Copy button
        # Structured rows (newest first) so the Logs view renders in the SAME
        # Component | Timestamp | Message table as Hub Logs. Parse "TS - COMPONENT
        # - LEVEL - msg"; a line without that shape (traceback continuation) gets
        # a blank component and shows verbatim.
        for line in reversed(tail):
            if not line.strip():
                continue
            parts = line.split(" - ", 2)
            module = parts[1].strip() if len(parts) >= 3 and line[:4].isdigit() else ""
            log_rows.append({"module": module, "log": line})
    except Exception as e:
        logs = f"Error reading logs from {get_log_path()}: {e}"
        log_rows = [{"module": "", "log": logs}]
    return templates.TemplateResponse(request=request, name="index.html",
                                      context={"view": "logs", "logs": logs,
                                               "log_rows": log_rows, "state": state})


@router.get("/hub-logs")
async def get_hub_logs_page(request: Request):
    config = load_config()
    hub_url = (config.get("HUB_QUERY_URL") or "").strip()
    fetch_error = None
    fetch_status = None
    # Sync model: the Hub Logs page reads the LOCAL mirror only — no live
    # GET_LOGS pull on every page view. The poller's scan_hub_logs →
    # sync_hub_logs refreshes the mirror once per cycle; this page just shows
    # the latest synced snapshot. Connectivity is reflected by whether the
    # mirror has recent data (and the Diagnostics card's hub status dot),
    # not by a per-view live probe.
    logs = get_hub_logs()
    if logs:
        fetch_status = 200
    else:
        fetch_error = ("No synced logs yet — waiting for the first scan cycle. "
                       "If this persists, confirm bugfixer is approved+connected "
                       "in the Hub WebUI (Setup → Spokes).")
    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"view": "hub-logs", "hub_logs": logs, "state": state,
                 "hub_fetch_time": fetch_time, "hub_fetch_error": fetch_error,
                 "hub_fetch_status": fetch_status, "hub_url": hub_url},
    )


@router.get("/api/hub-logs/raw")
async def hub_logs_raw():
    """Return the raw Hub logs (via the authenticated agent) for debugging."""
    client = _get_hub_agent_client()
    if not client:
        return JSONResponse({"error": "Hub agent not configured"}, status_code=400)
    result = client.request_sync("GET_LOGS", {}, timeout=20)
    if not isinstance(result, dict):
        return JSONResponse({"error": "Hub agent not approved/connected"}, status_code=503)
    return JSONResponse({
        "status_code": 200,
        "content_type": "application/json",
        "body_preview": json.dumps(result)[:5000],
        "body_length": len(json.dumps(result)),
        "logs": result.get("logs", []),
    })


_LOG_ANALYSIS_LOCK = threading.Lock()
_LOG_ANALYSIS_TASK = "LogAnalysis"
_LOG_ANALYSIS_MAX_CHARS = 16000
_LOG_ANALYSIS_WINDOW_DEFAULT = 30      # default window (min) — configurable via Settings


def _log_analysis_window_min():
    """Configured log-analysis window / precompute interval in minutes (Settings →
    log_analysis_interval_min; default 30). Governs both how far back the LLM looks AND
    how often the idle precompute runs."""
    try:
        return max(1, int(load_config().get("log_analysis_interval_min", _LOG_ANALYSIS_WINDOW_DEFAULT)))
    except (TypeError, ValueError):
        return _LOG_ANALYSIS_WINDOW_DEFAULT


def _parse_log_ts(line):
    """Parse the leading 'YYYY-MM-DD HH:MM:SS' of a log line, or None."""
    try:
        return datetime.strptime((line or "")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _window_lines_since(lines, minutes, get_text=lambda x: x, newest_first=False):
    """Keep only lines within the last `minutes`. `lines` may be strings or dicts
    (via get_text). Chronological (oldest-first) by default; set newest_first for
    a top-down list (e.g. hub rows). Continuation lines with no timestamp inherit
    the current keep state. Empty result → caller should fall back to a line tail."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(minutes=minutes)
    out = []
    if newest_first:
        for item in lines:
            ts = _parse_log_ts(get_text(item))
            if ts is not None and ts < cutoff:
                break
            out.append(item)
        out.reverse()
    else:
        keeping = False
        for item in lines:
            ts = _parse_log_ts(get_text(item))
            if ts is not None:
                keeping = ts >= cutoff
            if keeping:
                out.append(item)
    return out


def _collect_logs_for_analysis(source, window_minutes=None):
    """Return (title, text) of recent logs for `source` ('self' | 'hub'). With
    window_minutes, restrict to that trailing time window (falling back to a line
    tail if too little is in-window); else the last lines. Char-capped for the LLM."""
    if source == "hub":
        rows = get_hub_logs() or []
        if window_minutes:
            win = _window_lines_since(rows, window_minutes,
                                      get_text=lambda r: r.get("log", "") if isinstance(r, dict) else str(r),
                                      newest_first=True)
            rows = win if len(win) >= 3 else rows[:400]
        else:
            rows = rows[:600]
        lines = [f"[{r.get('module', '?')}] {r.get('log', '')}" if isinstance(r, dict) else str(r) for r in rows]
        text = "\n".join(lines)
        title = f"Hub logs (last {window_minutes} min)" if window_minutes else "Hub logs (mirrored)"
    else:
        try:
            with open(get_log_path(), "r") as f:
                all_lines = f.readlines()
        except Exception as e:  # noqa: BLE001
            return "BugFixer service logs", f"(could not read BugFixer log: {e})"
        if window_minutes:
            win = _window_lines_since(all_lines, window_minutes)
            all_lines = win if len(win) >= 5 else all_lines[-400:]
        else:
            all_lines = all_lines[-400:]
        text = "".join(all_lines)
        title = f"BugFixer service logs (last {window_minutes} min)" if window_minutes else "BugFixer service logs"
    if len(text) > _LOG_ANALYSIS_MAX_CHARS:
        text = text[-_LOG_ANALYSIS_MAX_CHARS:]
    return title, text


def _run_log_analysis(source, window_minutes=None, precomputed=False):
    """Read the current logs and ask BugFixer's own LLM whether anything is wrong,
    what it means, and what to check. Streams into the LogAnalysis task (live
    'thought process') and stores the final answer in state['log_analysis']."""
    from main import analyze_logs, parse_log_verdict, is_llm_cooldown_error  # re-exported from llm_client
    title, log_text = _collect_logs_for_analysis(source, window_minutes=window_minutes)
    update_task_state(task_id=_LOG_ANALYSIS_TASK, task_name=f"Analyzing {title}", action="start")
    try:
        raw = analyze_logs(log_text, title=f"{title} for the BugFixer system",
                           task_id=_LOG_ANALYSIS_TASK)
        verdict, result = parse_log_verdict(raw)  # strip the machine VERDICT line for display
        state["log_analysis"] = {
            "running": False, "source": source, "title": title, "precomputed": precomputed,
            "verdict": verdict,
            "result": result, "error": None, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if is_llm_cooldown_error(e):
            msg = f"All LLM providers are cooling down / unavailable: {e}"
        logger.warning(f"log-analysis failed: {e}")
        state["log_analysis"] = {
            "running": False, "source": source, "title": title, "precomputed": precomputed,
            "result": "", "error": msg, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        update_task_state(task_id=_LOG_ANALYSIS_TASK, action="end")


def _log_analysis_busy():
    """True if the system is doing LLM-heavy work (a fix build/review/triage/chat) —
    the idle pre-compute must not compete with that for the single LLM slot."""
    for t in (state.get("active_tasks") or {}).values():
        name = (t.get("name") or "").lower()
        if any(k in name for k in ("build", "review", "triag", "fix attempt", "verif", "chat", "identify")):
            return True
    return False


def log_health_worker():
    """When BugFixer is idle, pre-compute a health snapshot of the last N minutes of its
    own logs so the Log Analysis panel shows a ready answer on page open. Cheap, respects
    the single LLM slot (skips while a fix/chat is running), and refreshes at most every
    N minutes — N = log_analysis_interval_min (Settings, default 30). The user's Refresh
    button (runLogAnalysis) always overrides with a live run."""
    import time as _t
    from main import _startup_grace_remaining  # re-exported from workers
    last = 0.0
    while True:
        try:
            _t.sleep(60)
            if state.get("paused") or state.get("blackout"):
                continue
            if _startup_grace_remaining():
                continue  # let ollama/services finish starting before precomputing
            window_min = _log_analysis_window_min()
            if (_t.time() - last) < window_min * 60:
                continue
            if _log_analysis_busy():
                continue
            if not _LOG_ANALYSIS_LOCK.acquire(blocking=False):
                continue
            try:
                state["log_analysis"] = {"running": True, "source": "self", "title": None,
                                         "result": "", "error": None, "at": None, "precomputed": True}
                _run_log_analysis("self", window_minutes=window_min, precomputed=True)
                last = _t.time()
            finally:
                _LOG_ANALYSIS_LOCK.release()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"log_health_worker cycle error: {e}")
            _t.sleep(30)


@router.post("/api/log-analysis/run")
async def log_analysis_run(request: Request):
    """Kick off an LLM analysis of the current logs. Body: {"source": "self"|"hub"}.
    Non-blocking — the UI polls /api/log-analysis for progress + the final result."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = "hub" if str((body or {}).get("source")) == "hub" else "self"
    if not _LOG_ANALYSIS_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "An analysis is already running."}, status_code=409)
    try:
        # Seed a running marker the UI can poll immediately.
        state["log_analysis"] = {"running": True, "source": source, "title": None,
                                 "result": "", "error": None, "at": None}

        def _worker():
            try:
                # Analyze only the recent window (same as the idle precompute), so the
                # LLM sees just the last-N-min of activity, not the whole tail.
                _run_log_analysis(source, window_minutes=_log_analysis_window_min())
            finally:
                _LOG_ANALYSIS_LOCK.release()

        threading.Thread(target=_worker, name="log-analysis", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        _LOG_ANALYSIS_LOCK.release()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "task_id": _LOG_ANALYSIS_TASK}


@router.get("/api/log-analysis")
async def log_analysis_status():
    """Current log-analysis state: running flag, the live partial (LLM tokens streamed
    so far), and the final result/error once done."""
    la = dict(state.get("log_analysis") or {"running": False, "result": "", "error": None})
    # While running, surface the live streamed tokens as the partial.
    if la.get("running"):
        task = (state.get("active_tasks") or {}).get(_LOG_ANALYSIS_TASK) or {}
        la["partial"] = task.get("stream", "")
    return la


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
    "POLL_INTERVAL_SECONDS": "3600",
    "UPDATE_API_URL": "",
    "HUB_QUERY_URL": "",
    "HUB_WS_URL": "",
    "HUB_AGENT_ID": "bugfixer",
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
    "POLL_INTERVAL_SECONDS": "3600",
    "CHAT_SYSTEM_PROMPT": "",
    "CHAT_HISTORY_WINDOW": "20",
    "LLM_LOG_MAX_ENTRIES": "200",
    "LLM_LOG_MAX_CHARS": "60000",
    "SCHEDULER_WORK_START_HOUR": "7",
    "SCHEDULER_WORK_END_HOUR": "18",
    "SCHEDULER_DAILY_BUDGET": "50",
    "SCHEDULER_WORK_CAP_PCT": "25",
    "SCHEDULER_WORK_POLL_INTERVAL": "3600",
    "SCHEDULER_CRITICAL_LABEL": "critical",
    "SCHEDULER_BUG_LABEL": "bug",
}


# Live GitHub repo list for the settings "Monitored Repositories" multi-select.
# Cached in `state` with a TTL so we don't hit GitHub on every /settings load;
# a failed/missing-token fetch returns [] and the template falls back to the
# free-text "additional repos" input alone.
_GITHUB_REPOS_TTL = 300


def _fetch_github_repos_sync(token: str) -> list:
    """Best-effort list of the configured token's accessible GitHub repos
    (``owner/repo``). Filters to non-archived repos the user can push to (where
    BugFixer could actually file/fix issues), sorted by name, capped at 200 so
    the settings page stays snappy. Returns ``[]`` on any failure."""
    if not token:
        return []
    try:
        gh = Github(token)
        repos = []
        for r in gh.get_user().get_repos(affiliation="owner,organization_member"):
            try:
                if getattr(r, "archived", False):
                    continue
                perms = getattr(r, "permissions", None)
                # Keep repos we can push to; skip read-only collaborator repos.
                if perms is not None and not getattr(perms, "push", False):
                    continue
                repos.append(r.full_name)
            except Exception:
                continue
            if len(repos) >= 200:
                break
        repos.sort(key=lambda s: s.lower())
        return repos
    except Exception as e:
        logger.warning("settings: GitHub repo list fetch failed: %s", e)
        return []


@router.get("/settings")
async def settings_page(request: Request):
    load_dotenv(override=True)
    settings = DEFAULT_ENV.copy()
    for k in DEFAULT_ENV:
        val = os.getenv(k)
        if val: settings[k] = val
    config = load_config()
    # Self-log scan defaults ON (self-diagnosis) until explicitly turned off
    # via the Settings toggle; display-only default so the checkbox renders
    # checked on a never-saved install.
    config.setdefault("self_log_scan_enabled", True)
    # PR pre-review defaults OFF (opt-in); display-only default so the checkbox renders.
    config.setdefault("pr_review_enabled", False)
    # Source knobs (default ON keeps the LM bug-fix pipeline working; the per-
    # module log grid + fix-log-detected are opt-in, default OFF, so the operator
    # enables noisy sources one at a time).
    config.setdefault("bug_reports_enabled", True)
    config.setdefault("feature_requests_enabled", True)
    config.setdefault("fix_logdetected_enabled", False)
    config.setdefault("enabled_log_modules", [])
    # Module list for the per-module log-filing grid = the operator's module→repo
    # map keys (a module must map to a repo before its logs can be filed).
    _mrm = config.get("module_repo_map") or {}
    log_module_options = sorted(_mrm.keys()) if isinstance(_mrm, dict) else []
    repo_tests = config.get("repo_tests", {})
    repo_tests_str = ", ".join([f"{k}:{v}" for k, v in repo_tests.items()])
    settings["GITHUB_TOKEN"] = config.get("GITHUB_TOKEN") or settings.get("GITHUB_TOKEN", "")
    settings["LLM_TIMEOUT"] = config.get("LLM_TIMEOUT") or settings.get("LLM_TIMEOUT", "900")
    labels = config.get("monitored_labels", ["automated-fix"])
    settings["monitored_labels_str"] = ", ".join(labels)

    # Live multi-select options: cached GitHub repo list (TTL-bounded) UNION the
    # currently-monitored repos so any repo already monitored but not in the
    # fetched list (e.g. a fork the token can't enumerate) still shows as a
    # pre-checked checkbox the user can toggle off.
    cache = state.get("github_repos_cache") or {}
    now = time.time()
    if cache and (now - cache.get("ts", 0)) < _GITHUB_REPOS_TTL:
        github_repos = cache.get("repos", []) or []
    else:
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
        github_repos = await asyncio.to_thread(_fetch_github_repos_sync, token) if token else []
        state["github_repos_cache"] = {"ts": now, "repos": github_repos}
    monitored = list(config.get("monitored_repos") or [])
    monitored_set = set(monitored)
    extra_monitored = [r for r in monitored if r not in set(github_repos)]
    repo_options = list(github_repos) + extra_monitored  # union, monitored last
    # Trusted repos use the same checkbox treatment: fetched GitHub list UNION
    # any already-trusted repos not in that list, so an external trusted repo
    # still shows as a toggleable pre-checked box.
    trusted = list(config.get("trusted_repos") or [])
    trusted_set = set(trusted)
    extra_trusted = [r for r in trusted if r not in set(github_repos)]
    trusted_options = list(github_repos) + extra_trusted

    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "settings",
        "settings": {**settings, **config, "repo_tests_str": repo_tests_str, "monitored_labels_str": settings["monitored_labels_str"]},
        "available_labels": state.get("available_labels", []),
        "repo_options": repo_options,
        "monitored_set": monitored_set,
        "trusted_options": trusted_options,
        "trusted_set": trusted_set,
        "log_module_options": log_module_options,
        "state": state,
    })


@router.get("/diagnostics")
async def diagnostics_page(request: Request):
    """Diagnostics view — versions, stale-code state, per-provider status, and
    update/restart state. Data is fetched live via /api/diagnostics by refreshDiagnostics()."""
    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "diagnostics",
        "state": state,
    })


@router.post("/save_settings")
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

    # Monitored repos now arrive as repeated checkbox values (getlist) plus an
    # optional free-text "additional repos" field for repos not in the fetched
    # GitHub list. Merge + dedup (preserve order). Handled here, NOT in the
    # `updates` dict below — dict(form_data) collapses multi-values to the last
    # checkbox, which would silently drop the rest.
    if hasattr(form_data, "getlist"):
        checked_repos = form_data.getlist("monitored_repos")
    else:
        checked_repos = [data["monitored_repos"]] if data.get("monitored_repos") else []
    extra_raw = data.get("monitored_repos_extra", "") or ""
    extra_repos = [clean_repo_name(x.strip()) for x in extra_raw.replace("\\n", ",").split(",") if x.strip()]
    monitored_repos = [clean_repo_name(x) for x in checked_repos if x and str(x).strip()]
    for r in extra_repos:
        if r and r not in monitored_repos:
            monitored_repos.append(r)
    config_data["monitored_repos"] = list(dict.fromkeys(monitored_repos))

    # Trusted repos: same checkbox + free-text treatment as monitored.
    if hasattr(form_data, "getlist"):
        checked_trusted = form_data.getlist("trusted_repos")
    else:
        checked_trusted = [data["trusted_repos"]] if data.get("trusted_repos") else []
    extra_trusted_raw = data.get("trusted_repos_extra", "") or ""
    extra_trusted = [clean_repo_name(x.strip()) for x in extra_trusted_raw.replace("\\n", ",").split(",") if x.strip()]
    trusted_repos = [clean_repo_name(x) for x in checked_trusted if x and str(x).strip()]
    for r in extra_trusted:
        if r and r not in trusted_repos:
            trusted_repos.append(r)
    config_data["trusted_repos"] = list(dict.fromkeys(trusted_repos))

    updates = {
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
        # File-a-Bug: which repo bugfixer files user-submitted WebUI bug reports
        # into (and where the fix pipeline then runs). Defaults to lbockenstedt/lm.
        "bug_report_repo": lambda v: clean_repo_name(v.strip()) if v and v.strip() else "",
        "module_repo_map": lambda v: parse_module_repo_map(v),
        # Chat-agent numeric settings (stored as strings by the form; coerce to int).
        "CHAT_TOOL_MAX_ITERATIONS": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_TOOL_MAX_ITERATIONS"],
        "CHAT_TOOL_MAX_TOKENS": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_TOOL_MAX_TOKENS"],
        "CHAT_INDEX_ISSUE_LIMIT": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_INDEX_ISSUE_LIMIT"],
        "CHAT_INDEX_CACHE_TTL": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_INDEX_CACHE_TTL"],
        "CHAT_FIX_PROPOSAL_TTL": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_FIX_PROPOSAL_TTL"],
        "FIX_MAX_FILES": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_FILES"],
        "FIX_MAX_FILE_CHARS": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_FILE_CHARS"],
        "FIX_MAX_CONTEXT_CHARS": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_CONTEXT_CHARS"],
        "FIX_MAX_OUTPUT_TOKENS": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_OUTPUT_TOKENS"],
        "HEARTBEAT_STALE_S": lambda v: max(30, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["HEARTBEAT_STALE_S"],
        "heartbeat_exclude": lambda v: [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else ([s.strip() for s in str(v).replace(",", "\n").splitlines() if s.strip()] if v else []),
        "CHAT_SYSTEM_PROMPT": lambda v: v.strip() if v else "",
        "CHAT_HISTORY_WINDOW": lambda v: int(v) if str(v).strip().isdigit() else 20,
        "LLM_LOG_MAX_ENTRIES": lambda v: int(v) if str(v).strip().isdigit() else 200,
        "LLM_LOG_MAX_CHARS": lambda v: int(v) if str(v).strip().isdigit() else 60000,
        "SCHEDULER_WORK_START_HOUR": lambda v: int(v) if str(v).strip().isdigit() else 7,
        "SCHEDULER_WORK_END_HOUR": lambda v: int(v) if str(v).strip().isdigit() else 18,
        "SCHEDULER_DAILY_BUDGET": lambda v: int(v) if str(v).strip().isdigit() else 50,
        "SCHEDULER_WORK_CAP_PCT": lambda v: int(v) if str(v).strip().isdigit() else 25,
        "SCHEDULER_WORK_POLL_INTERVAL": lambda v: int(v) if str(v).strip().isdigit() else 3600,
        "SCHEDULER_CRITICAL_LABEL": lambda v: v.strip() if v else "critical",
        "SCHEDULER_BUG_LABEL": lambda v: v.strip() if v else "bug",
        "QA_API_URL": lambda v: v.strip() if v else "",
        "QA_REPO": lambda v: v.strip() if v else "",
        "QA_TEST_COMMAND": lambda v: v.strip() if v else "pytest",
        "HUB_QUERY_URL": lambda v: v.strip() if v else "",
        "HUB_WS_URL": lambda v: v.strip() if v else "",
        "HUB_AGENT_ID": lambda v: v.strip() if v else "bugfixer",
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
    # PR pre-review toggle (default OFF): comment parity/drift findings on open PRs.
    config_data["pr_review_enabled"] = data.get("pr_review_enabled") == "on"
    # File-a-Bug toggle (defaults on so the footer button works out of the box).
    config_data["bug_report_enabled"] = data.get("bug_report_enabled") != "off"
    config_data["qa_enabled"] = data.get("qa_enabled") == "on"
    config_data["skip_review"] = data.get("skip_review") == "on"
    config_data["local_ensemble"] = data.get("local_ensemble") == "on"
    config_data["ensemble_skip_external_at_full"] = data.get("ensemble_skip_external_at_full") == "on"
    _cct = str(data.get("CPU_CROSSCHECK_TARGET") or "").strip()
    try:
        config_data["CPU_CROSSCHECK_TARGET"] = max(0.5, min(1.0, float(_cct))) if _cct else 0.90
    except (TypeError, ValueError):
        config_data["CPU_CROSSCHECK_TARGET"] = 0.90
    # Log Analysis window / idle-precompute interval in minutes (default 30). Governs
    # both how far back the LLM looks and how often the idle snapshot refreshes.
    _lai = str(data.get("log_analysis_interval_min") or "").strip()
    try:
        config_data["log_analysis_interval_min"] = max(1, int(_lai)) if _lai else 30
    except (TypeError, ValueError):
        config_data["log_analysis_interval_min"] = 30
    # Minimum ensemble model size in billions of params (0 = use all). Set e.g. 14 to
    # skip the 7b/8b rungs that reliably whiff, while those models stay available for
    # the CPU slot / chat / cross-check elsewhere.
    _emm = str(data.get("ensemble_min_model_b") or "").strip()
    try:
        config_data["ensemble_min_model_b"] = max(0, int(float(_emm))) if _emm else 0
    except (TypeError, ValueError):
        config_data["ensemble_min_model_b"] = 0
    # Post-boot grace (seconds) before BugFixer runs LLM/scan work — lets ollama +
    # services finish starting after a reboot so it doesn't 404 on /api/chat. 0 = off.
    _sg = str(data.get("startup_grace_seconds") or "").strip()
    try:
        config_data["startup_grace_seconds"] = max(0, int(_sg)) if _sg else 300
    except (TypeError, ValueError):
        config_data["startup_grace_seconds"] = 300
    # Ollama context window (num_ctx). Default 16384 so fix/log prompts don't 400 with
    # "prompt is longer than the context length". Raise for very large prompts.
    _nc = str(data.get("ollama_num_ctx") or "").strip()
    try:
        config_data["ollama_num_ctx"] = max(2048, int(_nc)) if _nc else 32768
    except (TypeError, ValueError):
        config_data["ollama_num_ctx"] = 32768
    # Ollama CPU threads (num_thread). 0 = ollama default (~physical cores). Raise on a
    # CPU box to speed the big models — set to your allocated physical core count.
    _nt = str(data.get("ollama_num_thread") or "").strip()
    try:
        config_data["ollama_num_thread"] = max(0, int(_nt)) if _nt else 0
    except (TypeError, ValueError):
        config_data["ollama_num_thread"] = 0
    # Keep ollama models resident (keep_alive; -1 = forever) + preload them at startup so
    # the ensemble doesn't reload big models from disk on every switch.
    config_data["ollama_keep_alive"] = (str(data.get("ollama_keep_alive") or "").strip() or "-1")
    config_data["ollama_preload_models"] = data.get("ollama_preload_models") != "off"
    # OLLAMA_MAX_LOADED_MODELS — how many models the ollama SERVER keeps resident at once.
    # Written to ollama's systemd env by Local LLM Setup (set >= ensemble size to hold all
    # models loaded). Server-side setting, not per-request.
    _ml = str(data.get("ollama_max_loaded_models") or "").strip()
    try:
        config_data["ollama_max_loaded_models"] = max(1, int(_ml)) if _ml else 3
    except (TypeError, ValueError):
        config_data["ollama_max_loaded_models"] = 3
    # Heartbeat triage files issues for modules with a missing/stale heartbeat
    # but NO error in the logs. Off by default (error-log-only filing); opt-in
    # if you want dead-module detection independent of the error log.
    config_data["heartbeat_triage_enabled"] = data.get("heartbeat_triage_enabled") == "on"
    # ── Source noise-control knobs ────────────────────────────────────────────
    # BugFixes from LM (default ON — keeps the bug-fix pipeline working).
    config_data["bug_reports_enabled"] = data.get("bug_reports_enabled") != "off"
    # Feature Requests from LM (default ON; independently toggleable).
    config_data["feature_requests_enabled"] = data.get("feature_requests_enabled") != "off"
    # Auto-FIX log-detected / automated-fix issues (default OFF; Bug + Critical
    # always fix). Stops the fixer churning on log-scraped issues.
    config_data["fix_logdetected_enabled"] = data.get("fix_logdetected_enabled") == "on"
    # Per-module hub-log auto-filing: only CHECKED modules have their errors
    # filed as issues. Empty = OFF for every module (the default) → enable one at
    # a time. getlist so repeated checkbox values aren't collapsed by dict(form_data).
    if hasattr(form_data, "getlist"):
        config_data["enabled_log_modules"] = list(dict.fromkeys(form_data.getlist("enabled_log_modules")))
    else:
        _elm = form_data.get("enabled_log_modules", [])
        config_data["enabled_log_modules"] = _elm if isinstance(_elm, list) else ([_elm] if _elm else [])
    # Self-log scan: scan BugFixer's OWN logs for internal errors + file them
    # as GitHub issues in self_diagnosis_repo. On by default (self-diagnosis);
    # turn OFF to stop BugFixer from monitoring/filing its own logs.
    config_data["self_log_scan_enabled"] = data.get("self_log_scan_enabled") == "on"
    config_data["CHAT_TOOLS_ENABLED"] = data.get("CHAT_TOOLS_ENABLED") == "on"
    _cs = str(data.get("chat_slot") or "").strip()
    config_data["chat_slot"] = int(_cs) if _cs in ("1", "2", "3", "4") else ""
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

    _reset_llm_semaphore()

    try:
        validate_llm_config_on_startup()
    except Exception as ve:
        logger.warning(f"Post-save LLM validation failed (non-fatal): {ve}")

    # AJAX saves (Settings tabs) request JSON + a toast instead of a full
    # redirect/reload. Honor that when the client signals Accept: application/json.
    if "application/json" in (request.headers.get("accept") or ""):
        return {"status": "ok", "message": "Settings saved"}
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/api/llm/credentials")
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


def _copilot_poll_loop(device_code, interval, provider):
    """SERVER-SIDE device-flow poll: after the user authorizes in GitHub, poll for the
    token here (independent of the browser, so a page reload/restart can't strand it),
    store it as the copilot credential, and report progress via state['copilot_auth']."""
    import requests as _rq, time as _t
    from urllib.parse import parse_qs
    from main import COPILOT_CLIENT_ID, GITHUB_OAUTH_TOKEN_URL
    poll = max(5, int(interval or 5))
    deadline = _t.time() + 900  # GitHub device codes live ~15 min
    state["copilot_auth"] = {"status": "pending", "message": "Waiting for you to authorize in GitHub…"}
    while _t.time() < deadline:
        _t.sleep(poll)
        try:
            r = _rq.post(GITHUB_OAUTH_TOKEN_URL, headers={"Accept": "application/json"},
                         data={"client_id": COPILOT_CLIENT_ID, "device_code": device_code,
                               "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, timeout=20)
            if r.headers.get("content-type", "").startswith("application/json"):
                d = r.json()
            else:
                d = {k: v[0] for k, v in parse_qs(r.text).items()}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Copilot poll request error: {e}")
            continue
        if d.get("access_token"):
            cfg = load_config()
            cfg.setdefault("llm_credentials", {})[provider] = {"api_key": d["access_token"], "base_url": ""}
            cfg.pop("copilot_device", None)
            save_config(cfg)
            state["copilot_auth"] = {"status": "authorized", "message": "Copilot connected."}
            logger.info(f"Copilot device flow: authorized + stored credential for '{provider}'.")
            return
        err = d.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            poll += 5
            continue
        logger.warning(f"Copilot poll: GitHub error={err!r} desc={d.get('error_description')!r}")
        state["copilot_auth"] = {"status": "error",
                                 "message": (f"{err}: {d.get('error_description') or ''}".strip(": ")
                                             or "authorization failed")}
        return
    state["copilot_auth"] = {"status": "error", "message": "Device code expired — click Sign in again."}


@router.post("/api/copilot/device-start")
async def copilot_device_start():
    """Begin GitHub Copilot OAuth device flow: get a device+user code and kick off a
    SERVER-SIDE poll loop that completes the auth once the user authorizes in GitHub."""
    import requests as _rq
    from main import COPILOT_CLIENT_ID, GITHUB_DEVICE_CODE_URL
    try:
        r = _rq.post(GITHUB_DEVICE_CODE_URL, headers={"Accept": "application/json"},
                     data={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"}, timeout=20)
        d = r.json()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(e)})
    if not d.get("device_code"):
        return JSONResponse(status_code=502,
                            content={"error": d.get("error_description") or "device code request failed"})
    interval = int(d.get("interval", 5))
    logger.info("Copilot device flow: issued user code %s (verify at %s)",
                d.get("user_code"), d.get("verification_uri"))
    threading.Thread(target=_copilot_poll_loop, args=(d["device_code"], interval, "copilot"),
                     daemon=True, name="copilot-auth").start()
    return {"user_code": d.get("user_code"), "verification_uri": d.get("verification_uri"),
            "expires_in": d.get("expires_in"), "interval": interval}


@router.get("/api/copilot/status")
async def copilot_status():
    """Progress of the in-flight Copilot device-flow auth (polled by the UI)."""
    return state.get("copilot_auth") or {"status": "idle", "message": ""}


@router.post("/api/copilot/signout")
async def copilot_signout(request: Request):
    """Clear a stored Copilot authorization: drop the credential (GitHub token) + any
    in-flight device code, and evict the cached Copilot API token."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    provider = (body.get("provider") or "copilot").lower().strip() or "copilot"
    config = load_config()
    creds = config.get("llm_credentials") or {}
    gh = (creds.get(provider) or {}).get("api_key")
    creds.pop(provider, None)
    config["llm_credentials"] = creds
    config.pop("copilot_device", None)
    save_config(config)
    state.pop("copilot_auth", None)
    state.pop("copilot_device", None)
    try:
        from main import _COPILOT_TOKEN_CACHE
        if gh:
            _COPILOT_TOKEN_CACHE.pop(gh, None)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"Copilot: signed out '{provider}' (cleared credential + token cache).")
    return {"status": "ok"}


@router.get("/api/copilot/models")
async def copilot_models(provider: str = "copilot"):
    """List models available to this Copilot subscription (for the model dropdown)."""
    import requests as _rq
    from main import _copilot_api_token, _copilot_headers, COPILOT_API_BASE
    cred = (load_config().get("llm_credentials") or {}).get((provider or "copilot").lower()) or {}
    gh = cred.get("api_key")
    if not gh:
        return {"models": [], "error": "not authenticated — sign in with GitHub first"}
    try:
        tok = _copilot_api_token(gh)
        r = _rq.get(f"{COPILOT_API_BASE}/models", headers=_copilot_headers(tok), timeout=20)
        data = r.json()
        models = sorted({m.get("id") for m in (data.get("data") or []) if m.get("id")})
        return {"models": models}
    except Exception as e:  # noqa: BLE001
        return {"models": [], "error": str(e)}


@router.post("/api/llm/entries")
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
        # Per-entry base_url / api_key override the shared per-provider credential,
        # so e.g. three `ollama` entries can independently target local CPU
        # (http://localhost:11434), a remote-GPU box on the LAN, and Ollama Cloud.
        "base_url": (data.get("base_url") or "").strip(),
        "api_key": (data.get("api_key") or "").strip(),
        # Build-ratchet models for THIS slot: comma-separated (e.g. 7b,14b,32b) or
        # "*" = every model installed on this ollama endpoint, ramped smallest-first.
        "escalation_models": (data.get("escalation_models") or "").strip(),
    }
    if not entry["model"]:
        return JSONResponse(status_code=400, content={"error": "model required"})
    config = load_config()
    config.setdefault("llm_entries", []).append(entry)
    save_config(config)
    return {"status": "ok", "entry": entry}


@router.put("/api/llm/entries/{entry_id}")
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
            # Per-entry overrides. Only overwrite when the key is present in the
            # payload so a partial update doesn't wipe an existing value.
            if "base_url" in data:
                e["base_url"] = (data.get("base_url") or "").strip()
            if "api_key" in data:
                e["api_key"] = (data.get("api_key") or "").strip()
            if "escalation_models" in data:
                e["escalation_models"] = (data.get("escalation_models") or "").strip()
            save_config(config)
            return {"status": "ok", "entry": e}
    return JSONResponse(status_code=404, content={"error": "entry not found"})


@router.delete("/api/llm/entries/{entry_id}")
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


@router.post("/api/llm/slots")
async def update_llm_slots(request: Request):
    """Update the slot→entry_id assignment for P1-P4."""
    data = await request.json()  # {"1": "entry_id_or_null", ...}
    config = load_config()
    config["llm_slots"] = {str(k): (v or None) for k, v in data.items()}
    save_config(config)
    return {"status": "ok"}


@router.get("/api/llm/config")
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


@router.post("/api/local-llm/setup")
async def local_llm_setup(request: Request):
    """Kick off the one-click local (CPU-only) LLM setup in the background.

    Body (all optional, defaults applied): {model, num_ctx, cores, slot}.
    slot (1-4) is the provider slot to assign the local model to; honored as given.
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
    try:
        slot = int(data.get("slot") or 4)
    except (TypeError, ValueError):
        slot = 4
    if slot not in (1, 2, 3, 4):
        slot = 4
    if "LocalLLMSetup" in state.get("active_tasks", {}):
        return JSONResponse(status_code=409, content={"status": "busy", "message": "A local LLM setup is already running."})
    threading.Thread(target=run_local_llm_setup, args=(model, num_ctx, cores, slot), daemon=True).start()
    return {"status": "started", "task_id": "LocalLLMSetup"}


@router.get("/api/local-llm/status")
async def local_llm_status():
    """Whether a setup is running + the last-run summary + detected core count."""
    return {
        "running": "LocalLLMSetup" in state.get("active_tasks", {}),
        "last": state.get("local_llm_setup") or {},
        "cpu_count": state.get("cpu_count") or os.cpu_count() or 4,
    }


@router.get("/api/local-llm/models")
async def local_llm_models(base_url: str = ""):
    """List the models on an ollama endpoint (defaults to the local server).
    Pass ?base_url=http://<host>:11434 to manage a remote instance (e.g. the M4)."""
    url = (base_url or OLLAMA_BASE_URL).strip() or OLLAMA_BASE_URL
    return {"base_url": url, "models": _ollama_models_detailed(url)}


@router.post("/api/local-llm/pull")
async def local_llm_pull(request: Request):
    """Pull a model in the background. Body: {model, base_url?}. Poll progress via
    /api/task-details?task_id=LocalLLMPull."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = (data.get("model") or "").strip()
    if not model:
        return JSONResponse(status_code=400, content={"message": "model required"})
    base_url = (data.get("base_url") or OLLAMA_BASE_URL).strip() or OLLAMA_BASE_URL
    if "LocalLLMPull" in state.get("active_tasks", {}):
        return JSONResponse(status_code=409, content={"message": "A model pull is already running."})
    threading.Thread(target=run_local_llm_pull, args=(model, base_url), daemon=True).start()
    return {"status": "started", "task_id": "LocalLLMPull"}


@router.post("/api/local-llm/delete")
async def local_llm_delete(request: Request):
    """Delete a model. Body: {model, base_url?}."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = (data.get("model") or "").strip()
    if not model:
        return JSONResponse(status_code=400, content={"message": "model required"})
    base_url = (data.get("base_url") or OLLAMA_BASE_URL).strip() or OLLAMA_BASE_URL
    ok, msg = _ollama_delete(model, base_url)
    if not ok:
        return JSONResponse(status_code=502, content={"message": f"Delete failed: {msg}"})
    return {"status": "ok", "message": f"{model}: {msg}"}


@router.post("/clear_history")
async def clear_history():
    """Clears all processed issues and resets success/failure counters."""
    global state
    logger.info("Clearing all issue history and resetting counters.")

    state["processed"] = {}
    state["success_count"] = 0
    state["failure_count"] = 0

    save_processed({})

    return {"status": "success", "message": "All history and tasks have been cleared."}


def _close_issue_on_github(issue_id: str):
    """Close one issue on GitHub with the ``bugfixer-dismissed`` label + an
    explanatory comment. Returns (ok, message). Best-effort per step — label
    create/apply and the comment never raise. Shared by the single-issue
    delete and the bulk delete-all sweep so the close logic isn't duplicated."""
    if ":" not in issue_id:
        raise ValueError("Invalid issue_id")
    repo_name, issue_num_str = issue_id.rsplit(":", 1)
    issue_num = int(issue_num_str)
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
        return True, f"Issue #{issue_num} labelled '{label_name}' and closed on GitHub."
    return True, f"Issue #{issue_num} labelled '{label_name}' (was already closed)."


@router.post("/delete_issue")
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
        _ok, github_msg = _close_issue_on_github(issue_id)
        logger.info(f"Dismissed issue {issue_id}: removed from history, {github_msg}")
    except Exception as e:
        github_msg = f"GitHub close failed: {e}"
        logger.warning(f"Could not close {issue_id} on GitHub: {e}")

    return {
        "status": "success",
        "message": f"{'Removed from history. ' if was_in_history else ''}{github_msg}",
    }


@router.post("/delete_all_issues")
async def delete_all_issues(request: Request):
    """Clear every issue from local history and close them all on GitHub.
    Local history + counters are wiped immediately (the feed empties on
    reload); the GitHub closes run in a background thread so a large set
    can't time out the request. Mirrors the retry_all_failed background
    pattern. Each issue is closed best-effort — one failure doesn't abort
    the sweep."""
    global state
    processed = load_processed()
    to_close = list(processed.keys())
    if not to_close:
        return {"status": "no_issues", "message": "No issues in history to delete."}

    # Clear local history + counters now; close on GitHub in the background
    # against the snapshot so the clear doesn't race the sweep.
    state["processed"] = {}
    state["success_count"] = 0
    state["failure_count"] = 0
    save_processed({})

    def _bulk_close():
        closed = failed = 0
        for issue_id in to_close:
            try:
                ok, _msg = _close_issue_on_github(issue_id)
                if ok:
                    closed += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.warning(f"delete_all: could not close {issue_id} on GitHub: {e}")
        logger.info(f"delete_all_issues: closed {closed}/{len(to_close)} on GitHub, {failed} failed.")

    threading.Thread(target=_bulk_close, daemon=True).start()
    return {
        "status": "success",
        "message": f"Cleared {len(to_close)} issue(s) from history. Closing them on GitHub in the background.",
    }


@router.post("/resolve_issue")
async def resolve_issue(request: Request):
    """Mark an issue as resolved: close it on GitHub and set its local status to fixed."""
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

    # Close the issue on GitHub first. We only update the local status to fixed
    # if this succeeds (including the already-closed case); on failure we leave
    # the local state untouched so the UI and history don't claim a fix that
    # never landed on GitHub.
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=500, content={
            "status": "error",
            "message": "GitHub close failed: No GitHub token configured. Local status left unchanged.",
        })

    try:
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(issue_num)

        try:
            issue.create_comment(
                "🤖 **BugFixer**: This issue has been marked as **resolved** and is now closed. "
                "It will not be automatically reopened or processed again."
            )
        except Exception:
            pass

        if issue.state != "closed":
            issue.edit(state="closed")
            github_msg = f"Issue #{issue_num} closed on GitHub."
        else:
            github_msg = f"Issue #{issue_num} was already closed on GitHub."
        # Human sign-off → tell the hub the bug report is fixed, ALWAYS — even when the
        # issue was already closed on GitHub. (This used to be inside the "if not
        # closed" branch, so an already-closed issue never flipped the LM report to
        # Fixed.)
        try:
            from fix_engine import _notify_bug_fixed
            _notify_bug_fixed(issue)  # LM "File a Bug" → hub → UI shows "Fixed"
        except Exception:
            pass
        # Apply the closed label (best-effort; existing labels kept).
        _apply_closed_label(repo, issue, issue_id)
        logger.info(f"Resolved issue {issue_id}: status -> closed, {github_msg}")
    except Exception as e:
        logger.warning(f"Could not close {issue_id} on GitHub: {e}")
        return JSONResponse(status_code=502, content={
            "status": "error",
            "message": f"GitHub close failed: {e}. Local status left unchanged.",
        })

    # GitHub close succeeded — clicking Resolved is a HUMAN sign-off, so the issue
    # moves into the RESOLVED bucket (status "resolved"), NOT Closed. Counters are
    # re-derived from the store so it lands in exactly one bucket.
    processed = load_processed()
    local_msg = "No local history entry to update, but "
    if issue_id in processed:
        entry = processed[issue_id]
        entry["status"] = "resolved"
        entry["timestamp"] = datetime.now().isoformat()
        entry["decision_reason"] = "Human-confirmed resolved."
        processed[issue_id] = entry
        save_processed(processed)
        recompute_issue_counters(processed)
        state["processed"] = processed
        local_msg = "Marked resolved (human-confirmed). "

    return {
        "status": "success",
        "message": f"{local_msg}{github_msg}",
    }


@router.post("/update_now")
async def update_now():
    updated, msg = check_for_updates()
    logger.info(f"Manual update check: {msg}")
    return {"status": "success", "message": msg}


@router.post("/api/clear-credit-cooldown/{n}")
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


@router.post("/trigger_fix")
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


@router.post("/scan_now")
async def scan_now():
    def trigger():
        state["status"] = "Manual Scan"
        run_scan_cycle()
    threading.Thread(target=trigger, daemon=True).start()
    return {"status": "triggered", "message": "Manual scan cycle started in background."}


@router.post("/retry_issue")
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


@router.post("/reopen_issue")
async def reopen_issue(request: Request):
    """Reopen a BugFixer-closed issue on GitHub and re-queue it — for when BugFixer
    reported a fix that did NOT actually resolve the bug (e.g. it committed +
    verified in its sandbox but the push never landed). Reopens the issue, strips
    the ``bugfixer-closed`` / ``bugfixer-dismissed`` labels that suppress
    re-processing, clears its stored processed state, and kicks off a fresh fix."""
    data = await request.json()
    issue_id = data.get("issue_id")
    if not issue_id or ":" not in issue_id:
        return JSONResponse(status_code=400, content={"message": "Invalid issue_id format. Expected 'repo:num'"})
    repo_name, issue_num_str = issue_id.rsplit(":", 1)
    try:
        issue_num = int(issue_num_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"message": "Invalid issue number"})
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"message": "No GitHub token configured"})
    try:
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(issue_num)
        for lbl in ("bugfixer-closed", "bugfixer-dismissed"):
            try:
                issue.remove_from_labels(lbl)
            except Exception:  # noqa: BLE001 — label may not be present
                pass
        if issue.state != "open":
            issue.edit(state="open")
        try:
            issue.create_comment(
                "🔁 **BugFixer**: Reopened by the operator — the previous fix did not "
                "actually resolve this. Re-queued for another attempt."
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        logger.error(f"Reopen failed for {issue_id}: {e}")
        return JSONResponse(status_code=500, content={"message": f"Reopen failed: {e}"})
    # Mark the issue "reopened" (rather than deleting its record) so: (a) the base
    # closed/resolved counter drops NOW that it's no longer closed, and (b) the flag
    # carries into the eventual re-close so it's tallied in the ReOpened buckets — not
    # double-counted in the base "Issues Closed" total. Status "reopened" is not a
    # terminal status the scanner skips, and the direct re-queue below drives the fix.
    try:
        processed = load_processed()
        prior = processed.get(issue_id, {})
        processed[issue_id] = {
            "status": "reopened",
            "reopened": True,
            "original_body": prior.get("original_body", ""),
            # Preserve the prior fix so the re-fix can diff "what changed since" and
            # triage the regression (falls back to parsing the issue's Commit: comment).
            "prior_fix_commit": prior.get("commit"),
            "prior_fix_files": prior.get("files"),
            "timestamp": datetime.now().isoformat(),
        }
        save_processed(processed)
        recompute_issue_counters(processed)
        state["processed"] = processed
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Reopen: could not update processed state for {issue_id}: {e}")
    logger.info(f"Manual reopen: {issue_id}")

    def run_fix():
        success, msg = process_single_issue(repo_name, issue_num)
        logger.info(f"Reopen re-fix {'ok' if success else 'FAILED'} for {issue_id}: {msg}")

    threading.Thread(target=run_fix, daemon=True).start()
    return {"status": "triggered", "message": f"Reopened + re-queued {issue_id}"}


@router.post("/retry_all_failed")
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


@router.post("/restart")
async def restart_service():
    """Manual restart: flag the dedicated restart_worker instead of fire-and-forget
    sudo, so the restart goes through the same verified, detached, grace-windowed path
    as automatic self-updates."""
    logger.info("Manual restart requested — flagging restart_worker.")
    state["restart_pending"] = True
    _log_restart_event("manual_restart", "manual restart requested", ok=True)
    return {"status": "success", "message": "Restart scheduled (grace window applies)."}


@router.post("/trigger_hub_update")
async def trigger_hub_update():
    """Triggers an update on the Hub + all its spokes and agents.

    Uses the authenticated hub-agent WebSocket (TRIGGER_ALL_UPDATES) — the same
    path the post-fix auto-update uses. The old trigger_infrastructure_update()
    HTTP POST to UPDATE_API_URL is NOT used here: it hit a NetBox-sync endpoint
    (never a hub-update trigger) and the hub never honored those static-token
    HTTP calls, so the button silently no-op'd.
    """
    result = _trigger_spoke_updates(load_config())
    msg = result if isinstance(result, str) else "Hub update triggered"
    ok = msg.lower().startswith("hub update triggered")
    return {"status": "success" if ok else "error", "message": msg}


@router.get("/chat")
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


@router.post("/api/chat/new")
async def chat_new():
    """Creates a new empty conversation and makes it active."""
    cid = create_conversation()
    return {"chat_id": cid}


@router.post("/api/chat")
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


@router.get("/api/chat/stream")
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


@router.post("/api/chat/rename")
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


@router.post("/api/chat/delete")
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


@router.post("/api/chat/clear")
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


@router.post("/api/chat/confirm_fix")
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


__all__ = [
    'hub_agent_status',
    'hub_agent_reregister',
    'health_check',
    'toggle_pause',
    'toggle_blackout',
    'dashboard',
    'get_task_details',
    'get_models',
    'fetch_models_live',
    'scheduler_status',
    'diagnostics',
    'claude_cli_status',
    'claude_cli_auth_start',
    'claude_cli_auth_poll',
    'claude_cli_auth_submit_code',
    'toggle_model',
    'get_logs',
    'get_hub_logs_page',
    'hub_logs_raw',
    'settings_page',
    'diagnostics_page',
    'save_settings',
    'save_llm_credential',
    'create_llm_entry',
    'update_llm_entry',
    'delete_llm_entry',
    'update_llm_slots',
    'get_llm_config',
    'local_llm_setup',
    'local_llm_status',
    'clear_history',
    'delete_issue',
    'delete_all_issues',
    'resolve_issue',
    'update_now',
    'clear_credit_cooldown',
    'trigger_fix',
    'scan_now',
    'retry_issue',
    'retry_all_failed',
    'restart_service',
    'trigger_hub_update',
    'chat_page',
    'chat_new',
    'chat_send',
    'chat_stream',
    'chat_rename',
    'chat_delete',
    'chat_clear',
    'chat_confirm_fix',
    'log_analysis_run',
    'log_analysis_status',
    'log_health_worker',
    'DEFAULT_ENV',
    'router',
]
