import os, json, time, tempfile, threading, requests, logging, traceback, py_compile, random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from github import Github, GithubException
import ollama
import git

# Setup Logging
DEFAULT_LOG_FILE = "/var/log/bugfixer.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

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

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BugFixer")
logger.info(f"BugFixer started. Logging level: {LOG_LEVEL}. Logging to: {log_file}")

# Persistent Configuration Paths
CONFIG_DIR = "/etc/bugfixer"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
STATE_FILE = os.path.join(CONFIG_DIR, "processed_issues.json")
UPDATE_STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
VERSION_FILE = os.path.join(os.getcwd(), "VERSION")

def save_config(config):
    """Saves configuration to persistent storage, falling back to local if needed."""
    try:
        if os.path.exists(CONFIG_DIR) or os.access(CONFIG_DIR, os.W_OK):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            logger.info(f"Config saved to persistent storage: {CONFIG_FILE}")
        else:
            raise IOError("Persistent config directory not writable")
    except Exception as e:
        logger.warning(f"Could not save to persistent storage ({e}), falling back to local config.json")
        try:
            with open("config.json", "w") as f:
                json.dump(config, f, indent=2)
        except Exception as fe:
            logger.error(f"Critical failure saving config: {fe}")

def load_config():
    # Try persistent config first
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
                # Ensure enabled_models exists
                if "enabled_models" not in config:
                    config["enabled_models"] = []
                return config
        except Exception as e:
            logger.error(f"Error reading persistent config {CONFIG_FILE}: {e}")
    # Fallback to local config
    try:
        with open("config.json", "r") as f:
            config = json.load(f)
            if "enabled_models" not in config:
                config["enabled_models"] = []
            return config
    except:
        return {
            "monitored_repos": [],
            "trusted_repos": [],
            "default_branch": "main",
            "direct_push_enabled": False,
            "dev_branch": "dev",
            "repo_tests": {},
            "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", ""),
            "monitored_labels": ["automated-fix"],
            "enabled_models": [],
            "self_diagnosis_repo": ""
        }

# [Remaining code unchanged up to run_scan_cycle] ...

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
            logger.warning("No GitHub Token configured. Skipping scan.")
            return
        gh_current = Github(token)
        try:
            logger.info("Attempting to authenticate with GitHub API...")
            bot_user = gh_current.get_user().login
            logger.info(f"Authenticated as GitHub user: {bot_user}")
        except GithubException as ge:
            if ge.status == 401:
                logger.error("GitHub Authentication Failed: 401 Unauthorized. Please check your token.")
                return
            logger.error(f"GitHub API Error: {ge.status} - {ge.data}")
            return
        except Exception as e:
            logger.exception(f"Unexpected error during GitHub authentication: {e}")
            return

        raw_repos = config.get("monitored_repos", [])
        monitored_repos = []
        if isinstance(raw_repos, list):
            for r in raw_repos:
                for split_r in r.replace("\\n", ",").split(","):
                    cleaned = clean_repo_name(split_r)
                    if cleaned:
                        monitored_repos.append(cleaned)
        elif isinstance(raw_repos, str):
            for split_r in raw_repos.replace("\\n", ",").split(","):
                cleaned = clean_repo_name(split_r)
                if cleaned:
                        monitored_repos.append(cleaned)

        monitored_repos = list(set(monitored_repos))
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
                executor.submit(scan_hub_logs, gh_current, config),
                executor.submit(scan_repo_issues, gh_current, config, processed)
            ]
            for future in futures:
                future.result()

        save_processed(processed)
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

# [Rest of the file unchanged up to run_scan_cycle] ...

# The fix involves modifying the LLM request function to properly handle keyword arguments,
# especially removing the 'timeout' argument when it's not supported.

# Placeholder for the missing function definition
# Original error: call_llm.<locals>._request.<locals>.attempt_request() got an unexpected keyword argument 'timeout'
# This typically occurs when 'timeout' is passed to requests.get/post but not supported by a wrapper function.

# Suggested fix: Modify the call_llm function and its inner _request/attempt_request functions to:
# 1. Check if 'timeout' is in kwargs before passing it to requests
# 2. Handle ollama and other LLM providers correctly
# 3. Use default timeouts where appropriate

# Note: The original error suggests that somewhere in the code, 'timeout' is being passed to attempt_request()
# but attempt_request() does not accept it. This needs to be fixed in the actual function definition.
# Since the function definition is not provided in the original code, this fix assumes
# a common pattern where timeout should be handled explicitly.