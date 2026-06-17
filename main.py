import os, json, time, tempfile, threading, requests, logging, traceback, py_compile
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
            "GITHUB_TOKEN": "",
            "monitored_labels": ["automated-fix"],
            "enabled_models": []
        }

def load_processed():
    # Try persistent state first
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()} for issue_id in data}
                return data
        except: pass
    # Fallback to local state
    if os.path.exists("processed_issues.json"):
        try:
            with open("processed_issues.json", "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()} for issue_id in data}
                return data
        except: pass
    return {}

def save_processed(processed):
    with open(STATE_FILE, "w") as f: json.dump(processed, f, indent=2)

def load_update_state():
    """Loads the update state for recovery."""
    if os.path.exists(UPDATE_STATE_FILE):
        try:
            with open(UPDATE_STATE_FILE, "r") as f:
                return json.load(f)
        except: pass
    return {"last_known_good_commit": None, "failed_commits": []}

def save_update_state(state):
    """Saves the update state for recovery."""
    try:
        with open(UPDATE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving update state: {e}")


def get_version():
    try:
        with open(VERSION_FILE, "r") as f: return f.read().strip()
    except: return "Unknown"

load_dotenv(ENV_FILE)
app = FastAPI()

# Use absolute path for templates to avoid 500 errors if CWD changes
template_path = os.path.join(os.getcwd(), "templates")
templates = Jinja2Templates(directory=template_path)

def is_local_llm_allowed():
    """Checks if the local LLM is allowed to be used based on the configured schedule."""
    config = load_config()
    schedule_str = config.get("LOCAL_LLM_SCHEDULE") or os.getenv("LOCAL_LLM_SCHEDULE", "9-16,1-5")

    try:
        now = datetime.now().hour
        # Expected format: "9-16,1-5"
        ranges = schedule_str.split(',')
        for r in ranges:
            if '-' in r:
                start, end = map(int, r.split('-'))
                if start <= now < end:
                    return True
    except Exception as e:
        logger.error(f"Error parsing LLM schedule '{schedule_str}': {e}")
        # Fallback to a reasonable default if parsing fails
        if (9 <= now < 16) or (1 <= now < 5):
            return True
    return False

# Shared LLM Utility
def call_llm(prompt, system_prompt="You are a helpful AI assistant.", force_cloud=None, task_id=None, model_override=None, url_override=None):
    """Generic LLM caller with Local -> Cloud failover and JSON extraction. Now supports per-task streaming."""
    global state
    config = load_config()
    l_mod = model_override if model_override else (config.get("LOCAL_OLLAMA_MODEL") or os.getenv("LOCAL_OLLAMA_MODEL"))
    c_mod = model_override if model_override else (config.get("CLOUD_OLLAMA_MODEL") or os.getenv("CLOUD_OLLAMA_MODEL"))
    l_url = url_override if url_override else (config.get("LOCAL_OLLAMA_URL") or os.getenv("LOCAL_OLLAMA_URL"))
    c_url = config.get("CLOUD_OLLAMA_URL") or os.getenv("CLOUD_OLLAMA_URL")
    api_key = config.get("OLLAMA_API_KEY") or os.getenv("OLLAMA_API_KEY")

    def _request(url, model):
        if "api.ollama.com" in url:
            logger.warning(f"Detected potentially incorrect Cloud LLM URL: {url}. Official Ollama Cloud host is 'https://ollama.com'. Please check your settings.")

        is_cloud = ("ollama.com" in url) and ("local" not in url)

        if is_cloud:
            primary_endpoint = f"{url.rstrip('/')}/api/generate"
            primary_use_generate = True
        else:
            primary_endpoint = f"{url.rstrip('/')}/api/chat"
            primary_use_generate = False

        timeout_val = int(load_config().get("LLM_TIMEOUT", 900))

        def attempt_request(endpoint, use_generate_api, timeout=None):
            if timeout is None: timeout = 900

            if use_generate_api:
                full_prompt = f"System: {system_prompt}\n\nUser: {prompt}"
                payload = {"model": model, "prompt": full_prompt, "stream": True}
            else:
                payload = {
                    "model": model,
                    "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
                    "stream": True
                }

            headers = {}
            if api_key:
                clean_key = api_key.strip().strip('"').strip("'")
                token_only = clean_key.replace("Bearer ", "").strip()
                headers["Authorization"] = f"Bearer {token_only}"

            try:
                resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout, stream=True)
                if resp.status_code == 401:
                    logger.error(f"LLM 401 Unauthorized at {endpoint}. Verify OLLAMA_API_KEY.")
                    resp.raise_for_status()
                resp.raise_for_status()
                full_response = ""
                for line in resp.iter_lines():
                    if line:
                        chunk = json.loads(line.decode('utf-8'))
                        content = chunk.get('response') or chunk.get('message', {}).get('content', '')
                        full_response += content
                        state["llm_stream"] = full_response
                        if task_id and task_id in state["active_tasks"]:
                            state["active_tasks"][task_id]["stream"] = full_response
                return full_response
            except Exception as e:
                raise e

        try:
            return attempt_request(primary_endpoint, primary_use_generate, timeout=timeout_val)
        except Exception as e:
            if not is_cloud:
                fallback_endpoint = f"{url.rstrip('/')}/api/generate"
                if fallback_endpoint != primary_endpoint:
                    logger.info(f"Local /api/chat failed ({e}). Attempting fallback to /api/generate...")
                    try:
                        return attempt_request(fallback_endpoint, True, timeout=timeout_val)
                    except Exception as fe:
                        logger.error(f"Local fallback to /api/generate also failed: {fe}")
                        raise e
            raise e

    use_cloud = force_cloud if force_cloud is not None else state["force_cloud"]

    if not use_cloud and not is_local_llm_allowed():
        logger.info("Local LLM not allowed at this hour. Forcing fallback to Cloud.")
        use_cloud = True

    try:
        if use_cloud:
            state["active_llm"] = f"Cloud ({c_url})"
            return _request(c_url, c_mod)
        try:
            state["active_llm"] = f"Local ({l_url})"
            return _request(l_url, l_mod)
        except Exception as e:
            logger.warning(f"Local LLM failed: {e}. Falling back to Cloud...")
            state["active_llm"] = f"Cloud ({c_url})"
            return _request(c_url, c_mod)
    except Exception as e:
        logger.error(f"LLM request failed after all attempts: {e}")
        raise e

def run_sandboxed_command(command, cwd):
    """Executes a command in a Docker container if available, otherwise on host."""
    import subprocess

    docker_available = False
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        docker_available = True
    except:
        pass

    if not docker_available:
        logger.warning("⚠️ Docker not found. Running command on HOST with ROOT privileges. This is insecure.")
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=300, shell=True)

    image = "ubuntu:latest"
    files = os.listdir(cwd)
    if "package.json" in files: image = "node:18-slim"
    elif "requirements.txt" in files or "pyproject.toml" in files: image = "python:3.9-slim"
    elif "go.mod" in files: image = "golang:1.21-slim"

    logger.info(f"Running sandboxed command in Docker image {image}...")

    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{cwd}:/app",
        "-w", "/app",
        image,
        "sh", "-c", command
    ]

    try:
        result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=300)
        from dataclasses import dataclass
        @dataclass
        class MockResult:
            stdout: str
            stderr: str
            returncode: int

        return MockResult(result.stdout, result.stderr, result.returncode)
    except Exception as e:
        logger.error(f"Docker execution error: {e}")
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=300, shell=True)

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

_task_state_lock = threading.Lock()

def update_task_state(task_id, task_name="Unknown Task", action="start"):
    """Manages active tasks and their start times. action can be 'start' or 'end'.

    task_name is optional and defaults to 'Unknown Task' to safely handle cases
    where a caller cannot provide a meaningful name (e.g. during error cleanup
    before the task name is known). This prevents the
    'missing 1 required positional argument: task_name' error observed in the
    poller when an exception is raised prior to task_id/task_name assignment.

    All callers MUST use keyword arguments (action=..., task_name=...) so the
    positional-argument count can never be wrong, even if this function's
    signature evolves in the future.
    """
    global state
    if not task_id:
        logger.debug("update_task_state called with no task_id; ignoring.")
        return
    try:
        if action == "start":
            with _task_state_lock:
                state["active_tasks"][task_id] = {
                    "name": task_name,
                    "start_time": datetime.now(),
                    "stream": ""
                }
            logger.info(f"Task started: {task_id} - {task_name}")
        elif action == "end":
            with _task_state_lock:
                if task_id in state["active_tasks"]:
                    del state["active_tasks"][task_id]
            logger.info(f"Task completed: {task_id}")
    except Exception as e:
        logger.error(f"update_task_state failed for task_id={task_id!r} action={action!r}: {e}")

config_on_start = load_config()
processed_init = load_processed()
success_count = sum(1 for info in processed_init.values() if info.get("status") in ["fixed", "verified", "awaiting_prod_verification"])
failure_count = sum(1 for info in processed_init.values() if info.get("status") == "failed")

state = {
    "status": "Idle", "active_llm": "Unknown", "local_online": False, "cloud_online": False,
    "last_run": "Never", "api_status": "Not Triggered",
    "processed": processed_init, "force_cloud": config_on_start.get("force_cloud", os.getenv("FORCE_CLOUD", "False").lower() == "true"), "version": get_version(), "llm_stream": "",
    "active_tasks": {}, "qa_enabled": config_on_start.get("qa_enabled", True),
    "success_count": success_count, "failure_count": failure_count
}

def clean_repo_name(name):
    """Converts a full GitHub URL or a 'user/repo' string into 'user/repo' format."""
    name = name.strip()
    if name.startswith("http"):
        name = name.replace("https://", "").replace("http://", "")
        name = name.replace("github.com/", "")
        if name.endswith(".git"):
            name = name[:-4]
        name = name.rstrip("/")
    return name

def get_monitored_repos(config):
    """Extracts and normalizes a list of monitored repositories from config."""
    raw_repos = config.get("monitored_repos", [])
    monitored_repos = []
    if isinstance(raw_repos, list):
        for r in raw_repos:
            for split_r in r.replace("\\n", ",").split(","):
                cleaned = clean_repo_name(split_r)
                if cleaned: monitored_repos.append(cleaned)
    elif isinstance(raw_repos, str):
        for split_r in raw_repos.replace("\\n", ",").split(","):
            cleaned = clean_repo_name(split_r)
            if cleaned: monitored_repos.append(cleaned)
    return list(set(monitored_repos))

def discover_labels(gh_current, monitored_repos):
    """Fetches all unique labels from all monitored repositories, including built-in defaults."""
    all_labels = {"automated-fix", "bug", "critical", "high-priority"}
    for repo_name in monitored_repos:
        try:
            repo = gh_current.get_repo(repo_name)
            labels = repo.get_labels()
            for label in labels:
                all_labels.add(label.name)
        except Exception as e:
            logger.error(f"Error discovering labels for {repo_name}: {e}")
    return sorted(list(all_labels))

def bump_repo_version(repo_path):
    """Increments the version in the VERSION file of the target repository."""
    version_file = os.path.join(repo_path, "VERSION")
    current_version = "V.00"
    if os.path.exists(version_file):
        try:
            with open(version_file, "r") as f:
                current_version = f.read().strip()
        except Exception as e:
            logger.error(f"Error reading version file: {e}")

    if current_version.startswith("V."):
        try:
            ver_num = int(current_version[2:])
            new_version = f"V.{ver_num + 1:02d}"
        except ValueError:
            new_version = "V.01"
    else:
        new_version = "V.01"

    try:
        with open(version_file, "w") as f:
            f.write(new_version)
        return new_version
    except Exception as e:
        logger.error(f"Error writing version file: {e}")
        return None

def trigger_infrastructure_update():
    url = os.getenv("UPDATE_API_URL")
    if not url or "your-netbox" in url: return "URL not configured"
    try:
        resp = requests.post(url, json={}, timeout=10)
        return "SUCCESS: Sync Triggered" if resp.status_code == 200 else f"FAILED: {resp.status_code}"
    except Exception as e: return f"ERROR: {str(e)}"

def find_global_duplicate_issue(gh_current, monitored_repos, error_data):
    """Searches across all monitored repositories for an existing open issue that matches the error.

    Returns a tuple (issue, repo_name) where repo_name is the repository in which the
    duplicate issue was found. The repo_name is returned explicitly so callers do NOT
    need to read repository metadata off the Issue object itself.

    Safely handles error_data payloads that may be missing the 'title' or 'body' keys
    (the LLM may omit them). Missing fields are treated as empty strings so that the
    deduplication search degrades gracefully instead of raising a KeyError.
    """
    title_text = (error_data.get('title') or '').lower()
    body_text = (error_data.get('body') or '').lower()

    # If we have nothing to match against, there is no point scanning every repo.
    if not title_text and not body_text:
        return None, None

    for repo_name in monitored_repos:
        try:
            repo = gh_current.get_repo(repo_name)
            open_issues = repo.get_issues(state='open')
            for issue in open_issues:
                issue_body = issue.body or ""
                if title_text in issue.title.lower() or body_text in issue_body.lower():
                    return issue, repo_name
        except Exception as e:
            logger.debug(f"Could not search for duplicates in {repo_name}: {e}")
    return None, None

def create_automated_issue(gh_current, monitored_repos, gh_repo, error_data):
    """Creates a GitHub issue for a log-detected error, deduplicating globally across monitored repos.

    The 'body' field is required to create a meaningful issue. If it is missing or
    empty, the function logs a warning and returns None instead of raising a
    KeyError, which previously crashed automated issue creation with: 'body'.
    """
    try:
        title_text = error_data.get('title')
        body_text = error_data.get('body')

        # Ensure the body field is populated before attempting creation.
        if not body_text or not str(body_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'body' field is missing or empty. "
                f"Title was: {title_text!r}. Full error_data: {error_data}"
            )
            return None

        if not title_text or not str(title_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'title' field is missing or empty. "
                f"Body was: {str(body_text)[:120]!r}"
            )
            return None

        current_repo_name = error_data.get('repo') or gh_repo.full_name

        existing_issue, duplicate_repo_name = find_global_duplicate_issue(gh_current, monitored_repos, error_data)

        if existing_issue:
            duplicate_repo_display = duplicate_repo_name or current_repo_name
            logger.info(f"Global duplicate issue detected: #{existing_issue.number} in {duplicate_repo_display}. Adding info.")

            existing_body = existing_issue.body or ""
            if body_text.lower() not in existing_body.lower():
                existing_issue.create_comment(
                    f"🤖 **BugFixer Update**\n\nAdditional instance of this error detected in repository **{current_repo_name}:**\n\n"
                    f"```\n{body_text}\n```"
                )
                logger.info(f"Added additional evidence from {current_repo_name} to issue #{existing_issue.number}")

            return existing_issue

        full_title = f"🤖 Log Alert: {title_text}"
        full_body = (
            f"**Automated Error Detection**\n\n"
            f"The BugFixer Hub analysis detected a potential issue in the logs:\n\n"
            f"### Log Evidence:\n```\n{body_text}\n```\n\n"
            f"This issue has been automatically created for fixing."
        )
        issue = gh_repo.create_issue(
            title=full_title,
            body=full_body,
            labels=["automated-fix", "log-detected"]
        )
        logger.info(f"Created automated issue #{issue.number} for {current_repo_name}")
        return issue
    except Exception as e:
        logger.error(f"Failed to handle automated issue creation: {e}")
        return None

def get_hub_logs():
    """Fetches recent logs from the Hub for all modules. Returns a list of log entries."""
    config = load_config()
    url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
    if not url or "your-netbox" in url:
        logger.debug("Hub Query URL not configured. Skipping log fetch.")
        return None
    try:
        log_url = url.rstrip('/') + "/setup/logs/all"
        logger.debug(f"Fetching Hub logs from: {log_url}")
        resp = requests.get(log_url, timeout=15)
        if resp.status_code == 200:
            body = resp.text
            if not body or not body.strip():
                logger.warning(
                    f"Hub returned 200 OK but empty response body for {log_url}. "
                    f"Skipping JSON parse to avoid json.decode error."
                )
                return []
            try:
                data = resp.json()
                if isinstance(data, dict):
                    return data.get('logs', [])
                return data if isinstance(data, list) else []
            except Exception as e:
                logger.error(f"Hub returned 200 OK but failed to parse JSON: {e}. Content: {body[:200]}...")
                return None
        logger.error(f"Hub returned unexpected status code {resp.status_code} for {log_url}")
        return None
    except Exception as e:
        logger.error(f"Hub Log Fetch Error: {e}")
        return None

def get_hub_state():
    """Fetches the current state of the hub for verification."""
    config = load_config()
    url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
    if not url or "your-netbox" in url: return None
    try:
        resp = requests.get(url.rstrip('/'), timeout=15)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception as e:
        logger.error(f"Hub State Fetch Error: {e}")
        return None

def analyze_logs_for_errors(logs):
    """Uses LLM to identify actionable errors in aggregated logs."""
    if not logs: return []

    log_text = json.dumps(logs, indent=2)
    prompt = (
        f"Logs from Hub:\n{log_text}\n\n"
        "Analyze these logs for critical, recurring, or actionable errors that can be fixed in code. "
        "Ignore heartbeat messages or routine status updates. "
        "For each actionable error found, provide: \n"
        "1. The module/repo it belongs to.\n"
        "2. A concise summary of the bug.\n"
        "3. The specific log snippet that proves the error.\n\n"
        "Return ONLY a JSON array of objects: [{\"repo\": \"owner/repo\", \"title\": \"Error Summary\", \"body\": \"Log snippet and description\"}]. "
        "Every object MUST include non-empty 'repo', 'title', and 'body' fields."
    )
    try:
        res = call_llm(prompt, system_prompt="You are a log analysis expert. Return only a JSON array.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            # Defensive: drop any entries that are not dicts or are missing required fields.
            cleaned = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                if not entry.get('repo') or not entry.get('title') or not entry.get('body'):
                    logger.debug(f"Dropping malformed log-analysis entry (missing repo/title/body): {entry}")
                    continue
                cleaned.append(entry)
            return cleaned
        return []
    except Exception as e:
        logger.error(f"Error analyzing logs: {e}")
        return []

def connectivity_worker():
    """Hourly check to verify both local and cloud LLM responses."""
    while True:
        try:
            config = load_config()
            l_url = config.get("LOCAL_OLLAMA_URL") or os.getenv("LOCAL_OLLAMA_URL")
            c_url = config.get("CLOUD_OLLAMA_URL") or os.getenv("CLOUD_OLLAMA_URL")
            l_mod = config.get("LOCAL_OLLAMA_MODEL") or os.getenv("LOCAL_OLLAMA_MODEL")
            c_mod = config.get("CLOUD_OLLAMA_MODEL") or os.getenv("CLOUD_OLLAMA_MODEL")
            api_key = config.get("OLLAMA_API_KEY") or os.getenv("OLLAMA_API_KEY")

            if l_url:
                try:
                    payload = {"model": l_mod, "prompt": "ping", "stream": False}
                    resp = requests.post(f"{l_url.rstrip('/')}/api/generate", json=payload, timeout=10)
                    state["local_online"] = (resp.status_code == 200)
                except:
                    state["local_online"] = False

            if c_url:
                try:
                    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                    payload = {"model": c_mod, "prompt": "ping", "stream": False}
                    resp = requests.post(f"{c_url.rstrip('/')}/api/generate", json=payload, headers=headers, timeout=10)
                    state["cloud_online"] = (resp.status_code == 200)
                except:
                    state["cloud_online"] = False

            logger.info(f"Hourly Connectivity Check: Local={state['local_online']}, Cloud={state['cloud_online']}")
        except Exception as e:
            logger.error(f"Connectivity worker error: {e}")

        time.sleep(3600)

def heartbeat_worker():
    while True:
        try:
            config = load_config()
            local_url = config.get("LOCAL_OLLAMA_URL") or os.getenv("LOCAL_OLLAMA_URL")
            cloud_url = config.get("CLOUD_OLLAMA_URL") or os.getenv("CLOUD_OLLAMA_URL")

            if local_url:
                try:
                    requests.get(f"{local_url}/api/tags", timeout=2)
                    state["local_online"] = True
                except:
                    state["local_online"] = False
            else:
                state["local_online"] = False

            if state["force_cloud"]:
                state["active_llm"] = "Cloud"
            elif state["local_online"]:
                state["active_llm"] = "Local"
            else:
                if cloud_url:
                    state["active_llm"] = "Cloud"
                else:
                    state["active_llm"] = "No LLM Available"
        except Exception as e:
            logger.error(f"Heartbeat worker error: {e}")

        time.sleep(5)

def analyze_issue(issue):
    full_context = f"Issue Title: {issue.title}\nIssue Body: {issue.body}\n\n"
    comments = issue.get_comments()
    for i, comment in enumerate(comments, 1):
        full_context += f"Comment {i}: {comment.body}\n"

    config = load_config()
    strictness = config.get("TRIAGE_STRICTNESS", "Moderate")

    if strictness == "Strict":
        strictness_instruction = "Specifically, for UI or runtime errors, you MUST have full console logs or stack traces. If these are missing, it is non-actionable."
    elif strictness == "Lenient":
        strictness_instruction = "Be generous. If the issue describes a bug and the repository is accessible, mark it as actionable even if full logs are missing, provided there is a plausible lead."
    else:
        strictness_instruction = "Specifically, for UI or runtime errors, prefer console logs or stack traces, but if the description is detailed enough for a senior engineer to hypothesize the bug accurately, mark it as actionable."

    prompt = (
        f"{full_context}\n\n"
        f"Determine if this issue contains enough information to provide a code fix. \n"
        f"{strictness_instruction}\n"
        f"Note: If this is an automated log alert, the provided log snippet is the primary evidence. Do not request a stack trace if a clear error is already present in the logs.\n"
        f"If information is missing, specify exactly what is needed (e.g., 'Please provide the browser console output').\n\n"
        "Return ONLY a JSON object: {\"actionable\": boolean, \"request\": \"message if not actionable\"}"
    )
    try:
        res = call_llm(prompt, system_prompt="You are a triage bot. Only return a JSON object.")
        import re
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data.get("actionable", False), data.get("request", "More information is needed to proceed with a fix.")
        return False, "Information provided is not in a usable format. Please provide more details."
    except Exception as e:
        logger.error(f"Error analyzing issue: {e}")
        return True, ""

def identify_files_to_fix(repo_path, issue_body):
    logger.info("Identifying relevant files for fix...")
    all_files = []
    for root, _, files in os.walk(repo_path):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, repo_path)
            if any(x in rel_path for x in [".git", "node_modules", "__pycache__", "venv", ".env"]):
                continue
            all_files.append(rel_path)
    file_list_str = "\n".join(all_files)
    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Repository File List:\n{file_list_str}\n\n"
        "Identify which files are most likely relevant to fixing this issue. "
        "Return ONLY a JSON array of file paths: [\"path/to/file1\", \"path/to/file2\"]"
    )
    try:
        res = call_llm(prompt, system_prompt="You are a repository analyzer. Only return a JSON array of paths.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []
    except Exception as e:
        logger.error(f"Error identifying files: {e}")
        return []

def prepare_environment(repo_path):
    logger.info("Preparing environment (installing dependencies)...")
    files = os.listdir(repo_path)
    if "package.json" in files:
        logger.info("Detected Node.js project. Running npm install...")
        run_sandboxed_command("npm install", repo_path)
    elif "requirements.txt" in files:
        logger.info("Detected Python project with requirements.txt. Running pip install...")
        run_sandboxed_command("pip install -r requirements.txt", repo_path)
    elif "pyproject.toml" in files:
        logger.info("Detected Python project with pyproject.toml. Running pip install .")
        run_sandboxed_command("pip install .", repo_path)
    elif "go.mod" in files:
        logger.info("Detected Go project. Running go mod download...")
        run_sandboxed_command("go mod download", repo_path)
    elif "Makefile" in files:
        logger.info("Detected Makefile. Attempting 'make install'...")
        run_sandboxed_command("make install", repo_path)
    else:
        logger.info("No known dependency file detected. Skipping installation.")

def review_fix(repo_path, issue_body, proposed_fixes, force_cloud=None, task_id=None):
    logger.info("Running Reviewer Panel pass...")
    config = load_config()

    reviewers = []
    r1_mod = config.get("REVIEWER_MODEL_1")
    if r1_mod: reviewers.append({"name": "Reviewer 1", "model": r1_mod, "force_cloud": True})

    r2_mod = config.get("REVIEWER_MODEL_2")
    if r2_mod: reviewers.append({"name": "Reviewer 2", "model": r2_mod, "force_cloud": True})

    l_mod = config.get("LOCAL_OLLAMA_MODEL") or os.getenv("LOCAL_OLLAMA_MODEL")
    if l_mod and state["local_online"]:
        active_cloud_models = [r["model"] for r in reviewers]
        is_duplicate = any(l_mod == cm for cm in active_cloud_models)
        if not is_duplicate:
            reviewers.append({"name": "Reviewer 3 (Local)", "model": l_mod, "force_cloud": False})
        else:
            logger.info(f"Skipping Reviewer 3 (Local) as it is a duplicate of a cloud model: {l_mod}")

    if not reviewers:
        logger.warning("No active reviewers found. Falling back to default LLM review.")
        reviewers = [{"name": "Default Reviewer", "model": None, "force_cloud": None}]

    fix_details = ""
    for path, code in proposed_fixes.items():
        fix_details += f"\n--- FILE: {path} ---\n{code}\n"

    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Proposed Fixes:\n{fix_details}\n\n"
        "You are a Skeptical Senior Engineer. Your job is to review this proposed fix. "
        "Check for: \n"
        "1. Does it actually fix the described issue?\n"
        "2. Does it introduce new bugs or regressions?\n"
        "3. Is the code quality acceptable?\n"
        "4. Are there any obvious edge cases missed?\n\n"
        "Return ONLY a JSON object: {\"confidence\": float, \"verdict\": \"Approve\"|\"Reject\", \"critique\": \"detailed explanation\"}"
    )

    votes = []
    for r in reviewers:
        try:
            logger.info(f"Reviewer {r['name']} analyzing...")
            res = call_llm(prompt, system_prompt="You are a skeptical senior engineer. Be critical. Only return JSON.",
                           force_cloud=r["force_cloud"], task_id=task_id, model_override=r.get("model"))

            import re
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if match:
                votes.append(json.loads(match.group()))
        except Exception as e:
            logger.error(f"Reviewer {r['name']} error: {e}")

    if not votes:
        return {"confidence": 0.0, "verdict": "Reject", "critique": "All reviewers failed."}

    approvals = [v for v in votes if v.get("verdict") == "Approve"]
    avg_conf = sum(v.get("confidence", 0.0) for v in votes) / len(votes)

    if len(approvals) >= (len(votes) / 2 + 0.5):
        final_verdict = "Approve"
        critiques = " | ".join([v.get("critique", "") for v in votes])
    else:
        final_verdict = "Reject"
        critiques = " | ".join([v.get("critique", "") for v in votes])

    return {"confidence": avg_conf, "verdict": final_verdict, "critique": critiques}

def apply_ai_fix(repo_path, issue_body, error_context=None, force_cloud=None, task_id=None):
    relevant_files = identify_files_to_fix(repo_path, issue_body)
    if not relevant_files:
        logger.warning(f"No specific files identified for issue. Attempting general fix.")
    context_code = ""
    for f_path in relevant_files:
        full_p = os.path.join(repo_path, f_path)
        if os.path.exists(full_p):
            try:
                with open(full_p, 'r') as f:
                    context_code += f"\n--- FILE: {f_path} ---\n{f.read()}\n"
            except Exception as e:
                logger.error(f"Could not read file {f_path}: {e}")
    if error_context:
        prompt = (
            f"Issue: {issue_body}\n\n"
            f"Current Relevant Code:\n{context_code}\n\n"
            f"Previous attempt failed with error:\n{error_context}\n\n"
            "Provide a corrected version of the code. Return ONLY a JSON object with two keys: 'confidence' (a float from 0.0 to 1.0) and 'fixes' (another object where keys are file paths and values are the full new file content). "
            "Example: {\"confidence\": 0.98, \"fixes\": {\"src/main.py\": \"full code here\"}}"
        )
    else:
        prompt = (
            f"Issue: {issue_body}\n\n"
            f"Current Relevant Code:\n{context_code}\n\n"
            "Provide a corrected version of the code. Return ONLY a JSON object with two keys: 'confidence' (a float from 0.0 to 1.0) and 'fixes' (another object where keys are file paths and values are the full new file content). "
            "Example: {\"confidence\": 0.98, \"fixes\": {\"src/main.py\": \"full code here\"}}"
        )
    try:
        return call_llm(prompt, system_prompt="You are a master coder. Only return a JSON object.", force_cloud=force_cloud, task_id=task_id)
    except Exception as e:
        raise Exception(f"Fix generation failed: {e}")

def parse_and_apply(content, repo_path):
    try:
        import re
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            data = json.loads(content)
        fixes = data.get("fixes", {})
        confidence = data.get("confidence", 0.0)
        for filepath, code in fixes.items():
            full_path = os.path.join(repo_path, filepath)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(code.strip())
            logger.info(f"Applied fix to file: {filepath}")
        return True, fixes, confidence
    except Exception as e:
        logger.error(f"Error parsing or applying JSON fix: {e}")
        logger.debug(f"Failed content: {content}")
        return False, {}, 0.0

def verify_fix(repo_path, repo_name, config):
    logger.info(f"Verifying fix in {repo_path}...")
    repo_tests = config.get("repo_tests", {})
    test_cmd = repo_tests.get(repo_name)
    if test_cmd:
        logger.info(f"Using per-repo test command for {repo_name}: {test_cmd}")
    else:
        qa_repo = os.getenv("QA_REPO")
        test_cmd = os.getenv("QA_TEST_COMMAND", "pytest")
        if qa_repo:
            logger.info(f"Using external QA repository: {qa_repo}")
            token = os.getenv("GITHUB_TOKEN")
            qa_path = os.path.join(os.path.dirname(repo_path), "qa_suite")
            if not os.path.exists(qa_path):
                url = f"https://{token}@github.com/{qa_repo}.git"
                git.Repo.clone_from(url, qa_path)
                logger.info(f"Cloned QA repository to {qa_path}")
            logger.info(f"Executing QA command: {test_cmd}")
            full_cmd = f"{test_cmd} {repo_path}" if " " not in test_cmd else test_cmd
            result = run_sandboxed_command(full_cmd, qa_path)
            if result.returncode == 0:
                logger.info("External QA tests passed!")
                return True, None
            else:
                error_msg = result.stdout + result.stderr
                logger.error(f"External QA tests failed:\n{error_msg}")
                return False, error_msg
        else:
            if not test_cmd or test_cmd == "pytest":
                files = os.listdir(repo_path)
                if "package.json" in files: test_cmd = "npm test"
                elif "requirements.txt" in files or "pyproject.toml" in files: test_cmd = "python3 -m pytest"
                elif "go.mod" in files: test_cmd = "go test ./..."
                elif "Makefile" in files: test_cmd = "make test"
            if not test_cmd:
                logger.info("No standard test framework detected. Assuming success (blind apply).")
                return True, "No tests found, assuming success"
            logger.info(f"Executing test command: {test_cmd}")
            result = run_sandboxed_command(test_cmd, repo_path)
            if result.returncode == 0:
                logger.info("Tests passed successfully!")
                return True, None
            else:
                error_msg = result.stdout + result.stderr
                logger.error(f"Tests failed:\n{error_msg}")
                return False, error_msg
    result = run_sandboxed_command(test_cmd, repo_path)
    if result.returncode == 0:
        logger.info(f"Per-repo tests for {repo_name} passed!")
        return True, None
    else:
        error_msg = result.stdout + result.stderr
        logger.error(f"Per-repo tests for {repo_name} failed:\n{error_msg}")
        return False, error_msg

def check_for_updates():
    """Checks GitHub for new versions, performs pre-flight syntax checks, and signals a restart if safe."""
    try:
        self_repo = git.Repo(os.getcwd())
        old_commit = self_repo.head.commit.hexsha

        update_state = load_update_state()
        update_state["last_known_good_commit"] = old_commit
        save_update_state(update_state)

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

            import subprocess
            subprocess.Popen(["sudo", "systemctl", "restart", "bugfixer"])
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
            check_for_updates()
        except Exception as e:
            logger.error(f"Updater worker error: {e}")
        time.sleep(3600)

def find_existing_pull_request(repo_obj, target_branch, base_branch):
    """Checks whether an open pull request already exists for the given head/base pair.

    Uses the proper 'owner:branch' format for the head parameter when querying
    the GitHub API, which is required for the filtered search to work correctly.
    Falls back to a manual scan of all open PRs if the filtered query fails.
    """
    existing_pr = None

    # GitHub API requires head in 'owner:ref' format for filtered PR queries.
    # Passing just the branch name (e.g. 'dev') without the owner prefix causes
    # the filter to silently return no results, leading to 422 errors on create_pull.
    owner = repo_obj.owner.login
    head_param = f"{owner}:{target_branch}"

    try:
        existing_prs = repo_obj.get_pulls(state='open', head=head_param, base=base_branch)
        for pr_item in existing_prs:
            existing_pr = pr_item
            break
    except Exception as e:
        logger.warning(f"Filtered PR check failed for {target_branch} -> {base_branch}: {e}")

    if not existing_pr:
        try:
            all_open_prs = repo_obj.get_pulls(state='open')
            for pr_item in all_open_prs:
                if pr_item.head.ref == target_branch and pr_item.base.ref == base_branch:
                    existing_pr = pr_item
                    break
        except Exception as e:
            logger.warning(f"Manual PR scan failed for {target_branch} -> {base_branch}: {e}")

    return existing_pr

def process_single_issue(repo_name, issue_num, llm_preference=None):
    """Core logic to fix a single issue. Used by poller and manual triggers."""
    global state
    issue_id = f"{repo_name}:{issue_num}"
    try:
        config = load_config()
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            logger.error(f"Manual trigger failed: No GitHub Token configured.")
            return False, "No GitHub Token configured"

        gh_current = Github(token)
        try:
            repo_obj = gh_current.get_repo(repo_name)
            issue = repo_obj.get_issue(int(issue_num))
        except GithubException as ge:
            if ge.status == 410:
                logger.warning(f"Issue {repo_name}:{issue_num} was deleted. Skipping.")
                return False, "Issue deleted"
            raise ge

        update_task_state(task_id=issue_id, task_name=f"Triaging {issue_id}", action="start")
        actionable, request_msg = analyze_issue(issue)
        if not actionable:
            logger.info(f"Issue {repo_name}:{issue_num} is non-actionable: {request_msg}")
            issue.create_comment(f"🤖 **BugFixer Triage**\n\nThis issue is currently non-actionable. To help me fix this, please provide: {request_msg}")
            processed = load_processed()
            processed[f"{repo_name}:{issue_num}"] = {
                "status": "non-actionable",
                "timestamp": datetime.now().isoformat(),
                "reason": request_msg
            }
            save_processed(processed)
            state["processed"] = processed
            update_task_state(task_id=issue_id, action="end")
            return False, f"Non-actionable: {request_msg}"

        force_cloud = None
        if llm_preference == "cloud":
            force_cloud = True
        elif llm_preference == "local":
            force_cloud = False

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "repo")
            url = repo_obj.clone_url.replace("https://", f"https://{token}@")
            logger.info(f"Cloning {repo_name} for manual fix...")
            repo_git = git.Repo.clone_from(url, path, depth=1)

            max_attempts = 3
            success = False
            error_context = None

            for attempt in range(1, max_attempts + 1):
                update_task_state(task_id=issue_id, task_name=f"Fix Attempt {attempt}/{max_attempts} for {issue_id}", action="start")
                logger.info(f"AI Fix Attempt {attempt}/{max_attempts} for {repo_name}:{issue_num}...")
                fix_code = apply_ai_fix(path, issue.body, error_context, force_cloud=force_cloud, task_id=issue_id)

                success_applied, fixes, confidence = parse_and_apply(fix_code, path)
                if not success_applied:
                    verified = False
                    failure_msg = "AI generated invalid JSON format"
                else:
                    if config.get("skip_review", False):
                        logger.info("Skeptical Reviewer bypassed by configuration.")
                        review_conf = confidence
                        review_verdict = "Approve"
                    else:
                        update_task_state(task_id=issue_id, task_name=f"Reviewing {issue_id}", action="start")
                        review = review_fix(path, issue.body, fixes, force_cloud=force_cloud, task_id=issue_id)
                        review_conf = review.get("confidence", 0.0)
                        review_verdict = review.get("verdict", "Reject")

                    if config.get("qa_enabled", True):
                        prepare_environment(path)
                        update_task_state(task_id=issue_id, task_name=f"Verifying {issue_id}", action="start")
                        verified, failure_msg = verify_fix(path, repo_name, config)
                    else:
                        logger.info("QA Testing disabled. Assuming verified.")
                        verified, failure_msg = True, "QA disabled"

                    if verified:
                        success = True
                        state["success_count"] += 1
                        final_confidence = (confidence + review_conf) / 2
                        final_verdict = review_verdict
                        break
                    else:
                        error_context = failure_msg

            if not success:
                state["failure_count"] += 1
                failure_reason = "AI failed to find a verified fix after max attempts."
                if error_context:
                    failure_reason += f" Last attempt error: {error_context}"

                try:
                    issue.create_comment(f"🤖 **BugFixer Failure**\n\nI attempted to fix this issue {max_attempts} times, but I could not find a solution that passed verification.\n\n**Final Error:** `{failure_reason}`")
                except Exception as ge:
                    logger.error(f"Failed to post failure comment to issue {issue_id}: {ge}")

                processed = load_processed()
                processed[f"{repo_name}:{issue_num}"] = {
                    "status": "failed",
                    "timestamp": datetime.now().isoformat(),
                    "error": failure_reason
                }
                save_processed(processed)
                state["processed"] = processed
                update_task_state(task_id=issue_id, action="end")
                return False, failure_reason

            repo_git.git.add(A=True)

            confidence_threshold = 0.95
            is_trusted = repo_name in config["trusted_repos"]
            bot_user = gh_current.get_user().login
            is_owner = repo_obj.owner.login == bot_user
            direct_push_setting = config.get("direct_push_enabled")
            can_direct_push = direct_push_setting and is_trusted and is_owner

            logger.info(f"Deployment decision for {repo_name}: DirectPushSetting={direct_push_setting}, IsTrusted={is_trusted}, IsOwner={is_owner} -> can_direct_push={can_direct_push}")


            version_bumped = False
            new_v = None
            if can_direct_push and final_confidence >= confidence_threshold and final_verdict == "Approve":
                new_v = bump_repo_version(path)
                if new_v:
                    version_bumped = True
                    logger.info(f"Bumped target repository {repo_name} version to {new_v}")

            commit_msg = f"AI Fix #{issue.number}: {issue.title[:50]}..."
            if version_bumped:
                commit_msg += f" (Version Bump to {new_v})"
            repo_git.index.commit(commit_msg)

            if can_direct_push and final_verdict == "Approve":
                logger.info(f"Decision: Direct Commit to main. Reason: can_direct_push=True AND verdict='Approve' ({final_verdict})")
                decision_reason = "Trusted repo & approved"
                repo_git.remotes.origin.push()
                commit_type = "Direct Commit"
                detail_msg = f"The fix was verified and pushed directly to the main branch. Avg Confidence: {final_confidence:.2%}"
            else:
                reason = "Skeptical Reviewer rejected" if final_verdict != "Approve" else "Trust/Ownership requirements not met (can_direct_push=False)"
                decision_reason = reason
                logger.info(f"Decision: Pull Request. Reason: {reason}. (can_direct_push={can_direct_push}, verdict={final_verdict})")
                target_branch = config.get("dev_branch", "dev") if final_confidence < confidence_threshold else f"ai-fix-issue-{issue.number}"
                try:
                    repo_git.git.checkout(target_branch)
                except:
                    repo_git.create_head(target_branch).checkout()
                repo_git.remotes.origin.push(target_branch, force=True)
                base_branch = config.get("default_branch", "main")

                # Check for existing PR before attempting creation to avoid 422 errors.
                existing_pr = find_existing_pull_request(repo_obj, target_branch, base_branch)

                if existing_pr:
                    pr = existing_pr
                    logger.info(f"Found existing open PR for {target_branch} -> {base_branch}: {pr.html_url}")
                else:
                    try:
                        pr = repo_obj.create_pull(
                            title=f"AI Fix #{issue.number}",
                            body=f"Automated fix for issue #{issue.number}. Avg Confidence: {final_confidence:.2%}",
                            head=target_branch,
                            base=base_branch
                        )
                        logger.info(f"Created new PR for {target_branch} -> {base_branch}: {pr.html_url}")
                    except GithubException as ge:
                        if ge.status == 422:
                            # A PR may have been created by a concurrent process between
                            # our check and our create call. Re-check for the existing PR.
                            logger.warning(
                                f"PR creation returned 422 (likely already exists for "
                                f"{target_branch} -> {base_branch}). Re-checking for existing PR..."
                            )
                            time.sleep(2)
                            existing_pr = find_existing_pull_request(repo_obj, target_branch, base_branch)
                            if existing_pr:
                                pr = existing_pr
                                logger.info(
                                    f"Found existing open PR after 422 error: {pr.html_url}"
                                )
                            else:
                                logger.error(
                                    f"Could not find existing PR after 422 error for "
                                    f"{target_branch} -> {base_branch}. Re-raising."
                                )
                                raise ge
                        else:
                            raise ge

                commit_type = "Pull Request"
                detail_msg = f"The fix was verified and a Pull Request has been created on branch {target_branch}: {pr.html_url}"


            files_list = ", ".join(fixes.keys()) if fixes else "No files changed"
            commit_hash = repo_git.head.commit.hexsha

            comment_body = (
                f"🤖 **BugFixer AI Update**\n\n"
                f"The issue has been successfully resolved via {commit_type}.\n"
                f"{detail_msg}\n\n"
                f"**Changes:**\n- Files modified: `{files_list}`\n- Commit: `{commit_hash[:7]}`\n\n"
                f"Verification: ✅ Tests passed successfully."
            )
            issue.create_comment(comment_body)

            is_log_detected = "log-detected" in [lbl.name for lbl in issue.get_labels()]
            if not is_log_detected:
                issue.edit(state='closed')

            processed = load_processed()
            processed[f"{repo_name}:{issue_num}"] = {
                "status": "fixed" if not is_log_detected else "awaiting_prod_verification",
                "timestamp": datetime.now().isoformat(),
                "commit": commit_hash,
                "commit_msg": commit_msg,
                "files": list(fixes.keys()),
                "commit_type": commit_type,
                "decision_reason": decision_reason
            }

            save_processed(processed)
            state["processed"] = processed

            update_task_state(task_id=issue_id, action="end")
            return True, f"Fixed via {commit_type}"

    except Exception as e:
        logger.exception(f"Error in process_single_issue: {e}")
        try:
            update_task_state(task_id=issue_id, action="end")
        except Exception as cleanup_err:
            logger.error(f"Failed to clean up task state for {issue_id}: {cleanup_err}")
        return False, str(e)

def verify_production_fixes(gh_current, processed):
    """Verify issues that were 'fixed' but are awaiting log confirmation."""
    for issue_id, info in list(processed.items()):
        if info.get("status") == "awaiting_prod_verification":
            repo_name, issue_num = issue_id.split(":")
            logger.info(f"Verifying production fix for {issue_id}...")
            try:
                repo_obj = gh_current.get_repo(repo_name)
                issue = repo_obj.get_issue(int(issue_num))

                logs = get_hub_logs()
                if logs:
                    module_name = repo_name.split('/')[-1]
                    relevant_logs = [l['log'] for l in logs if l.get('module') == module_name]
                    full_log_text = "\n".join(relevant_logs)

                    import re
                    match = re.search(r"### Log Evidence:\n```\n(.*?)\n```", issue.body, re.DOTALL)
                    if match:
                        snippet = match.group(1).strip()
                        if snippet.lower() not in full_log_text.lower():
                            logger.info(f"Verified: Error snippet no longer found in logs for {issue_id}. Closing issue.")
                            issue.create_comment("🤖 **BugFixer AI Verification**\n\nProduction logs have been scanned and the error is no longer detected. Closing issue.")
                            issue.edit(state='closed')
                            processed[issue_id]["status"] = "verified"
                            state["success_count"] += 1
                            save_processed(processed)
                        else:
                            logger.info(f"Issue {issue_id} still failing in production logs.")
            except Exception as e:
                logger.error(f"Error verifying {issue_id}: {e}")

def scan_hub_logs(gh_current, config):
    """Phase: Scan Hub for new errors and create GitHub issues."""
    global state
    update_task_state(task_id="HubScan", task_name="Scanning Hub Logs", action="start")
    logger.info("Scanning Hub for new errors...")
    try:
        hub_logs = get_hub_logs()
        if hub_logs:
            actionable_errors = analyze_logs_for_errors(hub_logs)
            monitored_repos = get_monitored_repos(config)
            for error in actionable_errors:
                repo_name = error.get('repo')
                if not repo_name:
                    logger.warning(f"Skipping actionable error with no repo specified: {error.get('title')}")
                    continue
                # Ensure the body field is populated before attempting creation.
                if not error.get('body') or not str(error.get('body')).strip():
                    logger.warning(f"Skipping actionable error with no body specified: {error.get('title')} (repo={repo_name})")
                    continue
                try:
                    repo_obj = gh_current.get_repo(repo_name)
                    create_automated_issue(gh_current, monitored_repos, repo_obj, error)
                    logger.info(f"Handled automated issue for log error in {repo_name}")
                except Exception as e:
                    logger.error(f"Failed to create auto-issue for {repo_name}: {e}")
    except Exception as e:
        logger.error(f"Hub log scan failed: {e}")
    finally:
        update_task_state(task_id="HubScan", action="end")

def scan_repo_issues(gh_current, config, processed):
    """Phase: Scan monitored repos for issues and attempt fixes concurrently."""
    global state
    bot_user = gh_current.get_user().login

    monitored_repos = get_monitored_repos(config)
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

                to_fix = []
                for issue in issues:
                    try:
                        if issue.state != 'open' or issue.pull_request:
                            continue

                        issue_id = f"{repo_name}:{issue.number}"
                        if issue_id in processed:
                            status = processed[issue_id].get("status")
                            if status in ["fixed", "non-actionable", "failed", "awaiting_prod_verification"]:
                                continue
                        to_fix.append((repo_name, issue.number))
                    except Exception as e:
                        logger.exception(f"Failed to triage issue {issue_id}: {e}")

                if to_fix:
                    logger.info(f"Found {len(to_fix)} issues to fix in {repo_name}. Processing concurrently (max {max_workers})...")
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = [executor.submit(process_single_issue, r, n) for r, n in to_fix]
                        for future in futures:
                            future.result()

            except Exception as e:
                logger.exception(f"Unexpected error while processing {repo_name}: {e}")
    except Exception as e:
        logger.exception(f"scan_repo_issues failed: {e}")
    finally:
        update_task_state(task_id="RepoScan", action="end")

def scan_self_logs(gh_current, config):
    """Scans BugFixer's own logs and creates GitHub issues for internal errors."""
    global state
    update_task_state(task_id="SelfScan", task_name="Scanning Self Logs", action="start")
    logger.info("Scanning internal BugFixer logs for errors...")

    log_path = get_log_path()
    if not os.path.exists(log_path):
        logger.warning(f"BugFixer log file not found at {log_path}")
        update_task_state(task_id="SelfScan", action="end")
        return

    try:
        with open(log_path, "r") as f:
            lines = f.readlines()

        formatted_logs = []
        for line in lines:
            if "[ERROR]" in line or "[CRITICAL]" in line:
                ts = line[:23] if len(line) > 23 else "Unknown"
                formatted_logs.append({
                    "module": "bugfixer-core",
                    "timestamp": ts,
                    "log": line.strip()
                })

        if not formatted_logs:
            update_task_state(task_id="SelfScan", action="end")
            return

        actionable_errors = analyze_logs_for_errors(formatted_logs)
        if not actionable_errors:
            update_task_state(task_id="SelfScan", action="end")
            return

        try:
            repo = git.Repo(os.getcwd())
            remote_url = repo.remotes.origin.url
            import re
            match = re.search(r'github\.com[:/]([^/]+/[^./]+)', remote_url)
            if match:
                self_repo_name = match.group(1).replace('.git', '')
            else:
                self_repo_name = "lbockenstedt/bugfixer"
        except Exception as e:
            logger.debug(f"Could not determine self-repo name from git: {e}")
            self_repo_name = "lbockenstedt/bugfixer"

        for error in actionable_errors:
            error['repo'] = self_repo_name
            # Ensure the body field is populated before attempting creation.
            if not error.get('body') or not str(error.get('body')).strip():
                logger.warning(f"Skipping self-diagnosis error with no body specified: {error.get('title')}")
                continue
            try:
                repo_obj = gh_current.get_repo(self_repo_name)
                monitored_repos = get_monitored_repos(config)
                create_automated_issue(gh_current, monitored_repos, repo_obj, error)
                logger.info(f"Handled self-diagnosis issue for BugFixer: {error['title']}")
            except Exception as e:
                logger.error(f"Failed to create self-diagnosis issue: {e}")

    except Exception as e:
        logger.error(f"Error during self-log scan: {e}")
    finally:
        update_task_state(task_id="SelfScan", action="end")

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

def poller_worker():

    global state
    while True:
        run_scan_cycle()
        time.sleep(int(os.getenv("POLL_INTERVAL_SECONDS", 300)))

@app.get("/api/health")
async def health_check():
    """Heartbeat endpoint for the watchdog service."""
    return {"status": "ok"}

@app.get("/")
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

@app.get("/api/task-details")
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

@app.get("/api/models")
async def get_models():
    """Fetches available models from both local and cloud Ollama instances."""
    config = load_config()
    l_url = config.get("LOCAL_OLLAMA_URL") or os.getenv("LOCAL_OLLAMA_URL")
    c_url = config.get("CLOUD_OLLAMA_URL") or os.getenv("CLOUD_OLLAMA_URL")
    api_key = config.get("OLLAMA_API_KEY") or os.getenv("OLLAMA_API_KEY")

    results = {
        "local_models": [],
        "cloud_models": [],
        "enabled_models": config.get("enabled_models", [])
    }

    if l_url:
        try:
            resp = requests.get(f"{l_url.rstrip('/')}/api/tags", timeout=10)
            resp.raise_for_status()
            tags_data = resp.json()
            for m in tags_data.get("models", []):
                results["local_models"].append({
                    "name": m["name"],
                    "details": m.get("details", "No description available")
                })
        except Exception as e:
            logger.error(f"Error fetching local models: {e}")

    if c_url:
        try:
            headers = {}
            if api_key:
                clean_key = api_key.strip().strip('"').strip("'")
                token_only = clean_key.replace("Bearer ", "").strip()
                headers["Authorization"] = f"Bearer {token_only}"

            resp = requests.get(f"{c_url.rstrip('/')}/api/tags", headers=headers, timeout=10)
            resp.raise_for_status()
            tags_data = resp.json()
            for m in tags_data.get("models", []):
                results["cloud_models"].append({
                    "name": m["name"],
                    "details": m.get("details", "No description available")
                })
        except Exception as e:
            logger.error(f"Error fetching cloud models: {e}")

    return results

@app.post("/api/toggle-model")
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

@app.get("/logs")
async def get_logs(request: Request):
    try:
        current_log = get_log_path()
        with open(current_log, "r") as f:
            lines = f.readlines()
            logs = "".join(lines[-100:])
    except Exception as e: logs = f"Error reading logs from {get_log_path()}: {e}"
    return templates.TemplateResponse(request=request, name="index.html", context={"view": "logs", "logs": logs, "state": state})

@app.get("/hub-logs")
async def get_hub_logs_page(request: Request):
    logs = get_hub_logs()
    return templates.TemplateResponse(request=request, name="index.html", context={"view": "hub-logs", "hub_logs": logs, "state": state})

DEFAULT_ENV = {
    "GITHUB_TOKEN": "",
    "LOCAL_OLLAMA_MODEL": "gemma4:31b-coding-mtp-bf16",
    "CLOUD_OLLAMA_MODEL": "gemma4:31b-cloud",
    "LOCAL_OLLAMA_URL": "http://172.16.1.100:11434",
    "CLOUD_OLLAMA_URL": "",
    "OLLAMA_API_KEY": "",
    "QA_REPO": "",
    "QA_TEST_COMMAND": "pytest",
    "POLL_INTERVAL_SECONDS": "300",
    "UPDATE_API_URL": "",
    "HUB_QUERY_URL": "",
    "LOG_FILE_PATH": "/var/log/bugfixer.log",
    "DEV_BRANCH": "dev",
    "LLM_TIMEOUT": "900",
    "MAX_CONCURRENT_FIXES": "5",
    "LOCAL_LLM_SCHEDULE": "9-16,1-5",
    "TRIAGE_STRICTNESS": "Moderate",
    "REVIEWER_MODEL_1": "gemma4:31b-cloud",
    "REVIEWER_MODEL_2": "gemma4:31b-cloud",
}

@app.get("/settings")
async def settings_page(request: Request):
    load_dotenv(override=True)
    settings = DEFAULT_ENV.copy()
    for k in DEFAULT_ENV:
        val = os.getenv(k)
        if val: settings[k] = val
    config = load_config()
    repo_tests = config.get("repo_tests", {})
    repo_tests_str = ", ".join([f"{k}:{v}" for k, v in repo_tests.items()])
    settings["GITHUB_TOKEN"] = config.get("GITHUB_TOKEN") or settings.get("GITHUB_TOKEN", "")
    settings["LLM_TIMEOUT"] = config.get("LLM_TIMEOUT") or settings.get("LLM_TIMEOUT", "900")
    labels = config.get("monitored_labels", ["automated-fix"])
    settings["monitored_labels_str"] = ", ".join(labels)

    model_data = await get_models()
    local_models = [m["name"] for m in model_data["local_models"]]
    cloud_models = [m["name"] for m in model_data["cloud_models"]]

    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "settings",
        "settings": {**settings, **config, "repo_tests_str": repo_tests_str, "monitored_labels_str": settings["monitored_labels_str"]},
        "available_labels": state.get("available_labels", []),
        "state": state,
        "local_models": local_models,
        "cloud_models": cloud_models
    })

@app.post("/save_settings")
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
        try:
            labels_list = form_data.getlist("monitored_labels")
        except AttributeError:
            val = form_data.get("monitored_labels", [])
            labels_list = [val] if isinstance(val, str) else (val if isinstance(val, list) else [])
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

    updates = {
        "monitored_repos": lambda v: [clean_repo_name(x.strip()) for x in v.replace("\\n", ",").split(",") if x.strip()],
        "trusted_repos": lambda v: [clean_repo_name(x.strip()) for x in v.replace("\\n", ",").split(",") if x.strip()],
        "default_branch": lambda v: v,
        "dev_branch": lambda v: v,
        "GITHUB_TOKEN": lambda v: v,
        "OLLAMA_API_KEY": lambda v: v,
        "CLOUD_OLLAMA_URL": lambda v: v,
        "LOCAL_OLLAMA_URL": lambda v: v,
        "CLOUD_OLLAMA_MODEL": lambda v: v,
        "LOCAL_OLLAMA_MODEL": lambda v: v,
        "LLM_TIMEOUT": lambda v: v,
        "MAX_CONCURRENT_FIXES": lambda v: v,
        "LOCAL_LLM_SCHEDULE": lambda v: v,
        "TRIAGE_STRICTNESS": lambda v: v,
        "REVIEWER_MODEL_1": lambda v: v,
        "REVIEWER_MODEL_2": lambda v: v,
    }


    for key, transform in updates.items():
        if key in data:
            val = data[key]
            if "repos" in key and not val:
                config_data[key] = []
            else:
                config_data[key] = transform(val)

    if "direct_push_enabled" in data:
        config_data["direct_push_enabled"] = data.get("direct_push_enabled") == "on"

    config_data["qa_enabled"] = data.get("qa_enabled") == "on"

    if "skip_review" in data:
        config_data["skip_review"] = data.get("skip_review") == "on"

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

    return RedirectResponse(url="/settings", status_code=303)

@app.post("/update_now")
async def update_now():
    updated, msg = check_for_updates()
    logger.info(f"Manual update check: {msg}")
    return {"status": "success", "message": msg}

@app.post("/toggle_cloud")
async def toggle_cloud():
    state["force_cloud"] = not state["force_cloud"]

    config = load_config()
    config["force_cloud"] = state["force_cloud"]
    save_config(config)

    return {"status": "success", "message": "Cloud override toggled successfully."}

@app.post("/trigger_fix")
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

@app.post("/scan_now")
async def scan_now():
    def trigger():
        state["status"] = "Manual Scan"
        run_scan_cycle()
    threading.Thread(target=trigger, daemon=True).start()
    return {"status": "triggered", "message": "Manual scan cycle started in background."}

@app.post("/retry_issue")
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

@app.post("/restart")
async def restart_service():
    logger.info("Restart request received. Triggering systemctl restart...")
    try:
        import subprocess
        subprocess.Popen(["sudo", "systemctl", "restart", "bugfixer"])
        return {"status": "success", "message": "Restart signal sent successfully."}
    except Exception as e:
        logger.error(f"Restart failed: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/trigger_hub_update")
async def trigger_hub_update():
    """Triggers an update on all spokes and agents via the Hub API."""
    result = trigger_infrastructure_update()
    return {"status": "success" if "SUCCESS" in result else "error", "message": result}

threading.Thread(target=connectivity_worker, daemon=True).start()
threading.Thread(target=heartbeat_worker, daemon=True).start()
threading.Thread(target=poller_worker, daemon=True).start()
threading.Thread(target=updater_worker, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)