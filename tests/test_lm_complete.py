"""lm_complete tries the LM hub only for mapped, prompt-only work and falls through on any miss."""
import ast
import sys
import types
from pathlib import Path


def _load(client):
    """Exec lm_complete and its map from llm_client.py with a fake hub client."""
    src = Path(__file__).resolve().parents[1].joinpath("llm_client.py").read_text(encoding="utf-8")
    segs = [ast.get_source_segment(src, n) for n in ast.parse(src).body
            if (isinstance(n, ast.FunctionDef) and n.name in ("lm_complete", "_lm_hub_request"))
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in
                ("LM_COMPLETE_TIMEOUT_S", "DEFAULT_LM_TASKS"))]
    workers = types.ModuleType("workers")
    workers._get_hub_agent_client = lambda: client
    sys.modules["workers"] = workers
    ns = {"logger": types.SimpleNamespace(info=lambda *a, **k: None)}
    exec("\n".join(segs), ns)
    return ns["lm_complete"]


class _Client:
    """Records requests and returns a canned reply."""
    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def request_sync(self, kind, data, timeout):
        """Return the canned reply."""
        self.sent.append((kind, data))
        return self.reply


def test_success_uses_mapped_task_and_joins_system_prompt():
    client = _Client({"status": "SUCCESS", "text": " [] "})
    assert _load(client)("identify_files", "p", "sys", {}) == " [] "
    assert client.sent == [("LLM_COMPLETE", {"task": "scout", "prompt": "sys\n\np"})]


def test_unmapped_disabled_and_failed_calls_fall_through():
    client = _Client({"status": "ERROR", "code": "no_capacity"})
    fn = _load(client)
    assert fn("identify_files", "p", "", {}) is None
    assert fn("chat", "p", "", {}) is None and len(client.sent) == 1
    assert fn("identify_files", "p", "", {"lm_llm_tasks": {}}) is None and len(client.sent) == 1
    assert _load(None)("identify_files", "p", "", {}) is None
