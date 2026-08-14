#!/usr/bin/env python3
"""Self-test for chat.py's requirements= conversion (LLM Selection Redesign,
Phase 5, call sites #8-11 — chat.py:733/790/807/813).

Run:  python3 test_chat_requirements.py

chat.py cannot be imported directly (it transitively pulls in main.py's
circular import chain), so this extracts _run_chat_reply_simple and
run_chat_reply via ast and execs them with stubbed dependencies — the
established convention in this repo (see test_dedup_llm_adjudication.py).

Covers, for each of the 4 call sites, that call_llm is invoked with
requirements= (not force_provider=_chat_force_provider(config)=):
1. site #8 (chat.py:733, _run_chat_reply_simple): complexity="small",
   latency_sensitive=True.
2. site #9 (chat.py:790, run_chat_reply's no-GitHub-token/no-tools branch):
   complexity="small", latency_sensitive=True.
3. site #10 (chat.py:807, run_chat_reply's tool-calling loop):
   complexity="medium", needs_tools=True, latency_sensitive=True.
4. site #11 (chat.py:813, run_chat_reply's degrade-to-index-only fallback
   when the tool-capable call raises): complexity="small",
   latency_sensitive=True.
"""
import ast
import json


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load_ns(func_names, extra_ns):
    src = open("chat.py").read()
    tree = ast.parse(src)
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in func_names:
            segs.append(ast.get_source_segment(src, node))
    ns = {"json": json, "logger": _NoLog(), "traceback": __import__("traceback")}
    ns.update(extra_ns)
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True

    # ---- site #8: chat.py:733 (_run_chat_reply_simple) ----
    captured1 = {}

    def _capturing_call_llm1(prompt, **kwargs):
        captured1["kwargs"] = kwargs
        return "a reply"

    ns1 = _load_ns(
        {"_run_chat_reply_simple"},
        {
            "load_chats": lambda: {},
            "get_conversation": lambda store, chat_id: {"messages": [{"role": "user", "content": "hi"}]},
            "call_llm": _capturing_call_llm1,
            "append_chat_message": lambda chat_id, msg: None,
            "_finalize_chat_stream": lambda chat_id, reply: None,
            "_set_chat_stream_error": lambda chat_id, msg: None,
            "datetime": __import__("datetime").datetime,
        },
    )
    ns1["_run_chat_reply_simple"]("chat1", {"CHAT_HISTORY_WINDOW": 20})
    kw1 = captured1.get("kwargs", {})
    ok &= _check("site #8 (:733): call_llm invoked with requirements= (not force_provider=)",
                 "requirements" in kw1 and "force_provider" not in kw1)
    reqs1 = kw1.get("requirements")
    ok &= _check("site #8 (:733): complexity='small', latency_sensitive=True",
                 reqs1 is not None and reqs1.complexity == "small" and reqs1.latency_sensitive is True)

    # ---- run_chat_reply: sites #9/#10/#11 ----
    def _make_run_chat_reply_ns(call_llm_fn, github_token):
        return _load_ns(
            {"run_chat_reply", "run_agent_loop", "_run_chat_reply_simple"},
            {
                "load_config": lambda: {"CHAT_TOOLS_ENABLED": True, "CHAT_HISTORY_WINDOW": 20,
                                        "GITHUB_TOKEN": github_token},
                "load_chats": lambda: {},
                "get_conversation": lambda store, chat_id: {"messages": [{"role": "user", "content": "hi"}]},
                "os": __import__("os"),
                "Github": lambda token: object() if token else None,
                "build_chat_context_index": lambda config, gh=None: "(index)",
                "call_llm": call_llm_fn,
                "CHAT_TOOLS": [{"type": "function", "function": {"name": "noop"}}],
                "_set_chat_stream_status": lambda chat_id, msg: None,
                "_set_chat_stream_error": lambda chat_id, msg: None,
                "_finalize_chat_stream": lambda chat_id, reply: None,
                "append_chat_message": lambda chat_id, msg: None,
                "datetime": __import__("datetime").datetime,
                "update_task_state": lambda *a, **k: None,
                "_parse_text_tool_calls": lambda text: (text, []),
            },
        )

    # site #9 (:790): no GitHub token -> gh is None -> no-tools streaming branch.
    captured2 = {}

    def _capturing_call_llm2(prompt, **kwargs):
        captured2["kwargs"] = kwargs
        return "a plain reply"

    ns2 = _make_run_chat_reply_ns(_capturing_call_llm2, github_token=None)
    ns2["run_chat_reply"]("chat2")
    kw2 = captured2.get("kwargs", {})
    ok &= _check("site #9 (:790): call_llm invoked with requirements= (not force_provider=)",
                 "requirements" in kw2 and "force_provider" not in kw2)
    reqs2 = kw2.get("requirements")
    ok &= _check("site #9 (:790): complexity='small', latency_sensitive=True",
                 reqs2 is not None and reqs2.complexity == "small" and reqs2.latency_sensitive is True)

    # site #10 (:807) + site #11 (:813): GitHub token present -> tool loop branch.
    # First call (807, tools=...) raises -> triggers the degrade fallback (813).
    captured3 = {"calls": []}

    def _capturing_call_llm3(prompt, **kwargs):
        captured3["calls"].append(kwargs)
        if "tools" in kwargs:
            raise RuntimeError("tool-calling not supported by this provider")
        return "degraded reply"

    ns3 = _make_run_chat_reply_ns(_capturing_call_llm3, github_token="tok")
    ns3["run_chat_reply"]("chat3")
    calls3 = captured3["calls"]
    ok &= _check("site #10/#11: exactly 2 call_llm invocations (tool attempt + fallback)",
                 len(calls3) == 2)
    if len(calls3) == 2:
        kw_tool, kw_fallback = calls3[0], calls3[1]
        ok &= _check("site #10 (:807): call_llm invoked with requirements= (not force_provider=)",
                     "requirements" in kw_tool and "force_provider" not in kw_tool)
        reqs_tool = kw_tool.get("requirements")
        ok &= _check("site #10 (:807): complexity='medium', needs_tools=True, latency_sensitive=True",
                     reqs_tool is not None and reqs_tool.complexity == "medium"
                     and reqs_tool.needs_tools is True and reqs_tool.latency_sensitive is True)

        ok &= _check("site #11 (:813): call_llm invoked with requirements= (not force_provider=)",
                     "requirements" in kw_fallback and "force_provider" not in kw_fallback)
        reqs_fallback = kw_fallback.get("requirements")
        ok &= _check("site #11 (:813): complexity='small', latency_sensitive=True",
                     reqs_fallback is not None and reqs_fallback.complexity == "small"
                     and reqs_fallback.latency_sensitive is True)

    # ---- run_agent_loop: iteration-cap forces a final tools-free answer ----
    # A model that keeps calling tools forever must not yield an empty reply;
    # at the cap the loop makes one tools-free call to force a written answer.
    ns_loop = _load_ns(
        {"run_agent_loop"},
        {
            "call_llm": None,  # replaced below
            "CHAT_TOOLS": [{"type": "function", "function": {"name": "noop"}}],
            "CHAT_TOOL_EXECUTORS": {"noop": lambda gh, config, args: {"ok": 1}},
            "_sanitize_tool_result": lambda out, config: out,
            "_trunc": lambda x, n=300: str(x)[:n],
            "_parse_text_tool_calls": lambda text: (text, []),
        },
    )

    def _always_tools_call_llm(prompt, **kwargs):
        if kwargs.get("tools"):
            return {"text": "", "tool_calls": [{"id": "c1", "function": {"name": "noop", "arguments": "{}"}}]}
        return "FINAL ANSWER"

    ns_loop["call_llm"] = _always_tools_call_llm
    forced_text = ns_loop["run_agent_loop"](
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        {}, object(), task_id="t", max_iter=2)
    ok &= _check("run_agent_loop: iteration cap forces a non-empty final answer",
                 forced_text == "FINAL ANSWER")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running chat.py requirements= self-test...")
    import sys
    sys.exit(main())
