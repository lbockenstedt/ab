"""Proposal creation and fix dispatch without GitHub or background workers."""

import ast
import asyncio
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

import main  # noqa: F401 - the routes' lazy github_ops import needs main loaded first


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
        "chat.py", {"_tool_propose_fix", "_trunc", "_register_fix_proposal", "_confirm_fix_marker",
                    "positive_issue_number"},
        uuid=uuid, time=time, state=state, _chat_lock=threading.Lock(),
    )
    descriptor = ns["_tool_propose_fix"](None, _MANAGED, {"repo": "o/r", "number": 42, "llm_preference": "local"})
    assert descriptor["kind"] == "confirm_fix"
    assert "llm_preference" not in descriptor

    # Even a legacy descriptor doesn't write or render a preference.
    legacy = {**descriptor, "llm_preference": "local"}
    ns["_register_fix_proposal"]("chat-1", legacy, {})
    assert "llm_preference" not in state["chat_fix_proposals"][descriptor["confirm_token"]]
    assert "pref=" not in ns["_confirm_fix_marker"](legacy)


# Config naming o/r as a monitored repo, with an explicit self-diagnosis repo so
# no git remote is read.
_MANAGED = {"monitored_repos": ["o/r"], "trusted_repos": ["o/trusted"], "self_diagnosis_repo": "o/ab"}


def _propose(args):
    """Run the real propose_fix tool offline against the managed-repo config."""
    ns = _load_functions("chat.py", {"_tool_propose_fix", "_trunc", "positive_issue_number"}, uuid=uuid)
    return ns["_tool_propose_fix"](None, _MANAGED, args)


@pytest.mark.parametrize("repo", ["attacker/repo", "o/r-evil", "o", "", "https://github.com/x/r"])
def test_propose_fix_refuses_a_repo_ab_does_not_manage(repo):
    """A model steered by injected text cannot get a Confirm button for an outside repo."""
    out = _propose({"repo": repo, "number": 1})
    assert "error" in out and "confirm_token" not in out


@pytest.mark.parametrize("repo,want", [("o/r", "o/r"), ("O/R", "o/r"), ("https://github.com/o/r.git", "o/r"),
                                       ("o/trusted", "o/trusted"), ("o/ab", "o/ab")])
def test_propose_fix_accepts_managed_repos_in_their_configured_spelling(repo, want):
    """Monitored, trusted and self-diagnosis repos are proposable, matched case-insensitively."""
    assert _propose({"repo": repo, "number": "7"})["repo"] == want


@pytest.mark.parametrize("config,want", [
    ({"monitored_repos": ["o/r"], "trusted_repos": ["O/R"], "self_diagnosis_repo": "o/ab"}, "O/R"),
    ({"monitored_repos": ["o/ab"], "trusted_repos": [], "self_diagnosis_repo": "O/AB"}, "O/AB"),
])
def test_a_repo_in_two_spellings_keeps_its_trusted_spelling(config, want):
    """fix_engine checks trust by exact spelling, so a repo that is both monitored
    and trusted (or self-diagnosis) must come back in the trusted spelling."""
    import github_ops
    for repo in ("o/r", "O/R", "o/ab", "O/AB"):
        if repo.lower() == want.lower():
            assert github_ops.repo_in_fix_scope(config, repo) == want


@pytest.mark.parametrize("number", [0, -3, "x", None, "1.5", 1.5, True, False, "", "٣", [1]])
def test_propose_fix_refuses_a_bad_issue_number(number):
    """A number that is not a positive integer is an error, not an exception, and
    int() coercion must not turn true or 1.5 into issue 1."""
    assert "error" in _propose({"repo": "o/r", "number": number})


@pytest.mark.parametrize("number", [7, 7.0, "7", " 7 "])
def test_propose_fix_accepts_a_positive_issue_number(number):
    """Whole numbers in any JSON form become the int issue number."""
    assert _propose({"repo": "o/r", "number": number})["number"] == 7


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
        threading=SimpleNamespace(Thread=_InlineThread), load_config=lambda: dict(_MANAGED),
        JSONResponse=lambda status_code, content: (status_code, content),
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


def test_confirm_refuses_a_stored_proposal_for_an_unmanaged_repo(routes):
    """A proposal for a repo outside the managed list (an older proposal, or a repo
    removed since) is consumed and refused, and no fix starts."""
    ns, calls = routes
    proposals = ns["state"]["chat_fix_proposals"]
    proposals["t"] = {"repo": "attacker/repo", "number": 1, "created": time.time()}
    status, body = asyncio.run(ns["chat_confirm_fix"](_Request({"token": "t"})))
    assert status == 403 and "attacker/repo" in body["message"]
    assert calls == [] and "t" not in proposals


def test_confirm_runs_under_the_configured_repo_spelling(routes):
    """A stored proposal in URL or case-variant form passes the scope check and
    must run as the configured owner/repo, not the raw string Github would reject."""
    ns, calls = routes
    proposals = ns["state"]["chat_fix_proposals"]
    proposals["t"] = {"repo": "https://github.com/O/R.git", "number": 5, "created": time.time()}
    result = asyncio.run(ns["chat_confirm_fix"](_Request({"token": "t"})))
    assert result["repo"] == "o/r" and result["task_id"] == "o/r:5"
    assert [c[:2] for c in calls] == [("o/r", 5)]


@pytest.mark.parametrize("number", [True, 1.5, "1.5", 0, "x"])
def test_confirm_refuses_a_stored_proposal_with_a_bad_issue_number(routes, number):
    """The same positive-integer rule holds for a proposal already stored."""
    ns, calls = routes
    proposals = ns["state"]["chat_fix_proposals"]
    proposals["t"] = {"repo": "o/r", "number": number, "created": time.time()}
    status, _body = asyncio.run(ns["chat_confirm_fix"](_Request({"token": "t"})))
    assert status == 400 and calls == []
