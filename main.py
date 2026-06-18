import os, json, time, tempfile, threading, requests, logging, traceback, py_compile, random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
# Pure, stdlib-only duplicate-detection helpers. Importable standalone for tests
# (unlike this module, which initializes FastAPI/logging at import time). The
# script dir is prepended to sys.path so the import resolves regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup import (
    _normalize_for_dedup as _normalize_for_dedup_impl,
    _token_set as _token_set_impl,
    _jaccard as _jaccard_impl,
    _is_duplicate_match as _is_duplicate_match_impl,
    MODULE_ALIASES,
    strip_boilerplate,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import RequestValidationError
from github import Github, GithubException
import ollama
import git
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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
SELF_SCAN_OFFSET_FILE = os.path.join(CONFIG_DIR, "self_scan_offset.json")
CHAT_HISTORY_FILE = os.path.join(CONFIG_DIR, "chat_history.json")
VERSION_FILE = os.path.join(os.getcwd(), "VERSION")

class QueueLocalException(Exception):
    """Raised when a task should be deferred to the local LLM's allowed window."""
    pass

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
            "enabled_models": [],
            "self_diagnosis_repo": ""
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
    """Saves processed issues to persistent storage, with fallback to local file."""
    try:
        # Primary: Persistent storage
        with open(STATE_FILE, "w") as f:
            json.dump(processed, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving to persistent state file {STATE_FILE}: {e}")
        try:
            # Fallback: Local directory
            with open("processed_issues.json", "w") as f:
                json.dump(processed, f, indent=2)
        except Exception as fe:
            logger.error(f"Critical failure saving processed history to both locations: {fe}")

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

def is_cloud_url(url):
    """Checks if a given URL is a managed cloud Ollama instance."""
    return ("ollama.com" in url) and ("local" not in url)


def validate_llm_config_on_startup():
    """Validates LLM configuration on startup and provides clear, actionable guidance."""
    config = load_config()
    c_url = (config.get("CLOUD_OLLAMA_URL") or os.getenv("CLOUD_OLLAMA_URL") or "").strip()
    api_key = (config.get("OLLAMA_API_KEY") or os.getenv("OLLAMA_API_KEY") or "").strip().strip('"').strip("'")

    if not c_url:
        logger.info("Startup LLM validation: No cloud Ollama URL configured. Skipping API key check.")
        return True

    is_cloud_host = ("ollama.com" in c_url) or ("api.ollama" in c_url)

    if is_cloud_host and not api_key:
        logger.error(
            "\n" + "=" * 78 + "\n"
            "!!  CRITICAL LLM CONFIGURATION WARNING  !!\n"
            "A Cloud Ollama URL is configured but OLLAMA_API_KEY is MISSING or EMPTY.\n"
            f"  Cloud URL : {c_url}\n"
            "Every cloud LLM request will fail with HTTP 401 Unauthorized.\n\n"
            "HOW TO FIX:\n"
            "  1. Open the BugFixer dashboard: http://localhost:8000/settings\n"
            "  2. Enter your OLLAMA_API_KEY in the Settings form and click Save, OR\n"
            "  3. Manually add this line to /etc/bugfixer/.env :\n"
            "       OLLAMA_API_KEY=<your-secret-key>\n"
            "  4. Restart the service: sudo systemctl restart bugfixer\n"
            + "=" * 78
        )
        return False

    if is_cloud_host:
        try:
            token_only = api_key.replace("Bearer ", "").strip()
            headers = {"Authorization": f"Bearer {token_only}"}
            test_resp = requests.get(f"{c_url.rstrip('/')}/api/tags", headers=headers, timeout=15)
            if test_resp.status_code == 401:
                logger.error(
                    "\n" + "=" * 78 + "\n"
                    "!!  CRITICAL LLM CONFIGURATION WARNING  !!\n"
                    "OLLAMA_API_KEY is set but the Cloud Ollama API rejected it (401 Unauthorized).\n"
                    f"  Cloud URL : {c_url}\n"
                    "The key may be expired, revoked, or pasted incorrectly.\n\n"
                    "HOW TO FIX:\n"
                    "  1. Verify the key is correct and still active in your Ollama account.\n"
                    "  2. Update it via http://localhost:8000/settings, OR\n"
                    "  3. Edit /etc/bugfixer/.env and restart: sudo systemctl restart bugfixer\n"
                    + "=" * 78
                )
                return False
            elif test_resp.status_code == 200:
                logger.info("Startup LLM validation: Cloud OLLAMA_API_KEY is valid and reachable.")
                return True
            else:
                logger.warning(
                    f"Startup LLM validation: Cloud returned unexpected status "
                    f"{test_resp.status_code}. Proceeding, but watch the logs."
                )
                return True
        except requests.exceptions.ConnectionError:
            logger.warning(
                f"Startup LLM validation: Could not reach cloud URL {c_url} (connection error). "
                f"The key will be validated on the first real LLM request."
            )
            return True
        except Exception as e:
            logger.warning(
                f"Startup LLM validation: Skipping live key check due to error: {e}. "
                f"The key will be validated on the first real LLM request."
            )
            return True

    logger.info(f"Startup LLM validation: Cloud URL '{c_url}' is not a managed cloud host; skipping API key check.")
    return True

load_dotenv(ENV_FILE)
app = FastAPI()

template_path = os.path.join(os.getcwd(), "templates")
templates = Jinja2Templates(directory=template_path)

def is_local_llm_allowed():
    """Checks if the local LLM is allowed to be used based on the configured schedule."""
    config = load_config()
    schedule_str = config.get("LOCAL_LLM_SCHEDULE") or os.getenv("LOCAL_LLM_SCHEDULE", "9-16,1-5")

    try:
        now = datetime.now().hour
        ranges = schedule_str.split(',')
        for r in ranges:
            if '-' in r:
                start, end = map(int, r.split('-'))
                if start <= now < end:
                    return True
    except Exception as e:
        logger.error(f"Error parsing LLM schedule '{schedule_str}': {e}")
        if (9 <= now < 16) or (1 <= now < 5):
            return True
    return False

# ============================================================================
# LLM Circuit Breaker & Global Rate Limiter
# ============================================================================

_LLM_CB_LOCK = threading.Lock()
_LLM_CB = {
    "cooldown_until": 0.0,
    "consecutive_429s": 0,
    "total_429s": 0,
    "last_trip_reason": None,
    "last_trip_time": None,
}

def _llm_cb_wait():
    while True:
        with _LLM_CB_LOCK:
            cd = _LLM_CB["cooldown_until"]
        remaining = cd - time.time()
        if remaining <= 0:
            time.sleep(random.uniform(0, 1.5))
            return
        sleep_chunk = min(remaining, 5.0)
        logger.warning(
            f"LLM circuit breaker active — pausing {sleep_chunk:.1f}s "
            f"({remaining:.1f}s remaining) to respect rate-limit cooldown."
        )
        time.sleep(sleep_chunk)

def _llm_cb_trip(wait_time, reason="429"):
    wait_time = max(0.5, min(wait_time, 3600.0))
    with _LLM_CB_LOCK:
        new_cd = max(_LLM_CB["cooldown_until"], time.time() + wait_time)
        _LLM_CB["cooldown_until"] = new_cd
        _LLM_CB["consecutive_429s"] += 1
        _LLM_CB["total_429s"] += 1
        _LLM_CB["last_trip_reason"] = reason
        _LLM_CB["last_trip_time"] = datetime.now().isoformat()
        consecutive = _LLM_CB["consecutive_429s"]
        total = _LLM_CB["total_429s"]
    logger.warning(
        f"LLM circuit breaker TRIPPED for {wait_time:.1f}s (reason={reason}). "
        f"consecutive_429s={consecutive}, total_429s={total}. "
        f"All LLM threads will pause."
    )

def _llm_cb_reset():
    with _LLM_CB_LOCK:
        if _LLM_CB["consecutive_429s"] > 0:
            logger.info(
                f"LLM circuit breaker reset after successful request "
                f"(was consecutive_429s={_LLM_CB['consecutive_429s']}, "
                f"total_429s={_LLM_CB['total_429s']})."
            )
            _LLM_CB["consecutive_429s"] = 0

def _llm_cb_snapshot():
    with _LLM_CB_LOCK:
        cd = _LLM_CB["cooldown_until"]
        return {
            "active": cd > time.time(),
            "cooldown_remaining_s": max(0, cd - time.time()),
            "consecutive_429s": _LLM_CB["consecutive_429s"],
            "total_429s": _LLM_CB["total_429s"],
            "last_trip_reason": _LLM_CB["last_trip_reason"],
            "last_trip_time": _LLM_CB["last_trip_time"],
        }

_LLM_SEMAPHORE = None
_LLM_SEM_LOCK = threading.Lock()

def _get_llm_semaphore():
    global _LLM_SEMAPHORE
    with _LLM_SEM_LOCK:
        if _LLM_SEMAPHORE is None:
            try:
                cfg = load_config()
                max_conc = int(cfg.get("LLM_MAX_CONCURRENT", 1))
            except Exception:
                max_conc = 1
            _LLM_SEMAPHORE = threading.Semaphore(max(1, max_conc))
            logger.info(f"LLM global concurrency limiter initialised: max_concurrent={max(1, max_conc)}")
        return _LLM_SEMAPHORE

# Shared LLM Utility
def call_llm(prompt, system_prompt="You are a helpful AI assistant.", force_cloud=None, task_id=None, model_override=None, url_override=None, messages=None):
    """Generic LLM caller with Local -> Cloud failover and JSON extraction. Now supports per-task streaming.

    If `messages` is provided (a list of {role, content} dicts), the call is
    multi-turn: the local /api/chat path passes them through directly, and the
    cloud /api/generate path flattens them into a transcript prompt string. When
    `messages` is given, `prompt` and `system_prompt` are ignored.
    """
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

        is_cloud = is_cloud_url(url)


        if is_cloud:
            # Cloud Ollama instances now use /api/chat for multi-turn (messages)
            # and /api/generate for single-shot (prompt).
            primary_endpoint = f"{url.rstrip('/')}/api/generate"
            primary_use_generate = True
        else:
            primary_endpoint = f"{url.rstrip('/')}/api/chat"
            primary_use_generate = False

        # Overlap: If we have conversation history (messages),
        # we prefer /api/chat, but if it's a cloud provider, we fall back
        # to /api/generate via flattening in attempt_request to avoid 500s.
        if messages is not None and not is_cloud:
            primary_endpoint = f"{url.rstrip('/')}/api/chat"
            primary_use_generate = False

        timeout_val = int(load_config().get("LLM_TIMEOUT", 900))

        def attempt_request(endpoint, use_generate_api, timeout=900):
            if messages is not None:
                # Multi-turn conversation. Cloud /api/generate takes a single
                # prompt, so flatten the message history into a transcript; local
                # /api/chat takes the messages array natively.
                if use_generate_api:
                    flattened = "\n\n".join(
                        f"{str(m.get('role', 'user')).capitalize()}: {m.get('content', '')}"
                        for m in messages
                    )
                    payload = {"model": model, "prompt": flattened, "stream": True}
                else:
                    payload = {"model": model, "messages": messages, "stream": True}
            elif use_generate_api:
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

            cfg = load_config()
            max_retries = int(cfg.get("LLM_MAX_RETRIES", 5))
            backoff_base = float(cfg.get("LLM_BACKOFF_BASE", 2.0))
            backoff_max = float(cfg.get("LLM_BACKOFF_MAX", 60.0))

            last_exception = None
            for attempt_num in range(max_retries + 1):
                is_last_attempt = (attempt_num == max_retries)

                _llm_cb_wait()

                try:
                    sem = _get_llm_semaphore()
                    sem.acquire()
                    try:
                        resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout, stream=True)
                    finally:
                        sem.release()

                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        wait_time = None
                        if retry_after:
                            try:
                                wait_time = float(retry_after)
                            except ValueError:
                                try:
                                    from email.utils import parsedate_to_datetime
                                    retry_date = parsedate_to_datetime(retry_after)
                                    if retry_date:
                                        wait_time = retry_date.timestamp() - datetime.now().timestamp()
                                except Exception:
                                    wait_time = None

                        if wait_time is None or wait_time < 0:
                            raw_backoff = min(backoff_base ** attempt_num, backoff_max)
                            wait_time = raw_backoff * random.uniform(0.5, 1.0)
                        else:
                            wait_time = min(wait_time, backoff_max)

                        _llm_cb_trip(wait_time, f"429 attempt {attempt_num + 1}/{max_retries + 1}")

                        if is_last_attempt:
                            logger.error(
                                f"LLM 429 Too Many Requests at {endpoint} "
                                f"after {max_retries + 1} attempts (including initial). "
                                f"Reporting as permanently failed."
                            )
                            resp.close()
                            resp.raise_for_status()

                        logger.warning(
                            f"LLM 429 Too Many Requests at {endpoint}. "
                            f"Backing off {wait_time:.1f}s before retry "
                            f"(attempt {attempt_num + 1}/{max_retries + 1}). "
                            f"Global circuit breaker tripped."
                        )
                        resp.close()
                        time.sleep(wait_time)
                        continue

                    if resp.status_code == 401:
                        key_state = "MISSING" if not api_key else f"set but INVALID (length={len(api_key)})"
                        logger.error(
                            f"LLM 401 Unauthorized at {endpoint}. "
                            f"OLLAMA_API_KEY is {key_state}. "
                            f"To fix: open http://localhost:8000/settings and set OLLAMA_API_KEY, "
                            f"or add 'OLLAMA_API_KEY=<your-key>' to {ENV_FILE}, "
                            f"then run: sudo systemctl restart bugfixer"
                        )
                        resp.raise_for_status()
                    if resp.status_code == 400:
                        err_body = ""
                        try:
                            err_body = resp.text or ""
                        except Exception:
                            err_body = "<unreadable body>"
                        err_body = err_body.strip().replace("\n", " ")[:1000]
                        logger.error(f"LLM 400 Bad Request at {endpoint}. Body: {err_body!r}")
                        resp.close()
                        resp.raise_for_status()

                    if 500 <= resp.status_code < 600:
                        raw_backoff = min(backoff_base ** attempt_num, backoff_max)
                        wait_time = raw_backoff * random.uniform(0.5, 1.0)

                        _llm_cb_trip(wait_time, f"{resp.status_code} attempt {attempt_num + 1}/{max_retries + 1}")

                        # Capture the upstream error body so we can actually root-cause
                        # 5xx failures (e.g. context-length exceeded, model not found,
                        # account/quota errors). The body is not streamed for error
                        # responses, so reading it here is cheap; we truncate to bound log size.
                        err_body = ""
                        try:
                            err_body = resp.text or ""
                        except Exception as be:
                            err_body = f"<unreadable body: {be}>"
                        err_body = err_body.strip().replace("\n", " ")[:1000]
                        # full_prompt/prompt length helps distinguish a model-mismatch
                        # failure from a prompt-size (context-overflow) failure.
                        try:
                            prompt_len = len(full_prompt) if use_generate_api else len(prompt)
                        except Exception:
                            prompt_len = -1

                        if is_last_attempt:
                            logger.error(
                                f"LLM {resp.status_code} server error at {endpoint} "
                                f"after {max_retries + 1} attempts. Reporting as permanently failed. "
                                f"model={model!r} prompt_len={prompt_len} body={err_body!r}"
                            )
                            resp.close()
                            resp.raise_for_status()
                        logger.warning(
                            f"LLM {resp.status_code} server error at {endpoint}. "
                            f"Backing off {wait_time:.1f}s before retry "
                            f"(attempt {attempt_num + 1}/{max_retries + 1}). "
                            f"model={model!r} prompt_len={prompt_len} body={err_body!r}"
                        )
                        resp.close()
                        time.sleep(wait_time)
                        continue

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

                    _llm_cb_reset()
                    return full_response

                except requests.exceptions.HTTPError as e:
                    resp_status = e.response.status_code if e.response is not None else None
                    if not is_last_attempt and resp_status is not None and (resp_status == 429 or 500 <= resp_status < 600):
                        raw_backoff = min(backoff_base ** attempt_num, backoff_max)
                        wait_time = raw_backoff * random.uniform(0.5, 1.0)
                        _llm_cb_trip(wait_time, f"HTTPError {resp_status} attempt {attempt_num + 1}/{max_retries + 1}")
                        logger.warning(
                            f"LLM {resp_status} (via HTTPError) at {endpoint}. "
                            f"Backing off {wait_time:.1f}s (attempt {attempt_num + 1}/{max_retries + 1})."
                        )
                        time.sleep(wait_time)
                        continue
                    raise
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.ChunkedEncodingError) as e:
                    last_exception = e
                    if not is_last_attempt:
                        raw_backoff = min(backoff_base ** attempt_num, backoff_max)
                        wait_time = raw_backoff * random.uniform(0.5, 1.0)
                        _llm_cb_trip(wait_time, f"transient {type(e).__name__} attempt {attempt_num + 1}/{max_retries + 1}")
                        logger.warning(
                            f"LLM transient error (attempt {attempt_num + 1}/{max_retries + 1}) "
                            f"at {endpoint}: {e}. Retrying in {wait_time:.1f}s..."
                        )
                        time.sleep(wait_time)
                        continue
                    raise
                except Exception as e:
                    last_exception = e
                    if not is_last_attempt:
                        raw_backoff = min(backoff_base ** attempt_num, backoff_max)
                        wait_time = raw_backoff * random.uniform(0.5, 1.0)
                        _llm_cb_trip(wait_time, f"error {type(e).__name__} attempt {attempt_num + 1}/{max_retries + 1}")
                        logger.warning(
                            f"LLM error (attempt {attempt_num + 1}/{max_retries + 1}) "
                            f"at {endpoint}: {e}. Retrying in {wait_time:.1f}s..."
                        )
                        time.sleep(wait_time)
                        continue
                    raise

            if last_exception:
                raise last_exception
            raise Exception(f"LLM request to {endpoint} exhausted all {max_retries + 1} attempts")

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
        if config.get("queue_local_llm", False):
            logger.info("Local LLM not allowed at this hour and 'Queue for Local LLM' is enabled. Signaling queue.")
            raise QueueLocalException("Local LLM off-hours: queuing for later.")
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
    """Executes a command in a Docker sandbox. Fails closed (returns an error result)
    if Docker is unavailable — NEVER runs untrusted repository code on the host as root."""
    import subprocess
    from dataclasses import dataclass

    @dataclass
    class MockResult:
        stdout: str
        stderr: str
        returncode: int

    docker_available = False
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True, timeout=15)
        docker_available = True
    except Exception:
        pass

    if not docker_available:
        msg = ("Docker is not available; refusing to run untrusted repository commands on the host "
               "(fail-closed). Install Docker and retry.")
        logger.error("⚠️ " + msg)
        return MockResult("", msg, 127)

    image = "ubuntu:latest"
    try:
        files = os.listdir(cwd)
    except Exception as e:
        logger.error(f"Cannot read sandbox working directory {cwd}: {e}")
        return MockResult("", str(e), 1)
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
        return MockResult(result.stdout, result.stderr, result.returncode)
    except Exception as e:
        logger.error(f"Docker execution error: {e}")
        return MockResult("", f"Docker execution error: {e}", 1)

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
_chat_lock = threading.RLock()

def update_task_state(task_id, task_name="Unknown Task", action="start"):
    """Manages active tasks and their start times. action can be 'start' or 'end'."""
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
    "success_count": success_count, "failure_count": failure_count,
    "llm_circuit_breaker": _llm_cb_snapshot(),
    "paused": False, "local_configured": False,
    "chat_streams": {}
}

try:
    validate_llm_config_on_startup()
except Exception as ve:
    logger.warning(f"Startup LLM validation failed (non-fatal): {ve}")

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
    """Extracts and normalizes a list of monitored repositories from config,
    always including the self-diagnosis repository if it can be resolved.
    """
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

    sd_repo = resolve_self_diagnosis_repo(config)
    if sd_repo:
        monitored_repos.append(sd_repo)

    return list(set(monitored_repos))

def resolve_module_repo(module, monitored_repos, config):
    """Maps a Hub log module name to the GitHub repo its issues should be filed in.

    Routing precedence (first match wins):
      1. Explicit 'module_repo_map' config key: {module_name: "owner/repo"}.
         Case-insensitive module lookup; lets the user override auto-matching
         for aliases or modules with no name-matching repo (e.g. "hub" -> "owner/lm").
      2. Auto-match: a monitored repo whose basename (the segment after the final
         '/') equals the module name, case-insensitive. e.g. module "pxmx" ->
         "lbockenstedt/pxmx".
      3. None if nothing matches — the caller should skip filing (NOT dump into
         the self-diagnosis repo, which is the behaviour the user explicitly
         wants to avoid).

    The returned repo is always a member of monitored_repos (auto-match) or a
    user-declared repo (explicit map); it is never invented.
    """
    if not module:
        return None
    mod_key = str(module).strip().lower()
    if not mod_key:
        return None

    # 1. Explicit user-provided mapping.
    module_map = config.get("module_repo_map") or {}
    if isinstance(module_map, dict):
        for k, v in module_map.items():
            if str(k).strip().lower() == mod_key and v and str(v).strip():
                resolved = clean_repo_name(str(v).strip())
                if resolved:
                    return resolved

    # 2. Auto-match against monitored repo basenames.
    for repo_name in monitored_repos:
        basename = str(repo_name).strip().split('/')[-1].lower()
        if basename == mod_key:
            return repo_name

    return None

def parse_module_repo_map(value):
    """Normalises a module_repo_map setting into {module: "owner/repo"}.

    Accepts a dict, a JSON object string, or a newline/comma-separated list of
    'module=owner/repo' pairs, so the Settings form can send any of these shapes.
    Values are cleaned via clean_repo_name; entries with empty module or repo are
    dropped. Module keys are stored as-is (case-insensitive lookup happens in
    resolve_module_repo), so callers see the original casing.
    """
    result = {}
    if value is None:
        return result
    if isinstance(value, dict):
        pairs = value.items()
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return result
        # Try JSON object first; fall back to line/separated 'module=repo' pairs.
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                pairs = obj.items()
            else:
                return result
        except Exception:
            pairs = []
            for part in s.replace(",", "\n").split("\n"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                mod, _, repo = part.partition("=")
                pairs = [(mod.strip(), repo.strip())]
    else:
        return result

    for mod, repo in pairs:
        mod_s = str(mod).strip()
        repo_s = clean_repo_name(str(repo).strip()) if repo else ""
        if mod_s and repo_s:
            result[mod_s] = repo_s
    return result

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

def _normalize_for_dedup(text):
    """Aggressively normalize text for duplicate comparison.

    Thin wrapper around the strengthened implementation in ``dedup.py`` (which
    additionally strips the automated-issue boilerplate wrapper and applies
    module aliases such as ``opns`` -> ``opnsense``). Kept here as a shim so
    existing call sites that import it from main continue to work.
    """
    return _normalize_for_dedup_impl(text)

def _token_set(text):
    return _token_set_impl(text)

def _jaccard(a, b):
    return _jaccard_impl(a, b)

def _is_duplicate_match(new_title, new_body, ex_title, ex_body):
    """Returns True if a new error matches an existing issue, using normalized +
    fuzzy comparison so LLM rephrasing, timestamp drift, boilerplate wrapper,
    and module-name variants (opns/opnsense) don't defeat dedup."""
    return _is_duplicate_match_impl(new_title, new_body, ex_title, ex_body)

# --- Duplicate issue detection configuration ---------------------------------
# How far back to look at CLOSED issues when searching for a recurrence. The bot
# previously only searched OPEN issues, so once a "fix" was merged and the issue
# closed, the next cycle's identical error was filed as a brand-new issue +
# spawned a new ai-fix-issue-* branch — the #25 -> #55 -> #78 -> #90 storm.
# Searching recently-closed issues lets us REOPEN the original instead.
DEDUP_CLOSED_WINDOW_DAYS = 60
# When the target repo has no match and we fall back to searching the OTHER
# monitored repos globally, require a stricter title-level signal to avoid
# cross-module false positives (e.g. an opnsense error matching a pxmx issue on
# incidental wording overlap).
GLOBAL_FALLBACK_JACCARD = 0.8

def find_global_duplicate_issue(gh_current, monitored_repos, error_data):
    """Searches across monitored repositories for an existing issue matching the error.

    Searches OPEN issues AND recently-CLOSED issues (within
    DEDUP_CLOSED_WINDOW_DAYS), because a recurring error whose prior issue was
    closed (the bot merged a "fix") must still be recognised so it can be
    REOPENED rather than re-filed — this is what previously caused the
    opnsense 'time' import storm (#25 -> #55 -> #78 -> #90).

    The target repository (error_data['repo']) is searched first; other
    monitored repos are searched as a fallback with a stricter title-level
    threshold (GLOBAL_FALLBACK_JACCARD) to avoid cross-module false positives.

    Returns a tuple (issue, repo_name, was_closed). ``was_closed`` is True when
    the matched issue is currently closed, signalling the caller to reopen it
    rather than treat it as an open duplicate. Returns (None, None, False) when
    no duplicate is found.

    Safely handles error_data payloads that may be missing the 'title' or 'body'
    keys (the LLM may omit them). Missing fields are treated as empty strings so
    that the deduplication search degrades gracefully instead of raising a
    KeyError.
    """
    # Defensive: ensure error_data is a dict before calling .get()
    if not isinstance(error_data, dict):
        logger.warning(f"find_global_duplicate_issue received non-dict error_data: {type(error_data)}")
        return None, None, False

    new_title = error_data.get('title') or ''
    new_body = error_data.get('body') or ''

    if not str(new_title).strip() and not str(new_body).strip():
        return None, None, False

    target_repo = error_data.get('repo')

    def _search_repo(repo_name, require_strict_global=False, is_self_diag=False):
        try:
            repo = gh_current.get_repo(repo_name)
            # state='all' so we see recently-closed recurrences too; newest-first
            # so the most relevant (recently updated) issues are scanned first.
            issues = repo.get_issues(state='all', sort='updated', direction='desc')
            now = datetime.utcnow()
            for issue in issues:
                # Skip closed issues older than the recurrence window — they are
                # unlikely to be the same recurrence and would risk stale matches.
                if issue.state == 'closed':
                    closed_at = getattr(issue, 'closed_at', None) or issue.updated_at
                    if closed_at and (now - closed_at).days > DEDUP_CLOSED_WINDOW_DAYS:
                        continue
                issue_body = issue.body or ""

                # Special case: Self-Diagnosis. Relax the match to rely primarily on title
                # since JSON error messages often vary by exactly one character (line number).
                if is_self_diag:
                    nt = _normalize_for_dedup(new_title)
                    et = _normalize_for_dedup(issue.title or "")
                    if nt and et and _jaccard(set(nt.split()), set(et.split())) >= 0.7:
                        return issue, repo_name, (issue.state == 'closed')

                if _is_duplicate_match(new_title, new_body, issue.title or "", issue_body):
                    # Global fallback (non-target repo): require a strong
                    # title-level signal so we don't cross-match unrelated
                    # modules on incidental body-wording overlap.
                    if require_strict_global:
                        nt = _normalize_for_dedup(new_title)
                        et = _normalize_for_dedup(issue.title or "")
                        if not (nt and et and
                                _jaccard(set(nt.split()), set(et.split())) >= GLOBAL_FALLBACK_JACCARD):
                            continue
                    return issue, repo_name, (issue.state == 'closed')
        except Exception as e:
            logger.debug(f"Could not search for duplicates in {repo_name}: {e}")
        return None

    # 1. Target repo first — the recurrence almost always lands in the same repo.
    if target_repo:
        config = load_config()
        self_diag_repo = config.get("self_diagnosis_repo")
        is_self_diag = (target_repo == self_diag_repo)

        if target_repo in monitored_repos:
            hit = _search_repo(target_repo, is_self_diag=is_self_diag)
            if hit:
                return hit
        elif is_self_diag:
            # If it's the self-diagnosis repo, search it even if it's not explicitly
            # in the monitored_repos list (though it usually is).
            hit = _search_repo(target_repo, is_self_diag=True)
            if hit:
                return hit


    # 2. Global fallback across the other monitored repos, stricter threshold.
    for repo_name in monitored_repos:
        if repo_name == target_repo:
            continue
        hit = _search_repo(repo_name, require_strict_global=True)
        if hit:
            return hit

    return None, None, False

def create_automated_issue(gh_current, monitored_repos, gh_repo, error_data):
    """Creates a GitHub issue for a log-detected error, deduplicating globally across monitored repos.

    The 'body' field is required to create a meaningful issue. If it is missing or
    empty, the function logs a warning and returns None instead of raising a
    KeyError, which previously crashed automated issue creation with: 'body'.

    Additionally validates that error_data is a dict and that both 'title' and
    'body' are present and non-empty strings before any GitHub API call is made.
    """
    try:
        # Defensive: ensure error_data is a dict; if the LLM returned a malformed
        # payload (e.g., a string or None), .get() would itself raise AttributeError.
        if not isinstance(error_data, dict):
            logger.warning(
                f"Skipping automated issue creation: error_data is not a dict "
                f"(type={type(error_data).__name__}). Value: {error_data!r}"
            )
            return None

        title_text = error_data.get('title')
        body_text = error_data.get('body')

        # Validate body FIRST — this is the field that was causing the KeyError crash.
        # We explicitly check for None, empty string, or whitespace-only strings.
        if body_text is None or not str(body_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'body' field is missing or empty. "
                f"Title was: {title_text!r}. Full error_data: {error_data}"
            )
            return None

        # Validate title as well — a GitHub issue cannot be created without a title.
        if title_text is None or not str(title_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'title' field is missing or empty. "
                f"Body was: {str(body_text)[:120]!r}"
            )
            return None

        # Normalise to strings (the LLM might return non-string types).
        title_text = str(title_text)
        body_text = str(body_text)

        current_repo_name = error_data.get('repo') or gh_repo.full_name

        existing_issue, duplicate_repo_name, was_closed = find_global_duplicate_issue(
            gh_current, monitored_repos, error_data
        )

        if existing_issue:
            duplicate_repo_display = duplicate_repo_name or current_repo_name

            if was_closed:
                # The matching issue was closed (typically the bot merged a "fix"
                # for it). Reopen it and record the recurrence instead of filing a
                # brand-new issue + spawning another ai-fix-issue-* branch. This
                # is the core fix for the recurring-error storm.
                logger.info(
                    f"Recurring CLOSED issue #{existing_issue.number} in "
                    f"{duplicate_repo_display} matched; reopening instead of filing a duplicate."
                )
                try:
                    existing_issue.edit(state='open')
                except Exception as reopen_err:
                    logger.warning(f"Could not reopen issue #{existing_issue.number}: {reopen_err}")
                try:
                    existing_issue.create_comment(
                        f"🔁 **Recurrence detected — reopening instead of filing a duplicate**\n\n"
                        f"BugFixer re-detected this error in **{current_repo_name}** after the "
                        f"issue was closed.\n\n"
                        f"```\n{body_text}\n```"
                    )
                    logger.info(f"Reopened issue #{existing_issue.number} for {current_repo_name}")
                except Exception as comment_err:
                    logger.warning(f"Could not add recurrence comment to #{existing_issue.number}: {comment_err}")
                return existing_issue

            # OPEN duplicate — keep the existing evidence-comment behavior.
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
        logger.debug(f"create_automated_issue error_data was: {error_data!r}")
        return None

def get_hub_logs():
    """Fetches recent logs from the Hub for all modules. Returns a list of log entries.

    Robustly handles non-JSON 200 responses (e.g., HTML login pages or error pages
    served by reverse proxies). The Hub endpoint may return HTTP 200 with an HTML
    body when an authentication redirect, maintenance page, or upstream error
    page is served. In such cases we detect the mismatch via the Content-Type
    header (and as a fallback by inspecting the body for HTML markers) and return
    None gracefully — logging a single WARNING instead of an ERROR — so we do
    not generate recurring error-log noise that itself triggers automated issue
    creation in a feedback loop.
    """
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

            # Empty body — nothing to parse.
            if not body or not body.strip():
                logger.warning(
                    f"Hub returned 200 OK but empty response body for {log_url}. "
                    f"Skipping JSON parse to avoid json.decode error."
                )
                return None

            # --- Content-Type guard for non-JSON 200 responses ---
            # The Hub endpoint may serve an HTML error page or login redirect with
            # HTTP 200 (e.g., behind a reverse proxy like nginx/traefik/caddy that
            # intercepts the request, or when an upstream app serves a custom
            # error page). We check the Content-Type header first, and as a
            # fallback inspect the body for HTML markers. This prevents the
            # recurring "Hub returned 200 OK but body was not valid JSON" ERROR
            # log entries that previously spammed the logs and triggered
            # automated issue creation in a noisy feedback loop.
            content_type = (resp.headers.get("Content-Type") or "").lower()
            stripped_body = body.lstrip()
            looks_like_html = (
                "text/html" in content_type
                or "application/xhtml" in content_type
                or stripped_body.startswith("<!DOCTYPE")
                or stripped_body.startswith("<html")
                or stripped_body.startswith("<?xml")
                or (stripped_body.startswith("<") and "<head" in stripped_body[:512].lower())
            )

            if looks_like_html:
                # The endpoint is serving an HTML page instead of JSON. This is
                # typically a login redirect, a maintenance page, or an upstream
                # error page. Log a single WARNING (not ERROR) so we do not
                # generate recurring error-log noise that itself triggers
                # automated issue creation. Return None so callers skip this
                # cycle gracefully. We also include a short content preview to
                # aid debugging without flooding the logs.
                logger.warning(
                    f"Hub returned 200 OK but received non-JSON content "
                    f"(Content-Type={content_type or 'unknown'}) for {log_url}. "
                    f"The endpoint may be serving an error page or login redirect. "
                    f"Skipping this cycle. First 200 chars: {body[:200]!r}"
                )
                return None

            # Content-Type looks JSON-compatible — attempt to parse.
            try:
                data = resp.json()
                if isinstance(data, dict):
                    logs = data.get('logs', [])
                    return list(reversed(logs)) if isinstance(logs, list) else []
                return list(reversed(data)) if isinstance(data, list) else []
            except Exception as e:
                # Even with a JSON-ish Content-Type, parsing could fail (truncated
                # body, BOM, etc.). Treat this as a soft failure (WARNING) and
                # return None so we don't crash the scan cycle or generate noise.
                logger.warning(
                    f"Hub returned 200 OK but failed to parse JSON: {e}. "
                    f"Content-Type={content_type}. Content: {body[:200]!r}"
                )
                return None

        logger.warning(f"Hub returned unexpected status code {resp.status_code} for {log_url}")
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

def filter_error_logs(logs):
    """Scrubs raw logs down to error-relevant entries before sending to the LLM.

    Why: HubScan previously JSON-dumped the *entire* Hub log set (every INFO line,
    last-500-lines-per-module file logs, recurring duplicates) into the LLM
    prompt. That both bloated the prompt toward the model's context limit
    (a likely cause of upstream HTTP 500s) and buried actionable errors in noise.

    This keeps only entries whose 'log' text carries an error signature
    ([ERROR]/[CRITICAL]/Traceback/Exception/Error/Failed), dedupes identical
    lines per module (recurring errors appear dozens/hundreds of times in file
    logs), and caps the total to bounded entry/character budgets so the prompt
    can never overflow context regardless of log volume.

    Schema-agnostic: handles the Hub shape {"module":..., "log":...} and the
    SelfScan shape {"module":..., "timestamp":..., "log":...} equally, since it
    only inspects the 'log' field (falling back to the stringified entry).
    """
    import re
    if not logs:
        return []

    # ERROR/CRITICAL level tags plus common error signatures (tracebacks,
    # raised exceptions, explicit "Error:"/"Failed"). WARNINGs are excluded:
    # the LLM task is to find actionable *errors*, not routine warnings.
    error_pattern = re.compile(
        r'\[(ERROR|CRITICAL)\]|Traceback|Exception|Error[: ]|Failed|Traceback \(most recent call last\)',
        re.IGNORECASE
    )

    cfg = load_config()
    max_entries = int(cfg.get("LLM_LOG_MAX_ENTRIES", 200))
    max_chars = int(cfg.get("LLM_LOG_MAX_CHARS", 60000))

    seen = set()
    kept = []
    total_chars = 0
    for entry in logs:
        if isinstance(entry, dict):
            module = str(entry.get('module', '') or '')
            text = entry.get('log')
            text = str(text) if text is not None else json.dumps(entry)
        else:
            module = ''
            text = str(entry)

        if not error_pattern.search(text):
            continue

        key = (module, text.strip())
        if key in seen:
            continue
        seen.add(key)

        line_len = len(text) + len(module) + 16
        if total_chars + line_len > max_chars:
            logger.info(
                f"filter_error_logs: reached {max_chars}-char budget after "
                f"{len(kept)} entries; stopping."
            )
            break
        kept.append(entry if isinstance(entry, dict) else {"module": "", "log": text})
        total_chars += line_len
        if len(kept) >= max_entries:
            logger.info(f"filter_error_logs: reached {max_entries}-entry cap; stopping.")
            break

    return kept

def analyze_logs_for_errors(logs):
    """Uses LLM to identify actionable errors in aggregated logs.

    Robustly validates the LLM's JSON response: every entry must be a dict with
    non-empty 'module', 'title', and 'body' fields. Malformed entries are dropped
    so they never reach create_automated_issue(), preventing the 'body' KeyError.

    The 'module' field (carried through from the source log entry) is the
    authoritative key for routing an issue to the correct repository — see
    resolve_module_repo(). The LLM may also suggest a 'repo', but it is treated
    as a hint only and is not required.
    """
    if not logs: return []

    log_text = json.dumps(logs, indent=2)
    prompt = (
        f"Logs from Hub:\n{log_text}\n\n"
        "Analyze these logs for critical, recurring, or actionable errors that can be fixed in code. "
        "Ignore heartbeat messages or routine status updates. "
        "For each actionable error found, provide: \n"
        "1. The exact 'module' value from the source log entry the error came from.\n"
        "2. A concise summary of the bug ('title').\n"
        "3. The specific log snippet that proves the error ('body').\n\n"
        "Return ONLY a JSON array of objects: [{\"module\": \"module-name\", \"title\": \"Error Summary\", \"body\": \"Log snippet and description\"}]. "
        "Every object MUST include non-empty 'module', 'title', and 'body' fields. "
        "The 'module' MUST be copied verbatim from the source log entry's module field."
    )
    try:
        res = call_llm(prompt, system_prompt="You are a log analysis expert. Return only a JSON array.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            # Defensive: the LLM might return a single object instead of an array.
            if isinstance(parsed, dict):
                logger.warning(f"LLM returned a single JSON object instead of an array for log analysis. Wrapping in list.")
                parsed = [parsed]
            if not isinstance(parsed, list):
                logger.warning(f"LLM returned non-array JSON for log analysis: {type(parsed).__name__}. Discarding.")
                return []
            cleaned = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    logger.debug(f"Dropping malformed log-analysis entry (not a dict): {entry}")
                    continue
                module_val = entry.get('module')
                title_val = entry.get('title')
                body_val = entry.get('body')
                if not module_val or not str(module_val).strip():
                    logger.debug(f"Dropping malformed log-analysis entry (missing/empty module): {entry}")
                    continue
                if not title_val or not str(title_val).strip():
                    logger.debug(f"Dropping malformed log-analysis entry (missing/empty title): {entry}")
                    continue
                if not body_val or not str(body_val).strip():
                    logger.debug(f"Dropping malformed log-analysis entry (missing/empty body): {entry}")
                    continue

                # Try to find the original source entry to preserve full context (host, path, etc.)
                source_entry = next((log for log in logs if isinstance(log, dict)
                                   and str(log.get('module')) == str(module_val)
                                   and str(body_val) in str(log.get('log', ''))), {})

                # Normalise all fields to strings so downstream code never receives None.
                cleaned.append({
                    'module': str(module_val),
                    'title': str(title_val),
                    'body': str(body_val),
                    'repo': str(entry.get('repo')) if entry.get('repo') and str(entry.get('repo')).strip() else '',
                    'source_data': source_entry
                })
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
                    headers = {}
                    if api_key:
                        clean_key = api_key.strip().strip('"').strip("'")
                        token_only = clean_key.replace("Bearer ", "").strip()
                        headers["Authorization"] = f"Bearer {token_only}"
                    payload = {"model": c_mod, "prompt": "ping", "stream": False}
                    resp = requests.post(f"{c_url.rstrip('/')}/api/generate", json=payload, headers=headers, timeout=10)
                    if resp.status_code == 401:
                        state["cloud_online"] = False
                        key_state = "MISSING" if not api_key else "INVALID (rejected by cloud)"
                        logger.error(
                            f"Hourly Cloud LLM connectivity check: 401 Unauthorized at {c_url}. "
                            f"OLLAMA_API_KEY is {key_state}. "
                            f"Fix at http://localhost:8000/settings or in {ENV_FILE}, "
                            f"then restart: sudo systemctl restart bugfixer"
                        )
                    elif resp.status_code == 429:
                        state["cloud_online"] = False
                        _llm_cb_trip(60.0, "connectivity-worker 429")
                        logger.warning(
                            f"Hourly Cloud LLM connectivity check: 429 at {c_url}. "
                            f"Tripping circuit breaker for 60s."
                        )
                    else:
                        state["cloud_online"] = (resp.status_code == 200)
                except Exception as e:
                    state["cloud_online"] = False
                    logger.debug(f"Cloud connectivity check error: {e}")

            logger.info(f"Hourly Connectivity Check: Local={state['local_online']}, Cloud={state['cloud_online']}")
        except Exception as e:
            logger.error(f"Connectivity worker error: {e}")

        time.sleep(900)

def heartbeat_worker():
    while True:
        try:
            config = load_config()
            local_url = config.get("LOCAL_OLLAMA_URL") or os.getenv("LOCAL_OLLAMA_URL")
            cloud_url = config.get("CLOUD_OLLAMA_URL") or os.getenv("CLOUD_OLLAMA_URL")

            if local_url:
                state["local_configured"] = True
                try:
                    requests.get(f"{local_url}/api/tags", timeout=2)
                    state["local_online"] = True
                except:
                    state["local_online"] = False
            else:
                state["local_online"] = False
                state["local_configured"] = False

            state["llm_circuit_breaker"] = _llm_cb_snapshot()

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

    # --- Cloud Availability Check for Reviewers ---
    # If we have cloud reviewers configured but the cloud is offline,
    # signal that we should queue for a retry.
    cloud_reviewers_configured = any(r["force_cloud"] is True for r in reviewers)
    if cloud_reviewers_configured and not state["cloud_online"]:
        logger.warning("Cloud reviewers configured but cloud is offline. Signaling retry queue.")
        return {"status": "queue_for_retry"}
    # ----------------------------------------------

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
        repo_root = os.path.abspath(repo_path)
        applied = {}
        for filepath, code in fixes.items():
            # Confine writes to the cloned repo: reject absolute paths, traversal,
            # and symlinks that escape the repo root (prevents arbitrary file write).
            if not isinstance(filepath, str) or os.path.isabs(filepath) or ".." in filepath.replace("\\", "/").split("/"):
                logger.error(f"Refusing to apply fix with unsafe path: {filepath!r}")
                continue
            full_path = os.path.abspath(os.path.join(repo_root, filepath))
            try:
                if os.path.commonpath([repo_root, full_path]) != repo_root:
                    logger.error(f"Refusing to apply fix escaping repo root: {filepath!r}")
                    continue
            except ValueError:
                logger.error(f"Refusing to apply fix with unresolvable path: {filepath!r}")
                continue
            if os.path.islink(full_path):
                try:
                    link_target = os.path.abspath(os.readlink(full_path))
                    if os.path.commonpath([repo_root, link_target]) != repo_root:
                        logger.error(f"Refusing to write through symlink escaping repo: {filepath!r}")
                        continue
                except Exception:
                    logger.error(f"Refusing to write through unresolvable symlink: {filepath!r}")
                    continue
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(code.strip())
            applied[filepath] = code
            logger.info(f"Applied fix to file: {filepath}")
        if not applied:
            logger.error("No fixes could be applied (all rejected as unsafe or out-of-repo).")
            return False, {}, 0.0
        return True, applied, confidence
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
    """Checks whether an open pull request already exists for the given head/base pair."""
    existing_pr = None

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
            if ge.status == 410 or ge.status == 404:
                logger.warning(f"Issue {repo_name}:{issue_num} was deleted or not found. Removing from history.")
                processed = load_processed()
                if issue_id in processed:
                    del processed[issue_id]
                    save_processed(processed)
                    state["processed"] = processed
                return False, "Issue deleted"
            raise ge

        # --- Resume from awaiting_review ---
        processed = load_processed()
        issue_info = processed.get(issue_id, {})
        if issue_info.get("status") == "awaiting_review":
            last_attempt = issue_info.get("timestamp")
            if last_attempt:
                try:
                    ts = datetime.fromisoformat(last_attempt)
                    if (datetime.now() - ts).total_seconds() < 3600:
                        logger.info(f"Issue {issue_id} is awaiting review. Next retry in 1 hour.")
                        return False, "Review queued: Cloud LLM offline (retrying in 1 hour)"
                except:
                    pass
            logger.info(f"Resuming review for {issue_id} after 1 hour timeout.")
            # We will use the saved fixes later in the loop.

        update_task_state(task_id=issue_id, task_name=f"Triaging {issue_id}", action="start")
        actionable, request_msg = analyze_issue(issue)

        if not actionable:
            logger.info(f"Issue {repo_name}:{issue_num} is non-actionable: {request_msg}")
            issue.create_comment(f"🤖 **BugFixer Triage**\n\nThis issue is currently non-actionable. To help me fix this, please provide: {request_msg}")
            processed = load_processed()
            processed[f"{repo_name}:{issue_num}"] = {
                "status": "non-actionable",
                "timestamp": datetime.now().isoformat(),
                "reason": request_msg,
                "original_body": issue.body
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
                try:
                    update_task_state(task_id=issue_id, task_name=f"Fix Attempt {attempt}/{max_attempts} for {issue_id}", action="start")
                    logger.info(f"AI Fix Attempt {attempt}/{max_attempts} for {repo_name}:{issue_num}...")

                    # --- Resume from awaiting_review ---
                    pending_fix = issue_info.get("pending_fix") if attempt == 1 and issue_info.get("status") == "awaiting_review" else None
                    if pending_fix:
                        logger.info(f"Resuming from queued review for {issue_id} using saved fix.")
                        success_applied, fixes, confidence = parse_and_apply(json.dumps(pending_fix), path)
                        # Clear the pending status now that we're processing it
                        processed = load_processed()
                        if issue_id in processed:
                            processed[issue_id]["status"] = "processing"
                            save_processed(processed)
                    elif not pending_fix:
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

                            # --- Handle Queue for Retry ---
                            if isinstance(review, dict) and review.get("status") == "queue_for_retry":
                                logger.info(f"Review queued for {issue_id}: Cloud LLM offline. Saving fix for retry in 1 hour.")
                                processed = load_processed()
                                processed[issue_id] = {
                                    "status": "awaiting_review",
                                    "timestamp": datetime.now().isoformat(),
                                    "pending_fix": {"confidence": confidence, "fixes": fixes},
                                    "original_body": issue.body
                                }
                                save_processed(processed)
                                state["processed"] = processed
                                update_task_state(task_id=issue_id, action="end")
                                return False, "Cloud offline: Review queued for retry in 1 hour."

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
                except QueueLocalException as qle:
                    logger.info(f"Queuing issue {issue_id} for Local LLM: {qle}")
                    processed = load_processed()
                    processed[issue_id] = {
                        "status": "awaiting_local",
                        "timestamp": datetime.now().isoformat(),
                        "reason": "Queued for local LLM window",
                        "original_body": issue.body
                    }
                    save_processed(processed)
                    state["processed"] = processed
                    update_task_state(task_id=issue_id, action="end")
                    return False, "Queued for local LLM"

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
                    "error": failure_reason,
                    "original_body": issue.body
                }
                save_processed(processed)
                state["processed"] = processed
                update_task_state(task_id=issue_id, action="end")
                return False, failure_reason

            repo_git.git.add(A=True)

            confidence_threshold = 0.95
            is_trusted = (repo_name in config["trusted_repos"]) or (repo_name == resolve_self_diagnosis_repo(config))
            bot_user = gh_current.get_user().login
            is_owner = repo_obj.owner.login == bot_user
            direct_push_setting = config.get("direct_push_enabled")
            can_direct_push = direct_push_setting and is_trusted and is_owner

            logger.info(f"Deployment decision for {repo_name}: DirectPushSetting={direct_push_setting}, IsTrusted={is_trusted}, IsOwner={is_owner} -> can_direct_push={can_direct_push}")


            version_bumped = False
            new_v = None
            if can_direct_push and final_verdict == "Approve":
                new_v = bump_repo_version(path)
                if new_v:
                    version_bumped = True
                    logger.info(f"Bumped target repository {repo_name} version to {new_v}")

            commit_msg = f"AI Fix #{issue.number}: {issue.title[:50]}..."
            if version_bumped:
                commit_msg += f" (Version Bump to {new_v})"
            repo_git.index.commit(commit_msg)

            base_branch = config.get("default_branch", "main")

            if can_direct_push and final_verdict == "Approve":
                logger.info(f"Decision: Direct Commit to {base_branch}. Reason: can_direct_push=True AND verdict='Approve' ({final_verdict})")
                decision_reason = "Trusted repo & approved"
                try:
                    repo_git.remotes.origin.push(f"HEAD:{base_branch}")
                except Exception as pe:
                    logger.warning(f"Direct push failed for {repo_name} ({pe}). Attempting rebase...")
                    try:
                        repo_git.remotes.origin.pull(base_branch, rebase=True)
                        repo_git.remotes.origin.push(f"HEAD:{base_branch}")
                        logger.info(f"Push successful after rebase for {repo_name}")
                    except Exception as re_err:
                        logger.error(f"Critical push failure for {repo_name} after rebase attempt: {re_err}")
                        raise Exception(f"Git push failed for {repo_name} despite rebase attempt: {re_err}")
                commit_type = "Direct Commit"
                detail_msg = f"The fix was verified and pushed directly to the {base_branch} branch. Avg Confidence: {final_confidence:.2%}"
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
                "decision_reason": decision_reason,
                "original_body": issue.body
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
    """Verify issues that were 'fixed' but are awaiting log confirmation.
    Now implements a configurable 'cooling period' (PROD_VERIFICATION_DAYS).
    The issue is only closed if the error snippet has been absent for the full period.
    """
    config = load_config()
    days_required = int(config.get("PROD_VERIFICATION_DAYS", 7))

    for issue_id, info in list(processed.items()):
        if info.get("status") == "awaiting_prod_verification":
            repo_name, issue_num = issue_id.split(":")
            logger.info(f"Verifying production fix for {issue_id} (Required clean period: {days_required} days)...")
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
                            # Snippet is gone. Check if we've been clean long enough.
                            clean_since = info.get("clean_since")
                            now = datetime.now()

                            if not clean_since:
                                logger.info(f"Issue {issue_id} is clean. Starting {days_required}-day cooling period.")
                                info["clean_since"] = now.isoformat()
                                processed[issue_id] = info
                                save_processed(processed)
                            else:
                                first_clean_ts = datetime.fromisoformat(clean_since)
                                days_clean = (now - first_clean_ts).days
                                if days_clean >= days_required:
                                    logger.info(f"Verified: Issue {issue_id} has been clean for {days_clean} days. Closing issue.")
                                    issue.create_comment(f"🤖 **BugFixer AI Verification**\n\nProduction logs have been scanned and the error is no longer detected. The issue has remained clean for {days_required} days. Closing issue.")
                                    issue.edit(state='closed')
                                    processed[issue_id]["status"] = "verified"
                                    state["success_count"] += 1
                                    save_processed(processed)
                                else:
                                    logger.info(f"Issue {issue_id} is clean, but only for {days_clean}/{days_required} days. Waiting...")
                        else:
                            # Error reappeared. Reset the clean timer.
                            if info.get("clean_since"):
                                logger.warning(f"Issue {issue_id} error reappeared in logs. Resetting cooling period.")
                                info["clean_since"] = None
                                processed[issue_id] = info
                                save_processed(processed)
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
            # Scrub to error-relevant entries only before paying for an LLM
            # call: keeps the prompt small (avoids context-overflow 500s) and
            # focuses the model on actionable errors instead of INFO noise.
            error_logs = filter_error_logs(hub_logs)
            logger.info(
                f"Hub logs scrubbed: {len(hub_logs)} entries -> {len(error_logs)} "
                f"error-relevant entries for LLM analysis."
            )
            actionable_errors = []
            if not error_logs:
                logger.info("No error-level Hub log entries this cycle. Skipping LLM analysis.")
            else:
                actionable_errors = analyze_logs_for_errors(error_logs)
            monitored_repos = get_monitored_repos(config)
            for error in actionable_errors:
                # Defensive: ensure error is a dict (analyze_logs_for_errors already
                # guarantees this, but we double-check to be absolutely safe).
                if not isinstance(error, dict):
                    logger.warning(f"Skipping non-dict actionable error: {error!r}")
                    continue
                if not error.get('body') or not str(error.get('body')).strip():
                    logger.warning(f"Skipping actionable error with no body specified: {error.get('title')}")
                    continue

                # Route the issue to the module's own repo rather than relying on
                # the LLM's repo guess (which previously dumped everything into the
                # self-diagnosis repo). The module is authoritative.
                module = error.get('module')
                repo_name = resolve_module_repo(module, monitored_repos, config)
                if not repo_name:
                    # Fall back to the LLM's repo hint only if it is itself a
                    # monitored repo (so we never file into an arbitrary repo).
                    llm_repo = error.get('repo') or ''
                    if llm_repo and llm_repo in monitored_repos:
                        repo_name = llm_repo
                    else:
                        source_info = error.get('source_data', {})
                        host_info = source_info.get('host', 'unknown host') if isinstance(source_info, dict) else 'unknown source'
                        logger.warning(
                            f"Skipping actionable error for module={module!r} (source host: {host_info}): no monitored repo "
                            f"maps to this module (LLM repo hint={llm_repo!r}). Add a "
                            f"'module_repo_map' entry in Settings if this module should be tracked."
                        )
                        continue
                # Make the resolved repo authoritative for downstream code.
                error['repo'] = repo_name
                try:
                    repo_obj = gh_current.get_repo(repo_name)
                    create_automated_issue(gh_current, monitored_repos, repo_obj, error)
                    logger.info(f"Handled automated issue for log error in {repo_name} (module={module})")
                except GithubException as ge:
                    if ge.status == 404:
                        logger.error(
                            f"Cannot create automated issue for '{repo_name}': repository not found (404). "
                            f"Verify that '{repo_name}' exists and the configured GITHUB_TOKEN has access. "
                            f"Skipping this error."
                        )
                    else:
                        logger.error(f"Failed to create auto-issue for {repo_name}: {ge}")
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
                                if status != "awaiting_review": # Allow resuming reviews
                                    continue
                            if status == "awaiting_local":
                                if not (is_local_llm_allowed() or state["force_cloud"]):
                                    continue
                            # For awaiting_review, we let it proceed to check the 1-hour timer in process_single_issue

                        to_fix.append((repo_name, issue.number))
                    except Exception as e:
                        logger.exception(f"Failed to triage issue {issue_id}: {e}")

                if to_fix:
                    logger.info(f"Found {len(to_fix)} issues to fix in {repo_name}. Processing concurrently (max {max_workers})...")
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = [executor.submit(process_single_issue, r, n) for r, n in to_fix]
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

    return clean_repo_name(self_repo_name)

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

def _empty_chats_store():
    """Returns a fresh multi-conversation store with one untitled, active chat."""
    conv = {"id": "c1", "title": "", "created": datetime.now().isoformat(), "messages": []}
    return {"next_id": 2, "active_id": "c1", "conversations": [conv]}

def _title_from_message(content):
    """Derives a short conversation title from the first user message."""
    title = (content or "").strip().splitlines()[0] if content else ""
    return title[:60]

def load_chats():
    """Returns the persisted multi-conversation store.

    Schema: {"next_id": int, "active_id": str|None, "conversations": [
        {"id": str, "title": str, "created": str, "messages": [{role,content,ts}]}
    ]}. Transparently migrates a legacy flat message list (pre-V.48 single-thread
    chat_history.json) into the first conversation so no history is lost.
    """
    with _chat_lock:
        try:
            with open(CHAT_HISTORY_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            return _empty_chats_store()

        if isinstance(data, list):
            # Legacy flat single-thread history -> wrap into one conversation.
            first_user = next((m.get("content", "") for m in data
                               if isinstance(m, dict) and m.get("role") == "user"), "")
            store = {
                "next_id": 2,
                "active_id": "c1",
                "conversations": [{
                    "id": "c1",
                    "title": _title_from_message(first_user) or "Chat 1",
                    "created": datetime.now().isoformat(),
                    "messages": [m for m in data if isinstance(m, dict)],
                }],
            }
            save_chats(store)
            return store

        if not isinstance(data, dict) or not isinstance(data.get("conversations"), list):
            return _empty_chats_store()

        store = {
            "next_id": int(data.get("next_id", 1) or 1),
            "active_id": data.get("active_id"),
            "conversations": [c for c in data["conversations"] if isinstance(c, dict)],
        }
        if not store["conversations"]:
            return _empty_chats_store()
        if not store["active_id"] or not get_conversation(store, store["active_id"]):
            store["active_id"] = store["conversations"][-1]["id"]
        return store

def save_chats(store):
    """Persists the whole multi-conversation store under _chat_lock."""
    with _chat_lock:
        try:
            with open(CHAT_HISTORY_FILE, "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save chats to {CHAT_HISTORY_FILE}: {e}")

def get_conversation(store, chat_id):
    """Returns the conversation dict for chat_id, or None if not found."""
    for c in store.get("conversations", []):
        if c.get("id") == chat_id:
            return c
    return None

def append_chat_message(chat_id, msg):
    """Atomically appends a message to a conversation and persists the store.

    Auto-titles the conversation from the first user message if it is untitled.
    Sets the conversation as active. Returns the message dict, or None if the
    conversation does not exist.
    """
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            return None
        conv["messages"].append(msg)
        if msg.get("role") == "user" and not conv.get("title"):
            conv["title"] = _title_from_message(msg.get("content", ""))
        store["active_id"] = chat_id
        save_chats(store)
        return msg

def create_conversation():
    """Creates a new empty conversation, makes it active, and persists. Returns its id."""
    with _chat_lock:
        store = load_chats()
        cid = f"c{store['next_id']}"
        store["next_id"] += 1
        store["conversations"].append({
            "id": cid,
            "title": "",
            "created": datetime.now().isoformat(),
            "messages": [],
        })
        store["active_id"] = cid
        save_chats(store)
        return cid

def rename_conversation(chat_id, title):
    """Renames a conversation. Returns True if found and updated."""
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            return False
        conv["title"] = (title or "").strip()[:120]
        save_chats(store)
        return True

def delete_conversation(chat_id):
    """Deletes a conversation and selects a new active one. Returns the new active id."""
    with _chat_lock:
        store = load_chats()
        store["conversations"] = [c for c in store["conversations"] if c.get("id") != chat_id]
        if not store["conversations"]:
            store = _empty_chats_store()
        else:
            store["active_id"] = store["conversations"][-1]["id"]
        save_chats(store)
        return store["active_id"]

def set_active_chat(chat_id):
    """Sets the active conversation if chat_id exists. Returns True on success."""
    with _chat_lock:
        store = load_chats()
        if not get_conversation(store, chat_id):
            return False
        store["active_id"] = chat_id
        save_chats(store)
        return True

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
            "BugFixer settings (http://localhost:8000/settings) to a valid, accessible "
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
                f"BugFixer settings (http://localhost:8000/settings) to point at a valid, "
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

        # Persist the new read position. Saved immediately after reading so a
        # crash or filing failure never causes the same lines to be re-read.
        save_self_scan_offset(os.path.getsize(log_path), current_inode)

        formatted_logs = []
        for line in new_text.splitlines():
            if "[ERROR]" in line or "[CRITICAL]" in line:
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
        scrubbed_self_logs = filter_error_logs(formatted_logs)
        logger.info(
            f"Self logs scrubbed: {len(formatted_logs)} -> {len(scrubbed_self_logs)} "
            f"unique error entries for LLM analysis."
        )
        actionable_errors = analyze_logs_for_errors(scrubbed_self_logs)
        if not actionable_errors:
            update_task_state(task_id="SelfScan", action="end")
            return

        monitored_repos = get_monitored_repos(config)
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
                create_automated_issue(gh_current, monitored_repos, repo_obj, error)
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

        monitored_repos = get_monitored_repos(config)
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
        if not state["paused"]:
            run_scan_cycle()
        else:
            logger.debug("Poller worker is paused. Skipping scan cycle.")
        time.sleep(int(os.getenv("POLL_INTERVAL_SECONDS", 300)))

@app.get("/api/health")
async def health_check():
    """Heartbeat endpoint for the watchdog service."""
    return {"status": "ok"}

@app.post("/api/toggle-pause")
async def toggle_pause():
    state["paused"] = not state["paused"]
    logger.info(f"BugFixer autonomous operations {'PAUSED' if state['paused'] else 'RESUMED'}")
    return {"status": "success", "paused": state["paused"]}

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
                name = m["name"]
                if "bf16" in name:
                    name = name[:name.find("bf16") + 4]
                details = m.get("details", "No description available")
                if not isinstance(details, str):
                    details = details.get("description", str(details)) if isinstance(details, dict) else str(details)
                results["local_models"].append({
                    "name": name,
                    "details": details
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
                details = m.get("details", "No description available")
                if not isinstance(details, str):
                    details = details.get("description", str(details)) if isinstance(details, dict) else str(details)
                results["cloud_models"].append({
                    "name": m["name"],
                    "details": details
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
            logs = "".join(reversed(lines[-100:]))
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
    "LLM_MAX_RETRIES": "5",
    "LLM_BACKOFF_BASE": "2.0",
    "LLM_BACKOFF_MAX": "600.0",
    "LLM_MAX_CONCURRENT": "1",
    "PROD_VERIFICATION_DAYS": "7",
    "QUEUE_LOCAL_LLM": "False",
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
        "LLM_MAX_RETRIES": lambda v: v,
        "LLM_BACKOFF_BASE": lambda v: v,
        "LLM_BACKOFF_MAX": lambda v: v,
        "LLM_MAX_CONCURRENT": lambda v: v,
        "PROD_VERIFICATION_DAYS": lambda v: v,
        "self_diagnosis_repo": lambda v: clean_repo_name(v.strip()) if v and v.strip() else "",
        "module_repo_map": lambda v: parse_module_repo_map(v),
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

    if "queue_local_llm" in data:
        config_data["queue_local_llm"] = data.get("queue_local_llm") == "on"

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

    global _LLM_SEMAPHORE
    with _LLM_SEM_LOCK:
        _LLM_SEMAPHORE = None

    try:
        validate_llm_config_on_startup()
    except Exception as ve:
        logger.warning(f"Post-save LLM validation failed (non-fatal): {ve}")

    return RedirectResponse(url="/settings", status_code=303)

@app.post("/clear_history")
async def clear_history():
    """Clears all processed issues and resets success/failure counters."""
    global state
    logger.info("Clearing all issue history and resetting counters.")

    state["processed"] = {}
    state["success_count"] = 0
    state["failure_count"] = 0

    save_processed({})

    return {"status": "success", "message": "All history and tasks have been cleared."}

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

@app.post("/retry_all_failed")
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
        for issue_id in to_retry:
            repo_name, issue_num = issue_id.split(":")
            process_single_issue(repo_name, int(issue_num), llm_preference=llm_pref)

    threading.Thread(target=bulk_run, daemon=True).start()
    return {"status": "triggered", "message": f"Bulk retry started for {len(to_retry)} issues using {llm_pref} LLM."}

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

def run_chat_reply(chat_id):
    """Background worker that streams an LLM reply for one conversation turn.

    Builds a sliding multi-turn window from that conversation's persisted
    messages (capped to avoid context-overflow 500s), calls call_llm with the
    messages array, then persists the assistant reply. Live progress is written
    into state["active_tasks"][chat_id]["stream"] by call_llm itself;
    completion/error is tracked in state["chat_streams"][chat_id].
    """
    try:
        config = load_config()
        window_size = int(config.get("CHAT_HISTORY_WINDOW", 20) or 20)
        system_prompt = config.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            with _chat_lock:
                if chat_id in state["chat_streams"]:
                    state["chat_streams"][chat_id].update({"done": True, "error": "Conversation not found"})
            return
        messages = conv.get("messages", [])
        # Keep the most recent `window_size` turns and prepend the system message.
        window = [{"role": "system", "content": system_prompt}] + messages[-window_size:]
        reply = call_llm("", messages=window, task_id=chat_id)
        if reply and reply.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": reply,
                "ts": datetime.now().isoformat(),
            })
        with _chat_lock:
            if chat_id in state["chat_streams"]:
                state["chat_streams"][chat_id].update({
                    "stream": reply or "",
                    "done": True,
                    "error": None,
                })
    except Exception as e:
        logger.error(f"run_chat_reply failed for {chat_id}: {e}\n{traceback.format_exc()}")
        with _chat_lock:
            if chat_id in state["chat_streams"]:
                state["chat_streams"][chat_id].update({
                    "done": True,
                    "error": f"LLM error: {e}",
                })
    finally:
        # Remove the chat task from the Dashboard activity feed.
        update_task_state(chat_id, "Chat", action="end")

@app.get("/chat")
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

@app.post("/api/chat/new")
async def chat_new():
    """Creates a new empty conversation and makes it active."""
    cid = create_conversation()
    return {"chat_id": cid}

@app.post("/api/chat")
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

@app.get("/api/chat/stream")
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

@app.post("/api/chat/rename")
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

@app.post("/api/chat/delete")
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

@app.post("/api/chat/clear")
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

threading.Thread(target=connectivity_worker, daemon=True).start()
threading.Thread(target=heartbeat_worker, daemon=True).start()
threading.Thread(target=poller_worker, daemon=True).start()
threading.Thread(target=updater_worker, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)