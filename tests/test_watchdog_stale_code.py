"""The watchdog restarts AB when the running process is on an older commit
than the checkout on disk, even with no update_pending file. Otherwise the old
process keeps serving while Jinja hot-reloads the new templates, and pages fail
with errors like "'feed_counts' is undefined"."""

import logging
from datetime import datetime, timedelta
from unittest import mock

# watchdog.py logs to /var/log/ab_watchdog.log on import; keep the test off it.
with mock.patch.object(logging, "FileHandler", lambda *a, **k: logging.NullHandler()):
    import watchdog


def _setup(monkeypatch, running, on_disk, age_s=3600):
    restarts = []
    monkeypatch.setattr(watchdog, "_last_stale_code_restart", 0.0)
    monkeypatch.setattr(watchdog, "is_service_active", lambda: True)
    monkeypatch.setattr(watchdog, "read_startup_time",
                        lambda: datetime.now() - timedelta(seconds=age_s))
    monkeypatch.setattr(watchdog, "read_startup_stamp", lambda: running)
    monkeypatch.setattr(watchdog, "disk_commit", lambda: on_disk)
    monkeypatch.setattr(watchdog, "spawn_restart", lambda: restarts.append(1))
    return restarts


def test_restarts_once_when_running_commit_differs_from_disk(monkeypatch):
    restarts = _setup(monkeypatch, "a" * 40, "b" * 40)
    assert watchdog.check_running_code_stale(now=10_000) is True
    # Rate-limited: a restart that didn't take can't loop.
    assert watchdog.check_running_code_stale(now=10_060) is False
    assert restarts == [1]


def test_no_restart_when_current_or_just_started(monkeypatch):
    restarts = _setup(monkeypatch, "a" * 40, "a" * 40)
    assert watchdog.check_running_code_stale(now=10_000) is False
    restarts = _setup(monkeypatch, "a" * 40, "b" * 40, age_s=10)
    assert watchdog.check_running_code_stale(now=10_000) is False
    assert restarts == []
