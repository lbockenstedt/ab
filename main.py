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
# Use /opt/bugfixer if it exists and is writable, otherwise use local directory
LOG_DIR = "/opt/bugfixer"
if not (os.path.exists(LOG_DIR) and os.access(LOG_DIR, os.W_OK)):
    LOG_DIR = os.getcwd()

log_file = os.path.join(LOG_DIR, "ai-fixer.log")

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
            content={"message": "Internal Server Error. Check ai-fixer.log for details.", "error": str(e)}
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
    except: return {"monitored_repos": [], "trusted_repos": [], "default_branch": "main"}

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

def apply_ai_fix(repo_path, issue_body):
    l_mod, c_mod = os.getenv("LOCAL_OLLAMA_MODEL"), os.getenv("CLOUD_OLLAMA_MODEL")
    l_url, c_url = os.getenv("LOCAL_OLLAMA_URL"), os.getenv("CLOUD_OLLAMA_URL")
    prompt = f"Issue: {issue_body}\n\nProvide the corrected code. Format exactly as:\nFILE: path/to/file\nCODE:\n\`\`\`\ncode\n\`\`\`"

    if state["force_cloud"]:
        try:
            state["active_llm"] = f"Cloud ({c_url})"
            return ollama.Client(host=c_url).chat(model=c_mod, messages=[{'role': 'user', 'content': prompt}])['message']['content']
        except Exception as e: raise Exception(f"Cloud LLM failed: {e}")

    try:
        state["active_llm"] = f"Local ({l_url})"
        return ollama.Client(host=l_url).chat(model=l_mod, messages=[{'role': 'user', 'content': prompt}])['message']['content']
    except Exception as e:
        logger.warning(f"Local LLM failed: {e}. Falling back to Cloud...")
        try:
            state["active_llm"] = f"Cloud ({c_url})"
            return ollama.Client(host=c_url).chat(model=c_mod, messages=[{'role': 'user', 'content': prompt}])['message']['content']
        except Exception as e_c: raise Exception(f"Both LLMs failed: {e_c}")

def parse_and_apply(content, repo_path):
    parts = content.split("FILE: ")
    for part in parts[1:]:
        lines = part.split("\n")
        filepath = lines[0].strip()
        try:
            code_block = part[part.find("```")+3 : part.rfind("```")]
            full_path = os.path.join(repo_path, filepath)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f: f.write(code_block.strip())
            logger.info(f"Applied fix to file: {filepath}")
        except Exception as e: logger.error(f"Error writing file {filepath}: {e}")

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
                raise Exception("GITHUB_TOKEN is missing from .env")

            gh_current = Github(token)
            try:
                bot_user = gh_current.get_user().login
            except GithubException as ge:
                if ge.status == 401:
                    raise Exception("Invalid GitHub Token (401 Unauthorized)")
                raise ge

            for repo_name in config["monitored_repos"]:
                state["current_task"] = f"Checking {repo_name}"
                repo_obj = gh_current.get_repo(repo_name)
                is_owner = repo_obj.owner.login == bot_user
                issues = repo_obj.get_issues(labels=["automated-fix"], state="open")

                for issue in issues:
                    issue_id = f"{repo_name}:{issue.number}"
                    if issue_id in processed: continue

                    state["current_task"] = f"Fixing {issue_id}"
                    logger.info(f"Processing issue {issue_id}")

                    with tempfile.TemporaryDirectory() as tmp_dir:
                        path = os.path.join(tmp_dir, "repo")
                        url = repo_obj.clone_url.replace("https://", f"https://{token}@")
                        repo_git = git.Repo.clone_from(url, path)

                        fix_code = apply_ai_fix(path, issue.body)
                        parse_and_apply(fix_code, path)

                        repo_git.git.add(A=True)
                        commit_msg = f"AI Fix #{issue.number}: {issue.title[:50]}..."
                        repo_git.index.commit(commit_msg)
                        logger.info(f"Committed changes: {commit_msg}")

                        if repo_name in config["trusted_repos"] and is_owner:
                            logger.info(f"Owner identified. Pushing directly to main for {repo_name}...")
                            repo_git.remotes.origin.push()
                            logger.info("Direct push successful.")
                        else:
                            branch = f"ai-fix-issue-{issue.number}"
                            logger.info(f"Pushing to new branch: {branch}")
                            repo_git.create_head(branch).checkout()
                            repo_git.remotes.origin.push(branch)
                            repo_obj.create_pull(title=f"AI Fix #{issue.number}", body=f"Automated fix for issue #{issue.number}", head=branch, base=config["default_branch"])
                            logger.info(f"Pull Request created for {repo_name}")

                    state["api_status"] = trigger_infrastructure_update()
                    processed.append(issue_id)
                    save_processed(processed)

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
        with open(log_file, "r") as f:
            lines = f.readlines()
            logs = "".join(lines[-100:])
    except Exception as e: logs = f"Error reading logs from {log_file}: {e}"
    return templates.TemplateResponse(request=request, name="index.html", context={"view": "logs", "logs": logs, "state": state})

DEFAULT_ENV = {
    "GITHUB_TOKEN": "",
    "LOCAL_OLLAMA_MODEL": "gemma4:31b-coding-mtp-bf16",
    "CLOUD_OLLAMA_MODEL": "gemma4:31b-cloud",
    "LOCAL_OLLAMA_URL": "http://172.16.1.100:11434",
    "CLOUD_OLLAMA_URL": "",
    "POLL_INTERVAL_SECONDS": "3600",
    "UPDATE_API_URL": ""
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
    return templates.TemplateResponse(request=request, name="index.html", context={"view": "settings", "settings": {**settings, **config}})

@app.post("/save_settings")
async def save_settings(request: Request):
    form_data = await request.form()
    data = dict(form_data)

    # Separate Config from Env
    config_data = {
        "monitored_repos": [x.strip() for x in data.get("monitored_repos", "").split(",") if x.strip()],
        "trusted_repos": [x.strip() for x in data.get("trusted_repos", "").split(",") if x.strip()],
        "default_branch": data.get("default_branch", "main")
    }

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
