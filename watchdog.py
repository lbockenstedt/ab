import os, json, time, requests, subprocess, logging
from datetime import datetime

# Config
CONFIG_DIR = "/etc/bugfixer"
UPDATE_STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
UPDATE_PENDING_FILE = os.path.join(CONFIG_DIR, "update_pending")
HEALTH_URL = "http://localhost:8000/api/health"
CHECK_INTERVAL = 5 # seconds
HEALTH_TIMEOUT = 60 # seconds

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [WATCHDOG] %(levelname)s %(message)s',
    handlers=[logging.FileHandler("/var/log/bugfixer_watchdog.log"), logging.StreamHandler()]
)
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

def rollback():
    logger.warning("WATCHDOG: Initiating rollback to last known good commit...")
    state = load_update_state()
    lkg = state.get("last_known_good_commit")
    if not lkg:
        logger.error("WATCHDOG: No last known good commit found. Cannot rollback.")
        return False

    try:
        # The app is typically installed in /opt/bugfixer
        app_dir = "/opt/bugfixer"
        if not os.path.exists(app_dir):
            # Fallback: try to find the directory containing the current process or based on common patterns
            # In a real deployment, this should be absolute.
            logger.error(f"WATCHDOG: Application directory {app_dir} not found. Rollback impossible.")
            return False

        subprocess.run(["git", "-C", app_dir, "reset", "--hard", lkg], check=True)
        logger.info(f"WATCHDOG: Rolled back to {lkg[:7]}")

        # Mark current commit as failed
        try:
            with open(UPDATE_PENDING_FILE, "r") as f:
                failed_commit = f.read().strip()
            if failed_commit and failed_commit not in state["failed_commits"]:
                state["failed_commits"].append(failed_commit)
                save_update_state(state)
        except:
            pass

        subprocess.run(["sudo", "systemctl", "restart", "bugfixer"], check=True)
        return True
    except Exception as e:
        logger.error(f"WATCHDOG: Rollback failed: {e}")
        return False

def main():
    logger.info("BugFixer Watchdog started.")
    while True:
        if os.path.exists(UPDATE_PENDING_FILE):
            logger.info("WATCHDOG: Update pending detected. Monitoring health...")
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
                # Update LKG to the commit that just passed
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

        time.sleep(30) # Poll for pending updates every 30s

if __name__ == "__main__":
    main()
