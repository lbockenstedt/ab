"""Acknowledged fix publication survives later completion errors without a rebuild."""

import ast
import contextlib
import copy
from datetime import datetime
from pathlib import Path
import traceback
from types import SimpleNamespace as N
from unittest.mock import Mock

import pytest

import main  # noqa: F401 -- preserve production import order
import config_store
import fix_engine
import workers


SHA = "0123456789abcdef0123456789abcdef01234567"
REBASED_SHA = "abcdef0123456789abcdef0123456789abcdef01"
PR_URL = "https://github.com/o/r/pull/123"


def _run_publication(kind="direct", failure=None, store_failure=False, after_store=None, wait_spokes=None):
    """Run the actual final publication branch and its actual outer handlers."""
    events, writes = [], []
    store = {"o/r:1": {"status": "processing", "title": "Keep identity"}}
    pushes = []

    class Issue:
        """Raise on real completion paths without network or OS effects."""
        number, title = 1, "Fix the problem"

        @property
        def body(self):
            """Expose metadata access that is unsafe to postpone until after push."""
            if failure == "late_metadata" and pushes:
                raise OSError("body unavailable after publication")
            return "Original issue body"

        def create_comment(self, body):
            """Model an optional comment and retain the actual text for assertions."""
            events.append(("comment", body))

        def edit(self, **kwargs):
            """Model a GitHub closure that can fail after a confirmed publication."""
            events.append(("close", kwargs))
            if failure == "close":
                raise OSError("close failed")

    class Remote:
        """Model the one rebase while keeping the actual pre-push HEAD observable."""
        def pull(self, branch, rebase):
            """A real rebase changes the SHA that the next push acknowledges."""
            events.append(("pull", branch))
            repo_git.head.commit.hexsha = REBASED_SHA

    class Git:
        """Supply only local git mutations reached by the real publication tail."""
        def add(self, **kwargs):
            """Staging is local and has no external effect."""

        def rebase(self, *args):
            """Record an abort so a confirmed push cannot hide a fallback."""
            events.append(("rebase", args))

        def checkout(self, *args):
            """Record topic-branch fallback without changing a real checkout."""
            events.append(("checkout", args))

    def commit(message):
        """Return the commit object GitPython returns, without creating a commit."""
        return repo_git.head.commit

    repo_git = N(head=N(commit=N(hexsha=SHA)), index=N(commit=commit),
                 git=Git(), remotes=N(origin=Remote()))
    issue = Issue()
    pr = N(number=123, html_url=PR_URL)

    def find_pr(*args):
        """Distinguish a known existing PR from an unsuccessful/uncertain lookup."""
        events.append(("lookup",))
        if failure == "lookup":
            raise OSError("PR lookup failed")
        if failure == "422_reused" and sum(event[0] == "lookup" for event in events) == 2:
            return pr
        return pr if kind == "existing_pr" else None

    def create_pr(**kwargs):
        """A creation error cannot prove that the remote PR does not exist."""
        events.append(("create_pr",))
        if failure in ("422_reused", "422_missing"):
            raise fix_engine.GithubException(422, {"message": "already exists"}, None)
        if failure == "created_but_unacknowledged":
            events.append(("remote_created", PR_URL))
        if failure in ("create", "created_but_unacknowledged"):
            raise OSError("PR creation response lost")
        return pr

    repo = N(clone_url="https://github.com/o/r.git", default_branch="main",
             create_pull=create_pr)

    def push(remote, refspec, **kwargs):
        """A return is an acknowledgement; exceptions before it are not."""
        pushes.append((refspec, repo_git.head.commit.hexsha))
        events.append(("push", refspec))
        if (failure == "unconfirmed" or failure == "rebased_cleanup" and len(pushes) == 1
                or failure == "direct_rejections" and len(pushes) <= 2):
            raise OSError("push rejected before acknowledgement")

    @contextlib.contextmanager
    def authenticated(*args):
        """Inject a context-exit failure only after an acknowledged push."""
        yield
        if failure == "cleanup" or failure == "rebased_cleanup" and len(pushes) == 2:
            raise OSError("remote cleanup failed")

    @contextlib.contextmanager
    def workspace():
        """Exercise the actual outer workspace context without touching a checkout."""
        try:
            yield "unused-no-filesystem"
        finally:
            if failure == "workspace_cleanup":
                raise OSError("workspace cleanup failed")

    def update(mutator):
        """Commit a detached candidate, or report a real typed storage failure."""
        candidate = copy.deepcopy(store)
        result = mutator(candidate)
        writes.append(copy.deepcopy(candidate))
        if store_failure:
            raise config_store.ProcessedStoreError("history disk full")
        store.clear()
        store.update(candidate)
        events.append(("stored", candidate["o/r:1"]["status"]))
        if after_store:
            after_store(copy.deepcopy(store["o/r:1"]))
        return result

    def complete(name):
        """Make a later completion failure observable without hiding its artifact."""
        def step(*args, **kwargs):
            """Run one named post-publication action."""
            events.append((name,))
            if failure == name:
                raise OSError(name + " failed")
        return step

    ns = dict(vars(fix_engine), state={}, issue=issue, issue_id="o/r:1",
              repo_name="o/r", issue_num=1, repo_obj=repo, repo_git=repo_git, config={},
              path="unused-no-filesystem", can_direct_push=kind == "direct",
              final_confidence=.91 if kind == "integration" else .99,
              confidence_threshold=.95, final_verdict="Approve", boundary_forced_pr=False,
              boundary_pr_reason="", direct_push_refused_reason=None, decision_reason=None,
              base_branch="dev", token="test-token", fixes={"page.py": "pass"},
              was_reopened=False, datetime=datetime, traceback=traceback,
              time=N(sleep=lambda seconds: None),
              tempfile=N(TemporaryDirectory=workspace),
              update_processed=update, update_task_state=complete("task_end"),
              recompute_issue_counters=lambda: None, _release_issue=lambda issue_id: None,
              _notify_bug_fixed=complete("notify"), _apply_closed_label=complete("label"),
              _opened_at=lambda item: "2026-01-01T00:00:00",
              _progress=complete("progress"), _trigger_spoke_updates=complete("spokes"),
              _wait_for_spokes_online=wait_spokes or complete("wait_spokes"),
              _authenticated_remote=authenticated, push_or_raise=push,
              may_force_push=lambda *args, **kwargs: (False, "protected"),
              integration_branch=lambda *args: ("dev", "integration"),
              auto_branch_name=lambda *args, **kwargs: "ai-fix/1",
              bump_repo_version=lambda path: None,
              _publication_boundary_requirement=lambda *args: (False, ""),
              _fix_pr_body=lambda *args: "Reviewed fix",
              find_existing_pull_request=find_pr)
    tree = ast.parse(Path(fix_engine.__file__).read_text())
    pipeline = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "process_single_issue")
    parent, index = next(
        (node, i) for node in ast.walk(pipeline) if isinstance(getattr(node, "body", None), list)
        for i, statement in enumerate(node.body)
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "version_bumped"
            for target in statement.targets))
    tail = copy.deepcopy(next(node for node in pipeline.body
                              if isinstance(node, ast.Try) and node.finalbody))
    assert isinstance(parent, ast.With)
    workspace_block = copy.deepcopy(parent)
    workspace_block.body = copy.deepcopy(parent.body[index:])
    tail.body = [workspace_block]
    function = ast.parse(
        "def publish(can_direct_push=can_direct_push, boundary_forced_pr=boundary_forced_pr,\n"
        "            boundary_pr_reason=boundary_pr_reason, base_branch=base_branch, issue_id=issue_id):\n"
        "    publication = {}\n"
        "    publication_context = {}\n"
        "    publication_counted = False\n"
        "    pass\n").body[0]
    function.body[-1] = tail
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name in ("_push_fix_with_evidence", "_record_fix_publication",
                                 "_fix_publication_outcome")]
    exec(compile(ast.fix_missing_locations(ast.Module(body=helpers + [function], type_ignores=[])),
                 str(fix_engine.__file__), "exec"), ns)
    result = ns["publish"]()
    return N(result=result, record=store["o/r:1"], events=events, pushes=pushes, writes=writes,
             count=ns["state"].get("daily_fixes_count", 0))


@pytest.mark.parametrize("kind", ["direct", "integration", "topic", "existing_pr"])
def test_outcome_is_durable_before_the_issue_is_closed(kind):
    """Even a normal completion must not close before its required history commits."""
    case = _run_publication(kind)
    assert case.result[0] is True and case.record["status"] == "pending_verification"
    assert [event[0] for event in case.events].index("stored") < [
        event[0] for event in case.events].index("close")
    assert case.record["commit"] == SHA
    assert case.count == 1


@pytest.mark.parametrize("kind", ["direct", "integration", "topic"])
def test_acknowledged_push_cleanup_does_not_repeat_or_lose_the_publication(kind):
    """A failed remote cleanup is not an unacknowledged push rejection."""
    case = _run_publication(kind, "cleanup")
    assert case.result[0] is False and case.record["status"] == "pending_verification"
    assert len(case.pushes) == 1 and not any(event[0] == "pull" for event in case.events)
    assert case.record["commit"] == SHA and SHA in case.result[1]
    assert "cleanup" in case.record["error"]
    if kind == "direct":
        assert case.record["commit_type"] == "Direct Commit"
    elif kind == "integration":
        assert case.record["commit_type"] == "Commit to dev"
        assert not any(event[0] in ("lookup", "create_pr") for event in case.events)
    else:
        assert "PR unconfirmed" in case.record["commit_type"] and not case.record.get("pr_url")


def test_rebased_acknowledgement_retains_the_actual_new_sha_without_fallback():
    """The failed initial push is distinct from a confirmed rebased push."""
    case = _run_publication("direct", "rebased_cleanup")
    assert len(case.pushes) == 2 and case.record["commit"] == REBASED_SHA
    assert case.record["status"] == "pending_verification"
    assert case.record["commit_type"] == "Direct Commit"
    assert not any(event[0] in ("lookup", "create_pr", "checkout") for event in case.events)


@pytest.mark.parametrize("failure", ["lookup", "create", "created_but_unacknowledged", "422_missing"])
def test_published_topic_branch_does_not_claim_a_pr_or_become_retryable(failure):
    """An uncertain PR result is not proof of no PR, nor an excuse to regenerate."""
    case = _run_publication("topic", failure)
    assert case.result[0] is False and case.record["status"] == "pending_verification"
    assert case.record["commit"] == SHA and not case.record.get("pr_url")
    assert "PR unconfirmed" in case.record["commit_type"]
    assert "PR" in case.result[1] and SHA in case.result[1]
    assert "no PR" not in case.result[1]


@pytest.mark.parametrize("failure", ["progress", "spokes", "wait_spokes", "close", "notify", "label", "task_end"])
def test_later_completion_failure_retains_nonretryable_publication_evidence(failure):
    """Every actual post-acknowledgement failure path retains the known phase."""
    case = _run_publication("direct", failure)
    assert case.result[0] is False and case.record["status"] == "pending_verification"
    assert case.record["commit"] == SHA and case.record["branch"] == "dev"
    assert failure in case.record["error"] and SHA in case.result[1]
    assert len(case.pushes) == 1
    assert case.count == 1


@pytest.mark.parametrize("kind", ["integration", "topic", "existing_pr"])
@pytest.mark.parametrize("failure", ["progress", "close", "notify", "label"])
def test_non_direct_completion_errors_preserve_the_exact_published_phase(kind, failure):
    """PR and shared-branch outcomes must not collapse into an unknown topic phase."""
    case = _run_publication(kind, failure)
    assert case.result[0] is False and case.record["status"] == "pending_verification"
    assert case.record["commit"] == SHA and failure in case.record["error"]
    assert case.count == 1
    if kind == "integration":
        assert case.record["commit_type"] == "Commit to dev" and not case.record.get("pr_url")
    else:
        assert case.record["commit_type"] == "Pull Request" and case.record["pr_url"] == PR_URL


@pytest.mark.parametrize("kind", ["direct", "integration", "topic", "existing_pr"])
def test_workspace_cleanup_after_completion_preserves_the_published_outcome(kind):
    """Even a cleanup error replacing the success return cannot erase publication."""
    case = _run_publication(kind, "workspace_cleanup")
    assert case.result[0] is False and case.record["status"] == "pending_verification"
    assert "workspace cleanup failed" in case.record["error"]
    assert case.record["commit"] == SHA and len(case.pushes) == case.count == 1
    if kind in ("topic", "existing_pr"):
        assert case.record["pr_url"] == PR_URL


@pytest.mark.parametrize("failure", ["422_reused", "direct_rejections"])
def test_unconfirmed_direct_rejections_and_422_recovery_keep_existing_success_behavior(failure):
    """Only acknowledged cleanup errors stop fallback; actual rejections still recover."""
    kind = "direct" if failure == "direct_rejections" else "topic"
    case = _run_publication(kind, failure)
    assert case.result[0] is True and case.record["commit_type"] == "Pull Request"
    assert case.record["pr_url"] == PR_URL
    assert len(case.pushes) == (3 if kind == "direct" else 1)
    assert case.record["commit"] == (REBASED_SHA if kind == "direct" else SHA)


def test_lazy_metadata_is_snapshotted_before_publication():
    """A post-push getter failure must not discard a known artifact."""
    case = _run_publication("direct", "late_metadata")
    assert case.result[0] is True and case.record["status"] == "pending_verification"
    assert case.record["original_body"] == "Original issue body"


@pytest.mark.parametrize("failure", [None, "progress"])
def test_required_store_failure_reports_known_evidence_without_failed_rewrite(failure):
    """The documented durability limit is explicit, never a success-shaped fallback."""
    case = _run_publication("direct", failure, store_failure=True)
    assert case.result[0] is False and SHA in case.result[1]
    assert "History persistence failed" in case.result[1]
    assert len(case.writes) == 1 and case.record["status"] == "processing"
    assert not any(event[0] == "close" for event in case.events)
    assert case.count == 0


def test_unacknowledged_push_remains_a_real_failure_not_a_published_phase():
    """Do not invent publication evidence for a rejected push."""
    case = _run_publication("direct", "unconfirmed")
    assert case.result[0] is False and case.record["status"] == "failed"
    assert not case.record.get("commit") and not case.record.get("pr_url")
    assert case.count == 0


def _scan_twice(record, label):
    """Run the actual scanner twice with one still-open, otherwise eligible issue."""
    dispatched = []
    issue = N(state="open", pull_request=None, number=1, body="A real bug",
              labels=[N(name=label)])
    repo = N(owner=N(login="bot"), get_issues=lambda **kwargs: [issue])
    gh = N(get_user=lambda: N(login="bot"), get_repo=lambda name: repo)
    logger, available = Mock(), Mock(return_value=(True, 0))
    ns = dict(vars(workers), state={}, logger=logger,
              main=N(get_monitored_repos=lambda config: ["o/r"],
                     reclassify_ops_alerts=lambda repo: None,
                     process_single_issue=lambda *args: dispatched.append(args)),
              update_task_state=Mock(),
              _schedule_check=lambda config: {"allowed": True, "reason": "allowed"},
              _any_provider_available=available, _retry_failed_issue=lambda *args: True,
              _count_retry=lambda *args: True, sync_github_issue_state=lambda *args, **kwargs: None,
              close_shipped_features=lambda *args, **kwargs: None)
    node = next(node for node in ast.parse(Path(workers.__file__).read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == "scan_repo_issues")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(workers.__file__), "exec"), ns)
    config = {"monitored_labels": ["ANY"], "fix_logdetected_enabled": True,
              "SCHEDULER_CRITICAL_LABEL": "critical", "MAX_CONCURRENT_FIXES": 1}
    for _ in range(2):
        ns["scan_repo_issues"](gh, config, {"o/r:1": copy.deepcopy(record)})
    logger.exception.assert_not_called()
    return dispatched, available.call_count


@pytest.mark.parametrize("label", ["bug", "critical", "automated-fix"])
@pytest.mark.parametrize("record,expected", [
    ({"status": "pending_verification", "commit": SHA}, 0),
    ({"status": "pending_verification"}, 2),
    ({"status": "failed"}, 2),
    ({"status": "awaiting_human"}, 0),
    ({"status": "triaged"}, 2),
    ({"status": "fixed"}, 0),
    ({"status": "awaiting_prod_verification"}, 0),
])
def test_scanner_preserves_publication_and_human_holds_in_every_auto_fix_tier(record, expected, label):
    """Human/publication holds skip every tier; retries and triaged work stay eligible."""
    dispatched, provider_checks = _scan_twice(record, label)
    assert len(dispatched) == provider_checks == expected


def test_failed_issue_closure_does_not_dispatch_another_model_on_later_scans():
    """Exercise the actual publication outcome followed by two actual scanner passes."""
    case = _run_publication("direct", "close")
    assert case.result[0] is False and case.record["status"] == "pending_verification"
    assert _scan_twice(case.record, "bug") == ([], 0)
