"""Proposal creation and fix dispatch without GitHub or background workers."""

import ast
import asyncio
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import pytest


def _load_functions(filename, names, **ns):
    """Extract real functions without importing the live application."""
    tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in functions} == names
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), filename, "exec"), ns)
    return ns


def test_generated_preference_is_not_a_proposal_field():
    """Generated preferences never enter stored proposals or confirmation markers."""
    state = {}
    ns = _load_functions(
        "chat.py", {"_tool_propose_fix", "_trunc", "_register_fix_proposal", "_confirm_fix_marker"},
        uuid=uuid, time=time, state=state, _chat_lock=threading.Lock(),
    )
    descriptor = ns["_tool_propose_fix"](None, {}, {"repo": "o/r", "number": 42, "llm_preference": "local"})
    assert descriptor["kind"] == "confirm_fix"
    assert "llm_preference" not in descriptor

    # Even a legacy descriptor doesn't write or render a preference.
    legacy = {**descriptor, "llm_preference": "local"}
    ns["_register_fix_proposal"]("chat-1", legacy, {})
    assert "llm_preference" not in state["chat_fix_proposals"][descriptor["confirm_token"]]
    assert "pref=" not in ns["_confirm_fix_marker"](legacy)


class _Request:
    def __init__(self, body):
        """Keep the request body for the offline route call."""
        self.body = body

    async def json(self):
        """Return the submitted body without an HTTP server."""
        return self.body


class _InlineThread:
    def __init__(self, *, target, daemon):
        """Capture a background target for synchronous execution."""
        self.target = target

    def start(self):
        """Execute the target in the test thread."""
        self.target()


@pytest.fixture
def routes():
    """Load real dispatch routes with isolated state and a recording fix runner."""
    calls = []

    def process_single_issue(repo, number, llm_preference=None):
        """Record routing without starting a fix."""
        calls.append((repo, number, llm_preference))
        return True, "ok"

    ns = _load_functions(
        "routes.py", {"chat_confirm_fix", "trigger_fix"}, Request=_Request,
        state={"chat_fix_proposals": {}}, time=time, _chat_lock=threading.Lock(),
        threading=SimpleNamespace(Thread=_InlineThread), load_config=lambda: {},
        logger=SimpleNamespace(info=lambda *args: None), process_single_issue=process_single_issue,
    )
    return ns, calls


@pytest.mark.parametrize("stored_preference", ["local", "cloud", None])
def test_confirm_preserves_auto_routing_and_escalation_for_stored_proposals(routes, stored_preference):
    """Generic confirmation ignores legacy routing metadata and consumes its token."""
    ns, calls = routes
    proposals = ns["state"]["chat_fix_proposals"]
    proposals["valid"] = {
        "repo": "o/r", "number": 42, "created": time.time(), "llm_preference": stored_preference,
    }
    result = asyncio.run(ns["chat_confirm_fix"](_Request({"token": "valid", "llm_preference": "local"})))
    assert result == {"status": "triggered", "task_id": "o/r:42", "repo": "o/r", "number": 42}
    assert "valid" not in proposals
    # None selects the automatic cloud-only picker and retains confidence escalation.
    assert calls == [("o/r", 42, None)]


def test_manual_trigger_preserves_operator_local_routing(routes):
    """A separate manual request can still explicitly choose local inference."""
    ns, calls = routes
    result = asyncio.run(ns["trigger_fix"](_Request({"repo_name": "o/r", "issue_num": 42,
                                                    "llm_preference": "local"})))
    assert result["status"] == "triggered"
    assert calls == [("o/r", 42, "local")]
