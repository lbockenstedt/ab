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

# Define is_cloud globally to fix 'name is not defined' error
is_cloud = os.getenv("IS_CLOUD", "false").lower() == "true"

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

# New utility to validate OLLAMA API configuration
# This prevents 401 Unauthorized by enforcing environment and config consistency
def validate_ollama_config():
    """Ensure OLLAMA_API_KEY is set if using Ollama cloud endpoints."""
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_api_key = os.getenv("OLLAMA_API_KEY", None)
    
    # If user explicitly configured a cloud endpoint, require API key
    if "ollama.com" in ollama_base_url:
        if not ollama_api_key:
            logger.warning("OLLAMA_API_KEY not set, but OLLAMA_BASE_URL points to cloud endpoint. API calls will fail with 401 Unauthorized.")
            return False
    
    return True

def run_scan_cycle():
    """Performs a single complete cycle of: Auth -> Label Discovery -> Prod Verification -> Scanning."""
    global state, is_cloud
    try:
        load_dotenv(override=True)
        config = load_config()

        # Fail fast on misconfigured LLM if OLLAMA_BASE_URL is set to ollama.com
        if not validate_ollama_config():
            logger.error("LLM misconfiguration detected. Aborting scan cycle.")
            return
        
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

# Suggested fix for LLM request retry logic, removing unsupported 'timeout' args
import functools

def safe_request(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Remove 'timeout' if not supported by the underlying function
        # We pop 'timeout' to prevent it from being passed as a keyword arg to functions like ollama.generate
        kwargs.pop('timeout', None)
        return func(*args, **kwargs)
    return wrapper

# Ollama generate wrapper to avoid passing invalid arguments
@safe_request
def call_llm_with_retry(model_name, prompt, max_retries=6, base_delay=1.0, **kwargs):
    """Calls the LLM with retry logic for 429 Too Many Requests errors."""
    # Remove unsupported arguments before sending to ollama.generate
    invalid_keys = {'timeout'}
    filtered_kwargs = {k: v for k, v in kwargs.items() if k not in invalid_keys}
    
    for attempt in range(1, max_retries + 1):
        try:
            response = ollama.generate(model=model_name, prompt=prompt, **filtered_kwargs)
            return response
        except ollama.ResponseError as e:
            error_msg = str(e)
            if "429" in error_msg and attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(f"Rate limited (429). Retrying in {delay}s... (Attempt {attempt}/{max_retries})")
                time.sleep(delay)
                continue
            else:
                logger.error(f"LLM request failed after {attempt} attempts: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error during LLM call (Attempt {attempt}): {e}")
            if attempt == max_retries:
                raise
            time.sleep(base_delay)
    raise Exception(f"LLM call failed after {max_retries} attempts due to rate limiting")

# Additional function to handle Hub logs JSON parsing safely
# This addresses: "Expecting value: line 1 column 1 (char 0)" and HTML response handling
def safe_json_response(response):
    """
    Safely parse JSON from response or log detailed error if content is non-JSON.
    Returns None if parsing fails.
    """
    try:
        return response.json()
    except json.JSONDecodeError as e:
        content_type = response.headers.get("Content-Type", "").lower()
        content_preview = response.text[:200] if response.text else "(empty)"
        logger.error(f"Failed to decode JSON from {response.url}. Status: {response.status_code}. "
                    f"Content-Type: {content_type}. Content preview: {content_preview}...")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing response JSON: {e}")
        return None