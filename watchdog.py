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

import os, sys, json, time, requests, subprocess, logging
from datetime import datetime

# Config
CONFIG_DIR = "/etc/ab"
UPDATE_STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
UPDATE_PENDING_FILE = os.path.join(CONFIG_DIR, "update_pending")
STARTUP_STAMP_FILE = os.path.join(CONFIG_DIR, "startup_stamp.json")
# Privileged ollama-setup delegation. ab.service runs under a locked
# CapabilityBoundingSet (CAP_NET_BIND_SERVICE only) so it can neither sudo
# (setgid(0) EPERM) nor systemd-run (polkit denied) a root helper. The watchdog
# service has no such restriction — sudo works here (same pattern as
# spawn_restart). The main service writes a request file; we run the helper as
# root via sudo and stream its output to a status file it polls + relays to the
# Setup log. Paths kept in sync with ollama_setup.py.
OLLAMA_SETUP_REQUEST = os.path.join(CONFIG_DIR, "ollama_setup_request.json")
OLLAMA_SETUP_STATUS = os.path.join(CONFIG_DIR, "ollama_setup_status.json")
OLLAMA_SETUP_HELPER = "/usr/local/bin/ab-ollama-setup"
# Same delegation for the Claude Code CLI install: the main service is
# cap-locked and cannot escalate, so it drops a request file and we run the
# root helper. Installed AS the service user (per-user session auth).
CLAUDE_INSTALL_REQUEST = os.path.join(CONFIG_DIR, "claude_install_request.json")
CLAUDE_INSTALL_STATUS = os.path.join(CONFIG_DIR, "claude_install_status.json")
CLAUDE_INSTALL_HELPER = "/usr/local/bin/ab-claude-install"
# Delegated restart. ab.service is cap-locked (CAP_NET_BIND_SERVICE only) so
# it can't restart itself (sudo setgid(0) EPERM); it writes this request file and
# the watchdog (unrestricted) runs the restart. Same privileged-arm pattern as
# ollama-setup. Kept in sync with workers.RESTART_REQUEST_FILE.
RESTART_REQUEST = os.path.join(CONFIG_DIR, "restart_request")
# Derive the health probe from the same env the server binds on (unified-443:
# HTTPS on :443 by default). Kept in lockstep with main.py's SERVER_PORT / SSL
# settings so the watchdog never probes the wrong scheme/port.
_HEALTH_PORT = os.environ.get("AB_PORT", "443")
_HEALTH_SCHEME = "https" if os.path.exists(
    os.environ.get("AB_SSL_CERT", "/etc/ab/cert.pem")) else "http"
HEALTH_URL = os.environ.get(
    "AB_HEALTH_URL", f"{_HEALTH_SCHEME}://127.0.0.1:{_HEALTH_PORT}/api/health")
CHECK_INTERVAL = 5 # seconds
HEALTH_TIMEOUT = 60 # seconds

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
            handlers = ([_logging.FileHandler(log_file), _logging.StreamHandler()]
                        if log_file else None)
            _logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)
# The AppBuilderWatchdog logger name carries identity via %(name)s, replacing
# the literal [WATCHDOG] format tag (now standard across all LM components).
configure_logging(log_file="/var/log/ab_watchdog.log")
logger = logging.getLogger("AppBuilderWatchdog")

def load_update_state():
    if os.path.exists(UPDATE_STATE_FILE):
        try:
            with open(UPDATE_STATE_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {"last_known_good_commit": None, "failed_commits": []}

def save_update_state(state):
    try:
        with open(UPDATE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving update state: {e}")

def is_service_active():
    try:
        res = subprocess.run(["systemctl", "is-active", "ab"], capture_output=True, text=True)
        return res.stdout.strip() == "active"
    except:
        return False

def read_startup_stamp():
    """Return the commit hash the running process booted on, or None."""
    try:
        with open(STARTUP_STAMP_FILE, "r") as f:
            return json.load(f).get("commit")
    except Exception:
        return None

def spawn_restart():
    """Detached `systemctl restart ab` that survives this process dying. The
    service runs as svc_bg, so non-root invokes the race-free root helper via
    sudoers (mirrors main.py _spawn_restart); root still calls systemctl directly
    for dev/standalone."""
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", "/usr/local/bin/ab-self-restart"]
    else:
        cmd = ["systemctl", "restart", "ab"]
    subprocess.Popen(cmd, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    logger.info("WATCHDOG: spawned detached restart.")

def handle_restart_request():
    """Consume a restart request from the cap-locked main service and restart it.

    ab.service can't restart itself (sudo can't setgid(0) under its locked
    CapabilityBoundingSet), so it writes RESTART_REQUEST and we — the unrestricted
    watchdog — perform the restart. Claim the request by deleting it first so a
    burst of requests collapses to one restart."""
    if not os.path.exists(RESTART_REQUEST):
        return
    try:
        os.remove(RESTART_REQUEST)
    except Exception:  # noqa: BLE001
        pass
    logger.info("WATCHDOG: restart requested by ab.service — restarting it.")
    spawn_restart()


def rollback():
    logger.warning("WATCHDOG: Initiating rollback to last known good commit...")
    state = load_update_state()
    lkg = state.get("last_known_good_commit")
    if not lkg:
        logger.error("WATCHDOG: No last known good commit found. Cannot rollback.")
        return False

    try:
        app_dir = "/opt/ab"
        if not os.path.exists(app_dir):
            logger.error(f"WATCHDOG: Application directory {app_dir} not found. Rollback impossible.")
            return False

        subprocess.run(["git", "-C", app_dir, "reset", "--hard", lkg], check=True)
        logger.info(f"WATCHDOG: Rolled back to {lkg[:7]}")

        try:
            with open(UPDATE_PENDING_FILE, "r") as f:
                failed_commit = f.read().strip()
            if failed_commit and failed_commit not in state["failed_commits"]:
                state["failed_commits"].append(failed_commit)
                save_update_state(state)
        except:
            pass

        # Detached restart (survives this process being killed), sudo-free as root.
        spawn_restart()
        return True
    except Exception as e:
        logger.error(f"WATCHDOG: Rollback failed: {e}")
        return False

def _write_ollama_status(state, stream, returncode=None):
    try:
        with open(OLLAMA_SETUP_STATUS, "w") as f:
            json.dump({"state": state, "stream": stream,
                       "returncode": returncode, "updated_at": time.time()}, f)
    except Exception as e:  # noqa: BLE001
        logger.error(f"ollama-setup: could not write status: {e}")


def _reset_stale_ollama_status():
    """On startup, mark a leftover 'running' status as failed so the main
    service's poll doesn't hang forever after the watchdog was restarted
    (e.g. via Update) mid-setup."""
    try:
        if os.path.exists(OLLAMA_SETUP_STATUS):
            with open(OLLAMA_SETUP_STATUS, "r") as f:
                st = json.load(f)
            if st.get("state") == "running":
                st["state"] = "failed"
                st["stream"] = (st.get("stream") or "") + "\n[watchdog restarted mid-setup]"
                st["returncode"] = -1
                st["updated_at"] = time.time()
                with open(OLLAMA_SETUP_STATUS, "w") as f:
                    json.dump(st, f)
                logger.warning("ollama-setup: reset stale 'running' status (watchdog restarted)")
    except Exception:  # noqa: BLE001
        pass


def handle_ollama_setup_request():
    """Run the privileged ollama-setup helper on behalf of ab.service.

    The main service is locked to CAP_NET_BIND_SERVICE and can't escalate; the
    watchdog can (sudo works here). It writes a request file; we claim it, run
    the helper as root via sudo, stream its combined stdout/stderr to the
    status file, and mark done/failed. Blocking — the helper is a rare manual
    action and bounded (≤900s); update-pending detection just waits it out."""
    if not os.path.exists(OLLAMA_SETUP_REQUEST):
        return
    try:
        with open(OLLAMA_SETUP_REQUEST, "r") as f:
            req = json.load(f)
        cores = str(int(req.get("cores", 0)))
        max_loaded = str(int(req.get("max_loaded", 3) or 3))
    except Exception as e:  # noqa: BLE001
        logger.error(f"ollama-setup: bad request file: {e}")
        try: os.remove(OLLAMA_SETUP_REQUEST)
        except Exception:  # noqa: BLE001
            pass
        _write_ollama_status("failed", f"bad request: {e}", -1)
        return
    # Claim the request so a second click doesn't double-run and the main
    # service sees 'running' immediately.
    try: os.remove(OLLAMA_SETUP_REQUEST)
    except Exception:  # noqa: BLE001
        pass
    _write_ollama_status("running", "")
    logger.info(f"ollama-setup: running helper (cores={cores}, max_loaded={max_loaded}) on behalf of ab.service")
    cmd = ["sudo", "-n", OLLAMA_SETUP_HELPER, cores, max_loaded]
    stream = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip("\n")
            stream.append(line)
            if len(stream) > 4000:  # cap memory + status file size
                stream = stream[-4000:]
            _write_ollama_status("running", "\n".join(stream))
        proc.wait(timeout=900)
        state = "done" if proc.returncode == 0 else "failed"
        _write_ollama_status(state, "\n".join(stream), proc.returncode)
        logger.info(f"ollama-setup: helper exited rc={proc.returncode} ({state})")
    except subprocess.TimeoutExpired:
        try: proc.kill()
        except Exception:  # noqa: BLE001
            pass
        _write_ollama_status("failed", "\n".join(stream) + "\n[helper timed out after 900s]", -1)
        logger.error("ollama-setup: helper timed out")
    except Exception as e:  # noqa: BLE001
        _write_ollama_status("failed", "\n".join(stream) + f"\n[watchdog error: {e}]", -1)
        logger.error(f"ollama-setup: watchdog error: {e}")


def _write_claude_status(state, stream, returncode=None):
    try:
        with open(CLAUDE_INSTALL_STATUS, "w") as f:
            json.dump({"state": state, "stream": stream, "returncode": returncode,
                       "at": time.time()}, f)
    except Exception as e:  # noqa: BLE001
        logger.error(f"claude-install: could not write status: {e}")


def handle_claude_install_request():
    """Install the Claude Code CLI on behalf of ab.service.

    Mirrors handle_ollama_setup_request: the main service writes a request file
    (it is locked to CAP_NET_BIND_SERVICE and cannot escalate), we claim it and
    run the root helper via sudo. The helper installs AS the service user, since
    `claude` authenticates per-user — a root-owned install would resolve but
    never authenticate for the account that actually runs it.
    """
    if not os.path.exists(CLAUDE_INSTALL_REQUEST):
        return
    try:
        with open(CLAUDE_INSTALL_REQUEST, "r") as f:
            req = json.load(f)
        svc_user = str(req.get("svc_user") or "svc_bg")
    except Exception as e:  # noqa: BLE001
        logger.error(f"claude-install: bad request file: {e}")
        try: os.remove(CLAUDE_INSTALL_REQUEST)
        except Exception:  # noqa: BLE001
            pass
        _write_claude_status("failed", f"bad request: {e}", -1)
        return
    # Claim it so a second click can't double-run.
    try: os.remove(CLAUDE_INSTALL_REQUEST)
    except Exception:  # noqa: BLE001
        pass
    _write_claude_status("running", "")
    logger.info(f"claude-install: running helper for user {svc_user}")
    stream = []
    try:
        proc = subprocess.Popen(["sudo", "-n", CLAUDE_INSTALL_HELPER, svc_user],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            stream.append(line.rstrip("\n"))
            if len(stream) > 2000:
                stream = stream[-2000:]
            _write_claude_status("running", "\n".join(stream))
        proc.wait(timeout=600)
        state = "done" if proc.returncode == 0 else "failed"
        _write_claude_status(state, "\n".join(stream), proc.returncode)
        logger.info(f"claude-install: helper exited rc={proc.returncode} ({state})")
    except subprocess.TimeoutExpired:
        try: proc.kill()
        except Exception:  # noqa: BLE001
            pass
        _write_claude_status("failed", "\n".join(stream) + "\n[timed out after 600s]", -1)
    except Exception as e:  # noqa: BLE001
        _write_claude_status("failed", "\n".join(stream) + f"\n[watchdog error: {e}]", -1)
        logger.error(f"claude-install: watchdog error: {e}")


_WATCHDOG_SRC = os.path.abspath(__file__)


def _maybe_reload_on_code_change(orig_mtime):
    """Re-exec the watchdog if watchdog.py changed on disk since we started.

    The watchdog is a long-lived process that restarts ab.service on
    update — but NOTHING restarts the watchdog itself, so after an update pulls
    a newer watchdog.py the running process keeps executing STALE in-memory code
    (a newly-added handler like handle_ollama_setup_request never runs → the
    ollama-setup request is silently never picked up). Compare the source mtime
    each idle loop; on a change, os.execv into the new code. Only fires when the
    loop is idle (between iterations), never mid-helper-run."""
    try:
        cur = os.path.getmtime(_WATCHDOG_SRC)
    except OSError:
        return
    if cur != orig_mtime:
        logger.info("WATCHDOG: watchdog.py changed on disk — re-execing to load new code.")
        try:
            os.execv(sys.executable, [sys.executable, _WATCHDOG_SRC])
        except Exception as e:  # noqa: BLE001 - if exec fails, keep running old code
            logger.error(f"WATCHDOG: self-reload exec failed ({e}); continuing on old code.")


def main():
    logger.info("AppBuilder Watchdog started.")
    _reset_stale_ollama_status()
    try:
        _src_mtime = os.path.getmtime(_WATCHDOG_SRC)
    except OSError:
        _src_mtime = None
    while True:
        if _src_mtime is not None:
            _maybe_reload_on_code_change(_src_mtime)
        try: handle_claude_install_request()
        except Exception as e:  # noqa: BLE001
            logger.error(f"claude-install dispatch failed: {e}")
        try: handle_ollama_setup_request()
        except Exception as e:  # noqa: BLE001
            logger.error(f"ollama-setup handler error: {e}")
        try: handle_restart_request()
        except Exception as e:  # noqa: BLE001
            logger.error(f"restart-request handler error: {e}")
        if os.path.exists(UPDATE_PENDING_FILE):
            pending_commit = ""
            try:
                with open(UPDATE_PENDING_FILE, "r") as f:
                    pending_commit = f.read().strip()
            except Exception:
                pass

            running_commit = read_startup_stamp()
            # CLOSE THE HOLE: only promote the new commit as known-good once the running
            # process is actually on it. If it isn't (the in-process restart_worker never
            # fired, or the process crashed before consuming the flag), force a restart so
            # the new code is loaded, THEN health-check it. Without this, the watchdog
            # would health-check the still-running OLD code (which is healthy), promote the
            # new commit as last_known_good, and clear update_pending — leaving the process
            # permanently stale with no signal left to ever restart.
            if pending_commit and running_commit != pending_commit:
                logger.info(f"WATCHDOG: running on {(running_commit or 'unknown')[:7]}, "
                            f"need {pending_commit[:7]}. Forcing restart to load new code.")
                spawn_restart()
                time.sleep(12)  # let systemd stop+start (RestartSec=10)

            logger.info("WATCHDOG: Update pending detected. Verifying health...")
            start_time = time.time()
            success = False

            while time.time() - start_time < HEALTH_TIMEOUT:
                if is_service_active():
                    try:
                        resp = requests.get(HEALTH_URL, timeout=2, verify=False)
                        if resp.status_code == 200 and resp.json().get("status") == "ok":
                            logger.info("WATCHDOG: Health check passed. Update verified.")
                            success = True
                            break
                    except:
                        pass
                time.sleep(CHECK_INTERVAL)

            if success:
                state = load_update_state()
                try:
                    with open(UPDATE_PENDING_FILE, "r") as f:
                        new_lkg = f.read().strip()
                    state["last_known_good_commit"] = new_lkg
                    save_update_state(state)
                except:
                    pass
                os.remove(UPDATE_PENDING_FILE)
            else:
                logger.error("WATCHDOG: Health check timed out. System may be bricked.")
                if rollback():
                    logger.info("WATCHDOG: Recovery successful.")
                else:
                    logger.critical("WATCHDOG: Recovery failed!")

                if os.path.exists(UPDATE_PENDING_FILE):
                    os.remove(UPDATE_PENDING_FILE)

        # Idle cadence. Kept short so delegated restart requests + ollama-setup
        # requests + on-disk code changes are picked up within ~10s (idle iterations
        # are just cheap file-existence checks). Not busy-spinning.
        time.sleep(10)

if __name__ == "__main__":
    main()