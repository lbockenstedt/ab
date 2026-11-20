"""History transactions: real disk, ordered views, and real production callers."""
import ast
import asyncio
import builtins
import copy
import contextlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import threading
from types import SimpleNamespace as N

import pytest

import main  # noqa: F401 -- preserve production import/re-export order
import app_state as app
import config_store as storage
import feature_build
import fix_engine
import github_ops
import log_scan
import routes
import workers


@pytest.fixture
def history(tmp_path, monkeypatch):
    """Use the imported production store and publisher, never an in-memory saver."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(storage, "STATE_FILE", str(tmp_path / "persistent" / "processed_issues.json"))
    monkeypatch.setattr(app, "state", {})
    storage.save_processed({})
    app.recompute_issue_counters()
    return Path(storage.STATE_FILE)


def seed(data):
    """Seed through the actual transaction so counters and feed start committed."""
    def replace(current):
        current.clear()
        current.update(copy.deepcopy(data))
    app.update_processed(replace)


def assert_view():
    """Check all derived buckets against the actual committed history."""
    saved = storage.load_processed()
    assert app.state["processed"] == saved
    expected = {
        "success_count": ("fixed", "verified", "awaiting_prod_verification", "resolved"),
        "closed_count": ("closed",), "pending_verification_count": ("pending_verification",),
        "non_actionable_count": ("non-actionable",), "failure_count": ("failed",),
        "feature_flagged_count": ("feature_flagged", "feature_classify_retry"),
        "feature_needs_info_count": ("feature_needs_info",), "feature_built_count": ("feature_built",),
    }
    for key, statuses in expected.items():
        assert app.state[key] == sum(rec.get("status") in statuses for rec in saved.values())


def test_write_only_lock_negative_control_loses_an_acknowledged_record(history):
    """Legacy full replacements still demonstrate why serializing only saves fails."""
    first, second = storage.load_processed(), storage.load_processed()
    first["o/r:1"] = {"status": "fixed"}
    second["o/r:2"] = {"status": "failed"}
    storage.save_processed(first)
    storage.save_processed(second)
    assert storage.load_processed() == {"o/r:2": {"status": "failed"}}


def test_concurrent_updates_keep_cross_records_fields_and_increments(history):
    seed({"o/r:0": {"status": "failed", "auto_retries": 0}})
    ready = threading.Barrier(2)

    def write(number):
        ready.wait(timeout=5)  # outside the serialized callback
        def mutate(current):
            current[f"o/r:{number}"] = {"status": "fixed"}
            current["o/r:0"][f"field{number}"] = number
            current["o/r:0"]["auto_retries"] += 1
        app.update_processed(mutate)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(write, (1, 2)))
    assert storage.load_processed()["o/r:0"] == {
        "status": "failed", "auto_retries": 2, "field1": 1, "field2": 2}
    assert set(storage.load_processed()) == {"o/r:0", "o/r:1", "o/r:2"}
    assert_view()


def test_publication_is_inside_transaction_lock_and_cannot_regress(history, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = app._publish_issue_counters

    def pause_first(snapshot):
        if "o/r:2" not in snapshot:
            entered.set()
            assert release.wait(5)
        return original(snapshot)

    monkeypatch.setattr(app, "_publish_issue_counters", pause_first)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(app.update_processed, lambda c: c.update({"o/r:1": {"status": "fixed"}}))
        assert entered.wait(5)
        # Probe from a different thread while publication is paused. No timing bound
        # distinguishes implementations; a released outer lock deterministically fails.
        acquired = storage._processed_lock.acquire(blocking=False)
        if acquired:
            storage._processed_lock.release()
        release.set()
        assert not acquired
        second = pool.submit(app.update_processed, lambda c: c.update({"o/r:2": {"status": "failed"}}))
        first.result(5)
        second.result(5)
    assert_view()


@pytest.mark.parametrize("bad", ["", "{", "null", "7", '{"o/r:1": []}', '[{}]'])
@pytest.mark.parametrize("location", ["primary", "cwd"])
def test_corrupt_authority_blocks_mutator_raw_clear_and_view_refresh(history, bad, location):
    seed({"o/r:1": {"status": "resolved"}})
    before_view = copy.deepcopy(app.state)
    cwd = Path("processed_issues.json")
    cwd.write_text('{"fallback": {}}')
    if location == "cwd":
        history.unlink()
        target = cwd
    else:
        target = history
    target.write_text(bad)
    calls = []
    with pytest.raises(storage.ProcessedUnreadableError):
        app.update_processed(lambda current: calls.append(True) or current.clear())
    with pytest.raises(storage.ProcessedUnreadableError):
        storage.save_processed({})
    with pytest.raises(storage.ProcessedUnreadableError):
        app.recompute_issue_counters()
    assert not calls and target.read_text() == bad
    assert app.state == before_view
    if location == "primary":
        assert cwd.read_text() == '{"fallback": {}}'
    else:
        assert not history.exists()


def test_unreadable_primary_is_not_treated_as_missing(history, monkeypatch):
    original = builtins.open
    def denied(path, *args, **kwargs):
        if str(path) == str(history):
            raise PermissionError("test denial")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(storage.ProcessedUnreadableError):
        app.update_processed(lambda c: c.clear())
    assert not Path("processed_issues.json").exists()


def test_cwd_authority_is_preserved_when_primary_fails_then_migrates(history, monkeypatch):
    history.unlink()
    Path("processed_issues.json").write_text('{"old": {"note": "keep"}}')
    original = storage._atomic_write_json
    def cwd_only(path, data, **kwargs):
        if path == str(history):
            raise PermissionError("primary unavailable")
        return original(path, data, **kwargs)
    monkeypatch.setattr(storage, "_atomic_write_json", cwd_only)
    app.update_processed(lambda c: c.update({"new": {}}))
    assert not history.exists()
    assert json.loads(Path("processed_issues.json").read_text()) == {"old": {"note": "keep"}, "new": {}}
    monkeypatch.setattr(storage, "_atomic_write_json", original)
    app.update_processed(lambda c: c["old"].update(note="updated"))
    assert json.loads(history.read_text()) == {"old": {"note": "updated"}, "new": {}}
    assert_view()


def test_existing_primary_failure_never_writes_ignored_fallback(history, monkeypatch):
    seed({"keep": {"status": "fixed"}})
    before, view = history.read_bytes(), copy.deepcopy(app.state)
    writes = []
    def fail(path, data, **kwargs):
        writes.append(path)
        raise OSError("disk full")
    monkeypatch.setattr(storage, "_atomic_write_json", fail)
    with pytest.raises(storage.ProcessedStoreError):
        app.update_processed(lambda c: c.clear())
    assert writes == [str(history)]
    assert history.read_bytes() == before and app.state == view


def test_both_missing_and_both_write_failures_propagate(history, monkeypatch):
    history.unlink()
    writes = []
    def fail(path, data, **kwargs):
        writes.append(path)
        raise OSError("read-only")
    monkeypatch.setattr(storage, "_atomic_write_json", fail)
    with pytest.raises(storage.ProcessedStoreError):
        app.update_processed(lambda c: c.update({"new": {}}))
    assert writes == [str(history), "processed_issues.json"]


def test_callback_serialization_replace_failures_leave_disk_and_view(history, monkeypatch):
    seed({"keep": {"status": "fixed"}})
    before, view = history.read_bytes(), copy.deepcopy(app.state)
    def raises(current):
        current.clear()
        raise ValueError("callback failed")
    with pytest.raises(ValueError, match="callback failed"):
        app.update_processed(raises)
    with pytest.raises(storage.ProcessedStoreError):
        app.update_processed(lambda c: c.update({"bad": {"set": {1}}}))
    def fail_replace(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(storage.ProcessedStoreError):
        app.update_processed(lambda c: c.clear())
    assert history.read_bytes() == before and app.state == view
    assert not list(history.parent.glob("*.tmp"))


def test_noop_validates_without_writing_and_snapshot_results_are_detached(history, monkeypatch):
    seed({"old": {}})
    def unexpected(*args, **kwargs):
        pytest.fail("no-op must not replace the file")
    monkeypatch.setattr(storage, "_atomic_write_json", unexpected)
    result = app.update_processed(lambda c: c["old"])
    result["mutated_outside"] = True
    snapshot = storage.load_processed()
    snapshot.clear()
    assert storage.load_processed() == {"old": {}}
    assert app.state["processed"] == {"old": {}}


def test_legacy_list_and_optional_record_fields_remain_supported(history):
    history.write_text('["o/r:1", "o/r:2"]')
    app.update_processed(lambda c: c["o/r:1"].update(note="legacy"))
    data = storage.load_processed()
    assert data["o/r:2"]["status"] == "fixed" and data["o/r:2"]["timestamp"]
    app.update_processed(lambda c: c.update({"o/r:3": {"optional": None}, "o/r:4": {}}))
    assert storage.load_processed()["o/r:4"] == {}
    assert_view()


def test_real_field_patch_replacement_retry_delete_and_clear(history):
    seed({"o/r:1": {"status": "failed", "title": "fresh", "issue_url": "u",
                     "auto_retries": 4, "obsolete": True}})
    stale = {"o/r:1": {"status": "failed", "auto_retries": 0, "title": "stale"}}
    assert workers._count_retry("o/r:1", stale, {}) is True
    assert stale["o/r:1"]["auto_retries"] == 5
    assert github_ops.note_recurrence("o/r:1")
    feature_build._mark_built("o/r:1", "pr")
    assert storage.load_processed()["o/r:1"]["title"] == "fresh"
    assert "obsolete" not in storage.load_processed()["o/r:1"]
    app.update_processed(lambda c: c.pop("o/r:1"))
    assert github_ops.note_recurrence("o/r:1") is False
    assert workers._update_record("o/r:1", stale, needs_human_at="now") is None
    assert workers._count_retry("o/r:1", stale, {}) is False
    seed({"one": {}, "two": {}})
    response = asyncio.run(routes.clear_history())
    assert response["status"] == "success" and storage.load_processed() == {}
    assert_view()


def test_worker_network_window_cannot_replay_or_resurrect(history):
    seed({"o/r:1": {"status": "failed"}, "o/r:2": {"status": "failed"}})
    fetched, resume = threading.Event(), threading.Event()
    def lookup(number):
        fetched.set()
        assert resume.wait(5)
        return N(state="closed", labels=[])
    gh = N(get_repo=lambda name: N(get_issue=lookup))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(workers.sync_github_issue_state, gh)
        assert fetched.wait(5)
        app.update_processed(lambda c: c.pop("o/r:1"))
        app.update_processed(lambda c: c.update({"other": {"status": "fixed"}}))
        resume.set()
        assert future.result(5) == ["o/r:2"]
    assert set(storage.load_processed()) == {"o/r:2", "other"}
    assert_view()


def test_shipped_observation_is_not_applied_to_a_different_pr(history, monkeypatch):
    old = "https://github.com/o/r/pull/1"
    new = "https://github.com/o/r/pull/2"
    seed({"o/r:7": {"status": "feature_built", "pr_url": old}})
    def get_pull(number):
        app.update_processed(lambda c: c["o/r:7"].update(pr_url=new))
        return N(merged=False)
    gh = N(get_repo=lambda name: N(get_pull=get_pull))
    assert workers.close_shipped_features(gh, {}) == []
    assert storage.load_processed()["o/r:7"] == {"status": "feature_built", "pr_url": new}


@pytest.mark.parametrize("change", ["delete", "status", "timer"])
def test_verification_rechecks_existing_status_and_timer(history, monkeypatch, change):
    old = (datetime.now() - timedelta(days=10)).isoformat()
    seed({"o/r:1": {"status": "awaiting_prod_verification", "clean_since": old}})
    edits = []
    def edit(**kwargs):
        edits.append(kwargs)
        app.update_processed(lambda c: c["o/r:1"].update(clean_since="new-timer"))
    issue = N(body="### Log Evidence:\n```\nerror\n```", create_comment=lambda text: None, edit=edit)
    gh = N(get_repo=lambda name: N(get_issue=lambda number: issue))
    def mirror(text):
        if change == "delete":
            app.update_processed(lambda c: c.pop("o/r:1"))
        elif change == "status":
            app.update_processed(lambda c: c["o/r:1"].update(status="resolved"))
        return False
    monkeypatch.setattr(log_scan, "_mirror_contains", mirror)
    monkeypatch.setattr(log_scan, "load_config", dict)
    monkeypatch.setattr(log_scan, "_apply_closed_label", lambda *args: None)
    log_scan.verify_production_fixes(gh, storage.load_processed())
    saved = storage.load_processed()
    if change == "delete":
        assert "o/r:1" not in saved and not edits
    elif change == "status":
        assert saved["o/r:1"]["status"] == "resolved" and not edits
    else:
        assert edits and saved["o/r:1"]["status"] == "awaiting_prod_verification"
        assert saved["o/r:1"]["clean_since"] == "new-timer"
    assert_view()


def request(issue_id):
    """Minimal async route request."""
    async def payload():
        return {"issue_id": issue_id}
    return N(json=payload)


@pytest.mark.parametrize("route", ["clear_history", "delete_issue", "delete_all_issues", "reevaluate_feature"])
def test_async_failed_local_commit_has_no_background_side_effect(history, monkeypatch, route):
    history.write_text("{")
    def forbidden(*args, **kwargs):
        pytest.fail("background dispatch after failed history commit")
    monkeypatch.setattr(routes, "threading", N(Thread=forbidden))
    fn = getattr(routes, route)
    result = asyncio.run(fn() if route == "clear_history" else fn(request("o/r:1")))
    assert result.status_code == 500
    assert b"History persistence failed" in result.body


@pytest.mark.parametrize("route", ["resolve_issue", "reopen_issue"])
def test_external_success_then_local_failure_is_truthful_and_does_not_dispatch(history, monkeypatch, route):
    history.write_text("{")
    edits = []
    issue = N(state="open" if route == "resolve_issue" else "closed",
              create_comment=lambda body: None, edit=lambda **kw: edits.append(kw),
              remove_from_labels=lambda label: None)
    monkeypatch.setattr(routes, "load_config", lambda: {"GITHUB_TOKEN": "test-token"})
    monkeypatch.setattr(routes, "Github", lambda token: N(get_repo=lambda name: N(get_issue=lambda num: issue)))
    monkeypatch.setattr(routes, "_apply_closed_label", lambda *args: None)
    monkeypatch.setattr(fix_engine, "_notify_bug_fixed", lambda *args: None)
    monkeypatch.setattr(routes, "threading", N(Thread=lambda **kw: pytest.fail("must not dispatch")))
    response = asyncio.run(getattr(routes, route)(request("o/r:1")))
    assert edits and response.status_code == 500
    assert b"History persistence failed" in response.body
    assert b"GitHub" in response.body
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


@pytest.mark.parametrize("route", [
    "resolve_issue", "reopen_issue", "retry_issue", "delete_issue",
    "clear_history", "delete_all_issues",
])
def test_operator_action_cannot_change_an_actively_published_issue(history, monkeypatch, route):
    """A visible publication still being completed must not lose an operator action."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "published-sha"}})
    before, calls = history.read_bytes(), []
    issue = N(state="closed", create_comment=lambda body: calls.append("comment"),
              edit=lambda **kwargs: calls.append(("edit", kwargs)),
              remove_from_labels=lambda label: calls.append(("remove", label)))
    monkeypatch.setattr(routes, "load_config", lambda: {"GITHUB_TOKEN": "test-token"})
    def github(token):
        """Record any forbidden external lookup rather than touching GitHub."""
        calls.append("github")
        return N(get_repo=lambda name: N(get_issue=lambda number: issue))
    monkeypatch.setattr(routes, "Github", github)
    monkeypatch.setattr(routes, "_apply_closed_label", lambda *args: calls.append("label"))
    monkeypatch.setattr(fix_engine, "_notify_bug_fixed", lambda *args: calls.append("notify"))
    monkeypatch.setattr(routes, "threading", N(
        Thread=lambda **kwargs: N(start=lambda: calls.append("thread"))))
    monkeypatch.setitem(routes.state, "dismiss_jobs", {})
    monkeypatch.setattr(storage, "_atomic_write_json",
                        lambda *args: pytest.fail("busy action must not write history"))
    assert fix_engine._claim_issue("o/r:1")
    try:
        fn = getattr(routes, route)
        response = asyncio.run(fn() if route == "clear_history" else fn(request("o/r:1")))
        assert getattr(response, "status_code", 200) == 409
        assert b"o/r:1" in response.body
        assert history.read_bytes() == before and not calls
        assert not routes.state["dismiss_jobs"]
    finally:
        fix_engine._release_issue("o/r:1")


def test_batch_claims_are_atomic_across_numeric_and_case_aliases():
    """Competing batches cannot partially claim or bypass the same GitHub identity."""
    ready, attempted = threading.Barrier(2), threading.Barrier(2)
    batches = [["O/R:001", "o/r:2"], ["o/r:1", "o/r:3"]]
    def claim_batch(issue_ids):
        """Keep the winning batch held until both atomic attempts have finished."""
        ready.wait(5)
        acquired, _ = fix_engine._claim_issues(issue_ids)
        try:
            attempted.wait(5)
            return acquired
        finally:
            if acquired:
                fix_engine._release_issues(issue_ids)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim_batch, batches))
    assert results.count(True) == 1
    all_ids = ["o/r:1", "O/R:001", "o/r:2", "o/r:3"]
    acquired, busy = fix_engine._claim_issues(all_ids)
    try:
        assert acquired and not busy
        assert not fix_engine._claim_issue("O/R:0001")
    finally:
        if acquired:
            fix_engine._release_issues(all_ids)


@pytest.fixture
def operator_remote(monkeypatch):
    """Use a stateful fake GitHub issue for actual operator-route workflows."""
    calls = []
    issue = N(state="closed", create_comment=lambda body: calls.append("comment"),
              remove_from_labels=lambda label: calls.append(("remove", label)))
    def edit(**kwargs):
        """Retain the remote state change as well as its ordering."""
        calls.append(("edit", kwargs))
        for key, value in kwargs.items():
            setattr(issue, key, value)
    issue.edit = edit
    monkeypatch.setattr(routes, "load_config", lambda: {"GITHUB_TOKEN": "test-token"})
    monkeypatch.setattr(routes, "Github",
                        lambda token: N(get_repo=lambda name: N(get_issue=lambda number: issue)))
    monkeypatch.setattr(routes, "_apply_closed_label", lambda *args: calls.append("label"))
    monkeypatch.setattr(fix_engine, "_notify_bug_fixed", lambda *args: calls.append("notify"))
    return N(issue=issue, calls=calls)


@pytest.mark.parametrize("route", ["resolve_issue", "reopen_issue", "retry_issue", "delete_issue"])
def test_operator_alias_cannot_bypass_the_active_pipeline_claim(history, operator_remote, route):
    """The actual route must conflict with the scanner's canonical numeric identity."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "published"}})
    before = history.read_bytes()
    assert fix_engine._claim_issue("o/r:1")
    try:
        response = asyncio.run(getattr(routes, route)(request("O/R:001")))
        assert response.status_code == 409 and history.read_bytes() == before
        assert not operator_remote.calls
    finally:
        fix_engine._release_issue("o/r:1")


def test_reopen_releases_its_action_claim_before_the_new_fix_starts(history, monkeypatch, operator_remote):
    """A successfully admitted Reopen cannot reject its own immediate re-fix."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "previous"}})
    invoked = []
    def fix(repo, number):
        """The new fix must be able to acquire the real pipeline claim."""
        assert fix_engine._claim_issue("o/r:1")
        try:
            invoked.append((repo, number))
            return True, "fixed"
        finally:
            fix_engine._release_issue("o/r:1")
    monkeypatch.setattr(routes, "process_single_issue", fix)
    monkeypatch.setattr(routes, "threading", N(
        Thread=lambda **kwargs: N(start=kwargs["target"])))
    response = asyncio.run(routes.reopen_issue(request("o/r:1")))
    assert response["status"] == "triggered" and invoked == [("o/r", 1)]
    assert storage.load_processed()["o/r:1"]["status"] == "reopened"
    assert operator_remote.issue.state == "open"


def test_reopen_thread_failure_retains_the_recorded_intent_without_a_claim(
        history, monkeypatch, operator_remote):
    """Failure to queue after the reopen must report its real partial outcome."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "previous"}})
    def fail():
        """Fail before any new fix worker has started."""
        raise RuntimeError("thread unavailable")
    monkeypatch.setattr(routes, "threading", N(Thread=lambda **kwargs: N(start=fail)))
    response = asyncio.run(routes.reopen_issue(request("o/r:1")))
    assert response.status_code == 500 and b"reopened and recorded" in response.body
    assert operator_remote.issue.state == "open"
    assert storage.load_processed()["o/r:1"]["status"] == "reopened"
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


@pytest.mark.parametrize("failure", [None, "close"])
def test_actual_publication_wait_rejects_resolve_until_all_completion_writes_finish(
        history, monkeypatch, operator_remote, failure):
    """Pause the actual publication tail at fan-out, then sign off after its release."""
    from test_fix_publication_outcomes import _run_publication
    entered, resume = threading.Event(), threading.Event()
    def publish_record(record):
        """Mirror the actual generated record into the real disk/view transaction."""
        app.update_processed(lambda current: current.update({"o/r:1": record}))
    def wait(*args, **kwargs):
        """Pause the real post-record spoke wait without an external OS action."""
        entered.set()
        assert resume.wait(5)
    def run():
        """Use the same real claim around the actual tail and its outer handlers."""
        assert fix_engine._claim_issue("o/r:1")
        try:
            return _run_publication(failure=failure, after_store=publish_record, wait_spokes=wait)
        finally:
            fix_engine._release_issue("o/r:1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert entered.wait(5)
            before = history.read_bytes()
            response = asyncio.run(routes.resolve_issue(request("o/r:1")))
            assert response.status_code == 409 and history.read_bytes() == before
            assert not operator_remote.calls
        finally:
            resume.set()
        result = future.result(5)
    assert result.result[0] is (failure is None)
    assert storage.load_processed()["o/r:1"]["status"] == "pending_verification"
    response = asyncio.run(routes.resolve_issue(request("o/r:1")))
    assert response["status"] == "success"
    assert storage.load_processed()["o/r:1"]["status"] == "resolved"


def test_cancelled_resolve_keeps_its_claim_until_the_sync_workflow_finishes(
        history, monkeypatch, operator_remote):
    """Disconnecting an HTTP waiter must not admit a conflicting operator action."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "published"}})
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    github, run_action = routes.Github, routes._run_issue_action
    def paused_github(token):
        """Keep the offloaded remote work alive after cancellation of its await."""
        entered.set()
        assert resume.wait(5)
        return github(token)
    def observed_action(*args):
        """Signal only after the actual synchronous workflow releases its claim."""
        try:
            return run_action(*args)
        finally:
            if args[1] is routes._resolve_issue_claimed:
                finished.set()
    monkeypatch.setattr(routes, "Github", paused_github)
    monkeypatch.setattr(routes, "_run_issue_action", observed_action)
    async def scenario():
        """Cancel the first request while its worker still owns the remote mutation."""
        task = asyncio.create_task(routes.resolve_issue(request("o/r:1")))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            response = await routes.reopen_issue(request("o/r:1"))
            assert response.status_code == 409
            assert not fix_engine._claim_issue("o/r:1")
        finally:
            resume.set()
        assert await asyncio.to_thread(finished.wait, 5)
    asyncio.run(scenario())
    assert storage.load_processed()["o/r:1"]["status"] == "resolved"
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


@pytest.mark.parametrize("route,error,status", [
    ("resolve_issue", OSError("GitHub offline"), 502),
    ("reopen_issue", OSError("GitHub offline"), 500),
    ("reopen_issue", fix_engine.GithubException(410, {"message": "was deleted"}, None), 410),
])
def test_operator_remote_failure_and_deleted_issue_release_the_claim(
        history, monkeypatch, operator_remote, route, error, status):
    """Every remote failure exit must allow a later operator attempt."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "published"}})
    def failed_github(token):
        """Raise the actual error handled by the real synchronous route workflow."""
        raise error
    monkeypatch.setattr(routes, "Github", failed_github)
    response = asyncio.run(getattr(routes, route)(request("o/r:1")))
    assert response.status_code == status
    assert ("o/r:1" in storage.load_processed()) is (status != 410)
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


@pytest.mark.parametrize("route", ["resolve_issue", "reopen_issue"])
def test_operator_missing_token_releases_the_claim(history, monkeypatch, route):
    """Pre-network validation failure cannot strand an admitted action."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "published"}})
    before = history.read_bytes()
    monkeypatch.setattr(routes, "load_config", dict)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    response = asyncio.run(getattr(routes, route)(request("o/r:1")))
    assert response.status_code in (400, 500) and history.read_bytes() == before
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


@pytest.mark.parametrize("route", ["delete_issue", "delete_all_issues"])
@pytest.mark.parametrize("failure", ["construct", "start"])
def test_dismissal_thread_failure_reports_the_partial_clear_and_releases_all_claims(
        history, monkeypatch, route, failure):
    """Failed ownership transfer must not leave an accepted-looking background job."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "published"}})
    def fail():
        """Model failure before the background thread has begun any work."""
        raise RuntimeError("thread unavailable")
    def thread(**kwargs):
        """Cover both constructor and start failures during claim transfer."""
        if failure == "construct":
            fail()
        return N(start=fail)
    monkeypatch.setattr(routes, "threading", N(Thread=thread))
    monkeypatch.setitem(routes.state, "dismiss_jobs", {})
    response = asyncio.run(getattr(routes, route)(request("o/r:1")))
    assert response.status_code == 500 and b"could not start" in response.body
    assert storage.load_processed() == {}
    if route == "delete_issue":
        assert routes.state["dismiss_jobs"]["o/r:1"]["status"] == "error"
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


@pytest.mark.parametrize("route", ["clear_history", "delete_all_issues"])
def test_bulk_failed_write_releases_the_current_batch_without_clearing_it(history, monkeypatch, route):
    """Real persistence failure rolls back every reversible admission reservation."""
    records = {"o/r:1": {"status": "pending_verification"}, "o/r:2": {"status": "failed"}}
    seed(records)
    before = history.read_bytes()
    def fail(*args):
        """Reject the actual atomic write after the callback reserved its batch."""
        raise OSError("disk full")
    monkeypatch.setattr(storage, "_atomic_write_json", fail)
    monkeypatch.setattr(routes, "threading", N(
        Thread=lambda **kwargs: pytest.fail("no background work after failed persistence")))
    fn = getattr(routes, route)
    response = asyncio.run(fn() if route == "clear_history" else fn(request("unused")))
    assert response.status_code == 500 and history.read_bytes() == before
    assert storage.load_processed() == records
    acquired, busy = fix_engine._claim_issues(records)
    try:
        assert acquired and not busy
    finally:
        if acquired:
            fix_engine._release_issues(records)


@pytest.mark.parametrize("route", ["clear_history", "delete_all_issues"])
def test_busy_bulk_action_does_not_partially_claim_or_clear_the_other_rows(history, route):
    """One active row refuses the whole operation without releasing its owner."""
    records = {"o/r:1": {"status": "resolved"}, "o/r:2": {"status": "pending_verification"}}
    seed(records)
    before = history.read_bytes()
    assert fix_engine._claim_issue("o/r:2")
    try:
        fn = getattr(routes, route)
        response = asyncio.run(fn() if route == "clear_history" else fn(request("unused")))
        assert response.status_code == 409 and history.read_bytes() == before
        assert not fix_engine._claim_issue("o/r:2")
        acquired = fix_engine._claim_issue("o/r:1")
        try:
            assert acquired
        finally:
            if acquired:
                fix_engine._release_issue("o/r:1")
    finally:
        fix_engine._release_issue("o/r:2")


@pytest.mark.parametrize("exhausted", [False, True])
def test_dismissal_owns_the_issue_through_every_background_retry(history, monkeypatch, exhausted):
    """A removed history row cannot be re-fixed while its GitHub close is running."""
    seed({"o/r:1": {"status": "pending_verification", "commit": "published"}})
    entered, resume, released = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    release = routes._release_issues
    def close(issue_id):
        """Pause the first real retry attempt and control its eventual outcome."""
        attempts.append(issue_id)
        if len(attempts) == 1:
            entered.set()
            assert resume.wait(5)
        if exhausted or len(attempts) < 3:
            raise OSError("GitHub temporarily unavailable")
        return True, "closed"
    def release_and_signal(issue_ids):
        """Signal only after the actual background owner releases its identities."""
        release(issue_ids)
        released.set()
    monkeypatch.setattr(routes, "_close_issue_on_github", close)
    monkeypatch.setattr(routes, "_release_issues", release_and_signal)
    monkeypatch.setattr(routes, "time", N(sleep=lambda seconds: None))
    monkeypatch.setitem(routes.state, "dismiss_jobs", {})
    try:
        response = asyncio.run(routes.delete_issue(request("o/r:1")))
        assert response["background"] and entered.wait(5)
        assert not fix_engine._claim_issue("O/R:001")
        assert storage.load_processed() == {}
    finally:
        resume.set()
    assert released.wait(5)
    assert len(attempts) == (routes._DISMISS_MAX_RETRIES if exhausted else 3)
    assert routes.state["dismiss_jobs"]["o/r:1"]["status"] == ("error" if exhausted else "done")
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


@pytest.mark.parametrize("failed", [False, True])
def test_bulk_background_close_keeps_alias_claims_until_its_last_effect(history, monkeypatch, failed):
    """Fresh raw row IDs stay scoped while duplicate identities release only once."""
    records = {"o/r:1": {"status": "resolved"}, "O/R:001": {"status": "pending_verification"}}
    seed(records)
    jobs, closed = [], []
    monkeypatch.setattr(routes, "threading", N(
        Thread=lambda **kwargs: N(start=lambda: jobs.append(kwargs["target"]))))
    def close(issue_id):
        """Check the batch claim during every actual background-loop invocation."""
        assert not fix_engine._claim_issue("o/r:1")
        closed.append(issue_id)
        if failed:
            raise OSError("GitHub offline")
        return True, "closed"
    monkeypatch.setattr(routes, "_close_issue_on_github", close)
    response = asyncio.run(routes.delete_all_issues(request("unused")))
    assert response["status"] == "success" and storage.load_processed() == {}
    assert not fix_engine._claim_issue("o/r:1")
    jobs[0]()
    assert closed == list(records)
    assert fix_engine._claim_issue("o/r:1")
    fix_engine._release_issue("o/r:1")


def test_bulk_clear_background_ids_are_the_committed_removed_set(history, monkeypatch):
    seed({"one": {}, "two": {}})
    jobs = []
    monkeypatch.setattr(routes, "threading", N(Thread=lambda **kw: N(start=lambda: jobs.append(kw["target"]))))
    closed = []
    monkeypatch.setattr(routes, "_close_issue_on_github", lambda key: (closed.append(key) or True, "ok"))
    response = asyncio.run(routes.delete_all_issues(request("unused")))
    app.update_processed(lambda c: c.update({"later": {}}))
    jobs[0]()
    assert response["status"] == "success" and closed == ["one", "two"]
    assert storage.load_processed() == {"later": {}}
    assert_view()


def test_pipeline_nonactionable_keeps_fresh_budget_and_handoff(history, monkeypatch):
    seed({"o/r:1": {"auto_retries": 1, "pipeline": "fix_engine"}})
    issue = N(title="title", body="body", create_comment=lambda text: None)
    monkeypatch.setattr(fix_engine, "load_config", lambda: {"GITHUB_TOKEN": "test"})
    monkeypatch.setattr(fix_engine, "Github", lambda token: N(get_repo=lambda name: N(get_issue=lambda n: issue)))
    def analyze(value):
        app.update_processed(lambda c: c["o/r:1"].update(auto_retries=8, classifier_reason="fresh"))
        return False, "needs detail"
    monkeypatch.setattr(fix_engine, "analyze_issue", analyze)
    result = fix_engine.process_single_issue("o/r", 1)
    rec = storage.load_processed()["o/r:1"]
    assert result == (False, "Non-actionable: needs detail")
    assert rec["auto_retries"] == 8 and rec["classifier_reason"] == "fresh"
    assert rec["status"] == "non-actionable"
    assert_view()


def test_pipeline_storage_failure_is_not_rewritten_or_sent_to_another_model(history, monkeypatch):
    seed({"o/r:1": {"status": "resolved", "keep": True}})
    before = history.read_bytes()
    issue = N(title="title", body="body")
    monkeypatch.setattr(fix_engine, "load_config", lambda: {"GITHUB_TOKEN": "test"})
    monkeypatch.setattr(fix_engine, "Github", lambda token: N(get_repo=lambda name: N(get_issue=lambda n: issue)))
    monkeypatch.setattr(fix_engine, "analyze_issue", lambda issue: pytest.fail("must stop before triage"))
    writes = []
    def fail(*args, **kwargs):
        writes.append(True)
        raise OSError("disk full")
    monkeypatch.setattr(storage, "_atomic_write_json", fail)
    result = fix_engine.process_single_issue("o/r", 1)
    assert result[0] is False and "History persistence failed" in result[1]
    assert len(writes) == 1 and history.read_bytes() == before
    assert_view()


@pytest.mark.parametrize("name,status", [
    ("non_actionable", "non-actionable"), ("queue_review", "awaiting_review"),
    ("await_human", "awaiting_human"), ("failed", "failed"), ("triaged", "triaged"),
])
def test_actual_pipeline_replacements_use_fresh_retained_fields(history, name, status):
    """Run the actual nested callbacks, not approximations of their record formats."""
    seed({"o/r:1": {"auto_retries": 7, "needs_human_at": "fresh",
                     "pipeline": "fix_engine", "classifier_reason": "fresh", "unretained": True},
          "other": {"status": "fixed"}})
    tree = ast.parse(Path(fix_engine.__file__).read_text())
    pipeline = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "process_single_issue")
    node = next(n for n in ast.walk(pipeline) if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(vars(fix_engine), issue_id="o/r:1", stamp="now", confidence=.95,
              fixes=[], original_body="body", request_msg="needs detail",
              failure_reason="review rejected", last_failure={}, max_attempts=3)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "pipeline_callback", "exec"), ns)
    app.update_processed(ns[name])
    rec = storage.load_processed()["o/r:1"]
    assert rec["status"] == status and rec["auto_retries"] == 7
    assert rec["needs_human_at"] == "fresh" and "unretained" not in rec
    if name in ("non_actionable", "failed"):
        assert rec["classifier_reason"] == "fresh" and rec["pipeline"] == "fix_engine"
    assert storage.load_processed()["other"] == {"status": "fixed"}
    assert_view()


@pytest.mark.parametrize("path", ["missing_skill", "handoff"])
def test_feature_early_storage_failures_keep_tuple_contract_and_stop_dispatch(history, monkeypatch, path):
    """Both early build exits precede the normal build try/except."""
    history.write_text("{")
    comments = []
    issue = N(number=1, title="feature", html_url="u",
              get_comments=list, get_labels=list, add_to_labels=lambda *a: None,
              create_comment=lambda body: comments.append(body))
    repo = N(full_name="o/r")
    monkeypatch.setattr(feature_build, "_ensure_label", lambda *a: True)
    monkeypatch.setattr(feature_build, "_build_mode", lambda config: None)
    monkeypatch.setattr(feature_build, "process_single_issue", lambda *a: pytest.fail("no dispatch after failure"))
    result = feature_build.build_feature(
        None, repo, issue, {} if path == "missing_skill" else {"skill": "skill"}, {})
    assert result[0] is False and "History persistence failed" in result[1]
    assert history.read_text() == "{"
    if path == "missing_skill":
        assert comments  # The response must allow for this external partial outcome.


def test_already_fixed_external_close_then_storage_failure_returns_false(history):
    history.write_text("{")
    edits = []
    issue = N(body="body", create_comment=lambda body: None, add_to_labels=lambda *a: None,
              edit=lambda **kw: edits.append(kw))
    result = fix_engine._resolve_as_already_fixed(
        issue, "o/r:1", "o/r", 1, {"confidence": .99, "evidence": "verified"}, 0, app.state)
    assert edits == [{"state": "closed"}]
    assert result[0] is False and "history persistence failed" in result[1]
    assert history.read_text() == "{"


def test_real_build_error_and_failed_failure_recording_are_both_reported(history, monkeypatch):
    history.write_text("{")
    monkeypatch.setattr(feature_build, "_build_mode", lambda config: "agent")
    def unavailable(skill):
        raise RuntimeError("recipe lookup unavailable")
    monkeypatch.setattr(feature_build.skills_loader, "skill_instructions", unavailable)
    result = feature_build.build_feature(
        None, N(full_name="o/r"), N(number=1), {"skill": "skill"}, {"GITHUB_TOKEN": "test"})
    assert result[0] is False and "recipe lookup unavailable" in result[1]
    assert "History persistence failed" in result[1] and history.read_text() == "{"


@pytest.mark.parametrize("existing", [False, True])
def test_full_feature_path_reports_known_pr_when_final_disk_commit_fails(history, monkeypatch, caplog, existing):
    """Run the full production build with fake external I/O and real failed history I/O."""
    import test_feature_build as harness

    # This existing AST harness reads source relative to the repository root.
    with monkeypatch.context() as location:
        location.chdir(Path(feature_build.__file__).parent)
        ns = harness._load_ns()
    ns.update(update_processed=app.update_processed, ProcessedStoreError=storage.ProcessedStoreError,
              logger=feature_build.logger)
    seed({"owner/repo:7": {"status": "feature_flagged", "title": "keep"},
          "other": {"status": "resolved"}})
    before, view = history.read_bytes(), copy.deepcopy(app.state)
    writes, url_reads = [], []
    url = "https://github.com/owner/repo/pull/456"

    class PR(harness._FakePR):
        @property
        def html_url(self):
            assert not storage._processed_lock._is_owned()
            url_reads.append(True)
            return url

        def __init__(self):
            self.number, self.added_labels = 456, []

    pr = PR()
    repo = harness._FakeGhRepo()
    def create_pull(**kwargs):
        repo.created_prs.append(kwargs)
        return pr
    repo.create_pull = create_pull
    ns["_pr_holder"]["existing"] = pr if existing else None
    ns["skills_loader"]._instructions["skill"] = "# Build this small documented change"
    ns["_llm_holder"]["responses"] = ['{"pr_body": "Built change and docs"}']
    git_repo = harness._FakeRepoGit(changed=["page.py", "docs/change.md"])
    def clone(url, path, branch=None):
        for name in git_repo._changed:
            dest = Path(path) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("")
        return git_repo
    monkeypatch.setattr(ns["git"].Repo, "clone_from", clone)
    def forbidden(*args, **kwargs):
        pytest.fail("final history failure must not create another failure record or model attempt")
    ns["_mark_failed"] = forbidden
    ns["process_single_issue"] = forbidden
    atomic_write = storage._atomic_write_json
    def trace_write(path, data, **kwargs):
        writes.append(copy.deepcopy(data))
        return atomic_write(path, data, **kwargs)
    def fail_replace(source, destination):
        assert destination == str(history)
        raise OSError("history disk full")
    monkeypatch.setattr(storage, "_atomic_write_json", trace_write)
    monkeypatch.setattr(storage.os, "replace", fail_replace)
    result = ns["build_feature"](
        None, repo, harness._FakeGhIssue(number=7), {"skill": "skill"},
        {"GITHUB_TOKEN": "test", "feature_require_docs": True})
    assert result[0] is False and url in result[1]
    assert "History persistence failed" in result[1] and url in caplog.text
    assert url_reads and len(writes) == 1
    assert writes[0]["owner/repo:7"]["status"] == "feature_built"
    assert len(ns["_llm_holder"]["calls"]) == 1
    assert len(git_repo.commits) == len(git_repo.remotes.origin.pushed) == 1
    assert len(repo.created_prs) == (0 if existing else 1)
    assert history.read_bytes() == before and app.state == view


@pytest.mark.parametrize("kind", ["Direct Commit", "Commit to dev", "Pull Request"])
def test_actual_fix_publication_tail_preserves_known_outcome_on_disk_failure(history, monkeypatch, caplog, kind):
    """Run actual publication/record/outer-handler statements, not a copied callback."""
    seed({"o/r:1": {"status": "processing", "title": "keep"},
          "other": {"status": "resolved"}})
    before, view = history.read_bytes(), copy.deepcopy(app.state)
    writes, callbacks, external, ended = [], [], [], []
    commit, url = "0123456789abcdef0123456789abcdef01234567", "https://github.com/o/r/pull/123"

    class PR:
        number = 123
        @property
        def html_url(self):
            assert not storage._processed_lock._is_owned()
            return url

    issue = N(number=1, title="Fix", body="body",
              create_comment=lambda body: external.append("comment"),
              edit=lambda **kwargs: external.append("closed"))
    repo = N(clone_url="https://github.com/o/r.git", default_branch="main",
             create_pull=lambda **kwargs: PR())
    git_repo = N(head=N(commit=N(hexsha=commit)),
                 git=N(checkout=lambda *args: None, add=lambda **kwargs: None),
                 index=N(commit=lambda message: N(hexsha=commit)), remotes=N(origin=N()))
    def update(mutator):
        callbacks.append(mutator.__name__)
        return app.update_processed(mutator)
    atomic_write = storage._atomic_write_json
    def trace_write(path, data, **kwargs):
        writes.append(copy.deepcopy(data))
        return atomic_write(path, data, **kwargs)
    def fail_replace(source, destination):
        assert destination == str(history)
        raise OSError("history disk full")
    monkeypatch.setattr(storage, "_atomic_write_json", trace_write)
    monkeypatch.setattr(storage.os, "replace", fail_replace)
    def forbidden(*args, **kwargs):
        pytest.fail("publication failure must not retry a model")

    ns = dict(vars(fix_engine), state=app.state, issue=issue, issue_id="o/r:1",
              repo_name="o/r", issue_num=1, repo_obj=repo, repo_git=git_repo, config={},
              path="unused-no-filesystem", can_direct_push=kind == "Direct Commit",
              final_confidence=.91 if kind == "Commit to dev" else .99,
              confidence_threshold=.95, final_verdict="Approve", boundary_forced_pr=False,
              boundary_pr_reason="", direct_push_refused_reason=None, decision_reason="approved",
              base_branch="dev", token="test", commit_msg="AI Fix #1", fixes={"page.py": "pass"},
              was_reopened=False, update_processed=update, update_task_state=lambda **kw: ended.append(kw),
              _notify_bug_fixed=lambda issue: external.append("notified"),
              _apply_closed_label=lambda *args: external.append("labelled"),
              _opened_at=lambda issue: None, _progress=lambda *args, **kwargs: None,
              _trigger_spoke_updates=lambda config: external.append("update requested"),
              _wait_for_spokes_online=lambda *args, **kwargs: None,
              _authenticated_remote=lambda *args: contextlib.nullcontext(),
              may_force_push=lambda *args, **kwargs: (False, "protected"),
              integration_branch=lambda *args: ("dev", "integration"),
              auto_branch_name=lambda *args, **kwargs: "ai-fix/1",
              bump_repo_version=lambda path: None,
              _publication_boundary_requirement=lambda *args: (False, ""),
              _fix_pr_body=lambda *args: "Reviewed fix",
              push_or_raise=lambda *args, **kwargs: external.append("pushed"),
              find_existing_pull_request=lambda *args: None,
              call_llm=forbidden, apply_ai_fix=forbidden,
              _release_issue=lambda issue_id: None)
    tree = ast.parse(Path(fix_engine.__file__).read_text())
    pipeline = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "process_single_issue")
    # Include actual pushes and their evidence capture, not just the record tail.
    parent, index = next(
        (node, i) for node in ast.walk(pipeline) if isinstance(getattr(node, "body", None), list)
        for i, stmt in enumerate(getattr(node, "body", []))
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "version_bumped" for target in stmt.targets))
    outer = next(n for n in pipeline.body if isinstance(n, ast.Try) and n.finalbody)
    tail = copy.deepcopy(outer)
    tail.body = copy.deepcopy(parent.body[index:])
    fn = ast.parse(
        "def publish(issue_id=issue_id, base_branch=base_branch, can_direct_push=can_direct_push,\n"
        "            boundary_forced_pr=boundary_forced_pr, boundary_pr_reason=boundary_pr_reason):\n"
        "    publication = {}\n"
        "    publication_context = {}\n"
        "    publication_counted = False\n"
        "    pass\n").body[0]
    fn.body[-1] = tail
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name in ("_push_fix_with_evidence", "_record_fix_publication",
                                 "_fix_publication_outcome")]
    exec(compile(ast.fix_missing_locations(ast.Module(body=helpers + [fn], type_ignores=[])),
                 "actual_fix_publication", "exec"), ns)
    result = ns["publish"]()
    assert result[0] is False and commit in result[1] and kind in result[1]
    assert "History persistence failed" in result[1] and commit in caplog.text
    if kind == "Pull Request":
        assert url in result[1] and url in caplog.text
        assert writes[0]["o/r:1"]["pr_url"] == url
    else:
        assert "https://" not in result[1] and "pr_url" not in writes[0]["o/r:1"]
    assert callbacks == ["fixed"] and len(writes) == 1
    assert writes[0]["o/r:1"]["commit"] == commit
    assert writes[0]["o/r:1"]["commit_type"] == kind
    assert not {"closed", "notified", "update requested"}.intersection(external)
    assert ended == [{"task_id": "o/r:1", "action": "end"}]
    assert history.read_bytes() == before and app.state == view


def owned_sources(root):
    """Use Git ownership, not directory recursion into CI dependency checkouts."""
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
                            check=True, capture_output=True, text=True)
    for name in result.stdout.split("\0"):
        if not name:
            continue
        path = Path(name)
        if (path.name.startswith("test_") or any(
                part.startswith(".") or part in ("tests", "venv", "node_modules")
                for part in path.parts)):
            continue
        yield root / path


def assert_history_writers(root):
    """Reject alternate history writers and stale application-view publication."""
    for path in owned_sources(root):
        tree = ast.parse(path.read_text())
        history_names = {
            alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            and node.module in ("config_store", "main")
            for alias in node.names if alias.name == "STATE_FILE"}
        history_modules = {
            alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names if alias.name in ("config_store", "main")}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and path.name != "config_store.py":
                assert all(alias.name != "save_processed" for alias in node.names), path
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                assert name != "save_processed", (path, node.lineno)
                if path.name != "config_store.py" and name in (
                        "open", "_atomic_write_json", "replace", "write_text", "write_bytes"):
                    assert not any(
                        (isinstance(part, ast.Name) and part.id in history_names)
                        or (isinstance(part, ast.Attribute) and part.attr == "STATE_FILE"
                            and isinstance(part.value, ast.Name) and part.value.id in history_modules)
                        or (isinstance(part, ast.Constant) and part.value == "processed_issues.json")
                        for arg in node.args for part in ast.walk(arg)), (path, node.lineno)
                if name == "recompute_issue_counters" and path.name != "app_state.py":
                    assert not node.args and not node.keywords, (path, node.lineno)
            if isinstance(node, ast.Assign) and path.name != "app_state.py":
                for target in node.targets:
                    assert not (isinstance(target, ast.Subscript)
                                and isinstance(target.value, ast.Name) and target.value.id == "state"
                                and isinstance(target.slice, ast.Constant)
                                and target.slice.value == "processed"), (path, node.lineno)


def test_source_guard_no_raw_production_savers_or_stale_view_publication():
    assert_history_writers(Path(__file__).resolve().parents[1])


def test_source_guard_ignores_nested_untracked_foreign_checkout_but_catches_owned(tmp_path):
    """A CI dependency clone must never enlarge the owning repository's guard."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("owned.py", "nested/module.py", "tests/test_skip.py", ".scratch/skip.py", "venv/lib.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n" if name in ("owned.py", "nested/module.py") else "save_processed({})\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", name], check=True)
    foreign = tmp_path / "arbitrary-checkout" / "nested"
    foreign.mkdir(parents=True)
    (foreign / "main.py").write_text("save_processed({})\nstate['processed'] = {}\n")
    assert sorted(str(path.relative_to(tmp_path)) for path in owned_sources(tmp_path)) == [
        "nested/module.py", "owned.py"]
    assert_history_writers(tmp_path)
    for filename, code in (
        ("owned.py", "save_processed({})\n"),
        ("nested/module.py", "state['processed'] = {}\n"),
        ("nested/module.py", "recompute_issue_counters(stale_snapshot)\n"),
        ("nested/module.py", "from config_store import STATE_FILE\n_atomic_write_json(STATE_FILE, snapshot)\n"),
    ):
        path = tmp_path / filename
        path.write_text(code)
        with pytest.raises(AssertionError):
            assert_history_writers(tmp_path)
        path.write_text("pass\n")
