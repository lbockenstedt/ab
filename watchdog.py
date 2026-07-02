import os, json, time, requests, subprocess, logging
from datetime import datetime

# Config
CONFIG_DIR = "/etc/bugfixer"
UPDATE_STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
UPDATE_PENDING_FILE = os.path.join(CONFIG_DIR, "update_pending")
STARTUP_STAMP_FILE = os.path.join(CONFIG_DIR, "startup_stamp.json")
HEALTH_URL = "http://localhost:8000/api/health"
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
# The BugFixerWatchdog logger name carries identity via %(name)s, replacing
# the literal [WATCHDOG] format tag (now standard across all LM components).
configure_logging(log_file="/var/log/bugfixer_watchdog.log")
logger = logging.getLogger("BugFixerWatchdog")

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
        res = subprocess.run(["systemctl", "is-active", "bugfixer"], capture_output=True, text=True)
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
    """Detached `systemctl restart bugfixer` that survives this process dying. The
    service runs as root, so sudo is only a fallback when not root."""
    cmd = ["systemctl", "restart", "bugfixer"]
    if os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    subprocess.Popen(cmd, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    logger.info("WATCHDOG: spawned detached restart.")

def rollback():
    logger.warning("WATCHDOG: Initiating rollback to last known good commit...")
    state = load_update_state()
    lkg = state.get("last_known_good_commit")
    if not lkg:
        logger.error("WATCHDOG: No last known good commit found. Cannot rollback.")
        return False

    try:
        app_dir = "/opt/bugfixer"
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

def main():
    logger.info("BugFixer Watchdog started.")
    while True:
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
                        resp = requests.get(HEALTH_URL, timeout=2)
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

        time.sleep(30)

if __name__ == "__main__":
    main()