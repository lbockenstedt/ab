"""Reviewed-head approval and merge tests, using extracted production functions."""
import ast
import asyncio
import json
import os
import re
import sys
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest


class GithubException(Exception):
    """Minimal PyGithub exception used by the real merge handlers."""

    def __init__(self, status, message):
        self.status, self.data = status, {"message": message}


class Logger:
    """Discard logging from the extracted functions."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def extract(path, names, ns):
    """Compile selected real definitions, without importing the live app."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), ns)


class PR:
    """Programmable GitHub PR with a count of API attempts."""

    def __init__(self, head="X", result=None, error=None, on_update=None):
        self.head = SimpleNamespace(sha=head)
        self.merged = False
        self.state = "open"
        self.result = result or SimpleNamespace(merged=True, message="merged")
        self.error, self.on_update = error, on_update
        self.calls = []

    def merge(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result

    def update(self):
        if self.on_update:
            self.on_update(self)


class Env:
    """Real state and merge functions sharing synthetic in-memory records."""

    def __init__(self, head="X", approved=True):
        self.state = {"pr_reviews": {"o/r#1": {"head": head, "approved": approved}}}
        self.saved, self.updates, self.deleted = [], [], []
        self.ns = {"state": self.state, "_task_state_lock": threading.Lock(),
                   "_UNSPECIFIED_HEAD": object(), "logger": Logger(),
                   "_stamp_pr_state": lambda *a: None,
                   "save_pr_reviews": lambda state: self.saved.append(dict(state["o/r#1"]))}
        extract("app_state.py", {"ReviewPersistenceError", "pr_approval_snapshot", "mark_pr_approved"}, self.ns)
        self.ns.update({"GithubException": GithubException,
                        "update_pr_review": lambda *a, **k: self.updates.append(k),
                        "_delete_pr_branch": lambda *a: self.deleted.append(True),
                        "_release_parked_checks": lambda *a: 0,
                        "_github_token": lambda: "fake"})
        extract("pr_actions.py", {"merge_pr", "_github_message", "_merge_is_transient",
                                  "_merge_refusal", "_is_merge_conflict", "_expects_missing_check"},
                self.ns)
        # _merge_is_transient needs the real constant too.
        tree = ast.parse(open("pr_actions.py", encoding="utf-8").read())
        assigns = [n for n in tree.body if isinstance(n, ast.Assign) and
                   any(getattr(t, "id", "") == "_TRANSIENT_MERGE_REFUSALS" for t in n.targets)]
        exec(compile(ast.Module(body=assigns, type_ignores=[]), "pr_actions.py", "exec"), self.ns)

    @property
    def rec(self):
        return self.state["pr_reviews"]["o/r#1"]

    def run(self, pr, **kwargs):
        repo = SimpleNamespace(get_pull=lambda number: pr)
        return self.ns["merge_pr"](SimpleNamespace(get_repo=lambda name: repo), "o/r", 1, **kwargs)


def test_matching_approval_is_bound_to_github_sha():
    env, pr = Env(), PR()
    status, result = env.run(pr)
    assert status == 200 and result["merged"] is True
    assert pr.calls == [{"sha": "X"}] and env.deleted


@pytest.mark.parametrize("reviewed,current", [("X", "Y"), (None, "X"), ("", "X"), ("X", None)])
def test_stale_or_missing_head_is_never_merged(reviewed, current):
    env, pr = Env(reviewed), PR(head=current)
    status, result = env.run(pr)
    assert status == 409 and result["needs_approval"] and result["head_moved"]
    assert not pr.calls and not env.rec["approved"]


def test_unapproved_matching_head_is_not_merged():
    env, pr = Env(approved=False), PR()
    status, result = env.run(pr)
    assert status == 409 and result["needs_approval"] and not pr.calls


def test_old_auto_decision_cannot_merge_newly_reviewed_and_approved_head():
    env, pr = Env(head="Y"), PR(head="Y")
    status, result = env.run(pr, expected_head="X")
    assert status == 409 and result["head_moved"] and not pr.calls
    assert env.rec == {"head": "Y", "approved": True}


@pytest.mark.parametrize("updated_head,status_code,message",
                         [("Y", 405, "conflict"), ("X", 409, "Head branch was modified")])
def test_race_at_api_requires_new_review_before_recovery(updated_head, status_code, message):
    env = Env()
    env.ns["_auto_resolve_pr_conflicts"] = lambda *a: pytest.fail("must not resolve")
    pr = PR(error=GithubException(status_code, message),
            on_update=lambda pr: setattr(pr.head, "sha", updated_head))
    status, result = env.run(pr)
    assert status == 409 and result["head_moved"] and result["needs_approval"]
    assert pr.calls == [{"sha": "X"}] and not env.rec["approved"]
    assert not env.updates


def test_concurrent_merge_wins_over_head_movement():
    env = Env()
    def refresh(pr):
        pr.merged = True
        pr.head.sha = "Y"
    pr = PR(error=GithubException(409, "Head branch was modified"), on_update=refresh)
    status, result = env.run(pr)
    assert status == 200 and result["status"] == "success"
    assert {"merged": True, "merge_blocked_reason": None} in env.updates


def test_conflict_recovery_needs_review_and_never_merges_again():
    env = Env()
    env.ns["_auto_resolve_pr_conflicts"] = lambda *a: (True, "pushed")
    pr = PR(error=GithubException(405, "Merge conflict"))
    status, result = env.run(pr)
    assert status == 409 and result["head_moved"] and result["needs_approval"]
    assert pr.calls == [{"sha": "X"}] and not env.rec["approved"]
    assert env.rec["head"] == "X" and not env.deleted and not env.updates


@pytest.mark.parametrize("concurrent", [False, True])
def test_unmerged_response_refreshes_without_false_success(concurrent):
    env = Env()
    pr = PR(result=SimpleNamespace(merged=False, message="not merged"),
            on_update=lambda pr: setattr(pr, "merged", concurrent))
    status, result = env.run(pr)
    if concurrent:
        assert status == 200 and result["message"] == "already merged"
        assert {"merged": True, "merge_blocked_reason": None} in env.updates
    else:
        assert status == 409 and result["deferred"] and result["retryable"]
        assert "not merged" in result["message"] and not env.updates
    assert not env.deleted


def test_approval_cas_and_legacy_revoke():
    env = Env("Y", False)
    mark = env.ns["mark_pr_approved"]
    assert not mark("o/r", 1, True, expected_head="X")
    assert not mark("o/r", 1, True)
    assert not env.rec["approved"] and not env.saved
    assert mark("o/r", 1, True, expected_head="Y") and env.rec["approved"]
    assert mark("o/r", 1, False) and not env.rec["approved"]


def test_failed_approval_persistence_does_not_leave_memory_approved():
    env = Env("X", False)
    def fail(_):
        raise OSError("disk failure")
    env.ns["save_pr_reviews"] = fail
    with pytest.raises(env.ns["ReviewPersistenceError"]):
        env.ns["mark_pr_approved"]("o/r", 1, True, expected_head="X")
    assert not env.rec["approved"]


def real_review_store(env, path):
    """Bind the real config-store writer to an isolated on-disk path."""
    ns = {"os": os, "json": json, "logger": Logger(), "PR_REVIEWS_FILE": str(path)}
    extract("config_store.py", {"_atomic_write_json", "save_pr_reviews"}, ns)
    env.ns["save_pr_reviews"] = ns["save_pr_reviews"]
    return ns


@pytest.mark.parametrize("approved", [True, False])
def test_real_store_write_failure_fails_closed(tmp_path, approved):
    """A real caught file I/O failure must not grant or restore approval."""
    env = Env("X", not approved)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    store = real_review_store(env, blocker / "reviews.json")
    errors = []
    env.ns["logger"] = SimpleNamespace(error=lambda message: errors.append(message))
    if approved:
        with pytest.raises(env.ns["ReviewPersistenceError"]):
            env.ns["mark_pr_approved"]("o/r", 1, approved, expected_head="X")
    else:
        assert not env.ns["mark_pr_approved"]("o/r", 1, approved, expected_head="X")
    assert not env.rec["approved"]
    assert any("mark_pr_approved failed" in message for message in errors)
    assert not list(tmp_path.glob("*.tmp"))
    assert store["PR_REVIEWS_FILE"].endswith("reviews.json")
    pr = PR()
    status, _ = env.run(pr)
    assert status == 409 and not pr.calls


@pytest.mark.parametrize("approved", [True, False])
def test_real_store_success_persists_grant_and_revoke(tmp_path, approved):
    """A successful real writer persists the requested approval state."""
    env = Env("X", not approved)
    path = tmp_path / "reviews.json"
    real_review_store(env, path)
    assert env.ns["mark_pr_approved"]("o/r", 1, approved, expected_head="X")
    assert env.rec["approved"] is approved
    assert json.loads(path.read_text(encoding="utf-8"))["o/r#1"]["approved"] is approved


def test_failed_reapproval_restores_previous_approved_state(tmp_path):
    """A redundant failed grant preserves the previously approved snapshot."""
    env = Env("X", True)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    real_review_store(env, blocker / "reviews.json")
    with pytest.raises(env.ns["ReviewPersistenceError"]):
        env.ns["mark_pr_approved"]("o/r", 1, True, expected_head="X")
    assert env.rec == {"head": "X", "approved": True}


@pytest.mark.parametrize("approved", [True, False])
def test_failed_old_head_write_cannot_overwrite_concurrent_new_review(tmp_path, approved):
    """Rollback or failed revocation cannot clobber a later review under the same lock."""
    env = Env("X", not approved)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    real_review_store(env, blocker / "reviews.json")
    in_lock, waiting = threading.Event(), threading.Event()
    def stamp(*args):
        """Pause the failing write while a new review waits for the state lock."""
        in_lock.set()
        assert waiting.wait(5)
    def replace_review():
        """Publish the new review only after the older mutation releases its lock."""
        assert in_lock.wait(5)
        waiting.set()
        with env.ns["_task_state_lock"]:
            env.state["pr_reviews"]["o/r#1"] = {"head": "Y", "approved": True}
    env.ns["_stamp_pr_state"] = stamp
    thread = threading.Thread(target=replace_review)
    thread.start()
    if approved:
        with pytest.raises(env.ns["ReviewPersistenceError"]):
            env.ns["mark_pr_approved"]("o/r", 1, approved, expected_head="X")
    else:
        assert not env.ns["mark_pr_approved"]("o/r", 1, approved, expected_head="X")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert env.rec == {"head": "Y", "approved": True}


@pytest.mark.parametrize("approved", [True, False])
def test_old_head_cas_does_not_write_or_change_newer_approval(tmp_path, approved):
    """A stale grant or revoke is rejected without touching disk or newer approval."""
    env = Env("Y", True)
    path = tmp_path / "reviews.json"
    real_review_store(env, path)
    assert not env.ns["mark_pr_approved"]("o/r", 1, approved, expected_head="X")
    assert env.rec == {"head": "Y", "approved": True} and not path.exists()


def test_failed_review_update_reports_failure_and_keeps_merge_milestone(tmp_path):
    """An already-completed merge keeps its independent lifetime milestone."""
    env = Env("X", True)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    real_review_store(env, blocker / "reviews.json")
    milestones = []
    env.ns.update({"datetime": datetime, "_record_merge_milestone": lambda *a: milestones.append(a)})
    extract("app_state.py", {"update_pr_review"}, env.ns)
    assert not env.ns["update_pr_review"]("o/r", 1, merged=True)
    assert env.rec["merged"] is True
    assert len(milestones) == 1 and milestones[0][1]["merged"] is True


def test_failed_review_record_write_keeps_moved_head_unapproved(tmp_path):
    """Failed persistence of a new head must not revive the old head's approval."""
    env = Env("X", True)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    real_review_store(env, blocker / "reviews.json")
    env.ns.update({"datetime": datetime, "_PR_REVIEWS_MAX": 100})
    extract("app_state.py", {"record_pr_review", "_panel_dissent_stats"}, env.ns)
    env.ns["record_pr_review"]("o/r", 1, "title", "url", [], "Y")
    assert env.rec["head"] == "Y" and not env.rec["approved"]


def test_human_approval_real_write_failure_reports_storage_unavailable(tmp_path):
    """The human path must not misreport a failed disk write as a moved head."""
    env = Env("X", False)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    real_review_store(env, blocker / "reviews.json")
    class Response:
        """Capture the human route's explicit HTTP status and JSON content."""
        def __init__(self, status_code, content):
            """Record the response without importing the live dashboard."""
            self.status_code, self.content = status_code, content
    async def data():
        """Submit approval for the unchanged reviewed head."""
        return {"repo": "o/r", "number": 1, "head": "X"}
    env.ns.update({"Request": object, "JSONResponse": Response, "asyncio": asyncio, "os": os,
                   "load_config": lambda: {"GITHUB_TOKEN": "fake"},
                   "Github": lambda token: object(), "approve_pr": lambda *a, **k: (None, PR("X"))})
    extract("routes.py", {"pr_review_approve"}, env.ns)
    result = asyncio.run(env.ns["pr_review_approve"](SimpleNamespace(json=data)))
    assert result.status_code == 503 and "storage" in result.content["message"].lower()
    assert "head_moved" not in result.content and not env.rec["approved"]
    pr = PR()
    assert env.run(pr)[0] == 409 and not pr.calls


def test_auto_approval_real_write_failure_logs_storage_and_never_merges(tmp_path, monkeypatch):
    """The auto path defers specifically for storage, never for a head mismatch."""
    env = Env("X", False)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker", encoding="utf-8")
    real_review_store(env, blocker / "reviews.json")
    messages, merges = [], []
    env.ns["logger"] = SimpleNamespace(
        info=lambda msg, *args: messages.append(("info", msg % args)),
        warning=lambda msg, *args: messages.append(("warning", msg % args)),
        exception=lambda msg, *args: messages.append(("exception", msg % args)),
        error=lambda msg, *args: messages.append(("error", msg % args if args else msg)))
    env.ns.update({"os": os, "feature_allowlist": SimpleNamespace(files_from_pr_files=lambda files: []),
                   "_FEATURE_DRIVE_MARKER_RE": re.compile("feature"),
                   "_automerge_decision": lambda *a: (True, "cleared"),
                   "approve_pr": lambda *a, **k: None,
                   "merge_pr": lambda *a, **k: merges.append(True)})
    monkeypatch.setitem(sys.modules, "pr_actions", SimpleNamespace(
        head_ci_conclusion=lambda *a: ("unknown", "")))
    monkeypatch.setitem(sys.modules, "pr_remediate", SimpleNamespace(
        remediation_pending=lambda *a: (False, "")))
    pr = PR()
    pr.number, pr.body, pr.mergeable = 1, "", True
    pr.base = SimpleNamespace(ref="dev")
    pr.get_files = lambda: []
    extract("pr_review.py", {"_maybe_auto_merge"}, env.ns)
    env.ns["_maybe_auto_merge"](None, SimpleNamespace(full_name="o/r"), pr, {})
    assert any(level == "warning" and "storage" in msg.lower() for level, msg in messages)
    assert not any("review head changed" in msg for _, msg in messages)
    assert not merges and not env.rec["approved"] and not env.rec.get("auto_merged")
    assert not env.rec.get("auto_merge_refusal")


@pytest.mark.parametrize("old", ["X", None])
def test_stale_invalidation_does_not_clear_new_review(old):
    env = Env("Y", True)
    assert not env.ns["mark_pr_approved"]("o/r", 1, False, expected_head=old)
    assert env.rec == {"head": "Y", "approved": True} and not env.saved


def test_head_moved_while_handling_merge_preserves_new_approval():
    env = Env()
    def refresh(pr):
        pr.head.sha = "Y"
        env.rec.update(head="Y", approved=True)
    pr = PR(error=GithubException(409, "Head branch was modified"), on_update=refresh)
    status, result = env.run(pr)
    assert status == 409 and result["head_moved"]
    assert env.rec == {"head": "Y", "approved": True}


def test_conflict_push_leaves_new_review_approval_intact_and_scannable():
    env = Env()
    def resolve(*args):
        env.rec.update(head="Y", approved=True)
        return True, "pushed"
    env.ns["_auto_resolve_pr_conflicts"] = resolve
    status, result = env.run(PR(error=GithubException(405, "conflict")))
    assert status == 409 and result["needs_approval"]
    assert env.rec == {"head": "Y", "approved": True}


def test_changed_recovered_head_remains_eligible_for_scanner():
    env = Env()
    env.ns["_auto_resolve_pr_conflicts"] = lambda *a: (True, "pushed")
    env.run(PR(error=GithubException(405, "conflict")))
    env.ns.update({"is_queued_for_retry_stale": lambda *a: False,
                   "_find_marker_comment": lambda pr: SimpleNamespace(body="<!-- head: X -->"),
                   "_desc_is_current": lambda *a: True, "_desc_digest": lambda pr: ""})
    extract("pr_review.py", {"_head_already_reviewed"}, env.ns)
    pr = SimpleNamespace(number=1, head=SimpleNamespace(sha="Y"))
    assert not env.ns["_head_already_reviewed"]("o/r", pr)
    assert env.rec["head"] == "X"


@pytest.mark.parametrize("merged,state,status", [(True, "open", 200), (False, "closed", 409)])
def test_terminal_pr_precedes_head_guard(merged, state, status):
    env, pr = Env(None), PR(head=None)
    pr.merged, pr.state = merged, state
    code, _ = env.run(pr)
    assert code == status and not pr.calls


def test_human_inflight_approval_cannot_approve_new_review():
    env = Env("X", False)
    def approve(*args, **kwargs):
        env.rec.update(head="Y", approved=False)
        return None, PR(head="X")
    class Response:
        def __init__(self, status_code, content):
            self.status_code, self.content = status_code, content
    async def data():
        return {"repo": "o/r", "number": 1, "head": "X"}
    env.ns.update({"Request": object, "JSONResponse": Response, "asyncio": asyncio, "os": os,
                   "load_config": lambda: {"GITHUB_TOKEN": "fake"},
                   "Github": lambda token: object(), "approve_pr": approve})
    extract("routes.py", {"pr_review_approve"}, env.ns)
    response = asyncio.run(env.ns["pr_review_approve"](SimpleNamespace(json=data)))
    assert response.status_code == 409 and response.content["head_moved"]
    assert env.rec == {"head": "Y", "approved": False}


def test_human_github_head_mismatch_invalidates_only_old_approval():
    env = Env("X", True)
    class Response:
        def __init__(self, status_code, content):
            self.status_code, self.content = status_code, content
    async def data():
        return {"repo": "o/r", "number": 1, "head": "X"}
    env.ns.update({"Request": object, "JSONResponse": Response, "asyncio": asyncio, "os": os,
                   "load_config": lambda: {"GITHUB_TOKEN": "fake"},
                   "Github": lambda token: object(), "approve_pr": lambda *a, **k: (None, PR("Y"))})
    extract("routes.py", {"pr_review_approve"}, env.ns)
    response = asyncio.run(env.ns["pr_review_approve"](SimpleNamespace(json=data)))
    assert response.status_code == 409 and not env.rec["approved"]


def test_human_matching_review_is_approved():
    env = Env("X", False)
    class Response:
        def __init__(self, status_code, content):
            self.status_code, self.content = status_code, content
    async def data():
        return {"repo": "o/r", "number": 1, "head": "X"}
    env.ns.update({"Request": object, "JSONResponse": Response, "asyncio": asyncio, "os": os,
                   "load_config": lambda: {"GITHUB_TOKEN": "fake"},
                   "Github": lambda token: object(), "approve_pr": lambda *a, **k: (None, PR("X"))})
    extract("routes.py", {"pr_review_approve"}, env.ns)
    response = asyncio.run(env.ns["pr_review_approve"](SimpleNamespace(json=data)))
    assert response["status"] == "success" and env.rec["approved"]


def test_human_head_mismatch_preserves_newer_approval():
    env = Env("X", True)
    class Response:
        def __init__(self, status_code, content):
            self.status_code, self.content = status_code, content
    async def data():
        return {"repo": "o/r", "number": 1, "head": "X"}
    def approve(*args, **kwargs):
        env.rec.update(head="Y", approved=True)
        return None, PR("Y")
    env.ns.update({"Request": object, "JSONResponse": Response, "asyncio": asyncio, "os": os,
                   "load_config": lambda: {"GITHUB_TOKEN": "fake"},
                   "Github": lambda token: object(), "approve_pr": approve})
    extract("routes.py", {"pr_review_approve"}, env.ns)
    response = asyncio.run(env.ns["pr_review_approve"](SimpleNamespace(json=data)))
    assert response.status_code == 409 and env.rec == {"head": "Y", "approved": True}


@pytest.mark.parametrize("head", ["X", None, "", "   ", 123, "__omitted__"])
def test_stale_or_missing_rendered_head_cannot_approve(head):
    env = Env("Y", False)
    calls = []
    class Response:
        def __init__(self, status_code, content):
            self.status_code, self.content = status_code, content
    async def data():
        result = {"repo": "o/r", "number": 1}
        if head != "__omitted__":
            result["head"] = head
        return result
    env.ns.update({"Request": object, "JSONResponse": Response, "asyncio": asyncio, "os": os,
                   "load_config": lambda: {"GITHUB_TOKEN": "fake"},
                   "Github": lambda token: object(),
                   "approve_pr": lambda *a, **k: calls.append(True) or (None, PR("Y"))})
    extract("routes.py", {"pr_review_approve"}, env.ns)
    response = asyncio.run(env.ns["pr_review_approve"](SimpleNamespace(json=data)))
    assert response.status_code == 409 and response.content["needs_approval"]
    assert not calls and not env.rec["approved"]
