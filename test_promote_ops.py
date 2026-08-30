#!/usr/bin/env python3
"""Self-tests for promote_ops (scheduled promotion).

pytest-style (assert-based) so CI's `pytest -q .` runs and gates them, unlike
the older main()-style files it collects nothing from. Also runnable directly:
`python3 test_promote_ops.py`. Nothing here touches the network or imports the
real app -- `main` is stubbed for CONFIG_DIR, `routes` is stubbed for the
allowlist/repos, and GitHub is faked via a stub `github` module.
"""
import os
import sys
import types
import functools
import tempfile
from datetime import datetime, timedelta, timezone


# Tests here temporarily replace real modules in sys.modules (main/routes/github
# stubs, plus a freshly-imported promote_ops). Under the full `pytest .` run that
# would leak into whatever test is collected next, so every test snapshots and
# restores those entries via @_isolated.
_ISOLATED_MODS = ("main", "routes", "github", "promote_ops")


def _isolated(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        saved = {m: sys.modules.get(m) for m in _ISOLATED_MODS}
        try:
            return fn(*a, **k)
        finally:
            for m, v in saved.items():
                if v is None:
                    sys.modules.pop(m, None)
                else:
                    sys.modules[m] = v
    return wrapper


def _fresh_promote_ops(config_dir):
    """Import promote_ops with a stub `main` (for CONFIG_DIR) so the state file
    lands in a temp dir and no real app startup happens."""
    sys.modules.pop("promote_ops", None)
    sys.modules["main"] = types.SimpleNamespace(CONFIG_DIR=config_dir)
    import promote_ops
    return promote_ops


def _now():
    return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _install_fake_routes(promotable):
    """Stub the routes module promote_ops reaches into at runtime."""
    fake = types.ModuleType("routes")
    fake.PROMOTE_ALL = "__all__"
    fake.PROMOTE_ROUTES = {("dev", "qa"): False, ("qa", "main"): False, ("dev", "main"): True}
    fake._promotable_repos = lambda cfg: (list(promotable), "")
    sys.modules["routes"] = fake


# --------------------------------------------------------------------------
# Pure schedule-decision logic
# --------------------------------------------------------------------------
@_isolated
def test_due_routes():
    po = _fresh_promote_ops(tempfile.mkdtemp())
    now = _now()

    # Never-run + positive cadence => due; qa->main at 0h is never scheduled.
    cfg = {"promote_schedule_dev_to_qa_hours": 24, "promote_schedule_qa_to_main_hours": 0}
    assert po.due_routes(cfg, {}, now) == [("dev", "qa")]

    # Within the interval => not due; at/after the interval => due.
    assert po.due_routes(cfg, {"dev->qa": now - timedelta(hours=10)}, now) == []
    assert po.due_routes(cfg, {"dev->qa": now - timedelta(hours=24)}, now) == [("dev", "qa")]

    # Both routes overdue => stable order dev->qa, then qa->main.
    cfg2 = {"promote_schedule_dev_to_qa_hours": 24, "promote_schedule_qa_to_main_hours": 168}
    last2 = {"dev->qa": now - timedelta(hours=48), "qa->main": now - timedelta(days=14)}
    assert po.due_routes(cfg2, last2, now) == [("dev", "qa"), ("qa", "main")]

    # Negative => off; garbage => falls back to the default (24h), so due.
    assert po.due_routes({"promote_schedule_dev_to_qa_hours": -5}, {}, now) == []
    assert po.due_routes({"promote_schedule_dev_to_qa_hours": "oops"}, {}, now) == [("dev", "qa")]


# --------------------------------------------------------------------------
# Last-run state round-trip
# --------------------------------------------------------------------------
@_isolated
def test_state_roundtrip():
    d = tempfile.mkdtemp()
    po = _fresh_promote_ops(d)
    now = _now()
    po.save_schedule_state({"dev->qa": now, "qa->main": now - timedelta(hours=5)})
    loaded = po.load_schedule_state()
    assert loaded.get("dev->qa") == now
    assert loaded.get("qa->main") == now - timedelta(hours=5)

    # Missing file => empty, not an error.
    po2 = _fresh_promote_ops(tempfile.mkdtemp())
    assert po2.load_schedule_state() == {}

    # Corrupt file => empty, not a crash.
    with open(os.path.join(d, "promote_schedule_state.json"), "w") as fh:
        fh.write("{not json")
    assert po.load_schedule_state() == {}


# --------------------------------------------------------------------------
# One full scheduler cycle (GitHub + routes faked)
# --------------------------------------------------------------------------
@_isolated
def test_run_cycle():
    d = tempfile.mkdtemp()
    po = _fresh_promote_ops(d)
    _install_fake_routes(["own/a", "own/b"])
    now = _now()

    calls = []

    def fake_dispatch(token, repo, src, tgt, is_override):
        calls.append((repo, src, tgt, is_override))
        return f"https://github.com/{repo}/actions"

    po.dispatch_promote_workflow = fake_dispatch

    # No token => short-circuit, nothing dispatched.
    r = po.run_scheduled_promotions({"promote_schedule_dev_to_qa_hours": 24}, now=now)
    assert r.get("reason") == "no-token" and not calls

    # Token + due dev->qa across all promotable repos, override flag never set.
    cfg = {"GITHUB_TOKEN": "t", "promote_schedule_dev_to_qa_hours": 24,
           "promote_schedule_qa_to_main_hours": 0, "promote_schedule_repo": "__all__"}
    r = po.run_scheduled_promotions(cfg, now=now)
    assert sorted(c[0] for c in calls) == ["own/a", "own/b"]
    assert not any(c[3] for c in calls), "scheduled dev->qa must never use the override flag"
    assert r["dispatched"] == 2

    # Timer reset => immediate re-run dispatches nothing; after the interval it fires.
    calls.clear()
    po.run_scheduled_promotions(cfg, now=now + timedelta(minutes=5))
    assert calls == []
    po.run_scheduled_promotions(cfg, now=now + timedelta(hours=25))
    assert len(calls) == 2

    # Single-repo scope.
    calls.clear()
    po.run_scheduled_promotions(dict(cfg, promote_schedule_repo="own/a"), now=now + timedelta(hours=50))
    assert [c[0] for c in calls] == ["own/a"]


@_isolated
def test_run_cycle_isolates_repo_failures():
    """A per-repo failure must not abort the fan-out, and the timer still resets
    because at least one repo succeeded."""
    po = _fresh_promote_ops(tempfile.mkdtemp())
    _install_fake_routes(["own/a", "own/b"])
    now = _now()

    def flaky(token, repo, src, tgt, is_override):
        if repo == "own/a":
            raise RuntimeError("boom")
        return "ok"

    po.dispatch_promote_workflow = flaky
    cfg = {"GITHUB_TOKEN": "t", "promote_schedule_dev_to_qa_hours": 24,
           "promote_schedule_qa_to_main_hours": 0, "promote_schedule_repo": "__all__"}
    r = po.run_scheduled_promotions(cfg, now=now)
    assert len([x for x in r["results"] if x["ok"]]) == 1
    assert len([x for x in r["results"] if not x["ok"]]) == 1
    assert po.load_schedule_state().get("dev->qa") == now


# --------------------------------------------------------------------------
# dispatch_promote_workflow safety contract (the routes twin is covered by
# test_promote_routes.py; this covers the promote_ops copy).
# --------------------------------------------------------------------------
def _fake_github_module():
    """A stub `github` module whose Github records create_dispatch calls."""
    mod = types.ModuleType("github")
    state = {"calls": [], "result": True, "raises": None, "default_branch": "main"}

    class _Wf:
        def create_dispatch(self, ref, inputs):
            state["calls"].append((ref, inputs))
            if state["raises"]:
                raise state["raises"]
            return state["result"]

    class _Repo:
        def __init__(self, name):
            self.default_branch = state["default_branch"]

        def get_workflow(self, name):
            assert name == "promote.yml"
            return _Wf()

    class _Github:
        def __init__(self, token):
            pass

        def get_repo(self, name):
            return _Repo(name)

    mod.Github = _Github
    return mod, state


@_isolated
def test_dispatch_contract():
    po = _fresh_promote_ops(tempfile.mkdtemp())
    mod, state = _fake_github_module()
    sys.modules["github"] = mod

    # Normal route omits the `target` input; ref is the repo default branch.
    state["calls"].clear()
    url = po.dispatch_promote_workflow("tok", "o/r", "dev", "qa", is_override=False)
    assert state["calls"] == [("main", {"source": "dev"})]
    assert url == "https://github.com/o/r/actions/workflows/promote.yml"

    # Override sends the explicit `target`.
    state["calls"].clear()
    po.dispatch_promote_workflow("tok", "o/r", "dev", "main", is_override=True)
    assert state["calls"] == [("main", {"source": "dev", "target": "main"})]

    # create_dispatch returning False is an ERROR, not a false success.
    state["result"] = False
    try:
        po.dispatch_promote_workflow("tok", "o/r", "dev", "qa", is_override=False)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a rejected dispatch (False) must raise, not report success")


def main():
    tests = [test_due_routes, test_state_roundtrip, test_run_cycle,
             test_run_cycle_isolates_repo_failures, test_dispatch_contract]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print("\nall promote_ops tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
