import ast
import atexit
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import model_registry
import model_selection
import repo_tools


#: Scratch roots are created under a per-run temp dir and removed at exit.
#: These used to be written to ``.test_agentic_repo_tools/`` in the repo root,
#: where nothing cleaned them up and nothing ignored them -- so a ``git add -A``
#: after a test run committed four scratch files, which were then promoted all
#: the way to qa (flagged on ab#267).
_SCRATCH_ROOT = tempfile.mkdtemp(prefix="ab-agentic-repo-tools-")
atexit.register(shutil.rmtree, _SCRATCH_ROOT, True)


def _scratch(name):
    p = Path(_SCRATCH_ROOT) / name
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    return p


def test_repo_tool_sandbox_rejects_path_escape():
    root = _scratch("sandbox") / "repo"
    root.mkdir()
    (root / "inside.txt").write_text("inside", encoding="utf-8")
    (root.parent / "outside.txt").write_text("outside", encoding="utf-8")
    ex = repo_tools.RepoToolExecutor(str(root))

    try:
        ex.read_file("../outside.txt")
    except ValueError as e:
        assert "escapes" in str(e)
    else:
        raise AssertionError("path traversal was not rejected")

    assert "absolute paths" in ex.execute("read_file", {"path": str((root / "inside.txt").resolve())})


def test_agentic_fix_loop_executes_tool_call_round_trip():
    root = _scratch("roundtrip")
    (root / "app.py").write_text("def target():\n    return 'NEEDLE'\n", encoding="utf-8")
    calls = []
    final = {"confidence": 0.9, "edits": [{"file": "app.py", "search": "return 'NEEDLE'", "replace": "return 'fixed'"}]}

    def fake_llm(messages, tools):
        calls.append((messages, tools))
        if len(calls) == 1:
            assert tools == repo_tools.REPO_TOOLS
            return {"text": "", "tool_calls": [{
                "id": "call_1",
                "function": {"name": "grep", "arguments": json.dumps({"pattern": "NEEDLE", "path_glob": "*.py"})},
            }]}
        assert messages[-1]["role"] == "tool"
        assert "app.py" in messages[-1]["content"]
        assert "NEEDLE" in messages[-1]["content"]
        return {"text": json.dumps(final), "tool_calls": None}

    out = repo_tools.run_agentic_fix(
        fake_llm,
        [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Fix NEEDLE."}],
        str(root),
    )

    assert json.loads(out) == final
    assert len(calls) == 2
    assert calls[1][0][0]["role"] == "system"
    assert "read-only repository tools" in calls[1][0][0]["content"]


def _load_llm_ns():
    src = Path("llm_client.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    want_funcs = {
        "_endpoint_key", "_cb_trip", "_cb_remaining", "_model_lock",
        "_iter_configured_endpoints", "_enumerate_candidates", "_configured_entries",
        "_try_candidate", "_call_llm_with_requirements", "_is_tool_unsupported_400",
        "_model_key", "get_llm_perf_snapshot", "_get_llm_perf_store",
        "_provider_configured", "_provider_is_nokey", "_is_ollama", "_is_ollama_cloud", "_is_lmstudio",
        "_routed_model_dead", "_get_category_semaphore",
        "_record_llm_failure", "_record_llm_success", "_entry_is_unhealthy",
        "_is_unsupported_model_error", "_min_capability_rank", "_apply_capability_floor",
    }
    want_assign = {
        "_ALL_SLOTS", "_CODE_SLOTS", "_LOG_SLOTS", "_REVIEW_SLOTS", "_TOOL_400_MARKERS",
        "_UNSUPPORTED_MODEL_MARKERS", "_ENTRY_UNSUPPORTED_RETRY_SECONDS",
        "_MIN_CAPABILITY_RANK_DEFAULT",
        "_ENDPOINT_CB_LOCK", "_ENDPOINT_CREDIT_CB", "_MODEL_RATE_CB",
        "_MODEL_LOCKS_LOCK", "_MODEL_LOCKS",
        "_LLM_PERF_STORE", "_LLM_PERF_LOCK",
        "_CATEGORY_SEMAPHORES", "_CATEGORY_SEM_LOCK",
        "_CREDIT_COOLDOWN_SECONDS", "_RATELIMIT_COOLDOWN_SECONDS",
        "_ROUTED_404", "_ROUTED_404_LOCK",
        "OLLAMA_CLOUD_PROVIDER", "OLLAMA_CLOUD_BASE_URL",
        "_ENTRY_HEALTH_LOCK", "_ENTRY_HEALTH", "_ENTRY_UNHEALTHY_THRESHOLD",
        "_ENTRY_UNHEALTHY_RETRY_SECONDS",
    }
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in want_assign:
                    segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") in want_assign:
            segs.append(ast.get_source_segment(src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    class LLMCreditExhausted(Exception):
        pass

    class LlmHumanEscalationNeeded(Exception):
        pass

    calls = {"provider_calls": []}

    def _call_provider_timed(provider, model, api_key, base_url, messages, tools, effective_stream, task_id,
                             config, **kwargs):
        calls["provider_calls"].append({"provider": provider, "model": model, "tools": tools, "messages": messages})
        if tools:
            return {"text": json.dumps({"confidence": 1, "edits": []}), "tool_calls": None}
        return "plain result"

    ns = {
        "threading": threading, "time": time, "os": os,
        "logger": _NoLog(), "datetime": __import__("datetime").datetime,
        "model_registry": model_registry, "model_selection": model_selection,
        "main": type("M", (), {"state": {}})(),
        "LLMCreditExhausted": LLMCreditExhausted,
        "LlmHumanEscalationNeeded": LlmHumanEscalationNeeded,
        "_call_provider_timed": _call_provider_timed,
        "llm_perf": type("FakeLlmPerf", (), {
            "snapshot": staticmethod(lambda store: {}),
            "load": staticmethod(lambda path: {}),
        })(),
        "config_store": type("FakeConfigStore", (), {"LLM_PERF_FILE": "local-test"})(),
        "load_config": lambda: {},
        "repo_tools": repo_tools,
    }
    exec("\n\n".join(segs), ns)
    ns["_test_calls"] = calls
    return ns


def _entry(id_, provider, model, api_key="k", base_url="", rpm=0, enabled=True):
    return {"id": id_, "provider": provider, "model": model, "api_key": api_key,
            "base_url": base_url, "rpm": rpm, "enabled": enabled}


def test_call_llm_requirements_path_does_not_send_tools_when_native_tools_disabled():
    ns = _load_llm_ns()
    cfg = {"min_capability_rank": 0, "llm_entries": [_entry("e1", "copilot", "gpt-4o")]}
    reqs = model_selection.LlmRequirements(complexity="small")

    out = ns["_call_llm_with_requirements"](
        reqs, "prompt", "system", None, None, False, "task", cfg,
        repo_checkout_path=str(_scratch("compat")), enable_native_tools=False,
    )

    assert out == "plain result"
    assert len(ns["_test_calls"]["provider_calls"]) == 1
    assert ns["_test_calls"]["provider_calls"][0]["tools"] is None


def test_call_llm_requirements_path_uses_repo_tools_when_native_tools_enabled():
    ns = _load_llm_ns()
    cfg = {"min_capability_rank": 0, "llm_entries": [_entry("e1", "copilot", "gpt-4o")], "FIX_AGENTIC_MAX_ITERATIONS": 2}
    reqs = model_selection.LlmRequirements(complexity="small")
    root = _scratch("enabled")
    (root / "x.py").write_text("VALUE = 1\n", encoding="utf-8")

    out = ns["_call_llm_with_requirements"](
        reqs, "prompt", "system", None, None, False, "task", cfg,
        repo_checkout_path=str(root), enable_native_tools=True,
    )

    assert json.loads(out) == {"confidence": 1, "edits": []}
    assert len(ns["_test_calls"]["provider_calls"]) == 1
    assert ns["_test_calls"]["provider_calls"][0]["tools"] == repo_tools.REPO_TOOLS
