"""connectivity_worker raises an ERROR when no LLM endpoint is online.

On 2026-11-17 AB had 0 LLM endpoints for 13 hours and the only trace of it was
the INFO line "Connectivity Check: ...; entries 0/0 online", so nothing alerted.
"""
import re
import sys
import types

import jinja2
import pytest

import main  # noqa: F401 - loads workers in import order
import workers


class _StopLoop(BaseException):
    """Ends connectivity_worker's endless loop from the patched sleep."""


def _run_worker(monkeypatch, rounds):
    """Run connectivity_worker once per entry in `rounds` (the endpoint list each
    probe returns); return the (errors, infos) it logged."""
    errors, infos = [], []
    pending = list(rounds)

    def stop_after_last(_seconds):
        """Stand in for the 15-minute sleep; end the loop once every round ran."""
        if not pending:
            raise _StopLoop

    monkeypatch.setattr(workers, "load_config", lambda: {})
    monkeypatch.setattr(workers, "_check_provider_online", lambda *_: False)

    def sweep(_cfg):
        """Return the next scripted endpoint list, or raise it when it is an exception."""
        item = pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(workers, "_check_entries_online", sweep)
    monkeypatch.setattr(workers.time, "sleep", stop_after_last)
    monkeypatch.setattr(workers.logger, "error", lambda msg, *a, **k: errors.append(msg))
    monkeypatch.setattr(workers.logger, "info", lambda msg, *a, **k: infos.append(msg))
    try:
        workers.connectivity_worker()
    except _StopLoop:
        pass
    return errors, infos


def test_outage_is_logged_once_at_error_and_clear_is_noted(monkeypatch):
    """Zero endpoints online -> one ERROR per outage (not one per 15-minute check),
    and an INFO when an endpoint comes back."""
    up = [{"online": True}]
    errors, infos = _run_worker(monkeypatch, [[], [{"online": False}], up])
    assert len(errors) == 1 and errors[0].startswith("LLM outage: 0 of 0 LLM endpoints")
    assert any(m.startswith("LLM outage cleared: 1 of 1") for m in infos)


def test_failing_to_enumerate_endpoints_is_not_an_outage(monkeypatch):
    """A sweep that could not list the endpoints says nothing about them: it is a monitoring
    error, not "0 configured". It does not alert an outage, and it does not reset the
    state either, so an outage that continues across it is still one outage."""
    down = [{"online": False}]
    errors, _ = _run_worker(monkeypatch, [RuntimeError("cannot enumerate"), [{"online": True}]])
    assert errors == ["Connectivity worker error: cannot enumerate"]
    errors, _ = _run_worker(monkeypatch, [down, RuntimeError("cannot enumerate"), down])
    assert [e for e in errors if e.startswith("LLM outage:")] == [errors[0]]
    assert len(errors) == 2


def test_check_entries_online_raises_when_llm_client_cannot_be_loaded(monkeypatch):
    """An import failure must reach the caller instead of returning [] (which reads as an outage)."""
    monkeypatch.setitem(sys.modules, "llm_client", types.ModuleType("llm_client"))
    with pytest.raises(ImportError):
        workers._check_entries_online({})


def test_throttled_endpoint_is_alive_so_not_an_outage(monkeypatch):
    """An endpoint that answered 429 is alive. All-throttled, or throttled plus offline,
    must not claim every AI function fails."""
    throttled = {"online": False, "throttled": True}
    errors, _ = _run_worker(monkeypatch, [[throttled, throttled], [throttled, {"online": False}]])
    assert errors == []


def test_throttled_is_degraded_not_cleared_and_not_a_silent_second_outage(monkeypatch):
    """offline -> throttled -> offline: the 429 sweep is reported as rate-limited, never as
    "cleared" (no endpoint is online), and the return to nothing answering alerts again."""
    down = [{"online": False}]
    throttled = [{"online": False, "throttled": True}]
    errors, infos = _run_worker(monkeypatch, [down, throttled, down])
    assert len(errors) == 2
    assert any("rate-limited" in m and "0 of 1 are online yet" in m for m in infos)
    assert not any(m.startswith("LLM outage cleared") for m in infos)


def test_outage_clears_only_when_an_endpoint_is_online(monkeypatch):
    """offline -> throttled -> online: one ERROR, one rate-limited note, then cleared."""
    down = [{"online": False}]
    throttled = [{"online": False, "throttled": True}]
    errors, infos = _run_worker(monkeypatch, [down, throttled, [{"online": True}]])
    assert len(errors) == 1
    assert [m for m in infos if m.startswith("LLM outage cleared")] == [
        "LLM outage cleared: 1 of 1 LLM endpoints are online"]


def test_throttled_then_online_without_an_outage_logs_nothing(monkeypatch):
    """A sweep that was only ever rate-limited never raised an outage, so coming back
    online has nothing to clear."""
    errors, infos = _run_worker(monkeypatch, [[{"online": False, "throttled": True}], [{"online": True}]])
    assert errors == []
    assert not any(m.startswith("LLM outage cleared") for m in infos)


def test_every_endpoint_unusable_is_an_outage(monkeypatch):
    """No key, rejected key, unreachable or model missing: nothing is throttled, so it is an
    outage. This is the 2026-11-17 shape, where the endpoints survived but their keys did not."""
    errors, _ = _run_worker(monkeypatch, [[{"online": False}, {"online": False, "throttled": False}]])
    assert len(errors) == 1 and "0 of 2" in errors[0]


def test_throttled_is_kept_apart_from_offline_in_the_endpoint_list(monkeypatch):
    """_check_entries_online marks only a 429 (probe result None) as throttled; a probe
    that raises counts as offline."""
    import llm_client
    entries = [(c, "openai", "k", f"m{c}", "", 0) for c in "abcd"]
    results = {"ma": True, "mb": False, "mc": None}

    def probe(_p, _k, model, *_a, **_kw):
        """Return the canned probe result; model md raises."""
        if model == "md":
            raise RuntimeError("boom")
        return results[model]

    monkeypatch.setattr(llm_client, "_iter_configured_endpoints", lambda _cfg: entries)
    monkeypatch.setattr(workers, "_probe_endpoint_online", probe)
    got = {e["model"]: (e["online"], e["throttled"]) for e in workers._check_entries_online({})}
    assert got == {"ma": (True, False), "mb": (False, False), "mc": (False, True), "md": (False, False)}


class _Reply:
    """Minimal requests.Response for the probe."""

    def __init__(self, status):
        """Hold the HTTP status."""
        self.status_code = status
        self.text = ""


@pytest.mark.parametrize("status, expected", [(429, None), (403, False), (500, False)])
def test_copilot_token_exchange_429_is_throttled(monkeypatch, status, expected):
    """The Copilot probe exchanges the GitHub token first. A 429 there is alive-but-throttled
    (None), like a 429 anywhere else; any other failed exchange is still unusable (False)."""
    import llm_client
    monkeypatch.setattr(llm_client, "_COPILOT_TOKEN_CACHE", {})
    monkeypatch.setattr(workers.requests, "get", lambda *a, **k: _Reply(status))
    assert workers._probe_endpoint_online("copilot", "gh-token", "m", "") is expected


@pytest.mark.parametrize("status, key, expected", [
    (200, "k", True),
    (429, "k", None),
    (401, "k", False),
    (500, "k", False),
    (200, "", False),
])
def test_probe_separates_throttled_from_unusable(monkeypatch, status, key, expected):
    """Only a 429 is None; a missing or rejected key and a server error are False."""
    monkeypatch.setattr(workers.requests, "get", lambda *a, **k: _Reply(status))
    assert workers._probe_endpoint_online("openai", key, "gpt", "") is expected


@pytest.mark.parametrize("provider", ["lmstudio", "ollama"])
@pytest.mark.parametrize("status, expected", [(429, None), (500, False), (401, False)])
def test_probe_429_is_throttled_for_local_servers_too(monkeypatch, provider, status, expected):
    """LM Studio and Ollama share the rule: 429 is None (alive), other failures are False."""
    monkeypatch.setattr(workers.requests, "get", lambda *a, **k: _Reply(status))
    assert workers._probe_endpoint_online(provider, "", "m", "http://127.0.0.1:1") is expected


def test_probe_network_error_is_offline(monkeypatch):
    """An unreachable host is False (every call fails), not None."""
    def refuse(*_a, **_k):
        """Stand in for a refused connection."""
        raise OSError("connection refused")

    monkeypatch.setattr(workers.requests, "get", refuse)
    assert workers._probe_endpoint_online("openai", "k", "gpt", "") is False


def test_healthy_endpoints_log_no_error(monkeypatch):
    """An online endpoint never raises the outage ERROR."""
    errors, _ = _run_worker(monkeypatch, [[{"online": True}, {"online": False}]])
    assert errors == []


def _header_pill(endpoints):
    """Render the header's LLM endpoint pill (the real template fragment) for `endpoints`."""
    src = open("templates/index.html", encoding="utf-8").read()
    start = src.index("{% set all_eps = state.llm_endpoints_online %}")
    end = src.index("{% endif %}", src.index("No LLM endpoints configured yet")) + len("{% endif %}")
    html = jinja2.Environment(autoescape=True).from_string(src[start:end]).render(
        state={"llm_endpoints_online": endpoints})
    return re.sub(r"\s+", " ", html)


def _ep(online, throttled=False):
    """One endpoint dict as _check_entries_online builds it."""
    return {"provider": "p", "model": "m", "online": online, "throttled": throttled,
            "used": False, "runs": 0}


def test_header_shows_a_throttled_endpoint_as_degraded_not_offline():
    """A 429 endpoint is reachable: amber in the header and its hover row, red only when
    nothing is usable, matching the up/degraded/down alert states."""
    throttled_only = _header_pill([_ep(False, True)])
    assert "bg-amber-400" in throttled_only and "bg-red-400" not in throttled_only
    assert "Rate-limited (HTTP 429)" in throttled_only
    down = _header_pill([_ep(False)])
    assert "bg-red-400" in down and "bg-amber-400" not in down
    healthy = _header_pill([_ep(True)])
    assert "bg-emerald-400" in healthy and "bg-red-400" not in healthy
