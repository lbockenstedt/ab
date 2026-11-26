"""lm_complete tries the LM hub only for mapped, prompt-only work and falls through on any miss."""
import ast
import sys
import time as _time
import types
from pathlib import Path


def _load(client):
    """Exec lm_complete and its map from llm_client.py with a fake hub client."""
    src = Path(__file__).resolve().parents[1].joinpath("llm_client.py").read_text(encoding="utf-8")
    segs = [ast.get_source_segment(src, n) for n in ast.parse(src).body
            if (isinstance(n, ast.FunctionDef) and n.name in ("lm_complete", "_lm_hub_request", "_lm_try", "lm_pool_only", "_fit_lm_prompt"))
            or (isinstance(n, ast.ClassDef) and n.name == "LmPoolUnavailable")
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in
                ("LM_COMPLETE_TIMEOUT_S", "DEFAULT_LM_TASKS", "LM_PROMPT_MAX_BYTES"))]
    workers = types.ModuleType("workers")
    workers._get_hub_agent_client = lambda: client
    sys.modules["workers"] = workers
    ns = {"logger": types.SimpleNamespace(info=lambda *a, **k: None),
          "time": types.SimpleNamespace(monotonic=_time.monotonic, sleep=lambda s: None)}
    exec("\n".join(segs), ns)
    return ns["lm_complete"], ns


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
    assert _load(client)[0]("identify_files", "p", "sys", {}) == " [] "
    assert client.sent == [("LLM_COMPLETE", {"task": "scout", "prompt": "sys\n\np"})]


def test_unmapped_disabled_and_failed_calls_fall_through_when_pool_only_is_off():
    client = _Client({"status": "ERROR", "code": "no_capacity"})
    fn = _load(client)[0]
    off = {"lm_pool_only": False}
    assert fn("identify_files", "p", "", off) is None
    assert fn("chat", "p", "", off) is None and len(client.sent) == 1
    assert fn("identify_files", "p", "", {"lm_llm_tasks": {}}) is None and len(client.sent) == 1
    assert _load(None)[0]("identify_files", "p", "", off) is None


def test_pool_only_waits_then_raises_and_never_returns_none():
    import pytest
    client = _Client({"status": "ERROR", "code": "no_capacity"})
    fn, ns = _load(client)
    with pytest.raises(ns["LmPoolUnavailable"]):
        fn("identify_files", "p", "", {"lm_pool_wait_s": 0})
    fn_none, ns_none = _load(None)
    with pytest.raises(ns_none["LmPoolUnavailable"]):
        fn_none("identify_files", "p", "", {"lm_pool_wait_s": 0})
    # A misconfigured hub raises at once instead of waiting.
    cfg = _Client({"status": "ERROR", "code": "not_configured"})
    fn_cfg, ns_cfg = _load(cfg)
    with pytest.raises(ns_cfg["LmPoolUnavailable"]):
        fn_cfg("identify_files", "p", "", {})
    assert len(cfg.sent) == 1


def test_oversized_prompt_is_trimmed_to_the_hub_limit():
    client = _Client({"status": "SUCCESS", "text": "ok"})
    _load(client)[0]("identify_files", "H" + "x" * 400000 + "T", None, {})
    sent = client.sent[0][1]["prompt"]
    assert len(sent.encode()) < 192 * 1024 and sent.startswith("H") and sent.endswith("T")


def test_busy_pool_is_retried_on_the_poll_interval_until_a_worker_frees():
    """A busy pool is polled every lm_pool_poll_s (default 15s), not every minute, and the job
    runs as soon as a worker answers."""
    replies = [{"status": "ERROR", "code": "no_capacity"}] * 2 + [{"status": "SUCCESS", "text": "done"}]

    class _Seq(_Client):
        def request_sync(self, kind, data, timeout):
            self.sent.append((kind, data))
            return replies[len(self.sent) - 1]

    for cfg, wait in (({}, 15.0), ({"lm_pool_poll_s": 5}, 5.0)):
        client = _Seq(None)
        fn, ns = _load(client)
        sleeps = []
        ns["time"].sleep = sleeps.append
        assert fn("log_review", "p", "", cfg) == "done"
        assert sleeps == [wait, wait] and len(client.sent) == 3
