"""Tests for ghost-alert detection and the sweep that closes them."""
import main  # noqa: F401  (imported first: log_scan and workers rely on main's import order)
import log_scan
import workers


def _body(line, mod="ab-core"):
    """An AB-filed issue body carrying a log line."""
    return (f"**Automated Error Detection**\n\n### Log Evidence:\n```\n{line}\n```\n\n"
            f"This issue has been automatically created for fixing.\n\n<!-- bf-module: {mod} -->")


def test_refused_edit_is_ghost():
    """AB's own refused-edit line is a ghost."""
    assert log_scan.ghost_reason(_body("Skipping edit: target file does not exist"), None)


def test_llm_outage_ghost_only_when_online():
    """An LLM outage alert is a ghost only while an endpoint is online."""
    b = _body("LLM outage: no endpoint online")
    assert log_scan.ghost_reason(b, True)
    assert not log_scan.ghost_reason(b, False)
    assert not log_scan.ghost_reason(b, None)


def test_person_report_and_real_error_are_not_ghosts():
    """Human reports and other modules' errors stay open."""
    assert not log_scan.ghost_reason("Skipping edit: x", True)
    assert not log_scan.ghost_reason(_body("LLM outage", mod="lm"), True)


class _Issue:
    """Minimal GitHub issue stub."""
    def __init__(self, n, body):
        self.number, self.body, self.state = n, body, "open"
        self.comments = []

    def create_comment(self, t):
        """Record a comment."""
        self.comments.append(t)

    def edit(self, state):
        """Record the state."""
        self.state = state

    def add_to_labels(self, *a):
        """Accept a label."""


class _Repo:
    """Minimal repo stub."""
    full_name = "o/ab"

    def __init__(self, issues):
        self.issues = issues

    def get_issues(self, **kw):
        """Return the stub issues."""
        return self.issues

    def get_label(self, n):
        """Pretend the label exists."""
        return object()


def test_sweep_closes_only_ghosts(monkeypatch):
    """The sweep closes ghosts, comments why, and leaves real issues."""
    ghost = _Issue(1, _body("Skipping malformed edit"))
    real = _Issue(2, _body("Traceback in scanner"))
    monkeypatch.setattr(workers, "update_processed", lambda fn: fn({}))
    monkeypatch.setattr(workers.main, "_apply_closed_label", lambda *a: None, raising=False)
    assert workers.close_ghost_issues(_Repo([ghost, real]), True) == [1]
    assert ghost.state == "closed" and "ghost" in ghost.comments[0]
    assert real.state == "open"


def test_unrelated_noise_in_body_is_not_ghost():
    """Other self-scan noise lines (e.g. a persistence failure) are real faults, not ghosts."""
    assert not log_scan.ghost_reason(_body("Could not save to persistent storage"), True)
    assert not log_scan.ghost_reason(_body("No matching LLM model available"), True)
    assert not log_scan.ghost_reason(_body("Skipping edit: x", mod="lm"), True)


def test_phrase_outside_evidence_is_not_ghost():
    """A real fault whose surrounding text quotes a refused edit stays open."""
    b = _body("KeyError: 'x'") + "\nnote: earlier Skipping edit lines"
    assert not log_scan.ghost_reason(b, True)


def test_sweep_skips_pull_requests(monkeypatch):
    """A labelled PR is never closed by the sweep."""
    pr = _Issue(3, _body("Skipping edit: x"))
    pr.pull_request = object()
    monkeypatch.setattr(workers, "update_processed", lambda fn: fn({}))
    assert workers.close_ghost_issues(_Repo([pr]), True) == []
    assert pr.state == "open"


def test_requeue_stale_holds_caps_and_needs_llm(monkeypatch):
    """Held-for-no-model records become failed (retry lane), at most `limit` per call, only with an LLM online."""
    store = {f"o/r:{i}": {"status": "awaiting_human", "reason": workers.HELD_NO_MODEL_REASON,
                          "timestamp": "2026-11-26T02:48:00"} for i in range(5)}
    store["o/r:9"] = {"status": "awaiting_human", "reason": "other", "timestamp": "2026-11-26T02:48:00"}
    monkeypatch.setattr(workers, "update_processed", lambda fn: fn(store))
    cycle = {}
    assert workers.requeue_stale_holds(cycle, False, {}) == []
    assert workers.requeue_stale_holds(cycle, True, {"held_requeue_enabled": False}) == []
    assert workers.requeue_stale_holds(cycle, True, {"failed_retry_max": 0}) == []
    store["o/r:0"]["needs_human_at"] = "2026-11-25T00:00:00"
    assert "o/r:0" not in workers.requeue_stale_holds(cycle, True, {})
    store["o/r:0"].pop("needs_human_at")
    for i in (1, 2, 3):
        store[f"o/r:{i}"]["status"] = "awaiting_human"
    done = workers.requeue_stale_holds(cycle, True, {})
    assert len(done) == 3 and all(store[i]["status"] == "failed" for i in done)
    assert store["o/r:9"]["status"] == "awaiting_human"
    rec = store[done[0]]
    assert rec["last_recurrence_at"] > rec["timestamp"]
    assert workers.failed_retry_decision(rec, {}, now=__import__("datetime").datetime(2026, 11, 28)) == "retry"


def test_clear_needs_human_label_removes_label():
    """The requeue drops ab-needs-human from the issue; a missing issue is swallowed."""
    calls = []

    class Issue:
        """Records label removals."""
        def remove_from_labels(self, name):
            """Record the removed label."""
            calls.append(name)

    class Repo:
        """Returns the fake issue."""
        def get_issue(self, n):
            """Return the issue for number n."""
            calls.append(n)
            return Issue()

    class Gh:
        """Returns the fake repo."""
        def get_repo(self, name):
            """Return the repo named name."""
            calls.append(name)
            return Repo()

    workers._clear_needs_human_label(Gh(), "o/r:7")
    assert calls == ["o/r", 7, "ab-needs-human"]
    workers._clear_needs_human_label(object(), "bad")
