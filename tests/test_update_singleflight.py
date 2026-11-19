"""Self-update checks preserve ownership, rollback authority and event-loop responsiveness."""
import ast
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import threading
from types import SimpleNamespace as N

import pytest


@pytest.fixture
def updater(tmp_path):
    """Execute the actual updater against fake git/process I/O and a private marker."""
    events, stored, state = [], {"last_known_good_commit": "older", "failed_commits": []}, {}
    scenario = {"kind": "no_change", "remote": "old"}
    repo = N(head=N(commit=N(hexsha="old")))
    marker = tmp_path / "update_pending"

    def reset(flag, target):
        """Record deployment transitions without touching any checkout."""
        events.append(("reset", target))
        repo.head.commit.hexsha = target

    def fetch():
        """Allow a test to pause an owner at the real fetch boundary."""
        events.append("fetch")
        if scenario.get("fetch"):
            scenario["fetch"]()

    def construct(path):
        """Record entry before the updater can read or replace rollback state."""
        events.append("repo")
        if scenario["kind"] == "exception":
            raise RuntimeError("simulated git failure")
        return repo

    def save(data):
        """Keep the saved state separate from each caller's working snapshot."""
        events.append(("save", copy.deepcopy(data)))
        stored.clear()
        stored.update(copy.deepcopy(data))

    class SyntaxProblem(Exception):
        """Represent a compiler rejection without executing repository code."""

    def compile_file(*args, **kwargs):
        """Expose the real syntax-failure exit without invoking a compiler."""
        if scenario["kind"] == "syntax":
            raise SyntaxProblem("simulated syntax failure")

    repo.git = N(reset=reset)
    repo.remotes = N(origin=N(fetch=fetch, pull=lambda: None))
    repo.active_branch = N(tracking_branch=lambda: N(name="origin/work"))
    repo.commit = lambda name: N(hexsha=scenario["remote"])
    repo.is_dirty = lambda **kwargs: False
    fake_os = N(getcwd=lambda: str(tmp_path), path=os.path, stat=os.stat,
                walk=lambda path: [(str(tmp_path), [], ["module.py"])])
    ns = {
        "git": N(Repo=construct), "os": fake_os, "state": state,
        "CONFIG_DIR": str(tmp_path), "logger": logging.getLogger("update-test"),
        "load_update_state": lambda: copy.deepcopy(stored), "save_update_state": save,
        "get_version": lambda: "test", "_UPDATE_CHECK_LOCK": threading.Lock(),
        "py_compile": N(compile=compile_file, PyCompileError=SyntaxProblem),
        "_requirements_changed": lambda *args: scenario["kind"] == "dependency",
        "_sync_dependencies": lambda: (False, "simulated dependency failure"),
    }
    source = Path(__file__).resolve().parents[1] / "workers.py"
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "check_for_updates")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
    return N(check=ns["check_for_updates"], events=events, stored=stored,
             state=state, scenario=scenario, repo=repo, marker=marker, os=fake_os)


def test_busy_check_cannot_touch_git_or_rollback_state_and_later_check_runs(updater):
    """A second call must not enter the deployment transaction owned by a paused call."""
    entered, release = threading.Event(), threading.Event()

    def pause():
        """Hold exactly one invocation at its fetch boundary."""
        entered.set()
        assert release.wait(5)

    updater.scenario["fetch"] = pause
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(updater.check)
        assert entered.wait(5)
        before = copy.deepcopy(updater.events)
        updater.scenario.pop("fetch")
        try:
            busy = updater.check()
            assert updater.events == before
            assert busy == (None, "Update check already in progress.")
        finally:
            release.set()
        assert first.result(5) == (None, "No updates available.")
    assert updater.check() == (None, "No updates available.")
    assert updater.events.count("repo") == 2


def test_unverified_update_defers_before_overwriting_last_known_good(updater):
    """The durable marker still protects rollback after restart_pending was cleared."""
    updater.marker.write_text("unverified")
    updater.repo.head.commit.hexsha = "unverified"
    updater.state["restart_pending"] = False
    before = copy.deepcopy(updater.stored)
    result = updater.check()
    assert result == (None, "Update deferred: previous update awaiting watchdog verification.")
    assert updater.events == [] and updater.stored == before
    assert updater.marker.read_text() == "unverified"


def test_marker_stat_failure_does_not_authorize_an_update(updater):
    """An inaccessible marker is not evidence that no update is pending."""
    def denied(path):
        """Simulate an unreadable marker directory without changing real permissions."""
        raise PermissionError("marker permission denied")
    updater.os.stat = denied
    result = updater.check()
    assert result[0] is None and "marker permission denied" in result[1]
    assert updater.events == []


@pytest.mark.parametrize("kind", ["exception", "known_bad", "syntax", "dependency", "success"])
def test_each_owner_exit_releases_the_update_guard(updater, kind):
    """Every real success or failure branch must permit a later verified cycle."""
    updater.scenario.update(kind=kind, remote="new")
    if kind == "known_bad":
        updater.stored["failed_commits"] = ["new"]
    first = updater.check()
    assert first[0] is (True if kind == "success" else None)
    if updater.marker.exists():
        updater.marker.unlink()
    updater.scenario.update(kind="no_change", remote=updater.repo.head.commit.hexsha)
    updater.stored["failed_commits"] = []
    assert updater.check() == (None, "No updates available.")


@pytest.mark.parametrize("restart_pending", [False, True])
def test_absent_marker_retains_normal_baseline_updates(updater, restart_pending):
    """A manual restart flag alone must not be confused with an unverified update."""
    updater.state["restart_pending"] = restart_pending
    assert updater.check() == (None, "No updates available.")
    assert updater.stored["last_known_good_commit"] == "old"


def test_manual_update_runs_blocking_checker_off_the_event_loop():
    """Check thread ownership directly, without a flaky elapsed-time threshold."""
    loop_thread = threading.get_ident()
    called = []

    def check():
        """Record the execution thread at the synchronous git-work boundary."""
        called.append(threading.get_ident())
        return None, "No updates available."

    source = Path(__file__).resolve().parents[1] / "routes.py"
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_now")
    node.decorator_list = []
    ns = {"asyncio": asyncio, "check_for_updates": check, "logger": logging.getLogger("route-test")}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
    assert asyncio.run(ns["update_now"]()) == {"status": "success", "message": "No updates available."}
    assert len(called) == 1 and called[0] != loop_thread
