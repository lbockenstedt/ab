"""Sandboxed read-only repository tools for agentic fix generation."""
import fnmatch
import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional


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


_AGENTIC_FIX_INSTRUCTIONS = (
    "You have read-only repository tools (grep, read_file, list_dir) scoped to the "
    "target checkout. Use them when the shown context is incomplete, the target file "
    "is uncertain, or you need an exact search anchor. Tool results are read-only; "
    "do not ask for writes. Your final answer must still be ONLY the requested JSON "
    "object with confidence and targeted {file, search, replace} edits. Copy search "
    "snippets from actual file content, not from line numbers, metadata, or markers."
)


class RepoToolExecutor:
    def __init__(self, repo_checkout_path: str, *, max_output_chars: int = 12000,
                 max_file_chars: int = 60000, max_grep_results: int = 80):
        self.root = os.path.realpath(repo_checkout_path)
        self.max_output_chars = max(1000, int(max_output_chars or 12000))
        self.max_file_chars = max(1000, int(max_file_chars or 60000))
        self.max_grep_results = max(1, int(max_grep_results or 80))

    def _resolve(self, path: Optional[str]) -> str:
        rel = "." if path in (None, "") else str(path)
        if os.path.isabs(rel):
            raise ValueError("absolute paths are not allowed")
        target = os.path.realpath(os.path.join(self.root, rel))
        if os.path.commonpath([self.root, target]) != self.root:
            raise ValueError("path escapes repository checkout")
        return target

    def _relpath(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def _bounded_json(self, data: Dict[str, Any]) -> str:
        text = json.dumps(data, ensure_ascii=False)
        if len(text) <= self.max_output_chars:
            return text
        data = dict(data)
        data["truncated"] = True
        data["content"] = str(data.get("content", ""))[: self.max_output_chars]
        text = json.dumps(data, ensure_ascii=False)
        return text[: self.max_output_chars] + "…"

    def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path)
        if not os.path.isdir(target):
            raise ValueError("path is not a directory")
        entries = []
        for name in sorted(os.listdir(target))[:200]:
            full = os.path.join(target, name)
            entries.append({"name": name, "type": "dir" if os.path.isdir(full) else "file"})
        return self._bounded_json({"path": self._relpath(target), "entries": entries})

    def read_file(self, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
        target = self._resolve(path)
        if not os.path.isfile(target):
            raise ValueError("path is not a file")
        if start_line is not None and int(start_line) < 1:
            raise ValueError("start_line must be >= 1")
        if end_line is not None and int(end_line) < 1:
            raise ValueError("end_line must be >= 1")
        if start_line is not None and end_line is not None and int(end_line) < int(start_line):
            raise ValueError("end_line must be >= start_line")
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            if start_line is None and end_line is None:
                content = f.read(self.max_file_chars + 1)
                truncated = len(content) > self.max_file_chars
                content = content[: self.max_file_chars]
                start, end = 1, content.count("\n") + 1
            else:
                start = int(start_line or 1)
                end = int(end_line or (start + 199))
                lines = []
                for idx, line in enumerate(f, start=1):
                    if idx < start:
                        continue
                    if idx > end:
                        break
                    lines.append(line)
                content = "".join(lines)
                truncated = False
        return self._bounded_json({
            "path": self._relpath(target),
            "start_line": start,
            "end_line": end,
            "content": content,
            "truncated": truncated,
        })

    def _iter_candidate_files(self, path_glob: Optional[str]) -> Iterable[str]:
        glob_pat = (path_glob or "**/*").replace(os.sep, "/")
        if os.path.isabs(glob_pat) or ".." in glob_pat.split("/"):
            raise ValueError("path_glob must stay inside the repository")
        for base, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "venv", "node_modules"}]
            for name in files:
                full = os.path.join(base, name)
                rel = self._relpath(full)
                if fnmatch.fnmatch(rel, glob_pat) or fnmatch.fnmatch("/" + rel, glob_pat):
                    yield full

    def grep(self, pattern: str, path_glob: Optional[str] = None, max_results: Optional[int] = None) -> str:
        if not pattern:
            raise ValueError("pattern is required")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        limit = min(max(1, int(max_results or 50)), self.max_grep_results)
        matches: List[Dict[str, Any]] = []
        scanned = 0
        for full in self._iter_candidate_files(path_glob):
            scanned += 1
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, start=1):
                        if rx.search(line):
                            matches.append({
                                "path": self._relpath(full),
                                "line": line_no,
                                "text": line.rstrip("\n")[:500],
                            })
                            if len(matches) >= limit:
                                return self._bounded_json({"matches": matches, "truncated": True, "files_scanned": scanned})
            except OSError:
                continue
        return self._bounded_json({"matches": matches, "truncated": False, "files_scanned": scanned})

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        try:
            if name == "grep":
                return self.grep(args.get("pattern"), args.get("path_glob"), args.get("max_results"))
            if name == "read_file":
                return self.read_file(args.get("path"), args.get("start_line"), args.get("end_line"))
            if name == "list_dir":
                return self.list_dir(args.get("path", "."))
            raise ValueError(f"unknown repo tool: {name}")
        except Exception as e:  # noqa: BLE001 - tool errors are returned to the model
            return self._bounded_json({"error": str(e), "tool": name})


def _tool_name_and_args(tool_call: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    fn = tool_call.get("function") or {}
    name = fn.get("name") or tool_call.get("name") or ""
    raw_args = fn.get("arguments") or tool_call.get("arguments") or {}
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
    out = [dict(m) for m in messages]
    for msg in out:
        if msg.get("role") == "system":
            msg["content"] = (msg.get("content") or "") + "\n\n" + _AGENTIC_FIX_INSTRUCTIONS
            return out
    return [{"role": "system", "content": _AGENTIC_FIX_INSTRUCTIONS}] + out


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        return result.get("text") or ""
    return str(result or "")


def run_agentic_fix(call_llm_fn: Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], Any],
                     messages: List[Dict[str, Any]], repo_checkout_path: str, *, max_iterations: int = 8,
                     max_result_chars: int = 12000, logger: Any = None) -> str:
    """Run a bounded read-only tool loop and return the model's final text."""
    executor = RepoToolExecutor(repo_checkout_path, max_output_chars=max_result_chars)
    loop_messages = _augment_messages(messages)
    max_iterations = max(1, int(max_iterations or 8))

    for _ in range(max_iterations):
        result = call_llm_fn(loop_messages, REPO_TOOLS)
        if not isinstance(result, dict):
            return str(result or "")
        tool_calls = result.get("tool_calls") or []
        text = result.get("text") or ""
        if not tool_calls:
            return text
        loop_messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
        for tc in tool_calls:
            name, args = _tool_name_and_args(tc)
            content = executor.execute(name, args)
            if len(content) > max_result_chars:
                content = content[:max_result_chars] + "…"
            loop_messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name or "repo_tool",
                "name": name or "repo_tool",
                "content": content,
            })

    if logger:
        logger.warning("agentic fix loop reached iteration cap; forcing final edit response")
    forced = loop_messages + [{"role": "user", "content": "Tool iteration limit reached. Return ONLY the final JSON edits object now."}]
    return _result_text(call_llm_fn(forced, None))
