#!/usr/bin/env python3
"""Self-test: over the in-flight cap, _dispatch_slow must SHED WITH A REPLY.

Run:  python3 ab/test_hub_agent_shed_reply.py

hub_agent caps concurrent slow handlers (_MAX_INFLIGHT_HANDLERS) so a burst of
ANALYZE_LOGS/HELP_ASK work cannot stall the WebSocket receive loop. Over the cap
it previously did a bare ``return`` and logged at ERROR, on the stated grounds
that "the hub will redeliver from its mailbox".

That reasoning does not hold for the command this actually fires on. The hub
sends HELP_ASK through ``request_response`` (core/src/routes/help_assistant.py,
console_llm_identify.py), and request_response ids are deliberately never placed
in ``mailbox.pending_ack`` (core/src/main.py, "request_response ids are never in
mailbox.pending_ack"). So nothing redelivered them: the drop was silent and the
hub-side waiter blocked for its entire timeout. To an operator that is a hung
Help assistant, not a busy one.

Logging it at ERROR also made the self-log scanner file an issue every time the
agent was merely busy -- ab#209, #183, #179, #13 and #12 are all this line.

Pins:
  1. under the cap, the message is dispatched normally and no reply is forged;
  2. over the cap, the handler is NOT run;
  3. over the cap, a COMMAND_RESULT with status FAILED is sent back, correlated
     to the inbound message_id so request_response resolves immediately;
  4. the reply names the cap so the cause is self-evident;
  5. it is logged at WARNING, not ERROR, so a busy agent stops filing issues;
  6. a failure to schedule the reply cannot propagate out of _dispatch_slow.
"""
import ast
import asyncio
import os
import sys


def _load():
    """_dispatch_slow only; hub_agent.py cannot be imported standalone."""
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_agent.py")
    src = open(src_path).read()
    tree = ast.parse(src)
    seg = None
    cap = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_dispatch_slow":
            seg = ast.get_source_segment(src, node)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_MAX_INFLIGHT_HANDLERS":
                    cap = ast.literal_eval(node.value)
    assert seg is not None, "_dispatch_slow not found"
    assert cap is not None, "_MAX_INFLIGHT_HANDLERS not found"
    return seg, cap, src


class _Log:
    def __init__(self):
        self.error = []
        self.warning = []

    def __getattr__(self, name):
        raise AttributeError(name)


class _Logger:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, *a, **k):
        self.errors.append(a[0] if a else "")

    def warning(self, *a, **k):
        self.warnings.append(a[0] if a else "")

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class _Agent:
    """Minimal stand-in exposing exactly what _dispatch_slow touches."""

    def __init__(self, inflight):
        self._inflight = set(inflight)
        self.acks = []
        self.handled = []
        self.ack_raises = False

    async def _ack(self, msg, status="SUCCESS", message=""):
        self.acks.append({"msg": msg, "status": status, "message": message})

    async def _handle_message(self, msg):
        self.handled.append(msg)


def _check(label, cond, state):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    state[0] = state[0] and cond


def main():
    print("Running hub_agent shed-with-reply self-test...")
    state = [True]
    seg, cap, src = _load()

    async def drive(n_inflight, ack_raises=False):
        logger = _Logger()
        ns = {"asyncio": asyncio, "logger": logger,
              "_MAX_INFLIGHT_HANDLERS": cap}
        exec(compile(seg, "hub_agent_extract", "exec"), ns)
        dispatch = ns["_dispatch_slow"]

        # Real asyncio.Tasks so len(self._inflight) is honest.
        held = []
        for _ in range(n_inflight):
            held.append(asyncio.ensure_future(asyncio.sleep(3600)))
        agent = _Agent(held)
        if ack_raises:
            async def boom(*a, **k):
                raise RuntimeError("ws gone")
            agent._ack = boom

        msg = {"header": {"message_id": "mid-123"},
               "payload": {"type": "HELP_ASK"}}
        dispatch(agent, msg)
        await asyncio.sleep(0)   # let scheduled tasks run
        await asyncio.sleep(0)
        for t in held:
            t.cancel()
        return agent, logger, msg

    loop = asyncio.new_event_loop()
    try:
        # --- under the cap -------------------------------------------------
        agent, logger, msg = loop.run_until_complete(drive(cap - 1))
        _check("under cap: message is dispatched", len(agent.handled) == 1, state)
        _check("under cap: no forged reply", agent.acks == [], state)
        _check("under cap: nothing logged at ERROR", logger.errors == [], state)

        # --- over the cap --------------------------------------------------
        agent, logger, msg = loop.run_until_complete(drive(cap))
        _check("over cap: handler NOT run", agent.handled == [], state)
        _check("over cap: a reply IS sent (not a silent drop)",
               len(agent.acks) == 1, state)
        if agent.acks:
            ack = agent.acks[0]
            _check("over cap: reply status is FAILED",
                   ack["status"] == "FAILED", state)
            _check("over cap: reply correlates to the inbound message",
                   ack["msg"].get("header", {}).get("message_id") == "mid-123", state)
            _check("over cap: reply explains the cause",
                   "busy" in ack["message"].lower() and str(cap) in ack["message"], state)
        _check("over cap: logged at WARNING, not ERROR",
               logger.errors == [] and len(logger.warnings) == 1, state)

        # --- reply cannot be scheduled -------------------------------------
        agent, logger, msg = loop.run_until_complete(drive(cap, ack_raises=True))
        _check("reply failure is caught and logged, not unhandled",
               any("failed to send" in w for w in logger.warnings), state)
        _check("reply failure still sheds (handler not run)",
               agent.handled == [], state)
    finally:
        loop.close()

    # --- source-level guards ----------------------------------------------
    _check("stale 'mailbox redelivers' justification removed",
           "durable mailbox redelivers" not in src, state)
    _check("drop path no longer logs at ERROR",
           'logger.error(\n                "Dropping %s' not in src, state)

    if state[0]:
        print("\nALL CASES PASSED")
        return 0
    print("\nSOME CASES FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
