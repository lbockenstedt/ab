"""Reopen failures stay distinguishable through real escalation and heartbeat callers."""
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from _history import patch_history
from test_recurrence_comment import Issue
import github_ops
import log_scan


@pytest.fixture
def recurrence(monkeypatch):
    """Keep actual issue creation and caller logic while faking external services."""
    issue = Issue()
    issue.state = "closed"
    repo = SimpleNamespace(full_name="o/nw")
    gh = SimpleNamespace(get_repo=Mock(return_value=repo))
    duplicate = Mock(return_value=(issue, "o/nw", True))
    create = Mock(wraps=github_ops.create_automated_issue)
    monkeypatch.setattr(github_ops, "_in_update_cooldown", lambda: (False, 0))
    monkeypatch.setattr(github_ops, "find_global_duplicate_issue", duplicate)
    patch_history(monkeypatch, github_ops, {})
    monkeypatch.setattr(log_scan, "Github", lambda token: gh)
    monkeypatch.setattr(log_scan, "MODULE_TYPE_REPO", {"nw": "o/nw"})
    monkeypatch.setattr(log_scan, "get_monitored_repos", lambda config: ["o/nw"])
    monkeypatch.setattr(log_scan, "recent_deploy_changes", lambda *args: [])
    monkeypatch.setattr(log_scan, "escalation_is_operational", lambda *args: False)
    monkeypatch.setattr(log_scan, "create_automated_issue", create)
    return SimpleNamespace(
        issue=issue, gh=gh, duplicate=duplicate, create=create,
        config={"GITHUB_TOKEN": "unused-test-token"})


def _escalate(recurrence):
    """Call the production tier-2 helper with an actual recurring issue."""
    return log_scan.file_escalated_issue(
        "nw", "ERROR record write failed", "Record write failed",
        config=recurrence.config)


@pytest.fixture
def heartbeat(monkeypatch, recurrence):
    """Present one gave-up spoke with a warmed-up telemetry pipeline."""
    client = SimpleNamespace(request_sync=Mock(return_value={
        "approved": {"test-spoke": True},
        "module_types": {"test-spoke": "nw"},
        "recovery": {"test-spoke": {"gave_up": True}},
    }))
    monkeypatch.setattr(log_scan, "_get_hub_agent_client", lambda: client)
    monkeypatch.setattr(log_scan, "state", {})
    monkeypatch.setattr(log_scan.scan_heartbeats, "_hub_hb_observed", True, raising=False)
    monkeypatch.setattr(log_scan.scan_heartbeats, "_gave_up_filed", {}, raising=False)
    return partial(
        log_scan.scan_heartbeats, recurrence.gh, {"heartbeat_exclude": ["hub"]}, [])


def test_failed_reopen_escalation_is_failure_then_successful_retry(monkeypatch, recurrence):
    """The real helper must not translate a failed reopen into a successful no-op."""
    edit = recurrence.issue.edit
    monkeypatch.setattr(
        recurrence.issue, "edit", Mock(side_effect=RuntimeError("vendor payload")))
    assert _escalate(recurrence) == (False, "Could not reopen issue #211")
    assert recurrence.issue.state == "closed"
    assert not recurrence.issue.comments

    monkeypatch.setattr(recurrence.issue, "edit", edit)
    assert _escalate(recurrence) == (True, "o/nw#211")
    assert recurrence.issue.state == "open"


def test_escalation_cooldown_remains_successful_noop(monkeypatch, recurrence):
    """A deliberate cooldown keeps its established handled/no-op contract."""
    monkeypatch.setattr(github_ops, "_in_update_cooldown", lambda: (True, 60))
    assert _escalate(recurrence) == (True, "deduped/no-op in o/nw")
    recurrence.duplicate.assert_not_called()
    assert recurrence.issue.state == "closed"


def test_escalation_comment_failure_keeps_confirmed_reopen(monkeypatch, recurrence):
    """An optional comment failure is not a reopen failure at the escalation boundary."""
    monkeypatch.setattr(
        recurrence.issue, "create_comment", Mock(side_effect=RuntimeError("comment unavailable")))
    assert _escalate(recurrence) == (True, "o/nw#211")
    assert recurrence.issue.state == "open"
    assert not recurrence.issue.comments


def test_heartbeat_failed_reopen_is_not_latched_and_retries_once(
        monkeypatch, caplog, recurrence, heartbeat):
    """Failure must not permanently suppress the next heartbeat escalation attempt."""
    caplog.set_level("INFO")
    edit = recurrence.issue.edit
    monkeypatch.setattr(
        recurrence.issue, "edit", Mock(side_effect=RuntimeError("vendor payload")))
    heartbeat()
    assert not log_scan.scan_heartbeats._gave_up_filed
    assert "triaged 1 module" not in caplog.text
    assert not recurrence.issue.comments

    monkeypatch.setattr(recurrence.issue, "edit", edit)
    heartbeat()
    assert log_scan.scan_heartbeats._gave_up_filed == {"test-spoke": True}
    assert "triaged 1 module" in caplog.text
    assert recurrence.issue.state == "open"
    heartbeat()
    assert recurrence.create.call_count == 2
    assert len(recurrence.issue.comments) == 1


def test_heartbeat_cooldown_stays_latched_without_claiming_issue(
        monkeypatch, caplog, recurrence, heartbeat):
    """Benign suppression remains once per down-period but never counts a filed issue."""
    caplog.set_level("INFO")
    monkeypatch.setattr(github_ops, "_in_update_cooldown", lambda: (True, 60))
    heartbeat()
    heartbeat()
    assert log_scan.scan_heartbeats._gave_up_filed == {"test-spoke": True}
    assert recurrence.create.call_count == 1
    assert "triaged 1 module" not in caplog.text
    assert recurrence.issue.state == "closed"
