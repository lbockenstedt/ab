"""A failed/non-actionable issue whose error kept recurring was skipped forever
(lm#1931, netbox#290). It is now retried on a bounded schedule, then handed to
a person with the ab-needs-human label."""
import copy
from datetime import datetime
from types import SimpleNamespace as N

import main  # noqa: F401 - loads workers in import order
import workers

CFG = {"failed_retry_hours": 24, "failed_retry_max": 2}
NOW = datetime(2026, 5, 3, 12, 0, 0)


def _rec(**kw):
    rec = {"status": "failed", "timestamp": "2026-05-01 10:00:00",
           "last_recurrence_at": "2026-05-02T09:00:00"}
    rec.update(kw)
    return rec


def test_recurring_failure_is_retried_after_the_wait():
    assert workers.failed_retry_decision(_rec(), CFG, NOW) == "retry"


def test_no_retry_without_a_new_recurrence_or_before_the_wait():
    assert workers.failed_retry_decision(_rec(last_recurrence_at=None), CFG, NOW) == "wait"
    assert workers.failed_retry_decision(
        _rec(last_recurrence_at="2026-05-01T09:00:00"), CFG, NOW) == "wait"
    assert workers.failed_retry_decision(_rec(), CFG, datetime(2026, 5, 2, 9, 0)) == "wait"


def test_after_the_cap_it_goes_to_a_human_once():
    assert workers.failed_retry_decision(_rec(auto_retries=2), CFG, NOW) == "needs_human"
    assert workers.failed_retry_decision(
        _rec(auto_retries=2, needs_human_at="x"), CFG, NOW) == "wait"
    assert workers.failed_retry_decision(_rec(), {"failed_retry_max": 0}, NOW) == "wait"


class _Issue:
    def __init__(self):
        self.labels, self.comments = [], []
        self.repository = N()

    def add_to_labels(self, name):
        self.labels.append(name)

    def create_comment(self, body):
        self.comments.append(body)


def _store(monkeypatch, rec):
    store = {"data": {"o/lm:1": rec}}
    monkeypatch.setattr(workers, "load_processed", lambda: copy.deepcopy(store["data"]))
    monkeypatch.setattr(workers, "save_processed", lambda p: store.update(data=copy.deepcopy(p)))
    monkeypatch.setattr(main, "_ensure_label", lambda repo, name: None, raising=False)
    return store


def test_retry_counts_and_persists(monkeypatch):
    store = _store(monkeypatch, _rec(timestamp="2000-01-01 00:00:00"))
    processed = copy.deepcopy(store["data"])
    assert workers._retry_failed_issue(_Issue(), "o/lm:1", processed, CFG) is True
    assert store["data"]["o/lm:1"]["auto_retries"] == 1
    assert processed["o/lm:1"]["auto_retries"] == 1


def test_cap_labels_and_comments_once(monkeypatch):
    store = _store(monkeypatch, _rec(timestamp="2000-01-01 00:00:00", auto_retries=2,
                                     error="Reviewers rejected the fix"))
    processed = copy.deepcopy(store["data"])
    issue = _Issue()
    assert workers._retry_failed_issue(issue, "o/lm:1", processed, CFG) is False
    assert issue.labels == ["ab-needs-human"] and len(issue.comments) == 1
    assert "Reviewers rejected the fix" in issue.comments[0]
    assert workers._retry_failed_issue(issue, "o/lm:1", processed, CFG) is False
    assert len(issue.comments) == 1
