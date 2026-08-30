#!/usr/bin/env python3
"""Self-test for the Phase 3 LLM instrumentation wiring: usage_out plumbing
through _call_provider's dispatch, and _call_provider_timed's latency/tok-s
capture + llm_perf recording.

Run:  python3 ab/test_llm_client_instrumentation.py

llm_client.py imports `main` (app-init side effects — see
test_skills_loader.py's docstring), so this extracts the relevant pieces by
source via ast and execs them with stubbed dependencies, following
test_llm_client_build_profile_threading.py's established pattern (that file
pins profile= plumbing the same way; this one pins usage_out= plumbing and
the new perf-recording wrapper).
"""
import ast
import sys
import threading
import time
from datetime import datetime

import llm_perf


def _load_dispatch_ns():
    """Extracts _call_provider, exactly like
    test_llm_client_build_profile_threading.py, but the stub _request_*
    functions record usage_out instead of profile."""
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_call_provider":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "_call_provider not found in llm_client.py"

    calls = []

    def _make_stub(name):
        def _stub(*a, **k):
            calls.append({"provider_fn": name, "usage_out": k.get("usage_out")})
            if k.get("usage_out") is not None:
                k["usage_out"]["output_tokens"] = 42
                k["usage_out"]["source"] = "api"
            return "stub result"
        return _stub

    ns = {
        "_request_claude_cli": _make_stub("claude_cli"),
        "_request_copilot": _make_stub("copilot"),
        "_request_anthropic": _make_stub("anthropic"),
        "_request_google": _make_stub("google"),
        "_request_ollama": _make_stub("ollama"),
        "_request_openai": _make_stub("openai"),
        "_is_copilot": lambda p: p == "copilot",
        "_is_ollama": lambda p: p.startswith("ollama"),
        "_is_lmstudio": lambda p: p == "lmstudio",
        "_normalize_lmstudio_url": lambda u: u,
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1", "OPENROUTER_HEADERS": {},
    }
    exec(seg, ns)
    ns["_calls"] = calls
    return ns


class _FakeConfigStore:
    def __init__(self, path):
        self.LLM_PERF_FILE = path


class _StubLogger:
    """No-op logger for the extracted timeout branches (they log on expiry)."""

    def _noop(self, *a, **k):
        pass

    debug = info = warning = error = exception = _noop


def _load_timed_ns(perf_path):
    """Extracts the whole perf-telemetry block added in Phase 3
    (_get_llm_perf_store/get_llm_perf_snapshot/_model_key/
    _call_provider_timed + their module-level state) as one segment — they're
    mutually dependent (closures over the same globals), so they must be
    exec'd together, unlike the single-function extractions elsewhere."""
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    names = {"_LLM_PERF_STORE", "_LLM_PERF_LOCK", "_llm_perf_last_save", "_LLM_PERF_SAVE_INTERVAL",
              # per-entry health globals (_call_provider_timed records against these
              # on every call — see llm_client.get_llm_entry_health's docstring)
              "_ENTRY_HEALTH_LOCK", "_ENTRY_HEALTH", "_ENTRY_UNHEALTHY_THRESHOLD",
              "_ENTRY_UNSUPPORTED_RETRY_SECONDS", "_UNSUPPORTED_MODEL_MARKERS"}
    fn_names = {"_get_llm_perf_store", "get_llm_perf_snapshot", "_model_key", "_call_provider_timed",
                "_call_provider_wrapper", "_record_llm_success", "_record_llm_failure",
                "_is_unsupported_model_error"}
    segs = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in names for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in fn_names:
            segs.append(ast.get_source_segment(src, node))
    assert len(segs) == 9 + 8, f"expected 9 globals + 8 functions, found {len(segs)} segments"

    calls = {"provider_calls": []}

    def _call_provider_stub(provider, model, api_key, base_url, messages, tools, effective_stream, task_id, config,
                            usage_out=None, **kwargs):
        calls["provider_calls"].append((provider, model, base_url))
        if calls.get("raise_exc"):
            raise calls["raise_exc"]
        if usage_out is not None:
            usage_out.update(calls.get("next_usage", {}))
        time.sleep(calls.get("sleep_s", 0))
        return "ok"

    ns = {
        "llm_perf": llm_perf, "config_store": _FakeConfigStore(perf_path),
        "threading": threading, "time": time, "datetime": datetime,
        "_call_provider": _call_provider_stub,
        # _call_provider_timed wraps the call in a timeout. Pin the threading
        # fallback (the Python 3.9 branch) rather than contextlib.timeout: it is
        # the branch that spawns a Thread and marshals args/kwargs, so it is the
        # one worth exercising here. `logger` is stubbed because the timeout
        # branches log on expiry.
        "_TIMEOUT_AVAILABLE": False,
        "timeout": None,
        "logger": _StubLogger(),
    }
    exec("\n\n".join(segs), ns)
    ns["_test_calls"] = calls
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running ab llm_client instrumentation self-test...")
    ok = True

    # --- usage_out reaches every provider branch of _call_provider's dispatch -

    ns = _load_dispatch_ns()
    cases = [
        ("claude_cli", "claude_cli"), ("copilot", "copilot"), ("anthropic", "anthropic"),
        ("google", "google"), ("ollama", "ollama"), ("openai", "openai"),
        ("groq", "openai"), ("openrouter", "openai"), ("lmstudio", "openai"),
    ]
    for provider, expected_fn in cases:
        ns["_calls"].clear()
        usage_out = {}
        ns["_call_provider"](provider, "m", "k", "", [], None, False, None, {}, usage_out=usage_out)
        ok &= _check(f"usage_out reaches _request_{expected_fn} when provider={provider!r}",
                    len(ns["_calls"]) == 1 and ns["_calls"][0]["usage_out"] is usage_out)

    # --- _call_provider_timed: latency + tps recorded on success ---------------

    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        perf_path = os.path.join(tmpdir, "llm_perf.json")
        tns = _load_timed_ns(perf_path)

        tns["_test_calls"]["next_usage"] = {"output_tokens": 100, "source": "api"}
        tns["_test_calls"]["sleep_s"] = 0.02
        result = tns["_call_provider_timed"]("anthropic", "claude-sonnet-5", "key", "https://api.anthropic.com",
                                             [], None, False, None, {})
        ok &= _check("_call_provider_timed returns the wrapped call's result unchanged", result == "ok")

        snap = tns["get_llm_perf_snapshot"]()
        key = ("anthropic", "https://api.anthropic.com", "claude-sonnet-5")
        ok &= _check("a perf sample lands under the exact 3-tuple ModelKey", key in snap)
        ok &= _check("n increments to 1 after one successful call", snap[key]["n"] == 1)
        ok &= _check("latency_ms is a positive wall-clock figure covering the (sleeping) stub call",
                    snap[key]["latency_ms"] is not None and snap[key]["latency_ms"] >= 15)
        ok &= _check("tps is derived from output_tokens / wall latency when no gen_duration_ms is given",
                    snap[key]["tps"] is not None and snap[key]["tps"] > 0)
        ok &= _check("a successful call also records healthy entry health",
                    key in tns["_ENTRY_HEALTH"] and tns["_ENTRY_HEALTH"][key]["consecutive_failures"] == 0)

        # --- server-measured gen_duration_ms is preferred over wall latency for tps

        tns["_test_calls"]["next_usage"] = {"output_tokens": 100, "gen_duration_ms": 50.0, "source": "server"}
        tns["_test_calls"]["sleep_s"] = 0.5  # wall time >> server-measured generation time
        tns["_call_provider_timed"]("ollama", "qwen2.5-coder:14b", "", "http://localhost:11434",
                                    [], None, False, None, {})
        snap2 = tns["get_llm_perf_snapshot"]()
        key2 = ("ollama", "http://localhost:11434", "qwen2.5-coder:14b")
        # 100 tokens / 0.05s = 2000 tok/s if gen_duration_ms wins; 100/0.5s = 200 if wall-clock wins.
        ok &= _check("tps prefers the server-measured gen_duration_ms over inflated wall-clock latency",
                    snap2[key2]["tps"] is not None and snap2[key2]["tps"] > 1000)

        # --- a failed call records no PERF sample but DOES record health -------
        # (the health/perf split: perf is a ranking signal with no meaning for a
        # failed call; health is exactly the opposite — see get_llm_entry_health)

        tns["_test_calls"]["raise_exc"] = RuntimeError("boom")
        threw = False
        try:
            tns["_call_provider_timed"]("openai", "gpt-9", "key", "", [], None, False, None, {})
        except RuntimeError:
            threw = True
        ok &= _check("_call_provider_timed re-raises the wrapped call's exception unchanged", threw)
        snap3 = tns["get_llm_perf_snapshot"]()
        ok &= _check("a failed call adds no perf sample for a never-before-seen model",
                    ("openai", "", "gpt-9") not in snap3)
        ok &= _check("but the failure IS recorded against entry health",
                    ("openai", "", "gpt-9") in tns["_ENTRY_HEALTH"]
                    and tns["_ENTRY_HEALTH"][("openai", "", "gpt-9")]["consecutive_failures"] == 1
                    and "boom" in tns["_ENTRY_HEALTH"][("openai", "", "gpt-9")]["last_error"])

        # --- distinct base_urls for the same (provider, model) stay isolated ---

        tns["_test_calls"]["raise_exc"] = None
        tns["_test_calls"]["next_usage"] = {"output_tokens": 10, "source": "server"}
        tns["_test_calls"]["sleep_s"] = 0
        tns["_call_provider_timed"]("ollama", "qwen2.5-coder:14b", "", "http://gpu-box:11434",
                                    [], None, False, None, {})
        snap4 = tns["get_llm_perf_snapshot"]()
        ok &= _check("a different base_url for the same provider+model is a DIFFERENT ModelKey "
                    "(a CPU box and a GPU box never average into one meaningless figure)",
                    ("ollama", "http://gpu-box:11434", "qwen2.5-coder:14b") in snap4
                    and snap4[("ollama", "http://localhost:11434", "qwen2.5-coder:14b")]["n"] == 1)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
