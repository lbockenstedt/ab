"""Unsupported explicit providers must fail before issue state or external progress changes."""
import ast
import copy
from datetime import datetime
import logging
import os
from pathlib import Path
import threading
from types import SimpleNamespace as N

import pytest


@pytest.mark.parametrize("prior", [
    {},
    {"o/r:1": {"status": "failed", "auto_retries": 2}},
    {"o/r:1": {"status": "feature_flagged", "pipeline": "fix_engine"}},
])
def test_missing_claude_provider_does_not_mark_processing_or_notify_hub(prior):
    """Run the actual dispatcher and preserve both new and existing issue records."""
    store, original = copy.deepcopy(prior), copy.deepcopy(prior)
    state = {"processed": copy.deepcopy(prior)}
    events = []
    issue = N(title="A fix request", body="A detailed issue")
    repo = N(get_issue=lambda number: issue)

    def github(token):
        """Track whether provider rejection was delayed until after external lookup."""
        events.append("github")
        return N(get_repo=lambda name: repo)

    def save(processed):
        """Represent acknowledged persistence so stale processing is observable."""
        events.append("save")
        store.clear()
        store.update(copy.deepcopy(processed))

    ns = {
        "state": state, "threading": threading, "datetime": datetime, "os": os,
        "logger": logging.getLogger("preflight-test"),
        "_inflight_lock": threading.Lock(), "_inflight_issues": set(),
        "load_config": lambda: {"GITHUB_TOKEN": "test-token"},
        "_find_claude_cli_slot": lambda config: None,
        "Github": github, "GithubException": RuntimeError,
        "load_processed": lambda: copy.deepcopy(store), "save_processed": save,
        "update_task_state": lambda **kwargs: events.append(("task", kwargs)),
        "recompute_issue_counters": lambda *args: None,
        "analyze_issue": lambda item: (True, ""),
        "_notify_bug_in_progress": lambda item: events.append("hub-in-progress"),
    }
    path = Path(__file__).resolve().parents[1] / "fix_engine.py"
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, ast.FunctionDef)
             and node.name in ("process_single_issue", "_claim_issue", "_release_issue")]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)

    result = ns["process_single_issue"]("o/r", 1, llm_preference="claude")
    assert result == (False, "Claude CLI is not configured in the LLM Vault.")
    assert store == original and state["processed"] == original
    assert events == []
    assert ns["_inflight_issues"] == set()
