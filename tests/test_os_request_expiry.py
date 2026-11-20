"""Expired OS requests never become delayed privileged actions."""

import ast
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import os_updates_ab as queue


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Keep every request private and make accidental OS actions fail the test."""
    monkeypatch.setattr(queue, "REQUEST_FILE", str(tmp_path / "request.json"))
    monkeypatch.setattr(queue, "STATUS_FILE", str(tmp_path / "status.json"))
    clock = [1000.0]
    monkeypatch.setattr(queue, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))

    def forbidden(*args, **kwargs):
        """Never invoke a real package manager or reboot from this suite."""
        pytest.fail("an expired request must not execute an OS action")

    monkeypatch.setattr(queue.os_update, "apply_updates", forbidden)
    monkeypatch.setattr(queue.os_update, "reboot_node", forbidden)
    return clock


def write_request(payload, at=None):
    """Write a private queued record with an optional legacy publication time."""
    path = Path(queue.REQUEST_FILE)
    path.write_text(json.dumps(payload))
    if at is not None:
        os.utime(path, (at, at))


def test_timeout_with_no_consumer_withdraws_queued_action(isolated):
    """The actual timeout path must retire a request rather than execute it later."""
    result = queue._delegate("reboot", None, 50)
    assert result["status"] == "ERROR" and "watchdog" in result["message"]
    assert not Path(queue.REQUEST_FILE).exists()
    queue.run_request()
    assert not Path(queue.STATUS_FILE).exists()


@pytest.mark.parametrize("action", ["apply", "reboot"])
def test_consumer_rejects_an_expired_lease(isolated, action):
    """A producer that died cannot leave an unbounded deferred action."""
    write_request({"id": "expired", "action": action, "packages": ["a"],
                   "expires_at": isolated[0] - 1})
    queue.run_request()
    status = json.loads(Path(queue.STATUS_FILE).read_text())
    assert status["id"] == "expired" and status["state"] == "done"
    assert status["result"]["status"] == "ERROR"
    assert "expired" in status["result"]["message"]
    assert not Path(queue.REQUEST_FILE).exists()


@pytest.mark.parametrize("action,wait", [("apply", 3500), ("reboot", 50)])
def test_legacy_request_expiry_uses_publication_mtime(isolated, action, wait):
    """Older producers retain compatibility, not indefinite stale execution."""
    write_request({"id": "legacy", "action": action},
                  at=isolated[0] - wait - 1)
    queue.run_request()
    status = json.loads(Path(queue.STATUS_FILE).read_text())
    assert status["id"] == "legacy"
    assert status["result"]["status"] == "ERROR"
    assert "expired" in status["result"]["message"]


def test_timeout_after_claim_reports_unknown_without_touching_running_work(isolated, monkeypatch):
    """A missing queue entry is not proof that the action was cancelled or failed."""
    claimed = Path(queue.REQUEST_FILE + ".running-test")

    def sleep(seconds):
        """Simulate a consumer taking ownership without completing yet."""
        if Path(queue.REQUEST_FILE).exists():
            os.rename(queue.REQUEST_FILE, claimed)
        isolated[0] += seconds

    monkeypatch.setattr(queue.time, "sleep", sleep)
    result = queue._delegate("reboot", None, 50)
    assert result["status"] == "ERROR" and "outcome unknown" in result["message"]
    assert claimed.exists()


def test_expiry_is_checked_after_the_atomic_claim(isolated, monkeypatch):
    """Winning ownership just before expiry does not permit a delayed start."""
    write_request({"id": "late-claim", "action": "reboot", "expires_at": isolated[0] + 1})
    rename = queue.os.rename

    def claim_then_expire(source, destination):
        """Advance the clock in the gap between claiming and validating."""
        rename(source, destination)
        isolated[0] += 2

    monkeypatch.setattr(queue.os, "rename", claim_then_expire)
    queue.run_request()
    status = json.loads(Path(queue.STATUS_FILE).read_text())
    assert "expired" in status["result"]["message"]


@pytest.mark.parametrize("expires", [None, True, "later", float("nan"), float("inf"), 10 ** 400])
def test_invalid_lease_is_not_an_unbounded_permission_to_execute(isolated, expires):
    """Malformed persisted state fails closed with the matching request id."""
    write_request({"id": "invalid", "action": "reboot", "expires_at": expires})
    queue.run_request()
    status = json.loads(Path(queue.STATUS_FILE).read_text())
    assert status["id"] == "invalid" and status["result"]["status"] == "ERROR"
    assert "invalid OS request deadline" in status["result"]["message"]


def test_stale_status_is_not_an_acknowledgement(isolated):
    """An old successful result must not make new unexecuted work successful."""
    Path(queue.STATUS_FILE).write_text(json.dumps({
        "id": "someone-else", "state": "done", "result": {"status": "SUCCESS"}}))
    result = queue._delegate("reboot", None, 50)
    assert result["status"] == "ERROR" and not Path(queue.REQUEST_FILE).exists()


def test_dead_producer_expired_queue_is_replaced_without_executing_old_work(isolated, monkeypatch):
    """A released file lock must not leave the service permanently wedged."""
    write_request({"id": "dead-producer", "action": "apply", "expires_at": isolated[0] - 1})
    ran = []
    monkeypatch.setattr(queue.os_update, "reboot_node",
                        lambda: ran.append("new reboot") or {"status": "SUCCESS"})

    def watchdog(seconds):
        """Pick up only the replacement request published by this caller."""
        isolated[0] += seconds
        queue.run_request()

    monkeypatch.setattr(queue.time, "sleep", watchdog)
    assert queue._delegate("reboot", None, 50) == {"status": "SUCCESS"}
    assert ran == ["new reboot"]
    assert json.loads(Path(queue.STATUS_FILE).read_text())["id"] != "dead-producer"


def test_second_thread_is_refused_even_when_consumer_already_claimed(isolated, monkeypatch):
    """Producer ownership lasts through waiting, not merely while a file exists."""
    waiting, release = threading.Event(), threading.Event()
    results = []

    def sleep(seconds):
        """Hold the first producer while the main thread attempts another request."""
        if threading.current_thread() is worker:
            waiting.set()
            assert release.wait(5)
        isolated[0] += seconds

    monkeypatch.setattr(queue.time, "sleep", sleep)
    worker = threading.Thread(target=lambda: results.append(queue._delegate("reboot", None, 50)))
    worker.start()
    try:
        assert waiting.wait(5)
        os.rename(queue.REQUEST_FILE, queue.REQUEST_FILE + ".running-test")
        second = queue._delegate("apply", None, 50)
        assert second["status"] == "ERROR" and "already queued" in second["message"]
        assert not Path(queue.REQUEST_FILE).exists()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and "outcome unknown" in results[0]["message"]


def test_producer_exclusion_also_holds_in_another_process(isolated):
    """Independent opens in different processes contend on the real file lock."""
    program = (
        "import json,sys,os_updates_ab as q;"
        "q.REQUEST_FILE=sys.argv[1];"
        "print(json.dumps(q._delegate('reboot',None,0)))"
    )
    with open(queue.REQUEST_FILE + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "-c", program, queue.REQUEST_FILE],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            check=True, timeout=10)
    assert "already queued" in json.loads(result.stdout)["message"]
    assert not Path(queue.REQUEST_FILE).exists()


def test_exclusive_publication_never_overwrites_a_racing_foreign_request(isolated, monkeypatch):
    """An older writer ignoring our lock still cannot be overwritten by link."""
    link = queue.os.link

    def competing_publish(source, destination):
        """Install another valid request after our preflight but before publication."""
        write_request({"id": "foreign", "action": "reboot", "expires_at": isolated[0] + 50})
        link(source, destination)

    monkeypatch.setattr(queue.os, "link", competing_publish)
    result = queue._delegate("reboot", None, 50)
    assert "already queued" in result["message"]
    assert json.loads(Path(queue.REQUEST_FILE).read_text())["id"] == "foreign"
    assert not list(Path(queue.REQUEST_FILE).parent.glob(".request.json.*"))


def test_foreign_timeout_record_is_preserved_not_deleted_or_restored(isolated, monkeypatch, caplog):
    """An unsupported legacy overwrite fails safe without losing its diagnostic file."""
    def sleep(seconds):
        """Model a noncooperating legacy producer replacing the queued record."""
        write_request({"id": "foreign", "action": "reboot", "expires_at": 2000})
        isolated[0] += seconds

    monkeypatch.setattr(queue.time, "sleep", sleep)
    result = queue._delegate("reboot", None, 50)
    preserved = list(Path(queue.REQUEST_FILE).parent.glob("request.json.cancel.*"))
    assert "outcome unknown" in result["message"] and len(preserved) == 1
    assert json.loads(preserved[0].read_text())["id"] == "foreign"
    assert "Foreign OS request preserved" in caplog.text


@pytest.mark.parametrize("stage", ["write", "close", "link"])
def test_failed_publication_leaves_no_queued_action_or_partial_file(isolated, monkeypatch, stage):
    """Neither an incomplete JSON file nor failed exclusive publication is executable."""
    def fail(*args, **kwargs):
        """Inject a bounded local filesystem failure before publication."""
        raise OSError("handoff disk error")

    if stage == "write":
        monkeypatch.setattr(queue.json, "dump", fail)
    elif stage == "close":
        temporary_file = queue.tempfile.NamedTemporaryFile

        @contextlib.contextmanager
        def closing_failure(*args, **kwargs):
            """Close the real staged file, then fail before it can be published."""
            with temporary_file(*args, **kwargs) as handle:
                yield handle
            raise OSError("handoff disk error")

        monkeypatch.setattr(queue.tempfile, "NamedTemporaryFile", closing_failure)
    else:
        monkeypatch.setattr(queue.os, "link", fail)
    result = queue._delegate("reboot", None, 50)
    assert result["status"] == "ERROR" and "handoff disk error" in result["message"]
    assert not Path(queue.REQUEST_FILE).exists()
    assert not list(Path(queue.REQUEST_FILE).parent.glob(".request.json.*"))


def test_postpublication_temp_cleanup_failure_does_not_discard_real_ack(isolated, monkeypatch, caplog):
    """A confirmed publication survives failed removal of its extra hard link."""
    unlink = queue.os.unlink
    ran = []

    def cleanup(path, *args, **kwargs):
        """Fail only cleanup of the staged request, not the queued or claimed file."""
        if Path(path).name.startswith(".request.json."):
            raise OSError("temp cleanup failed")
        return unlink(path, *args, **kwargs)

    def watchdog(seconds):
        """Complete the published request despite the leftover staging link."""
        isolated[0] += seconds
        queue.run_request()

    monkeypatch.setattr(queue.os, "unlink", cleanup)
    monkeypatch.setattr(queue.time, "sleep", watchdog)
    monkeypatch.setattr(queue.os_update, "reboot_node",
                        lambda: ran.append(True) or {"status": "SUCCESS"})
    assert queue._delegate("reboot", None, 50) == {"status": "SUCCESS"}
    assert ran == [True] and "temp cleanup failed" in caplog.text


def test_consumer_losing_atomic_claim_to_timeout_is_a_normal_noop(isolated, monkeypatch):
    """A concurrent withdrawal cannot crash the watchdog or start stale work."""
    write_request({"id": "withdrawn", "action": "reboot", "expires_at": isolated[0] + 50})

    def withdrawal_wins(source, destination):
        """Retire the queued name just before the consumer can rename it."""
        os.unlink(source)
        raise FileNotFoundError(source)

    monkeypatch.setattr(queue.os, "rename", withdrawal_wins)
    queue.run_request()
    assert not Path(queue.STATUS_FILE).exists()


@pytest.mark.parametrize("body", ["not-json", "[]"])
def test_invalid_existing_queue_is_not_silently_overwritten(isolated, body):
    """Unknown queued state needs an explicit error, never a successful replacement."""
    Path(queue.REQUEST_FILE).write_text(body)
    result = queue._delegate("reboot", None, 50)
    assert result["status"] == "ERROR" and "invalid" in result["message"]
    assert Path(queue.REQUEST_FILE).read_text() == body


def test_status_write_failure_never_reexecutes_a_claimed_action(isolated, monkeypatch):
    """A lost acknowledgement remains unknown, not a reason to run apt twice."""
    write_request({"id": "executed", "action": "reboot", "expires_at": isolated[0] + 50})
    ran = []
    monkeypatch.setattr(queue.os_update, "reboot_node",
                        lambda: ran.append(True) or {"status": "SUCCESS"})

    def fail(*args, **kwargs):
        """Fail the atomic status replacement after the stubbed action ran."""
        raise OSError("status disk full")

    monkeypatch.setattr(queue.os, "replace", fail)
    with pytest.raises(OSError, match="status disk full"):
        queue.run_request()
    queue.run_request()
    assert ran == [True] and not Path(queue.REQUEST_FILE).exists()
    assert not list(Path(queue.REQUEST_FILE).parent.glob("request.json.claim.*"))


def test_live_producer_wait_is_not_extended_by_a_backward_wall_clock_jump(isolated, monkeypatch):
    """The persisted lease uses epoch time, but the live wait uses monotonic time."""
    wall, mono = [1000.0], [0.0]

    def sleep(seconds):
        """Move epoch time backwards while elapsed process time moves forwards."""
        wall[0] -= 100
        mono[0] += seconds

    monkeypatch.setattr(queue, "time", SimpleNamespace(
        time=lambda: wall[0], monotonic=lambda: mono[0], sleep=sleep))
    result = queue._delegate("reboot", None, 50)
    assert result["status"] == "ERROR" and mono[0] == 50
    assert not Path(queue.REQUEST_FILE).exists()


@pytest.mark.parametrize("alive", [True, False, None])
def test_watchdog_reload_waits_for_os_result_publication(alive):
    """Exec must not kill the background OS worker before its status write."""
    source = (Path(__file__).resolve().parents[1] / "watchdog.py").read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef)
                and n.name == "_maybe_reload_on_code_change")
    executions = []
    ns = {
        "os": SimpleNamespace(
            path=SimpleNamespace(getmtime=lambda path: 2),
            execv=lambda *args: executions.append(args)),
        "sys": SimpleNamespace(executable="python"),
        "logger": SimpleNamespace(info=lambda *args: None, error=lambda *args: None),
        "_WATCHDOG_SRC": "/owned/watchdog.py",
        "_os_update_thread": None if alive is None else SimpleNamespace(is_alive=lambda: alive),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "watchdog_reload", "exec"), ns)
    ns["_maybe_reload_on_code_change"](1)
    assert len(executions) == (0 if alive else 1)
