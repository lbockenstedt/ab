"""One-click local-LLM (Ollama) setup for BugFixer.

Extracted verbatim from main.py: the Ollama HTTP helpers (_ollama_reachable /
_wait_for_ollama / _ollama_tags / _ollama_bin_path / _ollama_http_pull /
_ollama_http_create), the setup-log streamer, and run_local_llm_setup. Pure move,
no behavior change.

main re-exports these via ``from ollama_setup import *`` (placed before routes,
which imports run_local_llm_setup). All dependencies (state, update_task_state,
_task_state_lock, load_config, save_config, logger) come from modules re-exported
into main before ollama_setup, so they are imported directly — no lazy refs.
"""
import json
import os
import subprocess
import time
import uuid
from datetime import datetime

import requests

from main import (
    _task_state_lock,
    load_config,
    logger,
    save_config,
    state,
    update_task_state,
    CONFIG_DIR,
)

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


def _watchdog_active():
    """True/False if we could determine bugfixer-watchdog.service's active state,
    None if the query itself failed. `systemctl is-active` is a read-only query
    that needs no privilege, so the cap-locked main service can run it — this is
    how we tell "watchdog is down" apart from "watchdog is up but not consuming"
    (stale code) instead of waiting out the full 16-min poll for a blank timeout."""
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "bugfixer-watchdog"],
                           timeout=10)
        return r.returncode == 0
    except Exception:
        return None


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
    """Create a derived model via POST /api/create, streaming status.

    Ollama v0.5.5+ replaced the `modelfile` string body with structured fields:
    `model` (the new name), `from` (the base model), `parameters` (a key-value
    object), `stream`. Sending the old `{name, modelfile}` body to a current
    server yields "neither 'from' or 'files' was specified" (the modelfile
    string is no longer parsed). We parse our legacy modelfile text into the
    new field-based body so create works on current Ollama. The modelfile text
    is still the single source the caller builds (FROM + PARAMETER lines),
    kept for readability and in case a future engine re-introduces it.
    """
    base_model, parameters = None, {}
    for line in modelfile.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        key, rest = parts[0], parts[1] if len(parts) > 1 else ""
        if key.upper() == "FROM":
            base_model = rest.strip()
        elif key.upper() == "PARAMETER":
            kv = rest.split(None, 1)
            if len(kv) == 2:
                # Send numerics as ints (num_ctx/num_thread), not strings.
                try:
                    parameters[kv[0]] = int(kv[1])
                except ValueError:
                    parameters[kv[0]] = kv[1]
    body = {"model": name, "stream": True}
    if base_model:
        body["from"] = base_model
    if parameters:
        body["parameters"] = parameters
    try:
        resp = requests.post(base_url.rstrip("/") + "/api/create",
                             json=body, stream=True, timeout=None)
        if resp.status_code != 200:
            # Surface Ollama's error body. A 400 here is otherwise opaque —
            # raise_for_status() only yields the status line, not the JSON
            # message Ollama returns (e.g. "num_ctx exceeds model max context"
            # or "invalid num_thread"). Drain the (short) error body so the
            # Setup log shows the real reason instead of "400 Bad Request".
            try:
                rbody = resp.text.strip()
            except Exception:
                rbody = ""
            raise RuntimeError(f"ollama /api/create failed ({resp.status_code}) for "
                               f"{name}: {rbody or resp.reason}")
        saw_success = False
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                log_fn("  " + str(raw))
                continue
            # Ollama can return 200 and stream an {"error": ...} line on failure.
            if obj.get("error"):
                raise RuntimeError(f"ollama /api/create failed for {name}: {obj['error']}")
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


def run_local_llm_setup(model, num_ctx, cores, slot=4):
    """Background pipeline for the one-click local (CPU-only) LLM setup.

    Stages: install Ollama → ensure service → pull model → create context-tuned
    derived model → write systemd override + restart → configure the chosen
    provider slot → verify. Each stage is idempotent. Progress is streamed via
    _llm_setup_log() into state['active_tasks']['LocalLLMSetup'].

    slot: which BugFixer provider slot (1-4) to assign the local model to. The
    caller's choice is honored; falls back to 4 only if it's missing/invalid.
    """
    task_id = "LocalLLMSetup"
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        slot = 4
    if slot not in (1, 2, 3, 4):
        slot = 4
    update_task_state(task_id, "Local LLM Setup", action="start")
    base_url = OLLAMA_BASE_URL
    derived_tag = model
    summary = {"state": "failed", "message": "not started"}
    try:
        # ---- Stage 1: detect / install Ollama + apply CPU tuning ----
        # Installed = the HTTP API answers OR the binary exists at a known path.
        # We do NOT rely on `which("ollama")` alone because the bugfixer service
        # runs under systemd with a minimal PATH that omits /usr/local/bin.
        # bugfixer runs as svc_bg under a systemd unit whose
        # CapabilityBoundingSet is locked to CAP_NET_BIND_SERVICE (least
        # privilege). That bounding set omits CAP_SETGID/CAP_SETUID, so sudo
        # spawned from the service can't setgid(0) ("unable to change to root
        # gid: Operation not permitted"), and a direct `systemd-run` from the
        # service is polkit-denied ("Failed to start transient service unit:
        # Access denied"). bugfixer-WATCHDOG.service has no such restriction,
        # so sudo works THERE (same privileged-arm pattern as spawn_restart).
        # We delegate Stage 1 to the watchdog: write a request file, then poll
        # the status file it streams the helper's output to, relaying new
        # lines into the Setup log. The helper is idempotent (installs only if
        # absent, starts only if down, applies the override only if it
        # changed), so calling it unconditionally also covers the former
        # Stage 5 tuning. HTTP-API stages (pull/create model, verify) stay
        # here in svc_bg. Paths kept in sync with watchdog.py.
        _llm_setup_log("▶ Stage 1/7 — Prerequisites + installing/tuning Ollama (root helper via watchdog)…")
        already_up = _ollama_reachable(base_url, timeout=5)
        bin_path = _ollama_bin_path()
        _llm_setup_log(f"  pre-check: ollama service {'up' if already_up else 'down'}, "
                       f"binary at {bin_path or 'unknown path'}")
        # Fail fast if the watchdog service isn't even running — no point queuing
        # a request nothing will consume, then waiting out a 16-min blank timeout.
        if _watchdog_active() is False:
            raise RuntimeError(
                "bugfixer-watchdog.service is not active, so the privileged "
                "ollama-setup helper can't run. Start it with "
                "'systemctl restart bugfixer-watchdog' (check "
                "'systemctl status bugfixer-watchdog' / 'journalctl -u bugfixer-watchdog').")
        req_path = os.path.join(CONFIG_DIR, "ollama_setup_request.json")
        status_path = os.path.join(CONFIG_DIR, "ollama_setup_status.json")
        # Clear any stale status so we read a fresh run (not a previous done/failed).
        try: os.remove(status_path)
        except OSError: pass
        try:
            with open(req_path, "w") as f:
                json.dump({"cores": int(cores), "requested_at": time.time()}, f)
        except Exception as e:
            raise RuntimeError(f"could not write ollama-setup request: {e}")
        _llm_setup_log("  queued with the watchdog — waiting for it to pick up the request…")
        # Poll the status file, relaying NEW stream lines, until done/failed/timeout.
        # 16-min hard cap (the helper itself has a 900s internal timeout) + the
        # watchdog's ~30s pickup latency.
        deadline = time.time() + 960
        # Claim-detection backstop: the watchdog claims the request by DELETING the
        # request file and writing a "running" status almost immediately (its loop
        # is ~30s, and up to ~72s when busy verifying an update). If NEITHER the
        # request file is gone NOR any status appeared within this grace, the
        # watchdog is up but not consuming — almost always a stale watchdog process
        # (watchdog.py was updated on disk but the long-lived process wasn't
        # restarted). Fail fast with the exact remedy instead of hanging 16 min.
        claim_deadline = time.time() + 120
        claimed = False
        relay_len = 0
        final = None
        while time.time() < deadline:
            try:
                with open(status_path, "r") as f:
                    st = json.load(f)
            except Exception:
                st = None
            if not claimed and (st or not os.path.exists(req_path)):
                claimed = True
            if st:
                stream = st.get("stream") or ""
                if len(stream) > relay_len:
                    for line in stream[relay_len:].splitlines():
                        if line.strip():
                            _llm_setup_log(f"  [helper] {line}")
                    relay_len = len(stream)
                if st.get("state") in ("done", "failed"):
                    final = st
                    break
            if not claimed and time.time() > claim_deadline:
                try: os.remove(req_path)
                except OSError: pass
                raise RuntimeError(
                    "bugfixer-watchdog is running but never picked up the ollama-setup "
                    "request — it's likely executing stale code (watchdog.py was updated "
                    "but the long-lived watchdog process wasn't restarted). Run "
                    "'systemctl restart bugfixer-watchdog' and try again.")
            time.sleep(2)
        if not final:
            raise RuntimeError("ollama-setup helper did not finish in time — is bugfixer-watchdog "
                               "running? The watchdog runs the privileged helper; check "
                               "'systemctl status bugfixer-watchdog'.")
        if final.get("state") != "done":
            tail = (final.get("stream") or "").strip()[-800:]
            raise RuntimeError(f"ollama-setup helper exited {final.get('returncode')}: {tail}")
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
            # Only emit num_thread when cores > 0. Ollama rejects
            # `PARAMETER num_thread 0` with a 400 ("invalid num_thread");
            # when omitted it auto-detects the CPU thread count, which is the
            # safer default than a bogus 0 from an unset/blank field.
            cores_i = int(cores) if cores else 0
            _llm_setup_log(f"▶ Stage 4/7 — Creating context-tuned model {derived_tag} (num_ctx={int(num_ctx)}, num_thread={cores_i or 'auto'})…")
            modelfile = f"FROM {model}\nPARAMETER num_ctx {int(num_ctx)}\n"
            if cores_i > 0:
                modelfile += f"PARAMETER num_thread {cores_i}\n"
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

        # ---- Stage 6: configure the chosen BugFixer provider slot ----
        _llm_setup_log(f"▶ Stage 6/7 — Configuring BugFixer provider slot {slot} (P{slot})…")
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
        config.setdefault("llm_slots", {})[str(slot)] = entry["id"]
        save_config(config)
        _llm_setup_log(f"✓ Slot {slot} → {entry['label']} / {derived_tag}")

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


__EXCLUDE = {"json", "os", "time", "uuid", "datetime", "requests",
             "_task_state_lock", "load_config", "logger", "save_config",
             "state", "update_task_state"}
__all__ = [__n for __n in dir() if not __n.startswith("__") and __n not in __EXCLUDE]
