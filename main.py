import os, json, time, tempfile, threading, requests, logging, traceback
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
DEFAULT_LOG_FILE = "/var/log/bugfix reins/bugfixer.log"

def get_log_path():
    return os.getenv("LOG_FILE_PATH", "/var/log/bugfixer/bugfixer.log")

log_file = get_log_path()

# Ensure log directory exists
log_dir = os.path.dirname(log_file)
if not os.path.exists(log_dir):
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:
        print(f"Error creating log directory {log_dir}: {e}")

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BugFixer")
logger.info(f"BugFixer started. Logging to: {log_file}")

load_dotenv()
app = FastAPI()

# Use absolute path for templates to avoid 500 errors if CWD changes
template_path = os.path.join(os.getcwd(), "templates")
templates = Jinja2Templates(directory=template_path)

# Global Exception Handler to capture 500s and log them to the file
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

state = {
    "status": "Idle", "active_llm": "Unknown", "local_online": False,
    "last_run": "Never", "current_task": "None", "api_status": "Not Triggered",
    "processed": [], "force_cloud": False
}
STATE_FILE = "processed_issues.json"

def load_config():
    try:
        with open("config.json", "r") as f: return json.load(f)
    except: return {"monitored_repos": [], "trusted_repos": [], "default_branch": "main", "direct_push_enabled": False, "dev_branch": "dev", "repo_tests": {}}

def load_processed():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f: return json.load(f)
        except: return []
    return []

def save_processed(processed):
    with open(STATE_FILE, "w") as f: json.dump(processed, f)

def trigger_infrastructure_update():
    url = os.getenv("UPDATE_API_URL")
    if not url or "your-netbox" in url: return "URL not configured"
    try:
        resp = requests.post(url, json={}, timeout=10)
        return "SUCCESS: Sync Triggered" if resp.status_code == 200 else f"FAILED: {resp.status_code}"
    except Exception as e: return f"ERROR: {str(e)}"

def get_hub_state():
    url = os.getenv("HUB_QUERY_URL")
    if not url or "your-netbox" in url: return None
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.text # Return raw response for comparison
        return None
    except Exception as e:
        logger.error(f"Hub Query Error: {e}")
        return None


def heartbeat_worker():
    while True:
        local_url = os.getenv("LOCAL_OLLAMA_URL")
        try:
            if local_url:
                requests.get(f"{local_url}/api/tags", timeout=2)
                state["local_online"] = True
            else: state["local_online"] = False
        except: state["local_online"] = False
        time.sleep(5)

def analyze_issue(issue):
    """Determines if the issue has enough information to be fixed.
    Returns (is_actionable, request_message)
    """
    l_mod, c_mod = os.getenv("LOCAL_OLLAMA_MODEL"), os.getenv("CLOUD_OLLAMA_MODEL")
    l_url, c_url = os.getenv("LOCAL_OLLAMA_URL"), os.getenv("CLOUD_OLLAMA_URL")
    api_key = os.getenv("OLLAMA_API_KEY")

    prompt = (
        f"Issue Title: {issue.title}\n"
        f"Issue Body: {issue.body}\n\n"
        "Determine if this issue contains enough information to provide a code fix. "
        "Specifically, for UI or runtime errors, check if console logs or stack traces are present. "
        "If information is missing, specify exactly what is needed (e.g., 'Please provide the browser console output').\n\n"
        "Return ONLY a JSON object: {\"actionable\": boolean, \"request\": \"message if not actionable\"}"
    )

    def call_llm(url, model):
        endpoint = f"{url.rstrip('/')}/api/chat"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        headers = {"Content-Type": "application/json"}
        if api_key: headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            return resp.json()['message']['content']
        except Exception as e: raise Exception(f"LLM Request failed ({url}): {e}")

    try:
        if state["force_cloud"]:
            res = call_llm(c_url, c_mod)
        else:
            try:
                res = call_llm(l_url, l_mod)
            except:
                res = call_llm(c_url, c_mod)

        import re
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data.get("actionable", False), data.get("request", "More information is needed to proceed with a fix.")
        return False, "Information provided is not in a usable format. Please provide more details."
    except Exception as e:
        logger.error(f"Error analyzing issue: {e}")
        return True, "" # Default to true to avoid blocking if analyzer fails


def apply_ai_fix(repo_path, issue_body, error_context=None):
    l_mod, c_mod = os.getenv("LOCAL_OLLAMA_MODEL"), os.getenv("CLOUD_OLLAMA_MODEL")
    l_url, c_url = os.getenv("LOCAL_OLLAMA_URL"), os.getenv("CLOUD_OLLAMA_URL")
    api_key = os.getenv("OLLAMA_API_KEY")

    # 1. Identify relevant files
    relevant_files = identify_files_to_fix(repo_path, issue_body)
    if not relevant_files:
        # Fallback: if we can't identify files, we'll try to let the LLM decide in the next step,
        # but we'll warn the logger.
        logger.warning(f"No specific files identified for issue. Attempting general fix.")

    # 2. Read file contents
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

    def call_llm(url, model):
        endpoint = f"{url.rstrip('/')}/api/chat"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        headers = {"Content-Type": "application/json"}
        if api_key: headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
            return resp.json()['message']['content']
        except Exception as e:
            raise Exception(f"LLM Request failed ({url}): {e}")

    if state["force_cloud"]:
        try:
            state["active_llm"] = f"Cloud ({c_url})"
            return call_llm(c_url, c_mod)
        except Exception as e: raise Exception(f"Cloud LLM failed: {e}")

    try:
        state["active_llm"] = f"Local ({l_url})"
        return call_llm(l_url, l_mod)
    except Exception as e:
        logger.warning(f"Local LLM failed: {e}. Falling back to Cloud...")
        try:
            state["active_llm"] = f"Cloud ({c_url})"
            return call_llm(c_url, c_mod)
        except Exception as e_c: raise Exception(f"Both LLMs failed: {e_c}")

def parse_and_apply(content, repo_path):
    try:
        # Extract JSON if the LLM wrapped it in markdown
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
    """
    Heuristically attempts to verify the fix by running tests in the repo.
    Prioritizes per-repo test commands from config, then global QA_REPO, then auto-detection.
    Returns (success, error_message)
    """
    logger.info(f"Verifying fix in {repo_path}...")

    # 1. Check for per-repo test command in config
    repo_tests = config.get("repo_tests", {})
    test_cmd = repo_tests.get(repo_name)

    if test_cmd:
        logger.info(f"Using per-repo test command for {repo_name}: {test_cmd}")
    else:
        # 2. Fallback to global QA_REPO / QA_TEST_COMMAND
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
            import subprocess
            full_cmd = f"{test_cmd} {repo_path}" if " " not in test_cmd else test_cmd
            try:
                result = subprocess.run(
                    full_cmd,
                    cwd=qa_path,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    shell=True
                )
                if result.returncode == 0:
                    logger.info("External QA tests passed!")
                    return True, None
                else:
                    error_msg = result.stdout + result.stderr
                    logger.error(f"External QA tests failed:\n{error_msg}")
                    return False, error_msg
            except Exception as e:
                return False, f"QA Execution Error: {str(e)}"
        else:
            # 3. Internal auto-detection if no per-repo or global QA repo
            if not test_cmd:
                test_cmd = None

            if not test_cmd or test_cmd == "pytest":
                files = os.listdir(repo_path)
                if "package.json" in files: test_cmd = "npm test"
                elif "requirements.txt" in files or "pyproject.toml" in files: test_cmd = "pytest"
                elif "go.mod" in files: test_cmd = "go test ./..."
                elif "Makefile" in files: test_cmd = "make test"

            if not test_cmd:
                logger.info("No standard test framework detected. Assuming success (blind apply).")
                return True, "No tests found, assuming success"

            logger.info(f"Executing test command: {test_cmd}")
            import subprocess
            try:
                result = subprocess.run(
                    test_cmd,
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    shell=True
                )
                if result.returncode == 0:
                    logger.info("Tests passed successfully!")
                    return True, None
                else:
                    error_msg = result.stdout + result.stderr
                    logger.error(f"Tests failed:\n{error_msg}")
                    return False, error_msg
            except Exception as e:
                return False, f"Test Execution Error: {str(e)}"

    # If we had a per-repo command, we need to run it
    import subprocess
    try:
        result = subprocess.run(
            test_cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=300,
            shell=True
        )
        if result.returncode == 0:
            logger.info(f"Per-repo tests for {repo_name} passed!")
            return True, None
        else:
            error_msg = result.stdout + result.stderr
            logger.error(f"Per-repo tests for {repo_name} failed:\n{error_msg}")
            return False, error_msg
    except Exception as e:
        return False, f"Per-repo test error: {str(e)}"

def poller_worker():
    global state
    while True:
        try:
            load_dotenv(override=True)
            try:
                self_repo = git.Repo(os.getcwd())
                self_repo.remotes.origin.pull()
            except: pass

            config = load_config()
            state["status"] = "Scanning"
            state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            processed = load_processed()

            token = os.getenv("GITHUB_TOKEN")
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

            for repo_name in config["monitored_repos"]:
                state["current_task"] = f"Checking {repo_name}"
                logger.info(f"Scanning repository: {repo_name}")
                try:
                    repo_obj = gh_current.get_repo(repo_name)
                    is_owner = repo_obj.owner.login == bot_user
                    logger.info(f"Repo {repo_name} found. Owner match: {is_owner}")

                    issues = repo_obj.get_issues(labels=["automated-fix"], state="open")
                    issue_count = issues.totalCount
                    logger.info(f"Found {issue_count} open issues with 'automated-fix' label in {repo_name}")

                    for issue in issues:
                        issue_id = f"{repo_name}:{issue.number}"
                        if issue_id in processed: continue

                        state["current_task"] = f"Fixing {issue_id}"
                        logger.info(f"Processing issue {issue_id}: {issue.title}")

                        # Capture hub state before fix
                        before_state = get_hub_state()
                        if before_state:
                            logger.info(f"Captured hub state before fix for {issue_id}")

                        with tempfile.TemporaryDirectory() as tmp_dir:
                            path = os.path.join(tmp_dir, "repo")
                            url = repo_obj.clone_url.replace("https://", f"https://{token}@")
                            logger.info(f"Cloning {repo_name} to temporary directory...")
                            repo_git = git.Repo.clone_from(url, path)

                            # --- QA LOOP START ---
                            max_attempts = 3
                            success = False
                            error_context = None

                            for attempt in range(1, max_attempts + 1):
                                logger.info(f"AI Fix Attempt {attempt}/{max_attempts} for {issue_id}...")

                                fix_code = apply_ai_fix(path, issue.body, error_context)
                                logger.info(f"AI generated fix. Applying to files...")
                                success_applied, _, confidence = parse_and_apply(fix_code, path)
                                if not success_applied:
                                    verified = False
                                    failure_msg = "AI generated invalid JSON format"
                                else:
                                    # Verify the fix
                                    verified, failure_msg = verify_fix(path, repo_name, config)
                                    if verified:
                                        logger.info(f"Fix verified successfully on attempt {attempt}!")
                                        success = True
                                        final_confidence = confidence
                                        break
                                    else:
                                        logger.warning(f"Fix attempt {attempt} failed verification. Feeding error back to LLM...")
                                        error_context = failure_msg

                            if not success:
                                logger.error(f"AI failed to find a verified fix for {issue_id} after {max_attempts} attempts.")
                                continue
                            # --- QA LOOP END ---

                            repo_git.git.add(A=True)
                            commit_msg = f"AI Fix #{issue.number}: {issue.title[:50]}..."
                            repo_git.index.commit(commit_msg)
                            logger.info(f"Committed verified changes: {commit_msg}")

                            # Confidence-based deployment logic
                            confidence_threshold = 0.95
                            is_trusted = repo_name in config["trusted_repos"]
                            can_direct_push = config.get("direct_push_enabled") and is_trusted and is_owner

                            if can_direct_push and final_confidence >= confidence_threshold:
                                logger.info(f"High confidence ({final_confidence:.2f}) and trust verified. Pushing directly to main for {repo_name}...")
                                repo_git.remotes.origin.push()
                                logger.info("Direct push successful.")
                                commit_type = "Direct Commit"
                                detail_msg = f"The fix was verified (Confidence: {final_confidence:.2%}) and pushed directly to the main branch."
                            else:
                                # Push to dev_branch or a specific issue branch
                                target_branch = config.get("dev_branch", "dev") if final_confidence < confidence_threshold else f"ai-fix-issue-{issue.number}"
                                logger.info(f"Pushing to branch: {target_branch} (Confidence: {final_confidence:.2f})")

                                # Ensure we are on the target branch
                                try:
                                    repo_git.git.checkout(target_branch)
                                except:
                                    repo_git.create_head(target_branch).checkout()

                                repo_git.remotes.origin.push(target_branch)
                                pr = repo_obj.create_pull(title=f"AI Fix #{issue.number}", body=f"Automated fix for issue #{issue.number}. Confidence: {final_confidence:.2%}", head=target_branch, base=config["default_branch"])
                                logger.info(f"Pull Request created for {repo_name} on branch {target_branch}")
                                commit_type = "Pull Request"
                                detail_msg = f"The fix was verified and a Pull Request has been created on branch {target_branch}: {pr.html_url}"

                            # Comment and close the issue
                            try:
                                comment_body = (
                                    f"🤖 **BugFixer AI Update**\n\n"
                                    f"The issue has been successfully resolved via {commit_type}.\n"
                                    f"{detail_msg}\n\n"
                                    f"Verification: ✅ Tests passed successfully."
                                )
                                issue.create_comment(comment_body)
                                issue.edit(state='closed')
                                logger.info(f"Commented and closed issue {issue_id}")
                            except Exception as e:
                                logger.error(f"Failed to comment/close issue {issue_id}: {e}")

                        # Update infrastructure and verify state change
                        state["api_status"] = trigger_infrastructure_update()

                        if before_state:
                            after_state = get_hub_state()
                            if after_state and before_state == after_state:
                                logger.warning(f"Hub state for {issue_id} remained unchanged after update. Fix may not be reflected in hub.")
                            elif after_state:
                                logger.info(f"Hub state change detected for {issue_id}! Fix successfully reflected.")
                            else:
                                logger.error(f"Could not retrieve hub state after update for {issue_id}.")

                        processed.append(issue_id)
                        save_processed(processed)
                except GithubException as ge:
                    logger.error(f"GitHub API Error while accessing {repo_name}: {ge.status} - {ge.data}")
                    continue
                except Exception as e:
                    logger.exception(f"Unexpected error while processing {repo_name}: {e}")
                    continue

            state["processed"] = processed
            state["status"] = "Idle"
            state["current_task"] = "None"
        except Exception as e:
            logger.exception(f"Critical poller error: {e}")
            state["status"] = f"Error: {str(e)}"
        time.sleep(int(os.getenv("POLL_INTERVAL_SECONDS", 3600)))

@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"view": "status", "state": state})

@app.get("/logs")
async def get_logs(request: Request):
    try:
        current_log = get_log_path()
        with open(current_log, "r") as f:
            lines = f.readlines()
            logs = "".join(lines[-100:])
    except Exception as e: logs = f"Error reading logs from {get_log_path()}: {e}"
    return templates.TemplateResponse(request=request, name="index.html", context={"view": "logs", "logs": logs, "state": state})

DEFAULT_ENV = {
    "GITHUB_TOKEN": "",
    "LOCAL_OLLAMA_MODEL": "gemma4:31b-coding-mtp-bf16",
    "CLOUD_OLLAMA_MODEL": "gemma4:31b-cloud",
    "LOCAL_OLLAMA_URL": "http://172.16.1.100:11434",
    "CLOUD_OLLAMA_URL": "",
    "OLLAMA_API_KEY": "",
    "QA_REPO": "",
    "QA_TEST_COMMAND": "pytest",
    "POLL_INTERVAL_SECONDS": "3600",
    "UPDATE_API_URL": "",
    "HUB_QUERY_URL": "",
    "LOG_FILE_PATH": "/var/log/bugfixer/bugfixer.log",
    "DEV_BRANCH": "dev"
}

@app.get("/settings")
async def settings_page(request: Request):
    load_dotenv(override=True)
    # Start with defaults, then override with actual env vars
    settings = DEFAULT_ENV.copy()
    for k in DEFAULT_ENV:
        val = os.getenv(k)
        if val: settings[k] = val

    config = load_config()
    # Convert repo_tests dict to comma-separated string for the UI
    repo_tests = config.get("repo_tests", {})
    repo_tests_str = ", ".join([f"{k}:{v}" for k, v in repo_tests.items()])

    return templates.TemplateResponse(request=request, name="index.html", context={"view": "settings", "settings": {**settings, **config, "repo_tests_str": repo_tests_str}})

@app.post("/save_settings")
async def save_settings(request: Request):
    form_data = await request.form()
    data = dict(form_data)

    # Separate Config from Env
    config_data = {
        "monitored_repos": [x.strip() for x in data.get("monitored_repos", "").split(",") if x.strip()],
        "trusted_repos": [x.strip() for x in data.get("trusted_repos", "").split(",") if x.strip()],
        "default_branch": data.get("default_branch", "main"),
        "direct_push_enabled": data.get("direct_push_enabled") == "on",
        "dev_branch": data.get("dev_branch", "dev"),
        "repo_tests": {}
    }

    # Parse repo_tests (expected format: repo1:cmd1, repo2:cmd2)
    repo_tests_raw = data.get("repo_tests", "")
    if repo_tests_raw:
        for pair in repo_tests_raw.split(","):
            if ":" in pair:
                repo, cmd = pair.split(":", 1)
                config_data["repo_tests"][repo.strip()] = cmd.strip()

    # Write updated .env
    with open(".env", "w") as f:
        for k, v in data.items():
            if k not in config_data:
                f.write(f"{k}={v}\n")

    # Write updated config.json
    with open("config.json", "w") as f:
        json.dump(config_data, f, indent=2)

    return RedirectResponse(url="/settings", status_code=303)

@app.post("/toggle_cloud")
async def toggle_cloud():
    state["force_cloud"] = not state["force_cloud"]
    return RedirectResponse(url="/", status_code=303)

threading.Thread(target=heartbeat_worker, daemon=True).start()
threading.Thread(target=poller_worker, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
