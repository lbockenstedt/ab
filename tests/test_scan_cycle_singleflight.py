"""Overlapping manual and scheduled cycles must not duplicate work or publish false Idle."""
import ast
import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from github import GithubException


ROOT = Path(__file__).resolve().parents[1]


def _function(filename, name, namespace):
    """Execute the actual function without importing application startup code."""
    tree = ast.parse((ROOT / filename).read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), filename, "exec"), namespace)
    return namespace[name]


@pytest.fixture
def scans(monkeypatch):
    """Keep the real cycle body while replacing external phases and services."""
    state = {"status": "Idle", "available_labels": ["automated-fix"]}
    config = {"GITHUB_TOKEN": "unused-test-token", "self_log_scan_enabled": False}
    main = SimpleNamespace(
        get_monitored_repos=Mock(return_value=["o/r"]),
        verify_production_fixes=Mock(), scan_hub_logs=Mock())
    gh = Mock(return_value=SimpleNamespace(
        get_user=Mock(return_value=SimpleNamespace(login="test-bot"))))
    clock = Mock()
    clock.now.return_value.strftime.side_effect = map(str, count())
    namespace = {
        "state": state, "main": main, "Github": gh, "GithubException": GithubException,
        "load_dotenv": Mock(), "load_config": Mock(return_value=config),
        "load_processed": Mock(return_value={}), "datetime": clock, "time": time,
        "os": SimpleNamespace(getenv=lambda name: None), "logger": Mock(),
        "_label_cache": {"repos": ["o/r"], "at": time.time()},
        "LABEL_DISCOVERY_TTL_S": 1_000_000, "update_task_state": Mock(),
        "scan_self_logs": Mock(), "scan_repo_issues": Mock(),
        "ThreadPoolExecutor": ThreadPoolExecutor, "_SCAN_CYCLE_LOCK": threading.Lock(),
    }
    # Imports inside the cycle must not start actual provider or GitHub work.
    for name, members in {
        "skills_loader": {"load_skills": Mock(return_value={})},
        "pr_review": {"scan_open_prs": Mock()},
        "feature_drive": {"scan_feature_requests": Mock()},
        "gh_actions_health": {"outage": Mock(return_value=(False, ""))},
        "health_check": {"scan_health": Mock()},
    }.items():
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(**members))
    run = _function("workers.py", "run_scan_cycle", namespace)
    return SimpleNamespace(run=run, ns=namespace, main=main, gh=gh, state=state)


@pytest.mark.parametrize("manual", [False, True])
def test_second_cycle_does_not_duplicate_active_scan_or_publish_idle(scans, manual):
    """A second trigger coalesces without changing the active cycle's status or timestamp."""
    entered, release = threading.Event(), threading.Event()
    verify = scans.main.verify_production_fixes

    def block_first(*args):
        """Pause the first cycle after it publishes Scanning."""
        if verify.call_count == 1:
            entered.set()
            assert release.wait(10), "test did not release first cycle"

    verify.side_effect = block_first
    worker = threading.Thread(target=scans.run)
    worker.start()
    try:
        assert entered.wait(10), "first cycle did not reach verification"
        stamp = scans.state["last_run"]
        if manual:
            route_namespace = {
                "state": scans.state, "run_scan_cycle": scans.run,
                "threading": SimpleNamespace(Thread=lambda target, daemon:
                                             SimpleNamespace(start=target)),
            }
            response = asyncio.run(_function("routes.py", "scan_now", route_namespace)())
        else:
            scans.run()
        assert verify.call_count == 1
        assert scans.ns["load_config"].call_count == 1
        assert scans.gh.call_count == 1
        assert scans.state["status"] == "Scanning"
        assert scans.state["last_run"] == stamp
        if manual:
            assert response["status"] == "triggered"
            assert "requested" in response["message"].lower()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert scans.state["status"] == "Idle"

    scans.run()
    assert verify.call_count == 2
    assert scans.state["status"] == "Idle"


def test_scan_failure_releases_guard_and_later_cycle_runs(scans):
    """Failure before scanning starts must not permanently suppress later cycles."""
    scans.ns["load_config"].side_effect = RuntimeError("config unavailable")
    scans.run()
    assert scans.state["status"] == "Error: config unavailable"
    lock = scans.ns["_SCAN_CYCLE_LOCK"]
    assert lock.acquire(blocking=False)
    lock.release()
    scans.ns["load_config"].side_effect = None
    scans.run()
    assert scans.state["status"] == "Idle"
