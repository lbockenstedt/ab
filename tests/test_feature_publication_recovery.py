"""Two-cycle offline regression tests for recovering published feature outcomes (Issue #601).

Validates cross-cycle durability when history storage fails transiently:
1. Built-record persistence failure (ProcessedStoreError after successful PR push):
   Next cycle recovers the open PR and terminal built status without re-generating code
   or replacing the published head SHA.
2. Failed-completion persistence failure (ProcessedStoreError on branch-only push):
   Next cycle recovers the branch-only outcome as feature_failed without force-pushing.
3. Closed/merged PR state handling.
4. Preserves guardrail_rounds and review_confidence across cycles.
5. Storage error in cycle 2 does not cause re-generation or force-push.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from _history import bind_history
import branch_policy
import feature_build
import feature_drive
import config_store
from config_store import ProcessedStoreError


COMMIT_SHA_1 = "aaaa0123456789abcdef0123456789abcdef0123"
COMMIT_SHA_2 = "bbbb0123456789abcdef0123456789abcdef0123"


class _FakeCommit:
    def __init__(self, sha):
        self.sha = sha
        self.hexsha = sha


class _FakeBranch:
    def __init__(self, name, sha):
        self.name = name
        self.commit = _FakeCommit(sha)


class _FakePR:
    def __init__(self, number, title, body, head_ref, head_sha, base_ref, state="open", merged=False):
        self.number = number
        self.title = title
        self.body = body
        self.html_url = f"https://github.com/owner/repo/pull/{number}"
        self.state = state
        self.merged = merged
        self.labels = []
        head = MagicMock()
        head.ref = head_ref
        head.sha = head_sha
        self.head = head
        base = MagicMock()
        base.ref = base_ref
        self.base = base

    def add_to_labels(self, label):
        self.labels.append(label)


class _FakeIssue:
    def __init__(self, number, title, body):
        self.number = number
        self.title = title
        self.body = body
        self.html_url = f"https://github.com/owner/repo/issues/{number}"
        self.labels = []
        self.comments = []
        self.pull_request = None

    def add_to_labels(self, label):
        self.labels.append(label)

    def create_comment(self, body):
        self.comments.append(body)


class _FakeRepo:
    def __init__(self, full_name="owner/repo"):
        self.full_name = full_name
        self.name = full_name.split("/")[-1]
        owner = MagicMock()
        owner.login = full_name.split("/")[0]
        self.owner = owner
        self.clone_url = f"https://github.com/{full_name}.git"
        self.default_branch = "dev"
        self.branches = {}
        self.open_prs = []
        self.closed_prs = []
        self.created_prs = []

    def get_branch(self, name):
        if name in self.branches:
            return self.branches[name]
        raise RuntimeError(f"Branch not found: {name}")

    def get_pulls(self, state="open", head=None, base=None):
        pool = self.open_prs if state == "open" else (self.closed_prs if state == "closed" else self.open_prs + self.closed_prs)
        results = []
        for pr in pool:
            if base and pr.base.ref != base:
                continue
            if head:
                # head may be "owner:branch" or bare "branch"
                expected_head = head.split(":")[-1]
                if pr.head.ref != expected_head:
                    continue
            results.append(pr)
        return results

    def create_pull(self, title, body, head, base):
        num = len(self.created_prs) + len(self.open_prs) + 100
        pr = _FakePR(num, title, body, head, COMMIT_SHA_1, base)
        self.created_prs.append(pr)
        self.open_prs.append(pr)
        return pr


def test_two_cycle_recovery_of_published_feature_pr(monkeypatch, tmp_path):
    """Cycle 1 builds and publishes PR, but _mark_built hits ProcessedStoreError.
    Cycle 2 runs scan_feature_requests and recovers the published PR without
    re-generating code or replacing the head SHA.
    """
    history_file = tmp_path / "processed_issues.json"
    journal_file = tmp_path / "feature_publications.json"
    monkeypatch.setattr(config_store, "FEATURE_JOURNAL_FILE", str(journal_file))

    store = {}
    failing_store = {"fail": False}

    def fake_update(mutator):
        if failing_store["fail"]:
            raise ProcessedStoreError("transient disk full")
        mutator(store)
        return store, None

    monkeypatch.setattr(feature_build, "update_processed", fake_update)
    monkeypatch.setattr(feature_drive, "update_processed", fake_update)
    monkeypatch.setattr(feature_drive, "load_processed", lambda: dict(store))

    repo = _FakeRepo("owner/repo")
    issue = _FakeIssue(42, "Add clear dongles button", "Please add a clear dongles button <!-- ab-feature -->")
    branch_name = branch_policy.auto_branch_name("feature", issue=issue)

    # Mock git operations
    agent_calls = []
    pushed_refs = []

    def fake_run_agent(*args, **kwargs):
        agent_calls.append(True)
        return '{"pr_body": "Implemented button"}'

    monkeypatch.setattr(feature_build, "_run_build_agent", fake_run_agent)

    class _FakeGitRemote:
        def push(self, refspec, force=False):
            pushed_refs.append((refspec, force))
        def set_url(self, url):
            pass

    class _FakeGitIndex:
        def commit(self, message):
            return SimpleNamespace(hexsha=COMMIT_SHA_1)

    class _FakeGitRepo:
        def __init__(self, *args, **kwargs):
            self.remotes = SimpleNamespace(origin=_FakeGitRemote())
            self.index = _FakeGitIndex()
            self.git = SimpleNamespace(add=lambda *a, **k: None)

        def create_head(self, name, force=False):
            return SimpleNamespace(checkout=lambda: None)

    monkeypatch.setattr(feature_build.git.Repo, "clone_from", lambda *a, **k: _FakeGitRepo())

    # Pre-PR gates pass
    monkeypatch.setattr(feature_build, "_changed_files", lambda r: ["ui.js", "docs/btn.md"])
    monkeypatch.setattr(feature_build, "_syntax_errors", lambda c, f: [])
    monkeypatch.setattr(feature_build.fleet_checks, "gate", lambda *a: "")
    monkeypatch.setattr(feature_build, "review_fix", lambda *a, **k: {"verdict": "Approve", "confidence": 0.95})
    monkeypatch.setattr(feature_build, "prepare_environment", lambda p: None)
    monkeypatch.setattr(feature_build, "verify_fix", lambda *a, **k: (True, None))

    monkeypatch.setattr(feature_build, "_build_mode", lambda c: "mutating_agent")
    monkeypatch.setattr(feature_build.skills_loader, "skill_instructions", lambda s: "instructions")
    monkeypatch.setattr(feature_build, "find_existing_pull_request", lambda r, b, base: next((p for p in r.open_prs if p.head.ref == b), None))
    monkeypatch.setattr(feature_build, "push_or_raise", lambda r, b, force=False: pushed_refs.append((b, force)))

    config = {
        "feature_drive_enabled": True,
        "feature_drive_repos": ["owner/repo"],
        "feature_drive_label": "enhancement",
        "feature_drive_require_marker": False,
        "feature_drive_max_per_cycle": 5,
        "GITHUB_TOKEN": "test_tok",
        "feature_require_docs": True,
        "skip_review": False,
        "qa_enabled": True,
    }

    # --- CYCLE 1: Build succeeds, but history persistence fails with ProcessedStoreError ---
    # We want build to proceed, publish branch and PR, and then fail on _mark_built.
    original_mark_built = feature_build._mark_built
    def failing_mark_built(*a, **k):
        raise ProcessedStoreError("transient disk full")
    monkeypatch.setattr(feature_build, "_mark_built", failing_mark_built)

    ok, msg = feature_build.build_feature(None, repo, issue, {"skill": "skill"}, config)
    assert ok is False
    assert "History persistence failed" in msg
    assert len(agent_calls) == 1
    assert len(pushed_refs) == 1
    assert len(repo.created_prs) == 1

    # Published PR is live on remote
    published_pr = repo.created_prs[0]
    repo.branches[branch_name] = _FakeBranch(branch_name, COMMIT_SHA_1)

    # In cycle 1, store never recorded feature_built because of disk failure
    assert store.get("owner/repo:42", {}).get("status") != "feature_built"

    # --- CYCLE 2: Storage is now healthy, scan_feature_requests runs ---
    monkeypatch.setattr(feature_build, "_mark_built", original_mark_built)

    class _FakeGh:
        def get_repo(self, name):
            return repo

    gh = _FakeGh()
    repo.get_issues = lambda **kw: [issue]

    # Run Cycle 2 scan
    feature_drive.scan_feature_requests(gh, config)

    # Critical assertions:
    # 1. Agent was NOT called again (still 1)
    assert len(agent_calls) == 1
    # 2. Remote was NOT pushed again (still 1)
    assert len(pushed_refs) == 1
    # 3. No new PR was created
    assert len(repo.created_prs) == 1
    # 4. History store was recovered with feature_built and exact commit / PR URL
    rec = store.get("owner/repo:42")
    assert rec is not None
    assert rec["status"] == "feature_built"
    assert rec["commit"] == COMMIT_SHA_1
    assert rec["pr_url"] == published_pr.html_url
    assert rec["branch"] == branch_name
    assert rec.get("recovered") is True
    assert rec.get("review_confidence") == 0.95


def test_two_cycle_recovery_of_branch_only_publication(monkeypatch, tmp_path):
    """Cycle 1 pushes a branch, but PR creation fails and _mark_failed hits ProcessedStoreError.
    Cycle 2 discovers the published branch and records feature_failed without force-pushing.
    """
    store = {}
    failing_store = {"fail": False}

    def fake_update(mutator):
        if failing_store["fail"]:
            raise ProcessedStoreError("transient disk full")
        mutator(store)
        return store, None

    monkeypatch.setattr(feature_build, "update_processed", fake_update)
    monkeypatch.setattr(feature_drive, "update_processed", fake_update)
    monkeypatch.setattr(feature_drive, "load_processed", lambda: dict(store))

    repo = _FakeRepo("owner/repo")
    issue = _FakeIssue(43, "Branch only feature", "Feature request body <!-- ab-feature -->")
    branch_name = branch_policy.auto_branch_name("feature", issue=issue)

    # Mock remote branch existing with COMMIT_SHA_2, but no PR
    repo.branches[branch_name] = _FakeBranch(branch_name, COMMIT_SHA_2)

    monkeypatch.setattr(feature_build, "find_existing_pull_request", lambda r, b, base: None)

    agent_calls = []
    monkeypatch.setattr(feature_build, "_run_build_agent", lambda *a, **k: agent_calls.append(True) or "{}")

    class _FakeGh:
        def get_repo(self, name):
            return repo

    gh = _FakeGh()
    repo.get_issues = lambda **kw: [issue]
    config = {
        "feature_drive_enabled": True,
        "feature_drive_repos": ["owner/repo"],
        "feature_drive_label": "enhancement",
        "feature_drive_require_marker": False,
        "feature_drive_max_per_cycle": 5,
        "GITHUB_TOKEN": "test_tok",
    }

    # Cycle 2 scans and reconciles branch-only publication
    feature_drive.scan_feature_requests(gh, config)

    # Agent was NOT invoked
    assert len(agent_calls) == 0

    # History store recovered the failed completion record
    rec = store.get("owner/repo:43")
    assert rec is not None
    assert rec["status"] == "feature_failed"
    assert rec["commit"] == COMMIT_SHA_2
    assert rec["branch"] == branch_name
    assert rec["commit_type"] == "branch"
    assert rec.get("recovered") is True


def test_two_cycle_recovery_of_merged_and_closed_prs(monkeypatch):
    """If an open PR was merged or closed before the next cycle reconciles it,
    it records terminal status (feature_built if merged, feature_failed if closed)
    and does not rebuild or force-push.
    """
    store = {}
    monkeypatch.setattr(feature_build, "update_processed", lambda m: (m(store), None))
    monkeypatch.setattr(feature_drive, "update_processed", lambda m: (m(store), None))
    monkeypatch.setattr(feature_drive, "load_processed", lambda: dict(store))

    repo = _FakeRepo("owner/repo")
    issue_merged = _FakeIssue(50, "Merged PR feature", "body <!-- ab-feature -->")
    branch_merged = branch_policy.auto_branch_name("feature", issue=issue_merged)
    pr_merged = _FakePR(201, "AI Feature #50", "body", branch_merged, COMMIT_SHA_1, "dev", state="closed", merged=True)
    repo.closed_prs.append(pr_merged)

    issue_closed = _FakeIssue(51, "Closed PR feature", "body <!-- ab-feature -->")
    branch_closed = branch_policy.auto_branch_name("feature", issue=issue_closed)
    pr_closed = _FakePR(202, "AI Feature #51", "body", branch_closed, COMMIT_SHA_2, "dev", state="closed", merged=False)
    repo.closed_prs.append(pr_closed)

    monkeypatch.setattr(feature_build, "find_existing_pull_request", lambda r, b, base: None)

    agent_calls = []
    monkeypatch.setattr(feature_build, "_run_build_agent", lambda *a, **k: agent_calls.append(True) or "{}")

    class _FakeGh:
        def get_repo(self, name):
            return repo

    gh = _FakeGh()
    repo.get_issues = lambda **kw: [issue_merged, issue_closed]
    config = {
        "feature_drive_enabled": True,
        "feature_drive_repos": ["owner/repo"],
        "feature_drive_label": "enhancement",
        "feature_drive_require_marker": False,
        "feature_drive_max_per_cycle": 5,
        "GITHUB_TOKEN": "test_tok",
    }

    feature_drive.scan_feature_requests(gh, config)

    assert len(agent_calls) == 0

    rec_merged = store.get("owner/repo:50")
    assert rec_merged is not None
    assert rec_merged["status"] == "feature_built"
    assert rec_merged["pr_url"] == pr_merged.html_url
    assert rec_merged.get("merged") is True
    assert rec_merged.get("recovered") is True

    rec_closed = store.get("owner/repo:51")
    assert rec_closed is not None
    assert rec_closed["status"] == "feature_failed"
    assert rec_closed.get("closed") is True
    assert rec_closed.get("recovered") is True


def test_recovery_resilient_to_storage_error_in_cycle_2(monkeypatch):
    """If storage is still failing with ProcessedStoreError during Cycle 2 recovery,
    the cycle catches gracefully and does NOT regenerate or force-push.
    """
    store = {}
    failing_store = {"fail": True}

    def failing_update(mutator):
        if failing_store["fail"]:
            raise ProcessedStoreError("persistent disk failure")
        mutator(store)
        return store, None

    monkeypatch.setattr(feature_build, "update_processed", failing_update)
    monkeypatch.setattr(feature_drive, "update_processed", failing_update)
    monkeypatch.setattr(feature_drive, "load_processed", lambda: dict(store))

    repo = _FakeRepo("owner/repo")
    issue = _FakeIssue(60, "Persistent error feature", "body <!-- ab-feature -->")
    branch_name = branch_policy.auto_branch_name("feature", issue=issue)

    pr = _FakePR(301, "AI Feature #60", "body", branch_name, COMMIT_SHA_1, "dev", state="open")
    repo.open_prs.append(pr)

    monkeypatch.setattr(feature_build, "find_existing_pull_request", lambda r, b, base: pr if b == branch_name else None)

    agent_calls = []
    pushed_refs = []
    monkeypatch.setattr(feature_build, "_run_build_agent", lambda *a, **k: agent_calls.append(True) or "{}")
    monkeypatch.setattr(feature_build, "push_or_raise", lambda r, b, force=False: pushed_refs.append((b, force)))

    class _FakeGh:
        def get_repo(self, name):
            return repo

    gh = _FakeGh()
    repo.get_issues = lambda **kw: [issue]
    config = {
        "feature_drive_enabled": True,
        "feature_drive_repos": ["owner/repo"],
        "feature_drive_label": "enhancement",
        "feature_drive_require_marker": False,
        "feature_drive_max_per_cycle": 5,
        "GITHUB_TOKEN": "test_tok",
    }

    # Should not raise
    feature_drive.scan_feature_requests(gh, config)

    # Critical: did NOT call agent or push
    assert len(agent_calls) == 0
    assert len(pushed_refs) == 0

