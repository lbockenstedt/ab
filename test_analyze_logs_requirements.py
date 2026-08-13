#!/usr/bin/env python3
"""Self-test for analyze_logs' caller-supplied requirements= param
(LLM Selection Redesign, Phase 5, call site #17 — llm_client.py's
analyze_logs, and its two callers routes.py/_run_log_analysis and
hub_agent.py/_handle_analyze_logs).

Run:  python3 test_analyze_logs_requirements.py

llm_client.py, routes.py, and hub_agent.py cannot be imported directly (all
transitively pull in main.py's circular import chain), so this extracts the
specific functions under test via ast and execs them with stubbed
dependencies — the established convention in this repo (see
test_dedup_llm_adjudication.py).

Covers:
1. analyze_logs itself: passes requirements= (not task_kind=) through to
   call_llm; when the caller doesn't supply one, builds a sane default
   (complexity="small", latency_sensitive=False) rather than raising.
2. routes.py's _run_log_analysis: builds requirements with
   latency_sensitive=True (a human is watching the Log Analysis panel).
3. hub_agent.py's _handle_analyze_logs: builds requirements with
   latency_sensitive=False (runs on a background executor thread for the LM
   hub, no one blocking on the reply).
"""
import ast


def _load_analyze_logs_ns(call_llm_stub):
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "analyze_logs")
    seg = ast.get_source_segment(src, node)
    max_chars_node = next(n for n in tree.body if isinstance(n, ast.Assign)
                          and any(getattr(t, "id", None) == "_LOG_ANALYSIS_MAX_CHARS" for t in n.targets))
    prompt_node = next(n for n in tree.body if isinstance(n, ast.Assign)
                       and any(getattr(t, "id", None) == "LOG_ANALYSIS_SYSTEM_PROMPT" for t in n.targets))

    import model_selection
    ns = {
        "call_llm": call_llm_stub,
        "model_selection": model_selection,
    }
    exec(ast.get_source_segment(src, max_chars_node), ns)
    exec(ast.get_source_segment(src, prompt_node), ns)
    exec(seg, ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True

    # ---- 1. analyze_logs itself ----
    captured = {}

    def _capturing_call_llm(prompt, **kwargs):
        captured["kwargs"] = kwargs
        return "VERDICT: none\nAll good."

    ns = _load_analyze_logs_ns(_capturing_call_llm)
    import model_selection
    custom_reqs = model_selection.LlmRequirements(complexity="small", latency_sensitive=True)
    ns["analyze_logs"]("some log text", title="test logs", requirements=custom_reqs)
    kw = captured["kwargs"]
    ok &= _check("analyze_logs is invoked with requirements= (not task_kind=)",
                 "requirements" in kw and "task_kind" not in kw)
    ok &= _check("a caller-supplied requirements object is passed through unmodified",
                 kw["requirements"] is custom_reqs)

    # No requirements supplied -> a sane default is built, not a crash.
    captured2 = {}

    def _capturing_call_llm2(prompt, **kwargs):
        captured2["kwargs"] = kwargs
        return "VERDICT: none\nAll good."

    ns2 = _load_analyze_logs_ns(_capturing_call_llm2)
    ns2["analyze_logs"]("some log text", title="test logs")
    kw2 = captured2["kwargs"]
    default_reqs = kw2.get("requirements")
    ok &= _check("no requirements= supplied -> a default LlmRequirements is built",
                 default_reqs is not None and default_reqs.complexity == "small"
                 and default_reqs.latency_sensitive is False)

    # ---- 2. routes.py's _run_log_analysis: latency_sensitive=True ----
    src_routes = open("routes.py").read()
    tree_routes = ast.parse(src_routes)
    node_routes = next(n for n in tree_routes.body
                       if isinstance(n, ast.FunctionDef) and n.name == "_run_log_analysis")
    seg_routes = ast.get_source_segment(src_routes, node_routes)

    captured_routes = {}

    def _fake_analyze_logs(log_text, title=None, task_id=None, requirements=None):
        captured_routes["requirements"] = requirements
        return "VERDICT: none\nfine"

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns_routes = {
        "_collect_logs_for_analysis": lambda source, window_minutes=None: ("t", "log text here"),
        "update_task_state": lambda **k: None,
        "_LOG_ANALYSIS_TASK": "log_analysis_task",
        "state": {},
        "datetime": __import__("datetime").datetime,
        "logger": _NoLog(),
    }
    import sys, types
    fake_main = types.ModuleType("main")
    fake_main.analyze_logs = _fake_analyze_logs
    fake_main.parse_log_verdict = lambda raw: ("none", raw)
    fake_main.is_llm_cooldown_error = lambda e: False
    sys.modules["main"] = fake_main
    try:
        exec(seg_routes, ns_routes)
        ns_routes["_run_log_analysis"]("self")
    finally:
        del sys.modules["main"]

    reqs_routes = captured_routes.get("requirements")
    ok &= _check("routes.py builds requirements with latency_sensitive=True",
                 reqs_routes is not None and reqs_routes.latency_sensitive is True
                 and reqs_routes.complexity == "small")

    # ---- 3. hub_agent.py's _handle_analyze_logs: latency_sensitive=False ----
    src_hub = open("hub_agent.py").read()
    tree_hub = ast.parse(src_hub)
    node_hub = None
    for n in ast.walk(tree_hub):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_analyze_logs":
            node_hub = n
            break
    assert node_hub is not None, "_handle_analyze_logs not found in hub_agent.py"
    seg_hub = ast.get_source_segment(src_hub, node_hub)

    captured_hub = {}

    def _fake_analyze_logs2(log_text, title, requirements=None):
        captured_hub["requirements"] = requirements
        return "VERDICT: none\nfine"

    fake_llm_client = types.ModuleType("llm_client")
    fake_llm_client.analyze_logs = _fake_analyze_logs2
    fake_llm_client.parse_log_verdict = lambda raw: ("none", raw)
    fake_llm_client.is_llm_cooldown_error = lambda e: False
    sys.modules["llm_client"] = fake_llm_client

    # Build a bare object with just enough of the class shape (self.signer/_ws)
    # to exercise the function as an unbound method.
    class _FakeSelf:
        spoke_id = "spoke1"
        signer = None
        _ws = None
        logger = _NoLog()

    ns_hub = {"uuid": __import__("uuid"), "time": __import__("time"), "logger": _NoLog()}
    exec(seg_hub, ns_hub)
    import asyncio

    async def _run():
        await ns_hub["_handle_analyze_logs"](_FakeSelf(), {"header": {}}, {"title": "t", "logs": "some logs"})

    try:
        asyncio.run(_run())
    finally:
        del sys.modules["llm_client"]

    reqs_hub = captured_hub.get("requirements")
    ok &= _check("hub_agent.py builds requirements with latency_sensitive=False",
                 reqs_hub is not None and reqs_hub.latency_sensitive is False
                 and reqs_hub.complexity == "small")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running analyze_logs requirements= self-test...")
    import sys
    sys.exit(main())
