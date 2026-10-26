"""os_updates_ab: ab answers the hub's OS Updates commands fast and read-only.

Without a handler the hub waited its full 240s timeout on every check and showed
ab as unreachable. ab can't escalate to apt, so a check must report the pending
packages with an apply blocker, and apply/reboot must be refused outright.
"""
import ast
import os

import os_update
import os_updates_ab


def test_check_reports_packages_but_blocks_apply(monkeypatch):
    seen = {}

    def fake_check(refresh=True):
        seen["refresh"] = refresh
        return {"status": "SUCCESS", "eligible": True, "count": 2,
                "packages": [{"package": "a"}, {"package": "b"}],
                "features": ["packages", "reboot"], "apply_blocker": ""}

    monkeypatch.setattr(os_update, "check_updates", fake_check)
    res = os_updates_ab.handle_command("OS_UPDATE_CHECK", {"refresh": True})
    assert seen["refresh"] is False  # no root → never tries apt-get update
    assert res["count"] == 2
    assert res["apply_blocker"] == os_updates_ab.APPLY_BLOCKER
    assert res["features"] == []


def test_ineligible_host_passes_through(monkeypatch):
    monkeypatch.setattr(os_update, "check_updates",
                        lambda refresh=True: {"status": "SUCCESS", "eligible": False,
                                              "reason": "not apt"})
    res = os_updates_ab.handle_command("OS_UPDATE_CHECK", {})
    assert res["eligible"] is False and "apply_blocker" not in res


def test_apply_and_reboot_are_refused(monkeypatch):
    monkeypatch.setattr(os_update, "apply_updates",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran apt")))
    for cmd in ("OS_UPDATE_APPLY", "OS_UPDATE_APPLY_PACKAGES", "OS_REBOOT"):
        res = os_updates_ab.handle_command(cmd, {"packages": ["a"]})
        assert res["status"] == "ERROR" and "refusing" in res["message"]


def test_hub_agent_dispatches_os_update_commands_off_the_receive_loop():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_agent.py")).read()
    slow = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_SLOW_CMDS")
    names = {c.value for c in ast.walk(slow.value) if isinstance(c, ast.Constant)}
    assert set(os_updates_ab.COMMANDS) <= names
    assert "os_updates_ab.handle_command" in src


def test_vendored_os_update_matches_lm_when_checked_out_side_by_side():
    here = os.path.dirname(os.path.abspath(__file__))
    lm_copy = os.path.join(here, "..", "lm", "core", "src", "os_update.py")
    if not os.path.exists(lm_copy):
        return
    assert open(os.path.join(here, "os_update.py")).read() == open(lm_copy).read()
