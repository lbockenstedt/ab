"""Keep confirmed feature publication separate from later completion failures."""

import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_feature_build as harness


COMMIT = "0123456789abcdef0123456789abcdef01234567"


def run_build(monkeypatch, failure=None):
    """Run the actual builder with isolated history and counted external fakes."""
    ns = harness._load_ns()
    ns["logger"] = logging.getLogger(__name__)
    repo, issue = harness._FakeGhRepo(), harness._FakeGhIssue()
    git_repo = harness._FakeRepoGit(changed=["page.py", "docs/change.md"])
    ns["skills_loader"]._instructions["skill"] = "# Build this small documented change"
    ns["_llm_holder"]["responses"] = ['{"pr_body": "Built change and docs"}']
    ns["_store"]["owner/repo:42"] = {
        "title": "Keep identity", "issue_url": "https://github.com/owner/repo/issues/42",
        "commit": "stale", "branch": "old-branch", "pr_url": "old-pr",
    }

    def clone(url, path, branch=None):
        """Provide changed files so the real syntax and docs gates execute."""
        for name in git_repo._changed:
            dest = Path(path) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("")
        return git_repo

    monkeypatch.setattr(ns["git"].Repo, "clone_from", clone)
    original_commit = git_repo.index.commit

    def commit(message):
        """Return the commit GitPython actually returns after a successful commit."""
        if failure == "commit":
            raise RuntimeError("commit failed")
        original_commit(message)
        return SimpleNamespace(hexsha=COMMIT)

    git_repo.index.commit = commit
    lookups = []

    def lookup(*args):
        """Model post-push PR lookup and a concurrent-create recovery."""
        lookups.append(True)
        if failure == "existing":
            return harness._FakePR()
        if failure == "lookup" and git_repo.remotes.origin.pushed:
            raise RuntimeError("lookup failed")
        if failure == "422" and len(lookups) == 2:
            return harness._FakePR()
        return None

    ns["find_existing_pull_request"] = lookup
    create_pull = repo.create_pull

    def create(**kwargs):
        """Fail PR publication without undoing a confirmed branch push."""
        if failure in ("create", "422"):
            status = 422 if failure == "422" else 503
            raise ns["GithubException"](status, "PR publication failed", None)
        return create_pull(**kwargs)

    repo.create_pull = create
    if failure == "push":
        def push(*args, **kwargs):
            """Reject before a push has been confirmed."""
            raise RuntimeError("push failed")
        ns["push_or_raise"] = push
    if failure == "cleanup":
        @contextlib.contextmanager
        def remote(*args):
            """Fail remote cleanup after the body confirmed the push."""
            yield
            raise RuntimeError("remote cleanup failed")
        ns["_authenticated_remote"] = remote
    if failure in ("record", "record_store", "record_error", "built_store"):
        def mark_built(*args, **kwargs):
            """Expose a non-storage completion error after a PR URL is known."""
            if failure == "built_store":
                raise ns["ProcessedStoreError"]("history disk full")
            raise RuntimeError("completion failed")
        ns["_mark_built"] = mark_built
    if failure in ("record_store", "record_error", "built_store"):
        def mark_failed(*args, **kwargs):
            """Keep failed history publication separate from another build attempt."""
            if failure == "built_store":
                pytest.fail("a built-PR storage failure must not rewrite feature_failed")
            error = ns["ProcessedStoreError"] if failure == "record_store" else RuntimeError
            raise error("history disk full")
        ns["_mark_failed"] = mark_failed

    result = ns["build_feature"](
        None, repo, issue, {"skill": "skill"},
        {"GITHUB_TOKEN": "test", "feature_require_docs": True})
    return result, dict(ns["_store"]["owner/repo:42"]), ns, git_repo, repo


@pytest.mark.parametrize("failure", ["create", "lookup", "cleanup", "record"])
def test_confirmed_push_context_survives_later_completion_failure(monkeypatch, failure):
    """A later error is failed completion, not an unpublished or successful build."""
    result, record, ns, git_repo, repo = run_build(monkeypatch, failure)
    assert result[0] is False and "Published" in result[1] and COMMIT in result[1]
    assert record["status"] == "feature_failed"
    assert record["title"] == "Keep identity"
    assert record["commit"] == COMMIT
    assert record["branch"] == git_repo.remotes.origin.pushed[0][0]
    assert record["branch"] in result[1] and COMMIT in record["reason"]
    assert len(git_repo.commits) == len(git_repo.remotes.origin.pushed) == 1
    assert len(ns["_llm_holder"]["calls"]) == 1
    if failure == "record":
        assert record["pr_url"] == "https://github.com/owner/repo/pull/99"
        assert record["pr_url"] in result[1] and len(repo.created_prs) == 1
    else:
        assert "pr_url" not in record


@pytest.mark.parametrize("failure", ["commit", "push"])
def test_unconfirmed_publication_does_not_inherit_stale_artifacts(monkeypatch, failure):
    """No confirmed push means no new or retained publication claim."""
    result, record, ns, git_repo, _ = run_build(monkeypatch, failure)
    assert result[0] is False and "Published" not in result[1]
    assert record["status"] == "feature_failed"
    assert not {"commit", "branch", "pr_url"} & record.keys()
    assert not git_repo.remotes.origin.pushed
    assert len(ns["_llm_holder"]["calls"]) == 1


@pytest.mark.parametrize("failure", [None, "422", "existing"])
def test_success_and_422_reuse_retain_confirmed_artifacts(monkeypatch, failure):
    """Normal publication and recovered PR races remain successful, once only."""
    result, record, ns, git_repo, _ = run_build(monkeypatch, failure)
    assert result == (True, "https://github.com/owner/repo/pull/99")
    assert record["status"] == "feature_built" and record["commit"] == COMMIT
    assert record["branch"] == git_repo.remotes.origin.pushed[0][0]
    assert record["pr_url"] == result[1]
    assert len(git_repo.commits) == len(git_repo.remotes.origin.pushed) == 1
    assert len(ns["_llm_holder"]["calls"]) == 1


@pytest.mark.parametrize("failure", ["record_store", "record_error", "built_store"])
def test_history_failure_still_reports_every_known_artifact(monkeypatch, caplog, failure):
    """Even failed failure-recording cannot erase the external outcome evidence."""
    result, record, ns, git_repo, _ = run_build(monkeypatch, failure)
    assert result[0] is False and "History persistence failed" in result[1]
    assert COMMIT in result[1] and COMMIT in caplog.text
    assert git_repo.remotes.origin.pushed[0][0] in result[1]
    assert "https://github.com/owner/repo/pull/99" in result[1]
    assert record["commit"] == "stale"
    assert len(git_repo.commits) == len(git_repo.remotes.origin.pushed) == 1
    assert len(ns["_llm_holder"]["calls"]) == 1
