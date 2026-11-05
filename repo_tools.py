"""Sandboxed read-only repository tools for agentic fix generation."""
# Standard modules for glob matching, JSON, paths and regex search.
import fnmatch
import json
import os
import re
# Type hints for the tool interfaces.
from typing import Any, Callable, Dict, Iterable, List, Optional


# Read-only tool definitions (OpenAI function-calling format) offered to the model.
REPO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search repository text files with a Python regular expression. Returns matching path, line number, and line text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression to search for."},
                    "path_glob": {"type": "string", "description": "Optional repo-relative glob such as 'src/**/*.py'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a repo-relative text file, optionally constrained to 1-based inclusive line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List one repo-relative directory without recursing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative directory path; omit or use '.' for the repository root."},
                },
                "required": [],
            },
        },
    },
]


# Extra system-prompt text explaining the tools and the required final JSON answer.
_AGENTIC_FIX_INSTRUCTIONS = (
    "You have read-only repository tools (grep, read_file, list_dir) scoped to the "
    "target checkout. Use them when the shown context is incomplete, the target file "
    "is uncertain, or you need an exact search anchor. Tool results are read-only; "
    "do not ask for writes. Your final answer must still be ONLY the requested JSON "
    "object with confidence and targeted {file, search, replace} edits. Copy search "
    "snippets from actual file content, not from line numbers, metadata, or markers."
)


class RepoToolExecutor:
    """Runs the read-only repo tools inside one checkout, refusing paths that escape it."""
    def __init__(self, repo_checkout_path: str, *, max_output_chars: int = 12000,
                 max_file_chars: int = 60000, max_grep_results: int = 80):
        """Remember the checkout root and the output limits (each with a sane minimum)."""
        self.root = os.path.realpath(repo_checkout_path)
        self.max_output_chars = max(1000, int(max_output_chars or 12000))
        self.max_file_chars = max(1000, int(max_file_chars or 60000))
        self.max_grep_results = max(1, int(max_grep_results or 80))

    def _resolve(self, path: Optional[str]) -> str:
        """Make a repo-relative path absolute, rejecting anything outside the checkout."""
        # An empty path means the repo root; absolute paths are refused outright.
        rel = "." if path in (None, "") else str(path)
        if os.path.isabs(rel):
            raise ValueError("absolute paths are not allowed")
        # Resolve symlinks and "..", then make sure the result is still inside the root.
        target = os.path.realpath(os.path.join(self.root, rel))
        if os.path.commonpath([self.root, target]) != self.root:
            raise ValueError("path escapes repository checkout")
        return target

    def _relpath(self, path: str) -> str:
        """Return the path relative to the checkout, always with forward slashes."""
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def _bounded_json(self, data: Dict[str, Any]) -> str:
        """Encode a tool result as JSON, cutting it down to the output limit if needed."""
        # Small enough: return as-is.
        text = json.dumps(data, ensure_ascii=False)
        if len(text) <= self.max_output_chars:
            return text
        # Too big: flag it, cut the content field, and hard-cut the JSON text as a last resort.
        data = dict(data)
        data["truncated"] = True
        data["content"] = str(data.get("content", ""))[: self.max_output_chars]
        text = json.dumps(data, ensure_ascii=False)
        return text[: self.max_output_chars] + "…"

    def list_dir(self, path: str = ".") -> str:
        """List one directory (first 200 entries), marking each as file or dir."""
        # The path must be a directory inside the checkout.
        target = self._resolve(path)
        if not os.path.isdir(target):
            raise ValueError("path is not a directory")
        # Collect names in sorted order, capped at 200.
        entries = []
        for name in sorted(os.listdir(target))[:200]:
            full = os.path.join(target, name)
            entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file"})
        return self._bounded_json({"path": self._relpath(target), "entries": entries})

    def read_file(self, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
        """Read a file, whole (up to the size limit) or a 1-based inclusive line range."""
        # Check the path and that the line range makes sense.
        target = self._resolve(path)
        if not os.path.isfile(target):
            raise ValueError("path is not a file")
        if start_line is not None and int(start_line) < 1:
            raise ValueError("start_line must be >= 1")
        if end_line is not None and int(end_line) < 1:
            raise ValueError("end_line must be >= 1")
        if start_line is not None and end_line is not None and int(end_line) < int(start_line):
            raise ValueError("end_line must be >= start_line")
        # No range: read up to the size limit and note if it was cut.
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            if start_line is None and end_line is None:
                content = f.read(self.max_file_chars + 1)
                truncated = len(content) > self.max_file_chars
                content = content[: self.max_file_chars]
                start, end = 1, content.count("\n") + 1
            # Range given: default to 200 lines from the start line.
            else:
                start = int(start_line or 1)
                end = int(end_line or (start + 199))
                lines = []
                # Keep only lines inside the range.
                for idx, line in enumerate(f, start=1):
                    if idx < start:
                        continue
                    if idx > end:
                        break
                    lines.append(line)
                content = "".join(lines)
                truncated = False
        # Return the content with the range actually read.
        return self._bounded_json({
            "path": self._relpath(target),
            "start_line": start,
            "end_line": end,
            "content": content,
            "truncated": truncated,
        })

    def _iter_candidate_files(self, path_glob: Optional[str]) -> Iterable[str]:
        """Yield checkout files whose relative path matches the glob (all by default)."""
        # The glob must not be absolute or climb out with "..".
        glob_pat = (path_glob or "**/*").replace(os.sep, "/")
        if os.path.isabs(glob_pat) or ".." in glob_pat.split("/"):
            raise ValueError("path_glob must stay inside the repository")
        # Walk the tree, skipping VCS, cache and dependency folders.
        for base, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "venv", "node_modules"}]
            # Match the relative path, with or without a leading slash.
            for name in files:
                full = os.path.join(base, name)
                rel = self._relpath(full)
                if fnmatch.fnmatch(rel, glob_pat) or fnmatch.fnmatch("/" + rel, glob_pat):
                    yield full

    def grep(self, pattern: str, path_glob: Optional[str] = None, max_results: Optional[int] = None) -> str:
        """Search file lines with a regex; return matches up to a result limit."""
        # The pattern is required and must compile.
        if not pattern:
            raise ValueError("pattern is required")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        # Cap the result count at both the request and our own limit.
        limit = min(max(1, int(max_results or 50)), self.max_grep_results)
        matches: List[Dict[str, Any]] = []
        scanned = 0
        # Scan every candidate file line by line.
        for full in self._iter_candidate_files(path_glob):
            scanned += 1
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, start=1):
                        # Record each match (text cut to 500 chars); stop at the limit.
                        if rx.search(line):
                            matches.append({
                                "path": self._relpath(full),
                                "line": line_no,
                                "text": line.rstrip("\n")[:500],
                            })
                            if len(matches) >= limit:
                                return self._bounded_json({"matches": matches, "truncated": True, "files_scanned": scanned})
            # Unreadable files are skipped.
            except OSError:
                continue
        return self._bounded_json({"matches": matches, "truncated": False, "files_scanned": scanned})

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Run the named tool; errors are returned to the model as JSON, not raised."""
        # Dispatch to the matching tool.
        try:
            if name == "grep":
                return self.grep(args.get("pattern"), args.get("path_glob"), args.get("max_results"))
            if name == "read_file":
                return self.read_file(args.get("path"), args.get("start_line"), args.get("end_line"))
            if name == "list_dir":
                return self.list_dir(args.get("path", "."))
            raise ValueError(f"unknown repo tool: {name}")
        # Report the error to the model so it can correct itself.
        except Exception as e:  # noqa: BLE001 - tool errors are returned to the model
            return self._bounded_json({"error": str(e), "tool": name})


def _tool_name_and_args(tool_call: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Pull the tool name and arguments from a tool call, in either of its two shapes."""
    # The name may be under "function" or at the top level.
    fn = tool_call.get("function") or {}
    name = fn.get("name") or tool_call.get("name") or ""
    raw_args = fn.get("arguments") or tool_call.get("arguments") or {}
    # Arguments may be a JSON string or already a dict; anything else becomes empty.
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            args = {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    return name, args


def _augment_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copy the messages and add the tool instructions to the system prompt."""
    # Append to the existing system message if there is one ...
    out = [dict(m) for m in messages]
    for msg in out:
        if msg.get("role") == "system":
            msg["content"] = (msg.get("content") or "") + "\n\n" + _AGENTIC_FIX_INSTRUCTIONS
            return out
    # ... otherwise add a new one at the front.
    return [{"role": "system", "content": _AGENTIC_FIX_INSTRUCTIONS}] + out


def _result_text(result: Any) -> str:
    """Return the text of an LLM result, whether a dict or a plain value."""
    if isinstance(result, dict):
        return result.get("text") or ""
    return str(result or "")


# Sent when the model replies in prose instead of a tool call or the JSON answer.
_PROSE_NUDGE = ("Your last reply contained neither a tool call nor the JSON edits object. "
                "If you still need to inspect the repo, call a tool. Otherwise return ONLY "
                "the final JSON edits object now — no prose.")


def run_agentic_fix(call_llm_fn: Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], Any],
                     messages: List[Dict[str, Any]], repo_checkout_path: str, *, max_iterations: int = 8,
                     max_result_chars: int = 12000, logger: Any = None) -> str:
    """Run a bounded read-only tool loop and return the model's final text."""
    # Set up the executor, the conversation with tool instructions, and the step limit.
    executor = RepoToolExecutor(repo_checkout_path, max_output_chars=max_result_chars)
    loop_messages = _augment_messages(messages)
    max_iterations = max(1, int(max_iterations or 8))

    # A reply with no tool calls AND no JSON object is the model narrating
    # mid-investigation ("Now let me check the serializer…"), not its answer.
    # Returning it as final burned two of cs#184's three fix attempts as
    # "no JSON". Ask for the edits object instead — bounded, so a model that
    # only ever replies in prose still terminates.
    prose_nudges_left = 2
    # Each step: ask the model and handle its reply.
    for _ in range(max_iterations):
        result = call_llm_fn(loop_messages, REPO_TOOLS)
        # A plain (non-dict) reply is final.
        if not isinstance(result, dict):
            return str(result or "")
        tool_calls = result.get("tool_calls") or []
        text = result.get("text") or ""
        # No tool calls: accept the answer if it has JSON (or nudges are used up) ...
        if not tool_calls:
            if "{" in text or prose_nudges_left <= 0:
                return text
            # ... otherwise ask once more for the JSON answer.
            prose_nudges_left -= 1
            if logger:
                logger.info("agentic fix loop: reply had no tool call and no JSON object; "
                            "asking for the final edits object")
            loop_messages.append({"role": "assistant", "content": text})
            loop_messages.append({"role": "user", "content": _PROSE_NUDGE})
            continue
        # Record the assistant's tool calls, then run each one and add its result.
        loop_messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
        for tc in tool_calls:
            name, args = _tool_name_and_args(tc)
            content = executor.execute(name, args)
            # Cut long results to the limit.
            if len(content) > max_result_chars:
                content = content[:max_result_chars] + "…"
            loop_messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name or "repo_tool",
                "name": name or "repo_tool",
                "content": content,
            })

    # Out of steps: ask for the final answer with tools disabled.
    if logger:
        logger.warning("agentic fix loop reached iteration cap; forcing final edit response")
    forced = loop_messages + [{"role": "user", "content": "Tool iteration limit reached. Return ONLY the final JSON edits object now."}]
    return _result_text(call_llm_fn(forced, None))


# ---------------------------------------------------------------------------
# Mutating build tools — the Copilot (OpenAI-style function calling) build
# agent. Lets a tool-capable API model build a feature in a throwaway
# checkout when no claude_cli mutating agent is approved for the deployment.
# Writes are limited to exact search/replace edits and new-file creation
# inside the checkout; there is no shell, no git and no delete.
# ---------------------------------------------------------------------------

# Read tools plus the write tools for the build agent.
BUILD_TOOLS = REPO_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace ONE exact occurrence of `search` with `replace` in an existing repo file. "
                "`search` must match the file byte-for-byte (copy it from read_file output, including "
                "indentation) and must occur exactly once — add surrounding lines to make it unique. "
                "The file's syntax is re-checked after the edit and the result is returned."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path."},
                    "search": {"type": "string", "description": "Exact existing text to replace (unique in the file)."},
                    "replace": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a NEW repo file with the given content. Fails if the file already exists — use edit_file for existing files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative path of the new file."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_syntax",
            "description": "Syntax-check a repo file (.py via compile, .js/.mjs/.cjs via node --check, .sh via bash -n, .json via a JSON parse).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Repo-relative file path."}},
                "required": ["path"],
            },
        },
    },
]

# Extra system-prompt text for the build agent.
_BUILD_AGENT_INSTRUCTIONS = (
    "You are working through function-calling tools scoped to a throwaway git checkout: "
    "grep, read_file, list_dir (read), edit_file and create_file (write), check_syntax. "
    "Make the changes yourself with edit_file/create_file — describing a change in prose "
    "does NOT apply it. Read the exact surrounding code before every edit_file call and "
    "copy `search` verbatim from read_file output. Keep each edit small and unique. Fix any "
    "syntax error the tools report before moving on. When every change is made, reply with "
    "ONLY the requested JSON summary object (no prose, no code fences)."
)

# Sent when the build agent replies in prose without making changes.
_BUILD_PROSE_NUDGE = (
    "Your last reply had no tool call and no JSON summary. If work remains, make the change "
    "now with edit_file/create_file (prose does not modify files). If everything is done, "
    "reply with ONLY the JSON summary object.")

# Folders the build agent may never write to.
_PROTECTED_DIRS = {".git", "_lm", ".venv", "venv", "node_modules", "__pycache__"}


def check_file_syntax(path: str, timeout_s: int = 60) -> Dict[str, Any]:
    """Language-aware syntax check of one file. Returns {"ok": bool,
    "checker": str, "output": str}; ok is True when no checker applies or the
    checker binary is unavailable (reported in `checker`)."""
    # Imported lazily; only needed when a check runs.
    import shutil
    import subprocess

    # Pick the checker from the file extension.
    ext = os.path.splitext(path)[1].lower()
    try:
        # Python: compile it in-process.
        if ext == ".py":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                compile(f.read(), path, "exec")
            return {"ok": True, "checker": "python-compile", "output": ""}
        # JSON: parse it in-process.
        if ext == ".json":
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            return {"ok": True, "checker": "json", "output": ""}
        # JS and shell: use an external checker (node / bash).
        if ext in (".js", ".mjs", ".cjs"):
            argv, checker = ["node", "--check", path], "node --check"
        elif ext in (".sh", ".bash"):
            argv, checker = ["bash", "-n", path], "bash -n"
        # No checker for this file type: treat as OK.
        else:
            return {"ok": True, "checker": "none", "output": ""}
    # A syntax error or read failure is reported back.
    except SyntaxError as e:
        return {"ok": False, "checker": "python-compile", "output": f"line {e.lineno}: {e.msg}"}
    except (ValueError, OSError) as e:
        return {"ok": False, "checker": "json" if ext == ".json" else "read", "output": str(e)}
    # External checker missing: treat as OK but say so.
    if not shutil.which(argv[0]):
        return {"ok": True, "checker": f"{checker} (unavailable)", "output": ""}
    # Run the checker and return its result (last 2000 chars of output).
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"ok": False, "checker": checker, "output": "syntax check timed out"}
    out = (proc.stderr or proc.stdout or "").strip()
    return {"ok": proc.returncode == 0, "checker": checker, "output": out[-2000:]}


class MutatingRepoToolExecutor(RepoToolExecutor):
    """RepoToolExecutor plus edit_file/create_file/check_syntax. Remembers
    each touched file's original content so a failed agent run can be rolled
    back before the next candidate model starts on the same checkout."""

    def __init__(self, repo_checkout_path: str, **kwargs: Any):
        """Set up the base executor, plus storage for original file contents and an edit count."""
        super().__init__(repo_checkout_path, **kwargs)
        self._originals: Dict[str, Optional[bytes]] = {}
        self.edits = 0

    def _resolve_writable(self, path: Optional[str]) -> str:
        """Resolve a path for writing, refusing the repo root and protected folders."""
        if not path:
            raise ValueError("path is required")
        target = self._resolve(path)
        parts = self._relpath(target).split("/")
        if target == self.root or any(p in _PROTECTED_DIRS for p in parts):
            raise ValueError("writes to this path are not allowed")
        return target

    def _remember(self, target: str) -> None:
        """Save a file's original content (None if it is new) the first time it is touched."""
        # Only the first copy matters.
        if target in self._originals:
            return
        if os.path.exists(target):
            with open(target, "rb") as f:
                self._originals[target] = f.read()
        else:
            self._originals[target] = None

    def changed_paths(self) -> List[str]:
        """List the repo-relative paths this executor has touched."""
        return sorted(self._relpath(p) for p in self._originals)

    def rollback(self) -> None:
        """Undo every edit: delete new files and restore changed ones."""
        # Restore each touched file; errors are ignored so the rest still get restored.
        for target, original in self._originals.items():
            try:
                if original is None:
                    if os.path.exists(target):
                        os.remove(target)
                else:
                    with open(target, "wb") as f:
                        f.write(original)
            except OSError:
                pass
        # Clear the record and the edit count.
        self._originals.clear()
        self.edits = 0

    def check_syntax(self, path: str) -> str:
        """Run the syntax check on a repo file and return the result as JSON."""
        target = self._resolve(path)
        if not os.path.isfile(target):
            raise ValueError("path is not a file")
        return self._bounded_json({"path": self._relpath(target), **check_file_syntax(target)})

    def edit_file(self, path: str, search: Optional[str], replace: Optional[str]) -> str:
        """Replace one exact, unique match of `search` in a file, then syntax-check it."""
        # The file must exist and the search text must be given.
        target = self._resolve_writable(path)
        if not os.path.isfile(target):
            raise ValueError("file does not exist — use create_file for new files")
        if not search:
            raise ValueError("search must be non-empty exact text from the file")
        # Read the file without newline translation so matches are byte-exact.
        with open(target, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        # The search text must occur exactly once.
        count = text.count(search)
        if count == 0:
            raise ValueError("search text not found — re-read the file and copy it exactly "
                             "(whitespace and indentation included)")
        if count > 1:
            raise ValueError(f"search text occurs {count} times — include more surrounding "
                             "lines so it matches exactly once")
        # Save the original for rollback, then write the change.
        self._remember(target)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(search, replace or "", 1))
        self.edits += 1
        # Return the result with a fresh syntax check.
        return self._bounded_json({"path": self._relpath(target), "edited": True,
                                   "syntax": check_file_syntax(target)})

    def create_file(self, path: str, content: Optional[str]) -> str:
        """Create a new file (failing if it exists), then syntax-check it."""
        # Refuse to overwrite an existing file.
        target = self._resolve_writable(path)
        if os.path.exists(target):
            raise ValueError("file already exists — use edit_file to change it")
        # Save "did not exist" for rollback, create parent folders, then write.
        self._remember(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content or "")
        self.edits += 1
        return self._bounded_json({"path": self._relpath(target), "created": True,
                                   "syntax": check_file_syntax(target)})

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Run a write tool by name; read tools fall through to the base executor."""
        # Dispatch to the write tools.
        try:
            if name == "edit_file":
                return self.edit_file(args.get("path"), args.get("search"), args.get("replace"))
            if name == "create_file":
                return self.create_file(args.get("path"), args.get("content"))
            if name == "check_syntax":
                return self.check_syntax(args.get("path"))
        # Report the error to the model so it can correct itself.
        except Exception as e:  # noqa: BLE001 - tool errors are returned to the model
            return self._bounded_json({"error": str(e), "tool": name})
        return super().execute(name, args)


def _compact_history(messages: List[Dict[str, Any]], budget_chars: int, keep_recent: int = 8) -> None:
    """Elide the oldest tool results once the conversation outgrows the
    model's context budget. A 40-step build over a 7,000-line file would
    otherwise overflow a 64K-token window; earlier reads can be re-done."""
    def _size() -> int:
        """Return the total length of all message contents."""
        return sum(len(str(m.get("content") or "")) for m in messages)

    # Under budget: nothing to do.
    if _size() <= budget_chars:
        return
    # Shorten older tool results, oldest first, keeping the most recent ones intact.
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-keep_recent] if keep_recent else tool_idx:
        if len(str(messages[i].get("content") or "")) > 200:
            messages[i]["content"] = "[elided earlier tool output — call the tool again if still needed]"
            if _size() <= budget_chars:
                return


def run_mutating_agent(call_llm_fn: Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], Any],
                       messages: List[Dict[str, Any]], repo_checkout_path: str, *, max_iterations: int = 40,
                       max_result_chars: int = 12000, context_budget_chars: int = 160000,
                       logger: Any = None) -> str:
    """Bounded build loop for a function-calling model: the model edits the
    checkout through BUILD_TOOLS and finishes with a JSON summary. On an
    exception (provider error mid-run) every file it touched is restored, so
    the next candidate starts from the same tree this one did."""
    # Set up the executor and a copy of the conversation.
    executor = MutatingRepoToolExecutor(repo_checkout_path, max_output_chars=max_result_chars)
    loop_messages = [dict(m) for m in messages]
    # Add the build instructions to the system message, or insert one if missing.
    for msg in loop_messages:
        if msg.get("role") == "system":
            msg["content"] = (msg.get("content") or "") + "\n\n" + _BUILD_AGENT_INSTRUCTIONS
            break
    else:
        loop_messages.insert(0, {"role": "system", "content": _BUILD_AGENT_INSTRUCTIONS})
    # Step limit and how many prose replies we will nudge.
    max_iterations = max(1, int(max_iterations or 40))
    prose_nudges_left = 3
    try:
        # Each step: trim old history, ask the model and handle its reply.
        for _ in range(max_iterations):
            _compact_history(loop_messages, context_budget_chars)
            result = call_llm_fn(loop_messages, BUILD_TOOLS)
            # A plain (non-dict) reply is final.
            if not isinstance(result, dict):
                return str(result or "")
            tool_calls = result.get("tool_calls") or []
            text = result.get("text") or ""
            # No tool calls: accept JSON once edits were made (or nudges are used up) ...
            if not tool_calls:
                if ("{" in text and executor.edits) or prose_nudges_left <= 0:
                    return text
                # ... otherwise nudge the model to make changes or give the summary.
                prose_nudges_left -= 1
                if logger:
                    logger.info("build agent loop: reply had no tool call%s; nudging",
                                "" if executor.edits else " and no edits made yet")
                loop_messages.append({"role": "assistant", "content": text})
                loop_messages.append({"role": "user", "content": _BUILD_PROSE_NUDGE if executor.edits else (
                    "You have not changed any file yet. Implement the feature now with "
                    "edit_file/create_file; reply with the JSON summary only after the edits are made.")})
                continue
            # Record the tool calls, then run each one and add its result.
            loop_messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
            for tc in tool_calls:
                name, args = _tool_name_and_args(tc)
                content = executor.execute(name, args)
                # Cut long results to the limit.
                if len(content) > max_result_chars:
                    content = content[:max_result_chars] + "…"
                loop_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or name or "repo_tool",
                    "name": name or "repo_tool",
                    "content": content,
                })
        # Out of steps: ask for the final summary with tools disabled.
        if logger:
            logger.warning("build agent loop reached its %d-step cap after %d edit(s); "
                           "asking for the final summary", max_iterations, executor.edits)
        _compact_history(loop_messages, context_budget_chars)
        forced = loop_messages + [{"role": "user", "content": (
            "Tool step limit reached. Reply with ONLY the JSON summary now, listing any "
            "unfinished touch-point under touchpoints_skipped.")}]
        return _result_text(call_llm_fn(forced, None))
    # Any failure: undo all changes so the next model starts from a clean tree.
    except Exception:
        executor.rollback()
        raise
