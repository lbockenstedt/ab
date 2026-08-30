"""Tests for promote_ops (scheduled promotion).

main()-style like the rest of the AB suite (pytest collects 0 here; run with
`python3 test_promote_ops.py`). Covers the pure schedule-decision logic, the
last-run state round-trip, and one full scheduler cycle with GitHub/routes
faked out, so nothing here touches the network or imports the real app.
"""
import os
import sys
import types
import tempfile
from datetime import datetime, timedelta, timezone


def _fresh_promote_ops(config_dir):
    """Import promote_ops with a stub `main` (for CONFIG_DIR) so the state file
    lands in a temp dir and no real app startup happens."""
    sys.modules.pop("promote_ops", None)
    sys.modules["main"] = types.SimpleNamespace(CONFIG_DIR=config_dir)
    import promote_ops
    return promote_ops


def _now():
    return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_due_routes():
    po = _fresh_promote_ops(tempfile.mkdtemp())
    now = _now()
    fails = []

    # Never-run + positive cadence => due.
    cfg = {"promote_schedule_dev_to_qa_hours": 24, "promote_schedule_qa_to_main_hours": 0}
    due = po.due_routes(cfg, {}, now)
    if due != [("dev", "qa")]:
        fails.append(f"never-run dev->qa should be the only due route, got {due}")

    # qa->main cadence 0 => never scheduled even if never run.
    if ("qa", "main") in due:
        fails.append("qa->main with 0 hours must not be due")

    # Within the interval => not due.
    last = {"dev->qa": now - timedelta(hours=10)}
    if po.due_routes(cfg, last, now) != []:
        fails.append("dev->qa 10h after last run (24h cadence) must not be due")

    # Exactly at/after the interval => due.
    last = {"dev->qa": now - timedelta(hours=24)}
    if po.due_routes(cfg, last, now) != [("dev", "qa")]:
        fails.append("dev->qa exactly 24h later must be due")

    # Both routes enabled and both overdue => stable order dev->qa, qa->main.
    cfg2 = {"promote_schedule_dev_to_qa_hours": 24, "promote_schedule_qa_to_main_hours": 168}
    last2 = {"dev->qa": now - timedelta(hours=48), "qa->main": now - timedelta(days=14)}
    if po.due_routes(cfg2, last2, now) != [("dev", "qa"), ("qa", "main")]:
        fails.append(f"both overdue routes should return in order, got {po.due_routes(cfg2, last2, now)}")

    # Negative / garbage cadence treated as off.
    if po.due_routes({"promote_schedule_dev_to_qa_hours": -5}, {}, now) != []:
        fails.append("negative cadence must be treated as off")
    if po.due_routes({"promote_schedule_dev_to_qa_hours": "oops"}, {}, now) != [("dev", "qa")]:
        # 'oops' -> default 24 -> never run -> due
        fails.append("garbage cadence should fall back to the default (24h), so a never-run route is due")

    return fails


def test_state_roundtrip():
    d = tempfile.mkdtemp()
    po = _fresh_promote_ops(d)
    fails = []
    now = _now()
    po.save_schedule_state({"dev->qa": now, "qa->main": now - timedelta(hours=5)})
    loaded = po.load_schedule_state()
    if loaded.get("dev->qa") != now:
        fails.append(f"round-trip lost dev->qa time: {loaded.get('dev->qa')} != {now}")
    if loaded.get("qa->main") != now - timedelta(hours=5):
        fails.append("round-trip lost qa->main time")
    # Missing file => empty, not an error.
    po2 = _fresh_promote_ops(tempfile.mkdtemp())
    if po2.load_schedule_state() != {}:
        fails.append("missing state file should load as {}")
    # Corrupt file => empty, not a crash.
    with open(os.path.join(d, "promote_schedule_state.json"), "w") as fh:
        fh.write("{not json")
    if po.load_schedule_state() != {}:
        fails.append("corrupt state file should load as {}")
    return fails


def _install_fake_routes(promotable):
    """Stub the routes module promote_ops reaches into at runtime."""
    fake = types.ModuleType("routes")
    fake.PROMOTE_ALL = "__all__"
    fake.PROMOTE_ROUTES = {("dev", "qa"): False, ("qa", "main"): False, ("dev", "main"): True}
    fake._promotable_repos = lambda cfg: (list(promotable), "")
    sys.modules["routes"] = fake


def test_run_cycle():
    d = tempfile.mkdtemp()
    po = _fresh_promote_ops(d)
    _install_fake_routes(["own/a", "own/b"])
    fails = []
    now = _now()

    calls = []

    def fake_dispatch(token, repo, src, tgt, is_override):
        calls.append((repo, src, tgt, is_override))
        return f"https://github.com/{repo}/actions"

    po.dispatch_promote_workflow = fake_dispatch

    # No token => short-circuit, nothing dispatched.
    r = po.run_scheduled_promotions({"promote_schedule_dev_to_qa_hours": 24}, now=now)
    if r.get("reason") != "no-token" or calls:
        fails.append("missing token must short-circuit with no dispatch")

    # Token + due dev->qa across all promotable repos.
    cfg = {"GITHUB_TOKEN": "t", "promote_schedule_dev_to_qa_hours": 24,
           "promote_schedule_qa_to_main_hours": 0, "promote_schedule_repo": "__all__"}
    r = po.run_scheduled_promotions(cfg, now=now)
    if sorted(c[0] for c in calls) != ["own/a", "own/b"]:
        fails.append(f"should dispatch to every promotable repo, got {calls}")
    if any(c[3] for c in calls):
        fails.append("scheduled dev->qa must never use the override flag")
    if r["dispatched"] != 2:
        fails.append(f"expected 2 dispatches, got {r['dispatched']}")

    # Timer was reset => immediate re-run dispatches nothing new.
    calls.clear()
    r = po.run_scheduled_promotions(cfg, now=now + timedelta(minutes=5))
    if calls:
        fails.append("route within its interval must not re-dispatch")

    # After the interval elapses it fires again.
    r = po.run_scheduled_promotions(cfg, now=now + timedelta(hours=25))
    if len(calls) != 2:
        fails.append("route should re-dispatch after the interval elapses")

    # Single-repo scope override.
    calls.clear()
    cfg_one = dict(cfg, promote_schedule_repo="own/a")
    po.run_scheduled_promotions(cfg_one, now=now + timedelta(hours=50))
    if [c[0] for c in calls] != ["own/a"]:
        fails.append(f"single-repo scope should dispatch only that repo, got {calls}")

    # A per-repo failure must not abort the fan-out, and the timer still resets
    # because at least one repo succeeded.
    _install_fake_routes(["own/a", "own/b"])
    d2 = tempfile.mkdtemp()
    po2 = _fresh_promote_ops(d2)
    _install_fake_routes(["own/a", "own/b"])

    def flaky(token, repo, src, tgt, is_override):
        if repo == "own/a":
            raise RuntimeError("boom")
        return "ok"

    po2.dispatch_promote_workflow = flaky
    r = po2.run_scheduled_promotions(dict(cfg), now=now)
    oks = [x for x in r["results"] if x["ok"]]
    bad = [x for x in r["results"] if not x["ok"]]
    if len(oks) != 1 or len(bad) != 1:
        fails.append(f"fan-out should report 1 ok + 1 failed, got {r['results']}")
    if po2.load_schedule_state().get("dev->qa") != now:
        fails.append("timer should reset when at least one repo dispatched")

    return fails


def main():
    all_fails = []
    for fn in (test_due_routes, test_state_roundtrip, test_run_cycle):
        fs = fn()
        for f in fs:
            print(f"FAIL [{fn.__name__}] {f}")
        all_fails.extend(fs)
        if not fs:
            print(f"PASS {fn.__name__}")
    if all_fails:
        print(f"\n{len(all_fails)} failure(s)")
        return 1
    print("\nall promote_ops tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
