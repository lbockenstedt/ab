"""RepoScan only reads OPEN issues, so a record left failed/non-actionable
stayed in those AB buckets forever after its GitHub issue was closed."""
import copy
from types import SimpleNamespace as N

from github import GithubException

import main  # noqa: F401 - loads workers in import order
import workers


class Repo:
    def __init__(self, issues):
        self.issues = issues

    def get_issue(self, number):
        if number not in self.issues:
            raise GithubException(404, {"message": "Not Found"}, None)
        return self.issues[number]


class Gh:
    def __init__(self, issues):
        self.repo = Repo(issues)
        self.repo_calls = 0

    def get_repo(self, name):
        self.repo_calls += 1
        return self.repo


def _issue(state, *labels):
    return N(state=state, labels=[N(name=n) for n in labels])


def _run(monkeypatch, processed, gh, **kw):
    store = {"data": copy.deepcopy(processed)}
    recomputed = []
    monkeypatch.setattr(workers, "load_processed", lambda: copy.deepcopy(store["data"]))
    monkeypatch.setattr(workers, "save_processed", lambda p: store.update(data=copy.deepcopy(p)))
    monkeypatch.setattr(workers, "recompute_issue_counters", lambda p: recomputed.append(p))
    changed = workers.sync_github_issue_state(gh, **kw)
    return changed, store["data"], recomputed


def test_closed_gone_and_ops_alert_issues_leave_the_failed_buckets(monkeypatch):
    processed = {
        "o/lm:1": {"status": "failed", "timestamp": "2026-11-01T00:00:00"},
        "o/lm:2": {"status": "non-actionable", "timestamp": "2026-11-02T00:00:00"},
        "o/lm:1786": {"status": "failed", "timestamp": "2026-11-03T00:00:00"},
        "o/lm:4": {"status": "failed", "timestamp": "2026-11-04T00:00:00"},
        "o/lm:5": {"status": "pending_verification", "timestamp": "2026-11-05T00:00:00"},
        "o/lm:6": {"status": "fixed", "timestamp": "2026-11-06T00:00:00"},
    }
    gh = Gh({1: _issue("closed"), 2: _issue("open", "ops-alert"), 4: _issue("open", "automated-fix"),
             5: _issue("closed"), 6: _issue("closed")})
    changed, saved, recomputed = _run(monkeypatch, processed, gh)

    assert sorted(changed) == ["o/lm:1", "o/lm:1786", "o/lm:2"]
    assert saved["o/lm:1"]["status"] == "closed" and saved["o/lm:1"]["status_before_close"] == "failed"
    assert saved["o/lm:1"]["closed_at"]
    assert saved["o/lm:1786"]["status"] == "closed"  # 404: deleted or a PR
    assert saved["o/lm:2"]["status"] == "ops-alert"
    assert saved["o/lm:2"]["status_before_ops_alert"] == "non-actionable"
    assert saved["o/lm:4"]["status"] == "failed" and saved["o/lm:4"]["github_checked_at"]
    # Closed on GitHub by design while a person verifies the fix.
    assert saved["o/lm:5"]["status"] == "pending_verification"
    assert saved["o/lm:6"] == processed["o/lm:6"]
    assert recomputed and gh.repo_calls == 1


def test_lookups_are_bounded_and_rotate_oldest_first(monkeypatch):
    processed = {f"o/lm:{n}": {"status": "failed", "timestamp": f"2026-11-{n:02d}T00:00:00"}
                 for n in range(1, 6)}
    gh = Gh({n: _issue("open") for n in range(1, 6)})
    _, saved, recomputed = _run(monkeypatch, processed, gh, limit=2)
    assert sorted(k for k, v in saved.items() if v.get("github_checked_at")) == ["o/lm:1", "o/lm:2"]
    assert not recomputed

    _, saved, _ = _run(monkeypatch, saved, gh, limit=2)
    assert sorted(k for k, v in saved.items() if v.get("github_checked_at")) == \
        ["o/lm:1", "o/lm:2", "o/lm:3", "o/lm:4"]
