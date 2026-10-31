"""os_updates_ab: ab answers the hub's OS Updates commands. ab.service can't
sudo, so apply/reboot are handed to the watchdog through a request file."""
import ast
import json
import os
import threading

import pytest

import os_update
import os_updates_ab


@pytest.fixture
def ab(tmp_path, monkeypatch):
    helper = tmp_path / "lm-os-update"
    helper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(os_update, "HELPER", str(helper))
    monkeypatch.setattr(os_updates_ab, "REQUEST_FILE", str(tmp_path / "req.json"))
    monkeypatch.setattr(os_updates_ab, "STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(os_updates_ab, "POLL_S", 0.01)
    return helper


def _check(monkeypatch):
    seen = {}

    def fake_check(refresh=True):
        seen["refresh"] = refresh
        return {"status": "SUCCESS", "eligible": True, "count": 2,
                "packages": [{"package": "a"}, {"package": "b"}],
                "features": ["packages", "reboot", "fix_access"], "apply_blocker": "x"}
    monkeypatch.setattr(os_update, "check_updates", fake_check)
    return os_updates_ab.handle_command("OS_UPDATE_CHECK", {"refresh": True}), seen


def test_check_is_patchable_when_the_helper_is_installed(ab, monkeypatch):
    res, seen = _check(monkeypatch)
    assert seen["refresh"] is False  # ab.service can't run apt-get update
    assert res["count"] == 2 and res["apply_blocker"] == ""
    assert res["features"] == ["packages", "reboot"]


def test_without_the_helper_apply_is_blocked(ab, monkeypatch):
    ab.unlink()
    res, _ = _check(monkeypatch)
    assert res["apply_blocker"] == os_updates_ab.NO_HELPER
    res = os_updates_ab.handle_command("OS_UPDATE_APPLY", {})
    assert res["status"] == "ERROR" and "install.sh" in res["message"]


@pytest.mark.parametrize("cmd,data,expected", [
    ("OS_UPDATE_APPLY", {}, ("apply", None)),
    ("OS_UPDATE_APPLY_PACKAGES", {"packages": ["openssl"]}, ("apply", ["openssl"])),
    ("OS_REBOOT", {}, ("reboot", None)),
])
def test_apply_and_reboot_go_through_the_watchdog(ab, monkeypatch, cmd, data, expected):
    ran = []
    monkeypatch.setattr(os_update, "apply_updates",
                        lambda packages=None: ran.append(("apply", packages)) or {"status": "SUCCESS"})
    monkeypatch.setattr(os_update, "reboot_node",
                        lambda: ran.append(("reboot", None)) or {"status": "SUCCESS"})

    def watchdog():
        while not os.path.exists(os_updates_ab.REQUEST_FILE):
            pass
        os_updates_ab.run_request()
    t = threading.Thread(target=watchdog)
    t.start()
    res = os_updates_ab.handle_command(cmd, data)
    t.join()
    assert res == {"status": "SUCCESS"} and ran == [expected]
    assert not os.path.exists(os_updates_ab.REQUEST_FILE)


def test_gives_up_when_the_watchdog_never_answers(ab, monkeypatch):
    monkeypatch.setattr(os_updates_ab, "REBOOT_WAIT_S", 0.05)
    res = os_updates_ab.handle_command("OS_REBOOT", {})
    assert res["status"] == "ERROR" and "watchdog" in res["message"]
    # A request still queued refuses a second one.
    assert "already queued" in os_updates_ab.handle_command("OS_REBOOT", {})["message"]


def test_watchdog_records_a_failure(ab, monkeypatch):
    monkeypatch.setattr(os_update, "apply_updates",
                        lambda packages=None: (_ for _ in ()).throw(RuntimeError("boom")))
    with open(os_updates_ab.REQUEST_FILE, "w") as f:
        json.dump({"id": "x", "action": "apply"}, f)
    os_updates_ab.run_request()
    status = json.load(open(os_updates_ab.STATUS_FILE))
    assert status["id"] == "x" and "boom" in status["result"]["message"]


def test_hub_agent_dispatches_os_update_commands_off_the_receive_loop():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_agent.py")).read()
    slow = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_SLOW_CMDS")
    names = {c.value for c in ast.walk(slow.value) if isinstance(c, ast.Constant)}
    assert set(os_updates_ab.COMMANDS) <= names
    assert "os_updates_ab.handle_command" in src


@pytest.mark.parametrize("mine,lms", [("os_update.py", "core/src/os_update.py"),
                                      ("lm-os-update", "scripts/lm-os-update")])
def test_vendored_copies_match_lm_when_checked_out_side_by_side(mine, lms):
    here = os.path.dirname(os.path.abspath(__file__))
    lm_copy = os.path.join(here, "..", "lm", lms)
    if not os.path.exists(lm_copy):
        return
    assert open(os.path.join(here, mine)).read() == open(lm_copy).read()
