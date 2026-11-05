"""Chat assistant: conversation store, context index, tool executors, and reply loop (extracted from main.py)."""
# json: chat store + tool args; os: env/paths; re: text matching; threading: locks;
# time: cache ages; traceback: error detail; uuid: ids; datetime: message timestamps.
import json, os, re, threading, time, traceback, uuid
from datetime import datetime
# PyGithub client used by the GitHub-reading chat tools.
from github import Github

# Azure helpers behind the azure_cli / server_shell chat tools.
import az_console

# Shared state, locks, config, logging and LLM/log helpers re-exported by main.
from main import (
    CHAT_HISTORY_FILE,
    _atomic_write_json,
    _chat_lock,
    _task_state_lock,
    call_llm,
    filter_error_logs,
    get_hub_logs,
    CHAT_HUB_LOG_LINES_PER_MODULE,
    get_log_path,
    get_monitored_repos,
    load_config,
    load_processed,
    logger,
    resolve_self_diagnosis_repo,
    state,
    update_task_state,
)


# Cached context-index text, one entry per "has GitHub token or not".
_CHAT_INDEX_CACHE = {}            # key("gh"|"notoken") -> {"ts": float, "text": str}


# Guards the context-index cache against concurrent rebuilds.
_CHAT_INDEX_LOCK = threading.Lock()


# Appended to the grounded chat system prompt. AppBuilder's chat manages real
# production systems, so a confident hallucination (a fabricated "security scan"
# naming repos that don't exist, or invented results) is actively harmful. This
# forces the model to stay grounded in what the tools actually returned.
_CHAT_GROUNDING_RULE = (
    "GROUNDING RULES (mandatory):\n"
    "- You are AppBuilder (AB), an autonomous agent that manages a specific set "
    "of real repositories and servers. The ONLY repositories you manage are "
    "those returned by the list_repos tool — never reference placeholder or "
    "example repos (e.g. octocat/Hello-World, exampleUser/ProjectX).\n"
    "- State only facts you obtained from a tool call in THIS conversation. Never "
    "invent repository names, file paths, file contents, commands, tool output, "
    "or scan/audit results.\n"
    "- To do real work (scan code, read a file, inspect an issue, diagnose a "
    "server), you MUST call the appropriate tool and base your answer on its "
    "actual result. If a needed tool or capability is unavailable, say so plainly "
    "instead of describing hypothetical results.\n"
    "- If you could not complete something, report what you actually did and what "
    "remains — do not fill gaps with plausible-sounding fabrication."
)


def _build_chat_context_index_uncached(config, gh=None):
    """Build the chat's "what AppBuilder manages" context text from scratch (no cache)."""
    # Lines of the context block, starting with a heading that warns it may be stale.
    lines = ["## AppBuilder Context (snapshot — may be up to ~60s stale; use tools for live detail)"]
    # Repos to list, which are trusted, the self-diagnosis repo, issue labels and the per-repo
    # issue title limit, all from config.
    monitored = get_monitored_repos(config)
    trusted = list(config.get("trusted_repos", []) or [])
    sd = resolve_self_diagnosis_repo(config)
    labels = config.get("monitored_labels") or ["automated-fix"]
    issue_limit = int(config.get("CHAT_INDEX_ISSUE_LIMIT", 8) or 8)

    # Monitored + trusted repos, de-duplicated while keeping order.
    all_repos = list(dict.fromkeys(monitored + trusted))
    lines.append("")
    lines.append("Repositories (owner/repo):")
    # One line per repo with its tags and open monitored issues.
    for repo_name in all_repos:
        # Tag repos that are trusted or are AppBuilder's own self-diagnosis repo.
        tags = []
        if repo_name in trusted:
            tags.append("trusted")
        if repo_name == sd:
            tags.append("self-diagnosis")
        tagstr = f" [{', '.join(tags)}]" if tags else ""
        # Without a GitHub client we cannot count issues.
        if gh is None:
            lines.append(f"- {repo_name}{tagstr} (open monitored issues: unknown — no token)")
            continue
        # List open issues with the monitored labels; any API error is shown inline instead.
        try:
            issues = gh.get_repo(repo_name).get_issues(state="open", labels=list(labels))
            titles = []
            count = 0
            # Count every issue but only list the first issue_limit titles.
            for it in issues:
                count += 1
                if len(titles) < issue_limit:
                    titles.append(f"#{it.number} {_trunc(it.title, 70)}")
            # Note how many were left out.
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
        # Tally processed issues by status.
        counts = {}
        for info in processed.values():
            if isinstance(info, dict):
                st = info.get("status", "unknown")
                counts[st] = counts.get(st, 0) + 1
        # Render as "status=count" pairs, sorted.
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
        lines.append("")
        lines.append(f"Processed-issue totals (n={len(processed)}): {summary}")
    # A broken processed store just shows as unavailable.
    except Exception:
        lines.append("")
        lines.append("Processed-issue totals: (unavailable)")

    # Recent Hub error count (best-effort; may be None if Hub not configured).
    try:
        # Only query the Hub when a real (non-placeholder) URL is configured.
        hub_url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
        if hub_url and "your-netbox" not in str(hub_url):
            logs = get_hub_logs(CHAT_HUB_LOG_LINES_PER_MODULE)
            n = len(filter_error_logs(logs)) if logs else 0
            lines.append(f"Recent Hub errors: {n} (use get_recent_errors for detail)")
    # The Hub is optional; skip this line on any error.
    except Exception:
        pass

    # List the monitored labels and the tools the model can call.
    lines.append("")
    lines.append(f"Monitored labels: {', '.join(labels)}")
    lines.append("You have tools: list_repos, list_issues, get_issue, list_repo_files, "
                 "read_file, get_processed_issues, get_recent_errors, propose_fix. "
                 "Use them to answer precisely; do not guess issue/file contents.")
    # When Azure tools are enabled, describe them and whether they may change things.
    if config.get("CHAT_AZURE_ENABLED", False):
        rg = config.get("AZURE_LM_RESOURCE_GROUP") or "LM"
        mut = "mutation ENABLED" if config.get("CHAT_AZURE_ALLOW_MUTATION", False) else "read-only"
        lines.append(f"Azure fleet tools are ON ({mut}) for resource group {rg}: "
                     "azure_cli (control-plane, e.g. `vm list`/`vm get-instance-view`) and "
                     "server_shell (run a command ON a VM, e.g. `systemctl status ab`, "
                     "`journalctl -u ab -n 100 --no-pager`, `curl -s localhost/health`) — "
                     "use these to diagnose the live LM servers and verify a change deployed "
                     "and is functional with no new errors.")
    # Join everything into one text block.
    text = "\n".join(lines)
    # Defense-in-depth: never let a leaked token in an issue title reach the model.
    return _redact_text(text, _secret_denylist(config))


def build_chat_context_index(config, gh=None):
    """Returns the cached chat context-index text, rebuilding if older than
    CHAT_INDEX_CACHE_TTL. Cached per token-availability key so a no-token turn's
    sparse index is not served to a later token-enabled turn within the TTL."""
    # Cache lifetime in seconds, and which cache slot (token or no token) applies.
    ttl = int(config.get("CHAT_INDEX_CACHE_TTL", 60) or 60)
    key = "gh" if gh is not None else "notoken"
    # Serve from cache when fresh; otherwise rebuild and store under the lock.
    with _CHAT_INDEX_LOCK:
        cached = _CHAT_INDEX_CACHE.get(key)
        if cached and (time.time() - cached["ts"]) < ttl:
            return cached["text"]
        text = _build_chat_context_index_uncached(config, gh)
        _CHAT_INDEX_CACHE[key] = {"ts": time.time(), "text": text}
        return text


def _empty_chats_store():
    """Returns a fresh multi-conversation store with one untitled, active chat."""
    # One empty conversation with id "c1", which is also the active one.
    conv = {"id": "c1", "title": "", "created": datetime.now().isoformat(), "messages": []}
    return {"next_id": 2, "active_id": "c1", "conversations": [conv]}


def _title_from_message(content):
    """Derives a short conversation title from the first user message."""
    # First line of the message (empty if none), capped at 60 characters.
    title = (content or "").strip().splitlines()[0] if content else ""
    return title[:60]


def load_chats():
    """Returns the persisted multi-conversation store.

    Schema: {"next_id": int, "active_id": str|None, "conversations": [
        {"id": str, "title": str, "created": str, "messages": [{role,content,ts}]}
    ]}. Transparently migrates a legacy flat message list (pre-V.48 single-thread
    chat_history.json) into the first conversation so no history is lost.
    """
    # Hold the chat lock so we never read a half-written file.
    with _chat_lock:
        # Read the store; a missing or corrupt file starts a fresh one.
        try:
            with open(CHAT_HISTORY_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            return _empty_chats_store()

        # Old format: a plain list of messages.
        if isinstance(data, list):
            # Legacy flat single-thread history -> wrap into one conversation.
            first_user = next((m.get("content", "") for m in data
                               if isinstance(m, dict) and m.get("role") == "user"), "")
            # Wrap the old messages into a single conversation titled from the first user message.
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
            # Save in the new format right away so migration happens only once.
            save_chats(store)
            return store

        # Anything else that is not the expected shape starts fresh.
        if not isinstance(data, dict) or not isinstance(data.get("conversations"), list):
            return _empty_chats_store()

        # Keep only well-formed conversations.
        store = {
            "next_id": int(data.get("next_id", 1) or 1),
            "active_id": data.get("active_id"),
            "conversations": [c for c in data["conversations"] if isinstance(c, dict)],
        }
        # No conversations left: start fresh.
        if not store["conversations"]:
            return _empty_chats_store()
        # If the active chat is missing or gone, make the newest one active.
        if not store["active_id"] or not get_conversation(store, store["active_id"]):
            store["active_id"] = store["conversations"][-1]["id"]
        return store


def save_chats(store):
    """Persists the whole multi-conversation store under _chat_lock."""
    # Write atomically under the chat lock; log but do not raise on failure.
    with _chat_lock:
        try:
            _atomic_write_json(CHAT_HISTORY_FILE, store)
        except Exception as e:
            logger.error(f"Could not save chats to {CHAT_HISTORY_FILE}: {e}")


def get_conversation(store, chat_id):
    """Returns the conversation dict for chat_id, or None if not found."""
    # Linear search; there are only a handful of conversations.
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
    # Load, modify and save under one lock so concurrent appends cannot lose messages.
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, chat_id)
        # Unknown conversation.
        if conv is None:
            return None
        # Add the message and auto-title an untitled chat from its first user message.
        conv["messages"].append(msg)
        if msg.get("role") == "user" and not conv.get("title"):
            conv["title"] = _title_from_message(msg.get("content", ""))
        # Make this the active chat and persist.
        store["active_id"] = chat_id
        save_chats(store)
        return msg


def create_conversation():
    """Creates a new empty conversation, makes it active, and persists. Returns its id."""
    # Allocate the next id ("c<N>") under the lock.
    with _chat_lock:
        store = load_chats()
        cid = f"c{store['next_id']}"
        store["next_id"] += 1
        # Add an empty conversation and make it active.
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
    # Load the store and find the conversation.
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            return False
        # Store a trimmed title capped at 120 characters.
        conv["title"] = (title or "").strip()[:120]
        save_chats(store)
        return True


def delete_conversation(chat_id):
    """Deletes a conversation and selects a new active one. Returns the new active id."""
    with _chat_lock:
        # Remove the conversation by id.
        store = load_chats()
        store["conversations"] = [c for c in store["conversations"] if c.get("id") != chat_id]
        # If none remain, start over with one empty chat; otherwise activate the newest.
        if not store["conversations"]:
            store = _empty_chats_store()
        else:
            store["active_id"] = store["conversations"][-1]["id"]
        save_chats(store)
        return store["active_id"]


def set_active_chat(chat_id):
    """Sets the active conversation if chat_id exists. Returns True on success."""
    # Only switch to a conversation that exists.
    with _chat_lock:
        store = load_chats()
        if not get_conversation(store, chat_id):
            return False
        store["active_id"] = chat_id
        save_chats(store)
        return True


# Matches GitHub personal access tokens (classic ghp_/gho_/... and fine-grained github_pat_).
_GH_TOKEN_RE = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{80,})')


# Directory names never listed by the list_repo_files tool (noise or secrets).
_CHAT_FILE_SKIP = (".git", "node_modules", "__pycache__", "venv", ".env")


def _secret_denylist(config):
    """Builds the set of literal secret strings to redact from tool results."""
    # Set of secret strings to scrub.
    dl = set()
    # GitHub token and the four provider API keys, from both config and environment.
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
    # Keep each non-empty candidate, minus quotes; very short strings are skipped because
    # replacing them would mangle ordinary text.
    for src in candidates:
        if src and isinstance(src, str):
            s = src.strip().strip('"').strip("'")
            if len(s) >= 8:
                dl.add(s)
    return dl


def _redact_text(text, denylist):
    """Replace every known secret and any GitHub token pattern in text with ***REDACTED***."""
    # Empty values pass through unchanged.
    if not text:
        return text
    # Work on a string copy.
    t = text if isinstance(text, str) else str(text)
    # Replace each exact secret.
    for s in denylist:
        if s:
            t = t.replace(s, "***REDACTED***")
    # Then catch any GitHub token pattern we did not know about.
    return _GH_TOKEN_RE.sub("***REDACTED***", t)


def _sanitize_tool_result(obj, config):
    """Recursively redacts configured secrets + GitHub PAT patterns from a tool
    result (dict/list/str) before it is appended to the conversation or sent to
    the browser. Defense-in-depth: the executors never put keys into results in
    the first place, but an issue body or file may contain a leaked token."""
    # Build the secret list once for the whole walk.
    deny = _secret_denylist(config)
    def walk(o):
        """Return a copy of o with every string inside it redacted."""
        # Strings get redacted; dicts and lists are walked recursively; anything else is kept.
        if isinstance(o, str):
            return _redact_text(o, deny)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    # Redact the whole result.
    return walk(obj)


def _trunc(s, n):
    """Return s as a string cut to n characters, with a marker when it was cut."""
    # None becomes an empty string.
    if s is None:
        return ""
    # Convert to text and truncate if too long.
    s = str(s)
    return s if len(s) <= n else s[:n] + " …[truncated]"


# Tool definitions (name, description, JSON-schema parameters) offered to the chat LLM.
# Each name maps to an executor function further down.
CHAT_TOOLS = [
    {
        "name": "list_repos",
        "description": "List all repositories AppBuilder monitors (monitored + trusted + self-diagnosis), with the count of open issues matching monitored labels for each. Use this first to learn what repos exist.",
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
        "description": "Read a file's decoded contents from a repo's default branch. Returns up to max_bytes STARTING AT byte `offset`, plus total_bytes and next_offset — so you can PAGE THROUGH a large file by re-calling with offset=next_offset while has_more is true. To jump straight to a region, pass `pattern` (a regex): the window is centered on the first match and match_lines lists where it matched. Prefer `pattern` for big files instead of paging from the start.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 256, "maximum": 20000, "default": 8000},
                "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "Byte offset to start reading from (use next_offset from a prior call to page)."},
                "pattern": {"type": "string", "description": "Optional regex; if given, the returned window is centered on the first match so you don't have to page to find it."},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "get_processed_issues",
        "description": "Return AppBuilder's processed-issue state (statuses: fixed/verified/awaiting_prod_verification/failed/non-actionable/awaiting_review/processing). Optionally filter by repo.",
        "parameters": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_recent_errors",
        "description": "Fetch recent Hub + AppBuilder self log errors. Returns deduped, capped error entries.",
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


# Extra tools offered only when Azure access is enabled in config.
AZURE_TOOLS = [
    {
        "name": "azure_cli",
        "description": "Run a read-only `az` (Azure CLI) command against the LM resource group to diagnose the live servers AppBuilder manages (e.g. list VMs, show a VM's power/agent state, list resources). Pass the arguments AFTER `az` as an array in `args` (argv form, e.g. [\"vm\",\"list\",\"-g\",\"LM\",\"-o\",\"table\"]). Read-only by default; mutating commands are refused unless the operator has enabled Azure mutation.",
        "parameters": {
            "type": "object",
            "properties": {
                "args": {"type": "array", "items": {"type": "string"},
                          "description": "az arguments AFTER 'az', argv form (no leading 'az')."},
            },
            "required": ["args"],
        },
    },
    {
        "name": "server_shell",
        "description": "Run a shell command ON one LM server VM (via Azure run-command) to verify functionality and check for errors — e.g. `systemctl status ab`, `journalctl -u ab -n 100 --no-pager`, `curl -s localhost/health`. Read-only by default (status/logs/health only); state-changing commands are refused unless the operator has enabled Azure mutation. Use azure_cli first to see valid VM names (e.g. LM-HUB, LM-AB, LM-NETBOX).",
        "parameters": {
            "type": "object",
            "properties": {
                "vm": {"type": "string", "description": "LM VM name, e.g. LM-HUB."},
                "command": {"type": "string", "description": "The shell command to run on that VM."},
            },
            "required": ["vm", "command"],
        },
    },
]


def _tool_list_repos(gh, config, args):
    """Chat tool: list every managed repo with its trust flags and open monitored-issue count."""
    # All GitHub tools need a token-backed client.
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    # Monitored repos, trusted repos, the self-diagnosis repo and the labels to count.
    monitored = get_monitored_repos(config)
    trusted = config.get("trusted_repos", []) or []
    sd = resolve_self_diagnosis_repo(config)
    seen, out = set(), []
    label_filter = config.get("monitored_labels", ["automated-fix"]) or ["automated-fix"]
    # One entry per distinct repo (monitored first, then trusted).
    for repo_name in list(dict.fromkeys(monitored + list(trusted))):
        entry = {"repo": repo_name, "is_trusted": repo_name in trusted,
                 "is_self_diagnosis": repo_name == sd, "open_monitored_issues": None}
        # Count open monitored issues; an API error is reported in place of the count.
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
    """Chat tool: list issues in one repo, filtered by state and label, up to a limit."""
    # All GitHub tools need a token-backed client.
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    # The repo argument is required.
    repo_name = (args.get("repo") or "").strip()
    if not repo_name:
        return {"error": "repo is required"}
    # State defaults to open; limit is clamped to 1..30.
    state = args.get("state") or "open"
    limit = max(1, min(30, int(args.get("limit") or 10)))
    # Use the requested label, or fall back to the monitored labels.
    label = args.get("label")
    labels = [label] if label else (config.get("monitored_labels") or ["automated-fix"])
    # Fetch issues and keep a compact summary of each; API errors become an error result.
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
    """Chat tool: fetch one issue's body, labels, state and up to 20 comments."""
    # All GitHub tools need a token-backed client.
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    # Both repo and issue number are required.
    repo_name = (args.get("repo") or "").strip()
    number = args.get("number")
    if not repo_name or number is None:
        return {"error": "repo and number are required"}
    # Fetch the issue; API errors become an error result.
    try:
        issue = gh.get_repo(repo_name).get_issue(int(number))
        comments = []
        # Collect comments, stopping after 20 to keep the reply small.
        for i, c in enumerate(issue.get_comments()):
            if i >= 20:
                comments.append({"author": "...", "body": "[more comments truncated]"})
                break
            # Comment author login, tolerating deleted users.
            try:
                author = c.user.login if c.user else "(unknown)"
            except Exception:
                author = "(unknown)"
            comments.append({"author": author, "body": _trunc(c.body, 1500)})
        # Return the issue with its body and comments truncated to sensible sizes.
        return {"number": issue.number, "title": issue.title, "state": issue.state,
                "labels": [lb.name for lb in issue.labels],
                "body": _trunc(issue.body, 4000), "comments": comments}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_list_repo_files(gh, config, args):
    """Chat tool: list file paths on a repo's default branch without cloning it."""
    # All GitHub tools need a token-backed client.
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    # The repo argument is required; limit is clamped to 1..500.
    repo_name = (args.get("repo") or "").strip()
    if not repo_name:
        return {"error": "repo is required"}
    limit = max(1, min(500, int(args.get("limit") or 300)))
    # Read the full git tree of the default branch; API errors become an error result.
    try:
        repo = gh.get_repo(repo_name)
        tree = repo.get_git_tree(repo.default_branch, recursive=True)
        files = []
        # Keep only files (blobs), skip noise/secret directories, stop at the limit.
        for el in tree.tree:
            if getattr(el, "type", "") != "blob":
                continue
            p = el.path
            if any(seg in p for seg in _CHAT_FILE_SKIP):
                continue
            files.append(p)
            if len(files) >= limit:
                break
        # Return the list and whether it was cut short.
        return {"repo": repo_name, "branch": repo.default_branch,
                "files": files, "truncated": len(files) >= limit}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_read_file(gh, config, args):
    """Chat tool: read a window of a file, by byte offset or centred on a regex match."""
    # All GitHub tools need a token-backed client.
    if gh is None:
        return {"error": "GitHub client unavailable (no token configured)."}
    # Repo and path are required.
    repo_name = (args.get("repo") or "").strip()
    path = (args.get("path") or "").strip()
    if not repo_name or not path:
        return {"error": "repo and path are required"}
    # Window size clamped to 256..20000 bytes.
    max_bytes = max(256, min(20000, int(args.get("max_bytes") or 8000)))
    # Starting offset; bad values start at 0.
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    # Optional regex used to jump to the interesting part of the file.
    pattern = args.get("pattern") or ""
    # Fetch the file; any API error becomes an error result.
    try:
        contents = gh.get_repo(repo_name).get_contents(path)
        # get_contents returns a list for a directory, which we cannot read as a file.
        if isinstance(contents, list):
            return {"error": f"{path} is a directory, not a file"}
        # Decode as UTF-8, replacing bad bytes, and note the total length.
        raw = contents.decoded_content or b""
        text = raw.decode("utf-8", "replace")
        total = len(text)

        # pattern: jump the window to the first match so large files don't have
        # to be paged from the start to reach the relevant region.
        match_lines = []
        # Compile the regex; an invalid pattern is reported back to the model.
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                return {"error": f"invalid pattern: {e}"}
            # Find the first match; if none, tell the model so it can page instead.
            m = rx.search(text)
            if m is None:
                return {"repo": repo_name, "path": path, "total_bytes": total,
                        "pattern": pattern, "match_found": False,
                        "note": "pattern not found; call again without pattern (optionally with offset) to page the file."}
            # Center the window on the match, clamped to the file.
            offset = max(0, m.start() - max_bytes // 4)
            # Report the line numbers of up to 50 matches.
            for lm in rx.finditer(text):
                match_lines.append(text.count("\n", 0, lm.start()) + 1)
                if len(match_lines) >= 50:
                    break

        # Cut the window and work out where the next page starts and which line it begins on.
        offset = min(offset, total)
        window = text[offset:offset + max_bytes]
        next_offset = offset + len(window)
        start_line = text.count("\n", 0, offset) + 1
        # Build the reply with paging details.
        result = {"repo": repo_name, "path": path, "total_bytes": total,
                  "offset": offset, "next_offset": next_offset,
                  "start_line": start_line, "has_more": next_offset < total,
                  "content": window}
        # Include match details when a pattern was used.
        if pattern:
            result["match_found"] = True
            result["match_lines"] = match_lines
        return result
    except Exception as e:
        return {"error": f"{type(e).__name__}: {_trunc(e, 300)}"}


def _tool_get_processed_issues(gh, config, args):
    """Chat tool: summarise AppBuilder's processed-issue store, optionally for one repo."""
    # Optional repo filter and the full processed store.
    repo_filter = (args.get("repo") or "").strip()
    processed = load_processed()
    # Per-status counts, a small sample of entries, and how many matched.
    counts = {}
    sample = []
    matched = 0
    # Walk every processed issue (ids look like "owner/repo:number").
    for issue_id, info in processed.items():
        # Skip malformed entries and those outside the filtered repo.
        if not isinstance(info, dict):
            continue
        if repo_filter and not issue_id.startswith(repo_filter + ":"):
            continue
        # Count this issue under its status and keep up to 20 samples.
        matched += 1
        st = info.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
        if len(sample) < 20:
            sample.append({"issue": issue_id, "status": st,
                           "timestamp": info.get("timestamp", "")})
    # Return the counts, totals and sample.
    return {"filter_repo": repo_filter or None, "counts": counts, "total": matched,
            "total_all": len(processed), "sample": sample}


def _tool_get_recent_errors(gh, config, args):
    """Chat tool: return recent Hub errors plus error lines from AppBuilder's own log."""
    # Limit clamped to 1..50.
    limit = max(1, min(50, int(args.get("limit") or 15)))
    # Pull recent Hub logs and keep only the error entries.
    hub_errors = []
    logs = get_hub_logs(CHAT_HUB_LOG_LINES_PER_MODULE)
    if logs:
        try:
            hub_errors = filter_error_logs(logs)[:limit]
        except Exception as e:
            hub_errors = [{"error": f"filter failed: {type(e).__name__}"}]
    # Self errors: tail the local AppBuilder log and keep ERROR/Traceback lines.
    self_errors = []
    # Read the last 500 lines of the local log; a missing or unreadable log is skipped.
    try:
        path = get_log_path()
        if path and os.path.exists(path):
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()[-500:]
            # Keep lines that look like errors or tracebacks, up to the limit.
            for ln in lines:
                if re.search(r'\[ERROR\]|\[CRITICAL\]|Traceback|Exception|Error[: ]', ln):
                    self_errors.append(_trunc(ln.strip(), 300))
                    if len(self_errors) >= limit:
                        break
    except Exception:
        pass
    # Return both lists with a hint on where to look next.
    return {"hub_errors": hub_errors, "self_errors": self_errors,
            "note": "Use get_issue/list_issues for detail on any error filed as a GitHub issue."}


def _tool_propose_fix(gh, config, args):
    """Chat tool: prepare (but do not run) an automated fix that the user must confirm."""
    # Repo and issue number are required.
    repo_name = (args.get("repo") or "").strip()
    number = args.get("number")
    if not repo_name or number is None:
        return {"error": "repo and number are required"}
    # Only "cloud", "local" or no preference are accepted.
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
    # One-time token the UI sends back when the user confirms this fix.
    token = uuid.uuid4().hex
    # Descriptor the UI turns into a confirmation button.
    descriptor = {"kind": "confirm_fix", "repo": repo_name, "number": int(number),
                  "title": _trunc(title, 200), "llm_preference": pref, "confirm_token": token}
    return descriptor


def _tool_azure_cli(gh, config, args):
    """Diagnose the live LM fleet with a read-only `az` command. argv is taken
    from `args` (preferred) or a `command` string, which is shell-split. All
    vetting/login/execution lives in az_console (default-deny mutation)."""
    # Prefer the argv list; otherwise split a command string the way a shell would.
    argv = args.get("args")
    if argv is None and isinstance(args.get("command"), str):
        import shlex
        try:
            argv = shlex.split(args["command"])
        except ValueError:
            return {"error": "could not parse the az command string"}
    # A bare string in args is shell-split too.
    if isinstance(argv, str):
        import shlex
        argv = shlex.split(argv)
    # Nothing usable to run.
    if not isinstance(argv, list) or not argv:
        return {"error": "provide `args` as an array of az arguments (no leading 'az')"}
    # Drop a leading "az" if the model included it.
    if argv and str(argv[0]).lower() == "az":
        argv = argv[1:]
    # Vetting, login and execution all happen in az_console.
    return az_console.run_az(argv, config)


def _tool_server_shell(gh, config, args):
    """Run a read-only shell command ON one LM VM to verify functionality /
    inspect logs. Vetting + run-command execution lives in az_console."""
    # Pass the VM name and command straight to the vetted runner.
    return az_console.run_server_shell(args.get("vm"), args.get("command"), config)


# Maps each tool name the LLM may call to the Python function that executes it.
CHAT_TOOL_EXECUTORS = {
    "list_repos": _tool_list_repos,
    "list_issues": _tool_list_issues,
    "get_issue": _tool_get_issue,
    "list_repo_files": _tool_list_repo_files,
    "read_file": _tool_read_file,
    "get_processed_issues": _tool_get_processed_issues,
    "get_recent_errors": _tool_get_recent_errors,
    "propose_fix": _tool_propose_fix,
    "azure_cli": _tool_azure_cli,
    "server_shell": _tool_server_shell,
}


def _set_chat_stream_status(chat_id, text):
    """Publishes an interim status string (e.g. '[calling tool: list_issues …]')
    so /api/chat/stream shows progress during multi-turn tool resolution. Writes
    to both chat_streams (under _chat_lock) and active_tasks (under
    _task_state_lock), mirroring how chat_stream folds active_tasks in."""
    # Update the chat stream entry the browser polls.
    with _chat_lock:
        entry = state.setdefault("chat_streams", {}).setdefault(chat_id, {})
        entry["stream"] = text
        entry["done"] = False
        entry["error"] = None
    # Mirror the text into the active-task entry, if one exists for this chat.
    with _task_state_lock:
        task = state.get("active_tasks", {}).get(chat_id)
        if task is not None:
            task["stream"] = text


def _finalize_chat_stream(chat_id, text):
    """Mark a chat stream as finished with its final text."""
    # Replace the stream entry under the chat lock so readers see a consistent value.
    with _chat_lock:
        state.setdefault("chat_streams", {})[chat_id] = {
            "stream": text or "", "done": True, "error": None,
        }


def _set_chat_stream_error(chat_id, message):
    """Mark a chat stream as finished with an error message."""
    # Replace the stream entry under the chat lock with an empty, errored, finished state.
    with _chat_lock:
        state.setdefault("chat_streams", {})[chat_id] = {
            "stream": "", "done": True, "error": message,
        }


def _register_fix_proposal(chat_id, descriptor, config):
    """Stores a fix-proposal confirmation token server-side (under _chat_lock)
    with a creation timestamp so /api/chat/confirm_fix can validate + TTL it."""
    # Nothing to register without a token.
    token = descriptor.get("confirm_token")
    if not token:
        return
    # How long (seconds) the user has to confirm, default 10 minutes.
    ttl = int(config.get("CHAT_FIX_PROPOSAL_TTL", 600) or 600)
    # Remember the proposal under its token so the confirm endpoint can look it up.
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
    # Build the ":::confirm_fix ... :::" block the UI renders as a Confirm button.
    pref = descriptor.get("llm_preference") or ""
    return (
        f":::confirm_fix repo={descriptor.get('repo')} number={descriptor.get('number')} "
        f"token={descriptor.get('confirm_token')} pref={pref}\n"
        f"Run automated fix on #{descriptor.get('number')} "
        f"\"{descriptor.get('title', '')}\"? Click Confirm to proceed.\n:::"
    )


# Finds <tool_call>{json}</tool_call> blocks some models emit as plain text.
_TOOLCALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _parse_text_tool_calls(text):
    """Some models (esp. via ollama) emit tool calls as ``<tool_call>{...}</tool_call>``
    TEXT instead of the structured tool_calls field. Extract any such blocks so the
    agent loop can execute them, and return (cleaned_text, tool_calls) with the tags
    stripped so raw ``tool_calls`` JSON never leaks into the visible answer."""
    # Fast path: no tool-call tags at all.
    if not text or "<tool_call>" not in text:
        return text, []
    # Parse each tagged JSON block; skip blocks that are not valid JSON.
    calls = []
    for m in _TOOLCALL_TAG_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        # Accept both {"name", "arguments"} and OpenAI-style {"function": {...}} shapes.
        fn = obj.get("function") if isinstance(obj.get("function"), dict) else {}
        name = obj.get("name") or fn.get("name")
        args = obj.get("arguments") or fn.get("arguments") or obj.get("parameters") or {}
        # Normalise into the structured tool_calls shape the agent loop expects.
        if name:
            calls.append({"function": {"name": name, "arguments": args}})
    # Remove the tags from the visible text.
    cleaned = _TOOLCALL_TAG_RE.sub("", text).strip()
    return cleaned, calls


def _run_chat_reply_simple(chat_id, config):
    """Legacy single-turn chat path: used when CHAT_TOOLS_ENABLED is False (or as
    the graceful-degradation fallback). Streams one call_llm reply with a plain
    system prompt. Preserves the pre-tool chat behavior."""
    # Any failure is reported to the chat stream instead of raising.
    try:
        # How many past messages to send, and the system prompt to use.
        window_size = int(config.get("CHAT_HISTORY_WINDOW", 20) or 20)
        system_prompt = config.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
        # Load the conversation; it may have been deleted meanwhile.
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return
        # System prompt plus the most recent messages.
        messages = conv.get("messages", [])
        window = [{"role": "system", "content": system_prompt}] + messages[-window_size:]
        # Ask for a small, fast model (or the operator's pinned chat model).
        from model_selection import LlmRequirements
        reqs = LlmRequirements(complexity="small", latency_sensitive=True, pin_key=(config.get("chat_pin") or None))
        # One LLM call for the reply.
        reply = call_llm("", messages=window, task_id=chat_id, requirements=reqs)
        # Save a non-empty reply into the conversation.
        if reply and reply.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": reply,
                "ts": datetime.now().isoformat(),
            })
        # Mark the stream finished so the UI stops polling.
        _finalize_chat_stream(chat_id, reply or "")
    # Log the full traceback and show the error in the chat.
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
        # Load the conversation; a missing one is reported and counts as handled.
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return True
        # The request is the latest user message.
        history = conv.get("messages", [])
        user_msgs = [m for m in history if m.get("role") == "user"]
        request_text = (user_msgs[-1].get("content") if user_msgs else "") or ""
        # Nothing to answer.
        if not request_text.strip():
            _finalize_chat_stream(chat_id, "")
            return True

        # Imported lazily so chat works even if the orchestrator is never used.
        import agent_orchestrator
        # Running list of "task -> model" lines shown while agents work.
        progress = {"parts": []}

        # The context index is optional; carry on without it on error.
        # This path only runs when there is no GitHub token (no tool grounding),
        # so the orchestrator can't scan anything — but it must at least KNOW
        # which real repos/servers AB manages and must not fabricate. Prepend the
        # AB context index + grounding rule so its agents answer about AB, not
        # invented placeholder repos.
        try:
            index_text = build_chat_context_index(config, gh=None)
        except Exception:
            index_text = ""
        # Prefix the request with the context and grounding rules when available.
        grounded_request = request_text
        if index_text:
            grounded_request = (
                f"{index_text}\n\n{_CHAT_GROUNDING_RULE}\n\n"
                f"User request:\n{request_text}"
            )

        def _progress(ev):
            """Turn orchestrator progress events into chat-stream status messages."""
            # Which milestone this event marks.
            kind = ev.get("event")
            # Plan ready: say how many parallel agents there are.
            if kind == "planned":
                n = ev.get("task_count", 1)
                _set_chat_stream_status(
                    chat_id,
                    f"🧠 Planning — split into {n} parallel agents…" if n > 1 else "Thinking…",
                )
            # One agent finished: add a line naming its model.
            elif kind == "agent_done":
                progress["parts"].append(
                    f"• {ev.get('task_id')} → {ev.get('model') or '?'}"
                    + ("" if ev.get("ok") else " (failed)")
                )
                _set_chat_stream_status(chat_id, "Agents working…\n" + "\n".join(progress["parts"]))
            # All agents done: the merge step is running.
            elif kind == "merged":
                _set_chat_stream_status(chat_id, "Merging results…")

        # Run the multi-agent pipeline, reporting progress into the chat stream.
        result = agent_orchestrator.orchestrate(
            grounded_request, config, progress_cb=_progress, task_id=chat_id,
        )
        # The merged answer.
        final = result.final_text or ""
        # Only annotate when the request actually fanned out, so single-agent
        # answers read exactly like a normal chat reply.
        if result.planned and result.results:
            roster = ", ".join(f"{r.task_id}→{r.model_label or '?'}" for r in result.results)
            final = f"{final}\n\n---\n🧠 *Handled by {len(result.results)} agents: {roster}*"
        # Save a non-empty answer and finish the stream.
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
                   max_result_chars=48000, status_cb=None, on_fix_proposal=None,
                   tool_requirements=None, final_requirements=None):
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
    tool_requirements / final_requirements: optional LlmRequirements the caller
        supplies to steer model selection for the tool turns and the forced
        final-answer turn respectively. The dashboard chat leaves these None to
        stay cost-first (medium, latency-sensitive); the LLM router passes a
        stronger requirement (large + prefer_capable) so agentic diagnosis gets
        a smarter model. tool_requirements MUST keep needs_tools=True.

        When neither is supplied, the loop self-escalates: it starts fastest-in-
        tier and switches to most-capable-in-tier after
        `chat_escalate_after_iters` (default 2) tool rounds, so a hard question
        is not answered by the fast model forever. See the loop preamble.
    """
    # Model requirements type, and dataclasses.replace to tweak them during escalation.
    from model_selection import LlmRequirements
    import dataclasses as _dc
    # Progress reporter (a no-op when the caller passed none).
    _status = status_cb or (lambda *_a, **_k: None)
    # Default requirements for tool turns: medium model, must support tools, fast.
    _tool_reqs = tool_requirements or LlmRequirements(complexity="medium", needs_tools=True, latency_sensitive=True)
    # In-loop escalation. The first turns are latency_sensitive so the dashboard
    # feels instant, but a question that is still calling tools after several
    # rounds is, empirically, one that needs more reasoning than the fastest
    # model in the tier has. From that point on, order the SAME tier by
    # capability instead of speed and stop optimising for latency.
    #
    # Deliberately prefer_capable_within_tier, not prefer_capable: this is the
    # dashboard's cost-first chat, so escalating must not start spending on the
    # frontier tier. (llm_proxy's agentic path is the one that does that, and it
    # passes its own tool_requirements.) Skipped entirely when the caller steered
    # selection itself or the operator pinned a model -- neither wants us
    # second-guessing the pick.
    _escalate_after = max(1, int((config or {}).get("chat_escalate_after_iters", 2) or 2))
    _can_escalate = tool_requirements is None and not (config or {}).get("chat_pin")
    # Becomes True once we switch to the more capable model ordering.
    _escalated = False
    # Azure fleet tools are advertised only when the operator has opted in, so a
    # model on a host without the login helper isn't tempted to call them.
    tools = CHAT_TOOLS + (AZURE_TOOLS if az_console.azure_enabled(config) else [])
    # Characters of tool output used so far, the final answer, and the latest model text.
    used_chars = 0
    final_text = None
    last_text = ""
    # Each iteration is one model turn plus the tools it asked for.
    for iteration in range(max_iter):
        # After enough tool rounds, prefer capability over speed within the same cost tier.
        if _can_escalate and not _escalated and iteration >= _escalate_after:
            _tool_reqs = _dc.replace(_tool_reqs, latency_sensitive=False,
                                     prefer_capable_within_tier=True)
            _escalated = True
            logger.info("Chat agent loop still working after %d tool rounds; escalating model "
                        "selection from fastest-in-tier to most-capable-in-tier.", iteration)
        # Tell the UI we are working.
        _status("Thinking…" if iteration == 0 else "Working…")
        # Ask the model for its next step, with tools available.
        try:
            result = call_llm("", messages=messages, task_id=task_id, tools=tools, stream=False, requirements=_tool_reqs)
        except Exception as e:
            # Tool-capable turn failed (e.g. cloud without tool support). Degrade
            # to one index-only turn and finish.
            logger.warning(f"Agent tool turn {iteration} failed ({e}); degrading to index-only answer.")
            _fallback_reqs = LlmRequirements(complexity="small", latency_sensitive=True, pin_key=(config.get("chat_pin") or None))
            reply = call_llm("", messages=messages[:], task_id=task_id, requirements=_fallback_reqs)
            final_text = reply or ""
            break

        # A non-dict result is plain text: that is the answer.
        if not isinstance(result, dict):
            final_text = str(result)
            break
        # Split the reply into its text and any structured tool calls.
        text = result.get("text") or ""
        tool_calls = result.get("tool_calls") or []
        # Fallback: model emitted tool calls as <tool_call>{…}</tool_call> text
        # rather than structured tool_calls — parse + execute them, and strip the
        # tags so the raw JSON never shows in the answer.
        if not tool_calls:
            text, tool_calls = _parse_text_tool_calls(text)
        # Remember the latest text in case we run out of iterations.
        last_text = text
        # No tool calls means the model has written its final answer.
        if not tool_calls:
            final_text = text
            break

        # Echo the assistant turn (with tool_calls) back for the next round.
        messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})

        # Set when propose_fix is called, which ends the loop.
        hit_proposal = False
        # Execute each tool call the model made.
        for tc in tool_calls:
            # Pull out the tool name and its arguments (which may be a JSON string or a dict).
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            args_raw = fn.get("arguments") if fn else tc.get("arguments")
            # Parse JSON-string arguments; unparseable arguments become an empty dict.
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw) if args_raw else {}
                except Exception:
                    args = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}

            # Unknown tools get an error result the model can see.
            if not name or name not in CHAT_TOOL_EXECUTORS:
                out = {"error": f"unknown tool: {name}"}
            # Run the tool, turning any exception into an error result.
            else:
                _status(f"[calling tool: {name} …]")
                try:
                    out = CHAT_TOOL_EXECUTORS[name](gh, config, args)
                except Exception as ee:
                    out = {"error": f"{type(ee).__name__}: {_trunc(ee, 300)}"}
            # Scrub secrets, then serialise for the conversation.
            out = _sanitize_tool_result(out, config)
            out_str = json.dumps(out)
            # Stop feeding output once the total result budget would be exceeded.
            if used_chars + len(out_str) > max_result_chars:
                out_str = json.dumps({"error": "tool result budget exceeded; narrow your query", "truncated": True})
            used_chars += len(out_str)
            # Append the tool result, linked to the call id the model used.
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
        # A proposal ends the whole loop, not just this batch of calls.
        if hit_proposal:
            break
    # for/else: this runs only when the loop ended without a break (iteration cap hit).
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
                _final_reqs = final_requirements or LlmRequirements(complexity="medium", latency_sensitive=True)
                # We only reach this branch by exhausting max_iter without a
                # written answer, so the loop already proved it was hard. If the
                # tool turns escalated, the synthesis that has to make sense of
                # them should not drop back to the fastest weakest model.
                if _escalated and final_requirements is None:
                    _final_reqs = _dc.replace(_final_reqs, latency_sensitive=False,
                                              prefer_capable_within_tier=True)
                # One last call without tools to get a written answer.
                forced = call_llm("", messages=messages, task_id=task_id, requirements=_final_reqs)
                # Some call paths return a dict; take its text.
                if isinstance(forced, dict):
                    forced = forced.get("text") or ""
                # Use it only if it actually says something.
                if forced and forced.strip():
                    final_text = forced
            # The forced turn is best-effort; keep whatever text we already had.
            except Exception as e:
                logger.warning(f"Agent final-answer turn failed ({e}); returning best-effort text.")

    # Fall back to the last text seen if nothing else was set.
    if final_text is None:
        final_text = last_text or ""
    # Defense in depth: strip any residual <tool_call> tags before returning.
    if final_text and "<tool_call>" in final_text:
        final_text, _ = _parse_text_tool_calls(final_text)
    # Always return a string.
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
    # Any failure is reported to the chat; the task entry is always cleared in finally.
    try:
        # Fresh config and an optional GitHub client (None without a token).
        config = load_config()
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        gh = Github(token) if token else None

        # The multi-agent orchestrator has NO tools and NO AppBuilder context —
        # its agents answer from the raw prompt alone. When it front-ran chat it
        # fabricated grounded work (e.g. a "security scan" that invented repos
        # like octocat/Hello-World and fake npm-audit results instead of scanning
        # the real monitored repos). It must therefore NEVER preempt the grounded
        # tool loop below: only use it when there is genuinely no tool grounding
        # available (no GitHub token) AND the operator opted in. Anything that can
        # be grounded with real tools goes through run_agent_loop.
        if gh is None and config.get("ORCHESTRATOR_ENABLED", False):
            if _run_chat_reply_orchestrated(chat_id, config):
                return
            logger.warning("orchestrated chat turn failed; falling back to the standard path")
        # Tools switched off: use the plain one-call path.
        if not config.get("CHAT_TOOLS_ENABLED", True):
            return _run_chat_reply_simple(chat_id, config)

        # History window, base system prompt and maximum tool rounds from config.
        window_size = int(config.get("CHAT_HISTORY_WINDOW", 20) or 20)
        base_system = config.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
        max_iter = int(config.get("CHAT_TOOL_MAX_ITERATIONS", 6) or 6)
        # Token-budget config is in ~tokens; apply a 4x char budget for results.
        max_result_chars = int(config.get("CHAT_TOOL_MAX_TOKENS", 12000) or 12000) * 4

        # Load the conversation; it may have been deleted meanwhile.
        store = load_chats()
        conv = get_conversation(store, chat_id)
        if conv is None:
            _set_chat_stream_error(chat_id, "Conversation not found")
            return

        # Index gives awareness even without tools; gh passed so issue titles fill in.
        index_text = build_chat_context_index(config, gh=gh)
        # System prompt = base prompt + context index + grounding rules, then recent history.
        system_prompt = base_system + "\n\n" + index_text + "\n\n" + _CHAT_GROUNDING_RULE
        history = conv.get("messages", [])
        window = history[-window_size:]
        messages = [{"role": "system", "content": system_prompt}] + list(window)

        # No GitHub token -> no tools, but the index still informs the answer.
        if gh is None:
            # Plain streamed reply on a small, fast model (or the pinned chat model).
            _set_chat_stream_status(chat_id, "Thinking…")
            from model_selection import LlmRequirements
            reqs = LlmRequirements(complexity="small", latency_sensitive=True, pin_key=(config.get("chat_pin") or None))
            reply = call_llm("", messages=messages, task_id=chat_id, requirements=reqs)  # streaming string
            # Save a non-empty reply and finish the stream.
            if reply and reply.strip():
                append_chat_message(chat_id, {
                    "role": "assistant",
                    "content": reply,
                    "ts": datetime.now().isoformat(),
                })
            _finalize_chat_stream(chat_id, reply or "")
            return

        def _chat_on_fix_proposal(descriptor, text):
            """Register a confirm token for a proposed fix and return text ending in a Confirm marker."""
            # Dashboard front-end: register a single-use confirm token and render
            # a Confirm button (:::confirm_fix marker). Non-mutating — the real
            # fix only runs when the user POSTs /api/chat/confirm_fix.
            _register_fix_proposal(chat_id, descriptor, config)
            marker = _confirm_fix_marker(descriptor)
            return (text + "\n\n" + marker).strip() if text else marker

        # Run the shared agent loop, streaming status into the chat.
        final_text = run_agent_loop(
            messages, config, gh,
            task_id=chat_id, max_iter=max_iter, max_result_chars=max_result_chars,
            status_cb=lambda s: _set_chat_stream_status(chat_id, s),
            on_fix_proposal=_chat_on_fix_proposal,
        )

        # Normalise a missing answer to empty text.
        if final_text is None:
            final_text = ""
        # Defense in depth: strip any residual <tool_call> tags before display.
        if final_text and "<tool_call>" in final_text:
            final_text, _ = _parse_text_tool_calls(final_text)
        # Save a non-empty answer, then mark the stream finished.
        if final_text and final_text.strip():
            append_chat_message(chat_id, {
                "role": "assistant",
                "content": final_text,
                "ts": datetime.now().isoformat(),
            })
        _finalize_chat_stream(chat_id, final_text or "")
    # Log the full traceback and show the error in the chat.
    except Exception as e:
        logger.error(f"run_chat_reply failed for {chat_id}: {e}\n{traceback.format_exc()}")
        _set_chat_stream_error(chat_id, f"LLM error: {e}")
    finally:
        # Remove the chat task from the Dashboard activity feed.
        update_task_state(chat_id, "Chat", action="end")


# Names re-exported by main via "from chat import *", including private helpers that
# other modules and tests use.
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
    '_tool_azure_cli',
    '_tool_server_shell',
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
    'AZURE_TOOLS',
    'CHAT_TOOL_EXECUTORS',
]
