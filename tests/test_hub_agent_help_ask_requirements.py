#!/usr/bin/env python3
"""Self-test for hub_agent.py's requirements= conversion (LLM Selection
Redesign, Phase 5, call site #9 -- hub_agent.py's HELP_ASK proxy inside
_handle_message).

Run:  python3 test_hub_agent_help_ask_requirements.py

hub_agent.py cannot be imported directly (it transitively pulls in main.py's
circular import chain), so this extracts _handle_message via ast and execs
it as a plain function bound onto a minimal fake "self" -- the established
convention in this repo (see test_dedup_llm_adjudication.py). The HELP_ASK
branch is reached directly (cmd_type dispatch is a flat if/return chain, and
_verify is stubbed to return True), so no other branch's dependencies need
stubbing.

Covers that the HELP_ASK proxy's call_llm invocation:
1. passes requirements= (not a bare unqualified call) with
   complexity="medium", latency_sensitive=True (a human is waiting on the
   hub's chat UI).
2. sets needs_tools=True when the hub supplies a caller tools list, and
   needs_tools=False when it doesn't -- tools are caller-supplied per the
   plan's call-site table, not a fixed capability of this site.
"""
import ast
import asyncio


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load_handle_message():
    src = open("hub_agent.py").read()
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_message")
    seg = ast.get_source_segment(src, node)

    import model_selection
    import time
    import uuid as uuid_mod

    ns = {
        "asyncio": asyncio,
        "uuid": uuid_mod,
        "time": time,
        "logger": _NoLog(),
        "encode_frame": lambda signer, reply: reply,
        "model_selection": model_selection,
    }
    exec(seg, ns)
    return ns["_handle_message"]


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(frame)


class _FakeSelf:
    def __init__(self):
        self._ws = _FakeWs()
        self.spoke_id = "test-spoke"
        self.signer = object()

    def _verify(self, msg):
        return True


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _run_help_ask(handle_message, call_llm_stub, tools):
    import sys
    fake_main = type(sys)("main")
    fake_main.call_llm = call_llm_stub
    sys.modules["main"] = fake_main
    try:
        fake_self = _FakeSelf()
        msg = {
            "header": {"message_id": "corr-1"},
            "payload": {
                "type": "HELP_ASK",
                "data": {
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": tools,
                    "system": "sys prompt",
                },
            },
        }
        asyncio.run(handle_message(fake_self, msg))
    finally:
        del sys.modules["main"]


def main():
    ok = True
    handle_message = _load_handle_message()

    # ---- 1. with tools supplied ----
    captured1 = {}

    def _capturing_call_llm1(prompt, **kwargs):
        captured1["kwargs"] = kwargs
        return {"text": "an answer", "tool_calls": []}

    _run_help_ask(handle_message, _capturing_call_llm1, tools=[{"type": "function", "function": {"name": "x"}}])
    kw1 = captured1.get("kwargs", {})
    ok &= _check("HELP_ASK: call_llm invoked with requirements=", "requirements" in kw1)
    reqs1 = kw1.get("requirements")
    ok &= _check("HELP_ASK: complexity='medium', latency_sensitive=True",
                 reqs1 is not None and reqs1.complexity == "medium" and reqs1.latency_sensitive is True)
    ok &= _check("HELP_ASK: needs_tools=True when hub supplies tools",
                 reqs1 is not None and reqs1.needs_tools is True)

    # ---- 2. with no tools supplied ----
    captured2 = {}

    def _capturing_call_llm2(prompt, **kwargs):
        captured2["kwargs"] = kwargs
        return {"text": "an answer", "tool_calls": []}

    _run_help_ask(handle_message, _capturing_call_llm2, tools=None)
    kw2 = captured2.get("kwargs", {})
    reqs2 = kw2.get("requirements")
    ok &= _check("HELP_ASK: needs_tools=False when hub supplies no tools",
                 reqs2 is not None and reqs2.needs_tools is False)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running hub_agent.py HELP_ASK requirements= self-test...")
    import sys
    sys.exit(main())
