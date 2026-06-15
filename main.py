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
DEFAULT_LOG_FILE = "/var/log/bugfixer.log"

def get_log_path():
    return os.getenv("LOG_FILE_PATH", "/var/log/bugfixer.log")

log_file = get_log_path()

# Ensure log directory exists
log_dir = os.path.dirname(log_file)
if not os.path.exists(log_dir):
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:
        print(f"Error creating log directory {log_dir}: {e}")

logging.basicConfig(
    level=logging.INFO,
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

# Shared LLM Utility
def call_llm(prompt, system_prompt="You are a helpful AI assistant."):
    """Generic LLM caller with Local -> Cloud failover and JSON extraction."""
    l_mod, c_mod = os.getenv("LOCAL_OLLAMA_MODEL"), os.getenv("CLOUD_OLLAMA_MODEL")
    l_url, c_url = os.getenv("LOCAL_OLLAMA_URL"), os.getenv("CLOUD_OLLAMA_URL")
    api_key = os.getenv("OLLAMA_API_KEY")

    def _request(url, model):
        endpoint = f"{url.rstrip('/')}/api/chat"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "stream": False
        }
        headers = {"Content-Type": "application/json"}
        if api_key: headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        return resp.json()['message']['content']

    try:
        if state["force_cloud"]:
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

    # Check if docker is available
    docker_available = False
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        docker_available = True
    except:
        pass

    if not docker_available:
        logger.warning("⚠️ Docker not found. Running command on HOST with ROOT privileges. This is insecure.")
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=300, shell=True)

    # Determine image based on project type
    image = "ubuntu:latest"
    files = os.listdir(cwd)
    if "package.json" in files: image = "node:18-slim"
    elif "requirements.txt" in files or "pyproject.toml" in files: image = "python:3.9-slim"
    elif "go.mod" in files: image = "golang:1.21-slim"

    logger.info(f"Running sandboxed command in Docker image {image}...")

    # Docker run command:
    # -v mounts the repo as a volume
    # -w sets working directory
    # --rm removes container after run
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

def discover_labels(gh_current, monitored_repos):
    """Fetches all unique labels from all monitored repositories."""
    all_labels = set()
    for repo_name in monitored_repos:
        try:
            repo = gh_current.get_repo(repo_name)
            labels = repo.get_labels()
            for label in labels:
                all_labels.add(label.name)
        except Exception as e:
            logger.error(f"Error discovering labels for {repo_name}: {e}")
    return sorted(list(all_labels))

def load_config():
    try:
        with open("config.json", "r") as f: return json.load(f)
    except: return {"monitored_repos": [], "trusted_repos": [], "default_branch": "main", "direct_push_enabled": False, "dev_branch": "dev", "repo_tests": {}, "GITHUB_TOKEN": "", "monitored_labels": ["automated-fix"]}

def load_processed():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {issue_id: {"status": "fixed", "timestamp": datetime.now().isoformat()} for issue_id in data}
                return data
        except: return {}
    return {}

def save_processed(processed):
    with open(STATE_FILE, "w") as f: json.dump(processed, f, indent=2)

def trigger_infrastructure_update():
    url = os.getenv("UPDATE_API_URL")
    if not url or "your-netbox" in url: return "URL not configured"
    try:
        resp = requests.post(url, json={}, timeout=10)
        return "SUCCESS: Sync Triggered" if resp.status_code == 200 else f"FAILED: {resp.status_code}"
    except Exception as e: return f"ERROR: {str(e)}"

def create_automated_issue(gh_repo, error_data):
    """Creates a GitHub issue for a log-detected error."""
    try:
        title = f"🤖 Log Alert: {error_data['title']}"
        body = (
            f"**Automated Error Detection**\n\n"
            f"The BugFixer Hub analysis detected a potential issue in the logs:\n\n"
            f"### Log Evidence:\n```\n{error_data['body']}\n```\n\n"
            f"This issue has been automatically created for fixing."
        )
        issue = gh_repo.create_issue(
            title=title,
            body=body,
            labels=["automated-fix", "log-detected"]
        )
        logger.info(f"Created automated issue #{issue.number} for {error_data['repo']}")
        return issue
    except Exception as e:
        logger.error(f"Failed to create automated issue: {e}")
        return None

def get_hub_logs():
    """Fetches recent logs from the Hub for all modules."""
    url = os.getenv("HUB_QUERY_URL")
    if not url or "your-netbox" in url: return None
    try:
        log_url = url.rstrip('/') + "/logs"
        resp = requests.get(log_url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logger.error(f"Hub Log Fetch Error: {e}")
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
        "Return ONLY a JSON array of objects: [{\"repo\": \"owner/repo\", \"title\": \"Error Summary\", \"body\": \"Log snippet and description\"}]"
    )
    try:
        res = call_llm(prompt, system_prompt="You are a log analysis expert. Return only a JSON array.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []
    except Exception as e:
        logger.error(f"Error analyzing logs: {e}")
        return []

def heartbeat_worker():
    while True:
        try:
            config = load_config()
            # Priority 1: config.json, Priority 2: Environment
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

            # Update active_llm based on current configuration and connectivity
            if state["force_cloud"]:
                state["active_llm"] = f"Cloud ({cloud_url or 'Not Configured'})"
            elif state["local_online"]:
                state["active_llm"] = f"Local ({local_url})"
            else:
                # If local is offline, we might still be using cloud if it's the only option
                # but for the UI, let's be explicit.
                if cloud_url:
                    state["active_llm"] = f"Cloud (Fallback - Local Offline)"
                else:
                    state["active_llm"] = "No LLM Available"
        except Exception as e:
            logger.error(f"Heartbeat worker error: {e}")

        time.sleep(5)

def analyze_issue(issue):
    prompt = (
        f"Issue Title: {issue.title}\n"
        f"Issue Body: {issue.body}\n\n"
        "Determine if this issue contains enough information to provide a code fix. "
        "Specifically, for UI or runtime errors, check if console logs or stack traces are present. "
        "If information is missing, specify exactly what is needed (e.g., 'Please provide the browser console output').\n\n"
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
        logger.info("Detected Go la project. Running go mod download...")
        run_sandboxed_command("go mod download", repo_path)
    elif "Makefile" in files:
        logger.info("Detected Makefile. Attempting 'make install'...")
        run_sandboxed_command("make install", repo_path)
    else:
        logger.info("No known dependency file detected. Skipping installation.")

def review_fix(repo_path, issue_body, proposed_fixes):
    logger.info("Running Skeptical Reviewer pass...")
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
    try:
        res = call_llm(prompt, system_prompt="You are a skeptical senior engineer. Be critical. Only return JSON.")
        import re
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"confidence": 0.0, "verdict": "Reject", "critique": "Reviewer returned invalid format."}
    except Exception as e:
        logger.error(f"Reviewer error: {e}")
        return {"confidence": 0.0, "verdict": "Reject", "critique": f"Reviewer crashed: {e}"}

def apply_ai_fix(repo_path, issue_body, error_context=None):
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
        return call_llm(prompt, system_prompt="You are a master coder. Only return a JSON object.")
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
                elif "requirements.txt" in files or "pyproject.toml" in files: test_cmd = "pytest"
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
    """Checks GitHub for new versions and restarts the service if an update is found."""
    try:
        self_repo = git.Repo(os.getcwd())
        # Check current commit hash
        old_commit = self_repo.head.commit.hexsha
        self_repo.remotes.origin.pull()
        new_commit = self_repo.head.commit.hexsha
        if old_commit != new_commit:
            logger.info(f"New version detected! {old_commit[:7]} -> {new_commit[:7]}. Triggering restart...")
            import subprocess
            subprocess.Popen(["sudo", "systemctl", "restart", "bugfixer"])
            return True, f"Update found: {old_commit[:7]} -> {new_commit[:7]}. Restarting..."
        return False, "No updates available."
    except Exception as e:
        logger.warning(f"Self-update check failed: {e}")
        return False, f"Update check failed: {e}"

def poller_worker():
    global state
    while True:
        try:
            load_dotenv(override=True)
            check_for_updates()
            config = load_config()

            state["status"] = "Scanning"
            state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            processed = load_processed()

            # Priority 1: config.json, Priority 2: Environment
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

            # --- Label Discovery Phase ---
            state["current_task"] = "Discovering Labels"
            state["available_labels"] = discover_labels(gh_current, config["monitored_repos"])
            logger.info(f"Discovered {len(state['available_labels'])} unique labels across monitored repos.")

            # --- Production Verification Phase ---
            # Verify issues that were "fixed" but are awaiting log confirmation
            for issue_id, info in list(processed.items()):
                if info.get("status") == "awaiting_prod_verification":
                    repo_name, issue_num = issue_id.split(":")
                    logger.info(f"Verifying production fix for {issue_id}...")
                    try:
                        repo_obj = gh_current.get_repo(repo_name)
                        issue = repo_obj.get_issue(int(issue_num))

                        # Get logs from hub
                        logs = get_hub_logs()
                        if logs:
                            module_name = repo_name.split('/')[-1]
                            relevant_logs = [l['log'] for l in logs if l.get('module') == module_name]
                            full_log_text = "\n".join(relevant_logs)

                            # Look for the original error snippet in the issue body
                            import re
                            match = re.search(r"### Log Evidence:\n```\n(.*?)\n```", issue.body, re.DOTALL)
                            if match:
                                snippet = match.group(1).strip()
                                if snippet.lower() not in full_log_text.lower():
                                    logger.info(f"Verified: Error snippet no longer found in logs for {issue_id}. Closing issue.")
                                    issue.create_comment("🤖 **BugFixer AI Verification**\n\nProduction logs have been scanned and the error is no longer detected. Closing issue.")
                                    issue.edit(state='closed')
                                    processed[issue_id]["status"] = "verified"
                                    save_processed(processed)
                                else:
                                    logger.info(f"Issue {issue_id} still failing in production logs.")
                    except Exception as e:
                        logger.error(f"Error verifying {issue_id}: {e}")

            # --- Hub Log Scanning Phase ---
            state["current_task"] = "Scanning Hub Logs"
            logger.info("Scanning Hub for new errors...")
            hub_logs = get_hub_logs()
            if hub_logs:
                actionable_errors = analyze_logs_for_errors(hub_logs)
                for error in actionable_errors:
                    repo_name = error['repo']
                    try:
                        repo_obj = gh_current.get_repo(repo_name)
                        # Create issue (will be picked up in the next loop or a later iteration)
                        create_automated_issue(repo_obj, error)
                        logger.info(f"Automatically created issue for log error in {repo_name}")
                    except Exception as e:
                        logger.error(f"Failed to create auto-issue for {repo_name}: {e}")

            for repo_name in config["monitored_repos"]:
                state["current_task"] = f"Checking {repo_name}"
                logger.info(f"Scanning repository: {repo_name}")
                try:
                    repo_obj = gh_current.get_repo(repo_name)
                    is_owner = repo_obj.owner.login == bot_user
                    logger.info(f"Repo {repo_name} found. Owner match: {is_owner}")
                    labels = config.get("monitored_labels", ["automated-fix"])
                    if "NONE" in labels:
                        logger.info(f"Label set to NONE for {repo_name}. Skipping issue scan.")
                        continue

                    if "ANY" in labels:
                        issues = repo_obj.get_issues(state="open")
                        logger.info(f"Scanning ALL open issues in {repo_name} (ANY label selected).")
                    else:
                        issues = repo_obj.get_issues(labels=labels, state="open")
                        logger.info(f"Scanning issues with labels {labels} in {repo_name}.")

                    issue_count = issues.totalCount
                    logger.info(f"Found {issue_count} matching open issues in {repo_name}")
                    for issue in issues:
                        issue_id = f"{repo_name}:{issue.number}"
                        if issue_id in processed:
                            # If it's awaiting prod verification, we don't re-fix it yet
                            if processed[issue_id].get("status") == "awaiting_prod_verification":
                                continue
                            logger.info(f"Issue {issue_id} was previously processed but is now OPEN again. Re-evaluating...")
                            del processed[issue_id]
                        state["current_task"] = f"Fixing {issue_id}"
                        logger.info(f"Processing issue {issue_id}: {issue.title}")
                        before_state = get_hub_state()
                        if before_state:
                            logger.info(f"Captured hub state before fix for {issue_id}")
                        with tempfile.TemporaryDirectory() as tmp_dir:
                            path = os.path.join(tmp_dir, "repo")
                            url = repo_obj.clone_url.replace("https://", f"https://{token}@")
                            logger.info(f"Cloning {repo_name} to temporary directory...")
                            repo_git = git.Repo.clone_from(url, path)
                            max_attempts = 3
                            success = False
                            error_context = None
                            for attempt in range(1, max_attempts + 1):
                                logger.info(f"AI Fix Attempt {attempt}/{max_attempts} for {issue_id}...")
                                fix_code = apply_ai_fix(path, issue.body, error_context)
                                logger.info(f"AI generated fix. Applying to files...")
                                success_applied, fixes, confidence = parse_and_apply(fix_code, path)
                                if not success_applied:
                                    verified = False
                                    failure_msg = "AI generated invalid JSON format"
                                else:
                                    review = review_fix(path, issue.body, fixes)
                                    review_conf = review.get("confidence", 0.0)
                                    review_verdict = review.get("verdict", "Reject")
                                    logger.info(f"Reviewer Verdict: {review_verdict} (Conf: {review_conf:.2f}) - {review.get('critique')}")
                                    prepare_environment(path)
                                    verified, failure_msg = verify_fix(path, repo_name, config)
                                    if verified:
                                        logger.info(f"Fix verified successfully on attempt {attempt}!")
                                        success = True
                                        final_confidence = (confidence + review_conf) / 2
                                        final_verdict = review_verdict
                                        break
                                    else:
                                        logger.warning(f"Fix attempt {attempt} failed verification. Feeding error back to LLM...")
                                        error_context = failure_msg
                            if not success:
                                logger.error(f"AI failed to find a verified fix for {issue_id} after {max_attempts} attempts.")
                                continue
                            repo_git.git.add(A=True)
                            commit_msg = f"AI Fix #{issue.number}: {issue.title[:50]}..."
                            repo_git.index.commit(commit_msg)
                            logger.info(f"Committed verified changes: {commit_msg}")
                            confidence_threshold = 0.95
                            is_trusted = repo_name in config["trusted_repos"]
                            can_direct_push = config.get("direct_push_enabled") and is_trusted and is_owner
                            if can_direct_push and final_confidence >= confidence_threshold and final_verdict == "Approve":
                                logger.info(f"High confidence ({final_confidence:.2f}) and trust verified. Pushing directly to main for {repo_name}...")
                                repo_git.remotes.origin.push()
                                logger.info("Direct push successful.")
                                commit_type = "Direct Commit"
                                detail_msg = f"The fix was verified (Avg Conf: {final_confidence:.2%}) and pushed directly to the main branch."
                            else:
                                target_branch = config.get("dev_branch", "dev") if final_confidence < confidence_threshold else f"ai-fix-issue-{issue.number}"
                                logger.info(f"Pushing to branch: {target_branch} (Avg Conf: {final_confidence:.2f})")
                                try:
                                    repo_git.git.checkout(target_branch)
                                except:
                                    repo_git.create_head(target_branch).checkout()
                                repo_git.remotes.origin.push(target_branch, force=True)
                                pr = repo_obj.create_pull(title=f"AI Fix #{issue.number}", body=f"Automated fix for issue #{issue.number}. Avg Confidence: {final_confidence:.2%}", head=target_branch, base=config["default_branch"])
                                logger.info(f"Pull Request created for {repo_name} on branch {target_branch}")
                                commit_type = "Pull Request"
                                detail_msg = f"The fix was verified and a Pull Request has been created on branch {target_branch}: {pr.html_url}"
                            try:
                                comment_body = (
                                    f"🤖 **BugFixer AI Update**\n\n"
                                    f"The issue has been successfully resolved via {commit_type}.\n"
                                    f"{detail_msg}\n\n"
                                    f"Verification: ✅ Tests passed successfully."
                                )
                                issue.create_comment(comment_body)
                                # Only close immediately if it wasn't log-detected
                                is_log_detected = "log-detected" in issue.get_labels()
                                if not is_log_detected:
                                    issue.edit(state='closed')
                                    logger.info(f"Commented and closed issue {issue_id}")
                                else:
                                    logger.info(f"Issue {issue_id} is log-detected. Leaving open until production verification.")
                            except Exception as e:
                                logger.error(f"Failed to comment/close issue {issue_id}: {e}")
                            state["api_status"] = trigger_infrastructure_update()
                            if before_state:
                                after_state = get_hub_state()
                                if after_state and before_state == after_state:
                                    logger.warning(f"Hub state for {issue_id} remained unchanged after update. Fix may not be reflected in hub.")
                                elif after_state:
                                    logger.info(f"Hub state change detected for {issue_id}! Fix successfully reflected.")
                                else:
                                    logger.error(f"Could not retrieve hub state after update for {issue_id}.")
                            processed[issue_id] = {
                                "status": "fixed" if not is_log_detected else "awaiting_prod_verification",
                                "timestamp": datetime.now().isoformat(),
                                "commit": repo_git.head.commit.hexsha
                            }
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
        time.sleep(int(os.getenv("POLL_INTERVAL_SECONDS", 300)))

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
    "DEV_BRANCH": "dev"
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
    # Ensure GITHUB_TOKEN comes from config if available
    settings["GITHUB_TOKEN"] = config.get("GITHUB_TOKEN") or settings.get("GITHUB_TOKEN", "")
    # Handle labels for the UI
    labels = config.get("monitored_labels", ["automated-fix"])
    settings["monitored_labels_str"] = ", ".join(labels)
    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "settings",
        "settings": {**settings, **config, "repo_tests_str": repo_tests_str, "monitored_labels_str": settings["monitored_labels_str"]},
        "available_labels": state.get("available_labels", [])
    })

@app.post("/save_settings")
async def save_settings(request: Request):
    form_data = await request.form()
    data = dict(form_data)

    # Handle labels logic
    labels_mode = data.get("label_mode", "SPECIFIC")
    if labels_mode == "ANY":
        labels = ["ANY"]
    elif labels_mode == "NONE":
        labels = ["NONE"]
    else:
        # Combine checkboxes and custom labels
        labels_list = data.getlist("monitored_labels")
        custom_labels_raw = data.get("custom_labels", "")
        if custom_labels_raw:
            custom_labels = [x.strip() for x in custom_labels_raw.split(",") if x.strip()]
            labels_list.extend(custom_labels)

        # Ensure we have at least one label (default to automated-fix if empty)
        if not labels_list:
            labels = ["automated-fix"]
        else:
            labels = list(set(labels_list)) # Deduplicate

    config_data = {
        "monitored_repos": [clean_repo_name(x.strip()) for x in data.get("monitored_repos", "").replace("\n", ",").split(",") if x.strip()],
        "trusted_repos": [clean_repo_name(x.strip()) for x in data.get("trusted_repos", "").replace("\n", ",").split(",") if x.strip()],
        "default_branch": data.get("default_branch", "main"),
        "direct_push_enabled": data.get("direct_push_enabled") == "on",
        "dev_branch": data.get("dev_branch", "dev"),
        "repo_tests": {},
        "GITHUB_TOKEN": data.get("GITHUB_TOKEN", ""),
        "monitored_labels": labels
    }
    repo_tests_raw = data.get("repo_tests", "")
    if repo_tests_raw:
        for pair in repo_tests_raw.split(","):
            if ":" in pair:
                repo, cmd = pair.split(":", 1)
                config_data["repo_tests"][repo.strip()] = cmd.strip()
    with open(".env", "w") as f:
        for k, v in data.items():
            if k not in config_data:
                f.write(f"{k}={v}\n")
    with open("config.json", "w") as f:
        json.dump(config_data, f, indent=2)
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/update_now")
async def update_now():
    updated, msg = check_for_updates()
    logger.info(f"Manual update check: {msg}")
    return RedirectResponse(url="/", status_code=303)

@app.post("/toggle_cloud")
async def toggle_cloud():
    state["force_cloud"] = not state["force_cloud"]
    return RedirectResponse(url="/", status_code=303)

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

threading.Thread(target=heartbeat_worker, daemon=True).start()
threading.Thread(target=poller_worker, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
