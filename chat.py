"""Chat assistant: conversation store, context index, tool executors, and reply loop (extracted from main.py)."""
import json, os, re, threading, time, traceback, uuid
from datetime import datetime
from github import Github

from main import (
    CHAT_HISTORY_FILE,
    _chat_lock,
    _task_state_lock,
    call_llm,
    filter_error_logs,
    get_hub_logs,
    get_log_path,
    get_monitored_repos,
    load_config,
    load_processed,
    logger,
    resolve_self_diagnosis_repo,
    state,
    update_task_state,
)


_CHAT_INDEX_CACHE = {}            # key("gh"|"notoken") -> {"ts": float, "text": str}


_CHAT_INDEX_LOCK = threading.Lock()


def _build_chat_context_index_uncached(config, gh=None):
    lines = ["## BugFixer Context (snapshot — may be up to ~60s stale; use tools for live detail)"]
    monitored = get_monitored_repos(config)
    trusted = list(config.get("trusted_repos", []) or [])
    sd = resolve_self_diagnosis_repo(config)
    labels = config.get("monitored_labels") or ["automated-fix"]
    issue_limit = int(config.get("CHAT_INDEX_ISSUE_LIMIT", 8) or 8)

    all_repos = list(dict.fromkeys(monitored + trusted))
    lines.append("")
    lines.append("Repositories (owner/repo):")
    for repo_name in all_repos:
        tags = []
        if repo_name in trusted:
            tags.append("trusted")
        if repo_name == sd:
            tags.append("self-diagnosis")
        tagstr = f" [{', '.join(tags)}]" if tags else ""
        if gh is None:
            lines.append(f"- {repo_name}{tagstr} (open monitored issues: unknown — no token)")
            continue
        try:
            issues = gh.get_repo(repo_name).get_issues(state="open", labels=list(labels))
            titles = []
            count = 0
            for it in issues:
                count += 1
                if len(titles) < issue_limit:
                    titles.append(f"#{it.number} {_trunc(it.title, 70)}")
            more = "" if count <= issue_limit else f"  (+{count - issue_limit} more)"
            if titles:
                lines.append(f"- {repo_name}{tagstr} — {count} open: " + "; ".join(titles) + more)
            else:
                lines.append(f"- {repo_name}{tagstr} — 0 open")
        except Exception as e:
            lines.append(f"- {repo_name}{tagstr} (unavailable: {_trunc(type(e).__name__, 40)})")

    # Processed-issue status totals.
    try:
        processed = load_processed()
        counts = {}
        for info in processed.values():
            if isinstance(info, dict):
                st = info.get("status", "unknown")
                counts[st] = counts.get(st, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
        lines.append("")
        lines.append(f"Processed-issue totals (n={len(processed)}): {summary}")
    except Exception:
        lines.append("")
        lines.append("Processed-issue totals: (unavailable)")

    # Recent Hub error count (best-effort; may be None if Hub not configured).
    try:
        hub_url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
        if hub_url and "your-netbox" not in str(hub_url):
            logs = get_hub_logs()
            n = len(filter_error_logs(logs)) if logs else 0
            lines.append(f"Recent Hub errors: {n} (use get_recent_errors for detail)")
    except Exception:
        pass

    lines.append("")
    lines.append(f"Monitored labels: {', '.join(labels)}")
    lines.append("You have tools: list_repos, list_issues, get_issue, list_repo_files, "
                 "read_file, get_processed_issues, get_recent_errors, propose_fix. "
                 "Use them to answer precisely; do not guess issue/file contents.")
    text = "\n".join(lines)
    # Defense-in-depth: never let a leaked token in an issue title reach the model.
    return _redact_text(text, _secret_denylist(config))


def build_chat_context_index(config, gh=None):
    """Returns the cached chat context-index text, rebuilding if older than
    CHAT_INDEX_CACHE_TTL. Cached per token-availability key so a no-token turn's
    sparse index is not served to a later token-enabled turn within the TTL."""
    ttl = int(config.get("CHAT_INDEX_CACHE_TTL", 60) or 60)
    key = "gh" if gh is not None else "notoken"
    with _CHAT_INDEX_LOCK:
        cached = _CHAT_INDEX_CACHE.get(key)
        if cached and (time.time() - cached["ts"]) < ttl:
            return cached["text"]
        text = _build_chat_context_index_uncached(config, gh)
        _CHAT_INDEX_CACHE[key] = {"ts": time.time(), "text": text}
        return text


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


_GH_TOKEN_RE = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{80,})')


_CHAT_FILE_SKIP = (".git", "node_modules", "__pycache__", "venv", ".env")


def _secret_denylist(config):
    """Builds the set of literal secret strings to redact from tool results."""
    dl = set()
    candidates = [
        config.get("GITHUB_TOKEN"), os.getenv("GITHUB_TOKEN"),
        config.get("LLM_API_KEY_1"), os.getenv("LLM_API_KEY_1"),
        config.get("LLM_API_KEY_2"), os.getenv("LLM_API_KEY_2"),
        config.get("LLM_API_KEY_3"), os.getenv("LLM_API_KEY_3"),
        config.get("LLM_API_KEY_4"), os.getenv("LLM_API_KEY_4"),
    ]
    # Also redact vault credentials.
    for cred in (config.get("llm_credentials") or {}).values():
        k = (cred.get("api_key") or "").strip()
        if k:
            candidates.append(k)
    for src in candidates:
        if src and isinstance(src, str):
            s = src.strip().strip('"').strip("'")
            if len(s) >= 8:
                dl.add(s)
    return dl


def _redact_text(text, denylist):
    if not text:
        return text
    t = text if isinstance(text, str) else str(text)
    for s in denylist:
        if s:
            t = t.replace(s, "***REDACTED***")
    return _GH_TOKEN_RE.sub("***REDACTED***", t)


def _sanitize_tool_result(obj, config):
    """Recursively redacts configured secrets + GitHub PAT patterns from a tool
    result (dict/list/str) before it is appended to the conversation or sent to
    the browser. Defense-in-depth: the executors never put keys into results in
    the first place, but an issue body or file may contain a leaked token."""
    deny = _secret_denylist(config)
    def walk(o):
        if isinstance(o, str):
            return _redact_text(o, deny)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return walk(obj)


def _trunc(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + " …[truncated]"


CHAT_TOOLS = [
    {
        "name": "list_repos",
        "description": "List all repositories BugFixer monitors (monitored + trusted + self-diagnosis), with the count of open issues matching monitored labels for each. Use this first to learn what repos exist.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_issues",
        "description": "List open issues for a repo, optionally filtered by state/label/limit. Defaults to issues matching the configured monitored labels.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                "label": {"type": "string", "description": "single label filter; omit to use monitored labels"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "get_issue",
        "description": "Fetch one issue with its body, labels, state, and all comments. Use after list_issues to drill into a specific issue.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
            },
            "required": ["repo", "number"],
        },
    },
    {
        "name": "list_repo_files",
        "description": "List files in a repo's default branch via the git tree API (no clone). Skips .git/node_modules/__pycache__/venv/.env.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 300},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a single file's decoded contents from a repo's default branch. For large files, ask the user to narrow scope. Returns up to max_bytes.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 256, "maximum": 20000, "default": 8000},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "get_processed_issues",
        "description": "Return BugFixer's processed-issue state (statuses: fixed/verified/awaiting_prod_verification/failed/non-actionable/awaiting_review/processing). Optionally filter by repo.",
        "parameters": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_recent_errors",
        "description": "Fetch recent Hub + BugFixer self log errors. Returns deduped, capped error entries.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}},
            "required": [],
        },
    },
    {
        "name": "propose_fix",
        "description": "Propose running a full automated fix on an issue. Does NOT execute the fix. Returns a confirmation descriptor the user must approve in the UI before the fix runs. Pass llm_preference as 'cloud' or 'local', or omit for default.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "llm_preference": {"type": "string", "enum": ["cloud", "local"]},
            },
            "required": ["repo", "number"],
        },
    },
]


def _tool_list_repos(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    monitored = get_monitored_repos(config)
    trusted = config.get("trusted_repos", []) or []
    sd = resolve_self_diagnosis_repo(config)
    seen, out = set(), []
    label_filter = config.get("monitored_labels", ["automated-fix"]) or ["automated-fix"]
    for repo_name in list(dict.fromkeys(monitored + list(trusted))):
        entry = {"repo": repo_name, "is_trusted": repo_name in trusted,
                 "is_self_diagnosis": repo_name == sd, "open_monitored_issues": None}
        try:
            issues = gh.get_repo(repo_name).get_issues(state="open", labels=list(label_filter))
            count = sum(1 for _ in issues)
            entry["open_monitored_issues"] = count
        except Exception as e:
            entry["open_monitored_issues"] = f"(unavailable: {_trunc(type(e).__name__, 40)})"
        out.append(entry)
        seen.add(repo_name)
    return {"repos": out}


def _tool_list_issues(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    if not repo_name:
        return {"error": "repo is required"}
    state = args.get("state") or "open"
    limit = max(1, min(30, int(args.get("limit") or 10)))
    label = args.get("label")
    labels = [label] if label else (config.get("monitored_labels") or ["automated-fix"])
    try:
        issues = gh.get_repo(repo_name).get_issues(state=state, labels=list(labels))
        out = []
        for it in issues:
            if len(out) >= limit:
                break
            out.append({"number": it.number, "title": _trunc(it.title, 200),
                        "state": it.state, "labels": [lb.name for lb in it.labels],
                        "updated_at": str(it.updated_at)})
        return {"repo": repo_name, "state": state, "labels": labels, "issues": out}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_get_issue(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    number = args.get("number")
    if not repo_name or number is None:
        return {"error": "repo and number are required"}
    try:
        issue = gh.get_repo(repo_name).get_issue(int(number))
        comments = []
        for i, c in enumerate(issue.get_comments()):
            if i >= 20:
                comments.append({"author": "...", "body": "[more comments truncated]"})
                break
            try:
                author = c.user.login if c.user else "(unknown)"
            except Exception:
                author = "(unknown)"
            comments.append({"author": author, "body": _trunc(c.body, 1500)})
        return {"number": issue.number, "title": issue.title, "state": issue.state,
                "labels": [lb.name for lb in issue.labels],
                "body": _trunc(issue.body, 4000), "comments": comments}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_list_repo_files(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    if not repo_name:
        return {"error": "repo is required"}
    limit = max(1, min(500, int(args.get("limit") or 300)))
    try:
        repo = gh.get_repo(repo_name)
        tree = repo.get_git_tree(repo.default_branch, recursive=True)
        files = []
        for el in tree.tree:
            if getattr(el, "type", "") != "blob":
                continue
            p = el.path
            if any(seg in p for seg in _CHAT_FILE_SKIP):
                continue
            files.append(p)
            if len(files) >= limit:
                break
        return {"repo": repo_name, "branch": repo.default_branch,
                "files": files, "truncated": len(files) >= limit}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_read_file(gh, config, args):
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    repo_name = (args.get("repo") or "").strip()
    path = (args.get("path") or "").strip()
    if not repo_name or not path:
        return {"error": "repo and path are required"}
    max_bytes = max(256, min(20000, int(args.get("max_bytes") or 8000)))
    try:
        contents = gh.get_repo(repo_name).get_contents(path)
        if isinstance(contents, list):
            return {"error": f"{path} is a directory, not a file"}
        raw = contents.decoded_content or b""
        text = raw.decode("utf-8", "replace")
        truncated = len(text) > max_bytes
        return {"repo": repo_name, "path": path, "truncated": truncated,
                "content": text[:max_bytes]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_get_processed_issues(gh, config, args):
    repo_filter = (args.get("repo") or "").strip()
    processed = load_processed()
    counts = {}
    sample = []
    matched = 0
    for issue_id, info in processed.items():
        if not isinstance(info, dict):
            continue
        if repo_filter and not issue_id.startswith(repo_filter + ":"):
            continue
        matched += 1
        st = info.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
        if len(sample) < 20:
            sample.append({"issue": issue_id, "status": st,
                           "timestamp": info.get("timestamp", "")})
    return {"filter_repo": repo_filter or None, "counts": counts, "total": matched,
            "total_all": len(processed), "sample": sample}


def _tool_get_recent_errors(gh, config, args):
    limit = max(1, min(50, int(args.get("limit") or 15)))
    hub_errors = []
    logs = get_hub_logs()
    if logs:
        try:
            hub_errors = filter_error_logs(logs)[:limit]
        except Exception as e:
            hub_errors = [{"error": f"filter failed: {type(e).__name__}"}]
    # Self errors: tail the local BugFixer log and keep ERROR/Traceback lines.
    self_errors = []
    try:
        path = get_log_path()
        if path and os.path.exists(path):
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()[-500:]
            for ln in lines:
                if re.search(r'\[ERROR\]|\[CRITICAL\]|Traceback|Exception|Error[: ]', ln):
                    self_errors.append(_trunc(ln.strip(), 300))
                    if len(self_errors) >= limit:
                        break
    except Exception:
        pass
    return {"hub_errors": hub_errors, "self_errors": self_errors,
            "note": "Use get_issue/list_issues for detail on any error filed as a GitHub issue."}


def _tool_propose_fix(gh, config, args):
    repo_name = (args.get("repo") or "").strip()
    number = args.get("number")
    if not repo_name or number is None:
        return {"error": "repo and number are required"}
    pref = args.get("llm_preference")
    if pref not in ("cloud", "local", None):
        pref = None
    # Best-effort issue title for the confirmation descriptor (no mutation).
    title = ""
    if gh is not None:
        try:
            title = gh.get_repo(repo_name).get_issue(int(number)).title or ""
        except Exception:
            title = ""
    token = uuid.uuid4().hex
    descriptor = {"kind": "confirm_fix", "repo": repo_name, "number": int(number),
                  "title": _trunc(title, 200), "llm_preference": pref, "confirm_token": token}
    return descriptor


CHAT_TOOL_EXECUTORS = {
    "list_repos": _tool_list_repos,
    "list_issues": _tool_list_issues,
    "get_issue": _tool_get_issue,
    "list_repo_files": _tool_list_repo_files,
    "read_file": _tool_read_file,
    "get_processed_issues": _tool_get_processed_issues,
    "get_recent_errors": _tool_get_recent_errors,
    "propose_fix": _tool_propose_fix,
}


def _set_chat_stream_status(chat_id, text):
    """Publishes an interim status string (e.g. '[calling tool: list_issues …]')
    so /api/chat/stream shows progress during multi-turn tool resolution. Writes
    to both chat_streams (under _chat_lock) and active_tasks (under
    _task_state_lock), mirroring how chat_stream folds active_tasks in."""
    with _chat_lock:
        entry = state.setdefault("chat_streams", {}).setdefault(chat_id, {})
        entry["stream"] = text
        entry["done"] = False
        entry["error"] = None
    with _task_state_lock:
        task = state.get("active_tasks", {}).get(chat_id)
        if task is not None:
            task["stream"] = text


def _finalize_chat_stream(chat_id, text):
    with _chat_lock:
        state.setdefault("chat_streams", {})[chat_id] = {
            "stream": text or "", "done": True, "error": None,
        }


def _set_chat_stream_error(chat_id, message):
    with _chat_lock:
        state.setdefault("chat_streams", {})[chat_id] = {
            "stream": "", "done": True, "error": message,
        }


def _register_fix_proposal(chat_id, descriptor, config):
    """Stores a fix-proposal confirmation token server-side (under _chat_lock)
    with a creation timestamp so /api/chat/confirm_fix can validate + TTL it."""
    token = descriptor.get("confirm_token")
    if not token:
        return
    ttl = int(config.get("CHAT_FIX_PROPOSAL_TTL", 600) or 600)
    with _chat_lock:
        state.setdefault("chat_fix_proposals", {})[token] = {
            "repo": descriptor.get("repo"),
            "number": descriptor.get("number"),
            "llm_preference": descriptor.get("llm_preference"),
            "chat_id": chat_id,
            "created": time.time(),
            "ttl": ttl,
        }


def _confirm_fix_marker(descriptor):
    """Renders the propose_fix descriptor as a fenced block the chat UI parses
    into a Confirm button. The confirm_token is a server-generated uuid (not a
    secret); the descriptor has already been through _sanitize_tool_result."""
    pref = descriptor.get("llm_preference") or ""
    return (
        f":::confirm_fix repo={descriptor.get('repo')} number={descriptor.get('number')} "
        f"token={descriptor.get('confirm_token')} pref={pref}\n"
        f"Run automated fix on #{descriptor.get('number')} "
        f"\"{descriptor.get('title', '')}\"? Click Confirm to proceed.\n:::"
    )


_TOOLCALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _parse_text_tool_calls(text):
    """Some models (esp. via ollama) emit tool calls as ``<tool_call>{...}</tool_call>``
    TEXT instead of the structured tool_calls field. Extract any such blocks so the
    agent loop can execute them, and return (cleaned_text, tool_calls) with the tags
    stripped so raw ``tool_calls`` JSON never leaks into the visible answer."""
    if not text or "<tool_call>" not in text:
        return text, []
    calls = []
    for m in _TOOLCALL_TAG_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        fn = obj.get("function") if isinstance(obj.get("function"), dict) else {}
        name = obj.get("name") or fn.get("name")
        args = obj.get("arguments") or fn.get("arguments") or obj.get("parameters") or {}
        if name:
            calls.append({"function": {"name": name, "arguments": args}})
    cleaned = _TOOLCALL_TAG_RE.sub("", text).strip()
    return cleaned, calls


def _run_chat_reply_simple(chat_id, config):
    """Legacy single-turn chat path: used when CHAT_TOOLS_ENABLED is False (or as
    the graceful-degradation fallback). Streams one call_llm reply with a plain
    system prompt. Preserves the pre-tool chat behavior."""
    try:
        window_size = int(config.get("CHAT_HISTORY_WINDOW", 20) or 20)
        system_prompt = config.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return
        messages = conv.get("messages", [])
        window = [{"role": "system", "content": system_prompt}] + messages[-window_size:]
        from model_selection import LlmRequirements
        reqs = LlmRequirements(complexity="small", latency_sensitive=True, pin_key=(config.get("chat_pin") or None))
        reply = call_llm("", messages=window, task_id=chat_id, requirements=reqs)
        if reply and reply.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": reply,
                "ts": datetime.now().isoformat(),
            })
        _finalize_chat_stream(chat_id, reply or "")
    except Exception as e:
        logger.error(f"_run_chat_reply_simple failed for {chat_id}: {e}\n{traceback.format_exc()}")
        _set_chat_stream_error(chat_id, f"LLM error: {e}")


def _run_chat_reply_orchestrated(chat_id, config):
    """Multi-agent chat path (ORCHESTRATOR_ENABLED): decompose the latest user
    message into a sub-task DAG, run the independent parts CONCURRENTLY each on
    the best available LLM, then merge. Interim status names which model is
    handling each part so /api/chat/stream shows the fan-out live. Returns True
    on success; returns False (without finalizing) to signal the caller should
    fall back to the normal path."""
    try:
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return True
        history = conv.get("messages", [])
        user_msgs = [m for m in history if m.get("role") == "user"]
        request_text = (user_msgs[-1].get("content") if user_msgs else "") or ""
        if not request_text.strip():
            _finalize_chat_stream(chat_id, "")
            return True

        import agent_orchestrator
        progress = {"parts": []}

        def _progress(ev):
            kind = ev.get("event")
            if kind == "planned":
                n = ev.get("task_count", 1)
                _set_chat_stream_status(
                    chat_id,
                    f"🧠 Planning — split into {n} parallel agents…" if n > 1 else "Thinking…",
                )
            elif kind == "agent_done":
                progress["parts"].append(
                    f"• {ev.get('task_id')} → {ev.get('model') or '?'}"
                    + ("" if ev.get("ok") else " (failed)")
                )
                _set_chat_stream_status(chat_id, "Agents working…\n" + "\n".join(progress["parts"]))
            elif kind == "merged":
                _set_chat_stream_status(chat_id, "Merging results…")

        result = agent_orchestrator.orchestrate(
            request_text, config, progress_cb=_progress, task_id=chat_id,
        )
        final = result.final_text or ""
        # Only annotate when the request actually fanned out, so single-agent
        # answers read exactly like a normal chat reply.
        if result.planned and result.results:
            roster = ", ".join(f"{r.task_id}→{r.model_label or '?'}" for r in result.results)
            final = f"{final}\n\n---\n🧠 *Handled by {len(result.results)} agents: {roster}*"
        if final.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": final,
                "ts": datetime.now().isoformat(),
            })
        _finalize_chat_stream(chat_id, final)
        return True
    except Exception as e:
        # Never dead-end chat: log and let the caller run the normal path.
        logger.error(f"_run_chat_reply_orchestrated failed for {chat_id}: {e}\n{traceback.format_exc()}")
        return False


def run_agent_loop(messages, config, gh, *, task_id, max_iter=6,
                   max_result_chars=48000, status_cb=None, on_fix_proposal=None):
    """Core CHAT_TOOLS agent loop, decoupled from the chat store / SSE stream so
    ANY front-end can reuse the exact same agentic pipeline and tools — the
    dashboard chat (run_chat_reply) and the Anthropic-compatible LLM-router
    proxy (llm_proxy's agentic mode) both call this.

    `messages` MUST already include the system prompt as messages[0]; it is
    mutated in place as the loop appends assistant/tool turns. Returns the final
    assistant text (always a string).

    status_cb(str): optional progress reporter. The dashboard writes it to the
        chat stream; the proxy logs it. Never None-crashes (defaulted below).
    on_fix_proposal(descriptor, text) -> str: optional handler invoked when the
        model calls propose_fix (which is itself non-mutating — it only returns
        a confirm_fix descriptor). The dashboard registers a confirm token and
        returns a :::confirm_fix marker; the router triggers the real fix run.
        When None, the raw descriptor is surfaced as text. Either way the loop
        stops after a proposal. ALL mutation lives in this callback, never here.
    """
    from model_selection import LlmRequirements
    _status = status_cb or (lambda *_a, **_k: None)
    tools = CHAT_TOOLS
    used_chars = 0
    final_text = None
    last_text = ""
    for iteration in range(max_iter):
        _status("Thinking…" if iteration == 0 else "Working…")
        try:
            _tool_reqs = LlmRequirements(complexity="medium", needs_tools=True, latency_sensitive=True)
            result = call_llm("", messages=messages, task_id=task_id, tools=tools, stream=False, requirements=_tool_reqs)
        except Exception as e:
            # Tool-capable turn failed (e.g. cloud without tool support). Degrade
            # to one index-only turn and finish.
            logger.warning(f"Agent tool turn {iteration} failed ({e}); degrading to index-only answer.")
            _fallback_reqs = LlmRequirements(complexity="small", latency_sensitive=True, pin_key=(config.get("chat_pin") or None))
            reply = call_llm("", messages=messages[:], task_id=task_id, requirements=_fallback_reqs)
            final_text = reply or ""
            break

        if not isinstance(result, dict):
            final_text = str(result)
            break
        text = result.get("text") or ""
        tool_calls = result.get("tool_calls") or []
        # Fallback: model emitted tool calls as <tool_call>{…}</tool_call> text
        # rather than structured tool_calls — parse + execute them, and strip the
        # tags so the raw JSON never shows in the answer.
        if not tool_calls:
            text, tool_calls = _parse_text_tool_calls(text)
        last_text = text
        if not tool_calls:
            final_text = text
            break

        # Echo the assistant turn (with tool_calls) back for the next round.
        messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})

        hit_proposal = False
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            args_raw = fn.get("arguments") if fn else tc.get("arguments")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw) if args_raw else {}
                except Exception:
                    args = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}

            if not name or name not in CHAT_TOOL_EXECUTORS:
                out = {"error": f"unknown tool: {name}"}
            else:
                _status(f"[calling tool: {name} …]")
                try:
                    out = CHAT_TOOL_EXECUTORS[name](gh, config, args)
                except Exception as ee:
                    out = {"error": f"{type(ee).__name__}: {_trunc(ee, 300)}"}
            out = _sanitize_tool_result(out, config)
            out_str = json.dumps(out)
            if used_chars + len(out_str) > max_result_chars:
                out_str = json.dumps({"error": "tool result budget exceeded; narrow your query", "truncated": True})
            used_chars += len(out_str)
            tool_call_id = tc.get("id") or f"call_{name}_{iteration}"
            messages.append({"role": "tool", "name": name or "unknown", "content": out_str, "tool_call_id": tool_call_id})

            # propose_fix is non-mutating: hand the descriptor to the front-end's
            # handler (confirm button, or a real fix trigger) and stop.
            if name == "propose_fix" and isinstance(out, dict) and out.get("kind") == "confirm_fix":
                if on_fix_proposal is not None:
                    final_text = on_fix_proposal(out, text)
                else:
                    final_text = (text + "\n\n" + json.dumps(out)).strip() if text else json.dumps(out)
                hit_proposal = True
                break
        if hit_proposal:
            break
    else:
        # Iteration cap reached without a no-tool_calls turn. If the model kept
        # calling tools and never wrote a final answer, last_text can be empty —
        # returning it would hand the caller a blank reply. Force ONE final
        # tools-free turn so the gathered context is turned into a written
        # answer. Best-effort: on failure, fall back to whatever text we have.
        final_text = last_text or ""
        if not (final_text and final_text.strip()):
            try:
                _status("Summarizing…")
                messages.append({"role": "system", "content": "You have gathered enough information from the tools. Do NOT call any more tools. Write your final answer to the user now, in plain text."})
                _final_reqs = LlmRequirements(complexity="medium", latency_sensitive=True)
                forced = call_llm("", messages=messages, task_id=task_id, requirements=_final_reqs)
                if isinstance(forced, dict):
                    forced = forced.get("text") or ""
                if forced and forced.strip():
                    final_text = forced
            except Exception as e:
                logger.warning(f"Agent final-answer turn failed ({e}); returning best-effort text.")

    if final_text is None:
        final_text = last_text or ""
    # Defense in depth: strip any residual <tool_call> tags before returning.
    if final_text and "<tool_call>" in final_text:
        final_text, _ = _parse_text_tool_calls(final_text)
    return final_text


def run_chat_reply(chat_id):
    """Background worker that produces an LLM reply for one conversation turn.

    With ORCHESTRATOR_ENABLED, the turn is first attempted as a multi-agent
    orchestration (_run_chat_reply_orchestrated); on any failure it falls
    through to the normal path below. With CHAT_TOOLS_ENABLED (default), runs
    an agent loop: the system prompt carries a compact repo/issue index
    (build_chat_context_index) and the model may call read-only tools
    (CHAT_TOOLS) to drill in. propose_fix does not mutate; it emits a
    :::confirm_fix block the UI renders as a Confirm button, and the real fix
    run only happens via /api/chat/confirm_fix after the user clicks. Without a
    GitHub token, tools are disabled but the index still gives the assistant
    repo/issue awareness. Tool turns are non-streaming so message.tool_calls
    parse cleanly; interim status is written to state["chat_streams"][chat_id]
    / active_tasks so /api/chat/stream shows progress. Completion/error is
    tracked in state["chat_streams"][chat_id].
    """
    try:
        config = load_config()
        if config.get("ORCHESTRATOR_ENABLED", False):
            if _run_chat_reply_orchestrated(chat_id, config):
                return
            logger.warning("orchestrated chat turn failed; falling back to the standard path")
        if not config.get("CHAT_TOOLS_ENABLED", True):
            return _run_chat_reply_simple(chat_id, config)

        window_size = int(config.get("CHAT_HISTORY_WINDOW", 20) or 20)
        base_system = config.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
        max_iter = int(config.get("CHAT_TOOL_MAX_ITERATIONS", 6) or 6)
        # Token-budget config is in ~tokens; apply a 4x char budget for results.
        max_result_chars = int(config.get("CHAT_TOOL_MAX_TOKENS", 12000) or 12000) * 4

        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return

        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        gh = Github(token) if token else None

        # Index gives awareness even without tools; gh passed so issue titles fill in.
        index_text = build_chat_context_index(config, gh=gh)
        system_prompt = base_system + "\n\n" + index_text
        history = conv.get("messages", [])
        window = history[-window_size:]
        messages = [{"role": "system", "content": system_prompt}] + list(window)

        # No GitHub token -> no tools, but the index still informs the answer.
        if gh is None:
            _set_chat_stream_status(chat_id, "Thinking…")
            from model_selection import LlmRequirements
            reqs = LlmRequirements(complexity="small", latency_sensitive=True, pin_key=(config.get("chat_pin") or None))
            reply = call_llm("", messages=messages, task_id=chat_id, requirements=reqs)  # streaming string
            if reply and reply.strip():
                append_chat_message(chat_id, {
                    "role": "assistant",
                    "content": reply,
                    "ts": datetime.now().isoformat(),
                })
            _finalize_chat_stream(chat_id, reply or "")
            return

        def _chat_on_fix_proposal(descriptor, text):
            # Dashboard front-end: register a single-use confirm token and render
            # a Confirm button (:::confirm_fix marker). Non-mutating — the real
            # fix only runs when the user POSTs /api/chat/confirm_fix.
            _register_fix_proposal(chat_id, descriptor, config)
            marker = _confirm_fix_marker(descriptor)
            return (text + "\n\n" + marker).strip() if text else marker

        final_text = run_agent_loop(
            messages, config, gh,
            task_id=chat_id, max_iter=max_iter, max_result_chars=max_result_chars,
            status_cb=lambda s: _set_chat_stream_status(chat_id, s),
            on_fix_proposal=_chat_on_fix_proposal,
        )

        if final_text is None:
            final_text = ""
        # Defense in depth: strip any residual <tool_call> tags before display.
        if final_text and "<tool_call>" in final_text:
            final_text, _ = _parse_text_tool_calls(final_text)
        if final_text and final_text.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": final_text,
                "ts": datetime.now().isoformat(),
            })
        _finalize_chat_stream(chat_id, final_text or "")
    except Exception as e:
        logger.error(f"run_chat_reply failed for {chat_id}: {e}\n{traceback.format_exc()}")
        _set_chat_stream_error(chat_id, f"LLM error: {e}")
    finally:
        # Remove the chat task from the Dashboard activity feed.
        update_task_state(chat_id, "Chat", action="end")


__all__ = [
    '_empty_chats_store',
    '_title_from_message',
    'load_chats',
    'save_chats',
    'get_conversation',
    'append_chat_message',
    'create_conversation',
    'rename_conversation',
    'delete_conversation',
    'set_active_chat',
    'build_chat_context_index',
    '_build_chat_context_index_uncached',
    '_secret_denylist',
    '_redact_text',
    '_sanitize_tool_result',
    '_trunc',
    '_tool_list_repos',
    '_tool_list_issues',
    '_tool_get_issue',
    '_tool_list_repo_files',
    '_tool_read_file',
    '_tool_get_processed_issues',
    '_tool_get_recent_errors',
    '_tool_propose_fix',
    '_set_chat_stream_status',
    '_finalize_chat_stream',
    '_set_chat_stream_error',
    '_register_fix_proposal',
    '_confirm_fix_marker',
    '_run_chat_reply_simple',
    'run_agent_loop',
    'run_chat_reply',
    '_CHAT_INDEX_CACHE',
    '_CHAT_INDEX_LOCK',
    '_GH_TOKEN_RE',
    '_CHAT_FILE_SKIP',
    'CHAT_TOOLS',
    'CHAT_TOOL_EXECUTORS',
]
