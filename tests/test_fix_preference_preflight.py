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


def _load_claude_lookup(ns, extra_names=()):
    """Load real slot discovery and configuration predicates without provider I/O."""
    path = Path(__file__).resolve().parents[1] / "llm_client.py"
    names = {"_find_claude_cli_slot", "_provider_configured", "_provider_is_nokey"}
    names.update(extra_names)
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)


@pytest.mark.parametrize("slots", [
    {}, {1: ("claude_cli", "", "", None)},
    *[{n: ("claude_cli", "", "claude-opus-4-6", None)} for n in (5, 6, 7, 8)],
])
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


@pytest.mark.parametrize("slot", [None, 5, 6, 7, 8])
def test_configured_cli_outside_projection_is_not_discovered(slot):
    """Characterize the discovery gap, not a new selection policy.

    An enabled, no-key CLI entry is configured in the registry even when
    unassigned or assigned outside slots 1-4. Owner-scoped selection work
    must revisit this expectation together with dashboard eligibility.
    """
    # Use real vault resolution, isolated from the runner's provider environment.
    config = {
        "llm_entries": [{"id": "cli-session", "provider": "claude_cli",
                         "model": "claude-opus-4-6", "enabled": True}],
        "llm_credentials": {},
        "llm_slots": {} if slot is None else {str(slot): "cli-session"},
    }
    ns = {"os": N(getenv=lambda name, default="": default),
          "_ALL_SLOTS": tuple(range(1, 9))}
    _load_claude_lookup(ns, ("_get_provider_config", "_iter_configured_endpoints",
                            "_configured_entries"))

    # Registry configured-state accepts session auth without an API key.
    entries = ns["_configured_entries"](config)
    assert len(entries) == 1
    assert entries[0]["id"] == "cli-session"
    assert entries[0]["api_key"] == ""
    assert entries[0]["_configured"] is True
    assert ns["_find_claude_cli_slot"](config) is None

    # The identical endpoint becomes discoverable when projected into slot 4.
    config["llm_slots"] = {"4": "cli-session"}
    assert ns["_find_claude_cli_slot"](config) == 4
