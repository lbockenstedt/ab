"""gh_actions_health — back off commits/merges while GitHub Actions is down.

During an Actions outage every workflow run fails with zero jobs and no check
run is ever created, so a pushed fix or a merge can only end in "Required
status check is expected". AppBuilder must wait (with a growing backoff) and
resume on its own once Actions recovers.
"""
import pytest

import gh_actions_health as gha


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    gha._state.update(status_at=0.0, status_down=False, status_text="",
                      symptoms={}, backoff_until=0.0, backoff_s=0.0, reason="")
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(gha.time, "time", lambda: clock["t"])
    page = {"status": "operational"}

    class _R:
        def json(self):
            return {"components": [{"name": "Actions", "status": page["status"]}]}

    monkeypatch.setattr(gha.requests, "get", lambda *a, **k: _R())
    yield clock, page


def _advance(clock, minutes):
    clock["t"] += minutes * 60 + 1


def test_healthy_actions_holds_nothing():
    assert gha.outage({}) == (False, "")


def test_status_page_outage_backs_off_and_grows_then_resets(_reset):
    clock, page = _reset
    page["status"] = "major_outage"
    cfg = {"gh_backoff_start_min": 5, "gh_backoff_max_min": 12}
    down, why = gha.outage(cfg)
    assert down and "major_outage" in why
    assert gha._state["backoff_s"] == 300
    # Inside the window: still held, no re-check needed.
    assert gha.outage(cfg)[0]
    _advance(clock, 5)
    assert gha.outage(cfg)[0] and gha._state["backoff_s"] == 600
    _advance(clock, 10)
    assert gha.outage(cfg)[0] and gha._state["backoff_s"] == 720  # capped
    page["status"] = "operational"
    _advance(clock, 12)
    assert gha.outage(cfg) == (False, "")
    assert gha._state["backoff_s"] == 0


def test_zero_job_failures_alone_trigger_backoff():
    gha.note_infra_failure("o/a", [101])
    assert gha.outage({}) == (False, "")
    gha.note_infra_failure("o/b", [202])
    down, why = gha.outage({})
    assert down and "zero jobs" in why


def test_old_sightings_expire(_reset):
    clock, _ = _reset
    gha.note_infra_failure("o/a", [101, 102])
    _advance(clock, 16)
    assert gha.outage({}) == (False, "")


def test_one_stuck_run_polled_repeatedly_is_not_an_outage(_reset):
    """A run that can never start (e.g. a workflow YAML error) is seen on every
    poll; counting each sighting made it an outage on the second poll."""
    clock, _ = _reset
    for _ in range(5):
        gha.note_infra_failure("o/a", [101])
        assert gha.outage({}) == (False, "")
        _advance(clock, 1)
    # Same id in another case, or as an int vs str, is still the same run.
    gha.note_infra_failure("O/A", ["101"])
    assert gha.outage({}) == (False, "")


def test_a_stuck_run_is_not_recounted_after_the_window(_reset):
    """Re-seeing an old stuck run must not pair with one new run to fake an outage."""
    clock, _ = _reset
    gha.note_infra_failure("o/a", [101])
    _advance(clock, 30)
    gha.note_infra_failure("o/a", [101])
    gha.note_infra_failure("o/b", [202])
    assert gha.outage({}) == (False, "")


def test_disabled_never_holds(_reset):
    _, page = _reset
    page["status"] = "major_outage"
    assert gha.outage({"gh_outage_backoff_enabled": False}) == (False, "")


def test_unreachable_status_page_is_not_an_outage(monkeypatch):
    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr(gha.requests, "get", boom)
    assert gha.outage({}) == (False, "")
