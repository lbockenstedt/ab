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


def _load_claude_lookup(ns):
    """Load real slot discovery and configuration predicates without provider I/O."""
    path = Path(__file__).resolve().parents[1] / "llm_client.py"
    names = {"_find_claude_cli_slot", "_provider_configured", "_provider_is_nokey"}
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)


@pytest.mark.parametrize("slots", [{}, {1: ("claude_cli", "", "", None)}])
@pytest.mark.parametrize("prior", [
    {},
    {"o/r:1": {"status": "failed", "auto_retries": 2}},
    {"o/r:1": {"status": "feature_flagged", "pipeline": "fix_engine"}},
])
def test_missing_claude_provider_does_not_mark_processing_or_notify_hub(prior, slots):
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
        "load_config": lambda: {"GITHUB_TOKEN": "test-token", "slots": slots},
        "_get_provider_config": lambda n, config: config["slots"].get(
            n, ("openai", "", "", None)),
        "Github": github, "GithubException": RuntimeError,
        "load_processed": lambda: copy.deepcopy(store), "save_processed": save,
        "update_task_state": lambda **kwargs: events.append(("task", kwargs)),
        "recompute_issue_counters": lambda *args: None,
        "analyze_issue": lambda item: (True, ""),
        "_notify_bug_in_progress": lambda item: events.append("hub-in-progress"),
    }
    _load_claude_lookup(ns)
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


@pytest.mark.parametrize("slots, expected", [
    ({1: ("claude_cli", "", "", None), 3: ("claude_cli", "", "model", None)}, 3),
    ({4: ("claude_cli", "", "model", None)}, 4),
])
def test_claude_discovery_skips_incomplete_slots_but_needs_no_api_key(slots, expected):
    """Select a usable later slot without mistaking a no-key provider for incomplete."""
    ns = {"_get_provider_config": lambda n, config: config.get(
        n, ("openai", "", "", None))}
    _load_claude_lookup(ns)
    assert ns["_find_claude_cli_slot"](slots) == expected
