"""Configured boundaries must force a topic PR on every publication fallback."""
import logging
import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import git

import feature_boundary


@pytest.mark.parametrize("final_check", ["hit", "error", "clear"])
def test_final_staged_tree_is_checked_after_publication_mutations(tmp_path, final_check):
    """The actual commit/direct-push sequence includes paths added by the version step."""
    repo = git.Repo.init(tmp_path, initial_branch="integration")
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.test")
    report = tmp_path / "report.py"
    report.write_text("value = 1\n")
    repo.index.add(["report.py"])
    repo.index.commit("base")
    report.write_text("value = 2\n")
    source = (Path(__file__).resolve().parents[1] / "fix_engine.py").read_text()
    start = source.index("            # Stage every change from the fix.")
    end = source.index("            # Pushed: report it", start)
    pushes = []
    staged_diff = repo.git.diff
    reads = []

    def diff(*args):
        """Only the final staged-diff check can fail."""
        reads.append(args)
        if final_check == "error" and len(reads) == 2:
            raise RuntimeError("final staged diff unavailable")
        return staged_diff(*args)

    def bump(path):
        """Model the existing late publisher mutation without a real VERSION file."""
        (path / "generated.txt").write_text("publication-generated content\n")
        return "synthetic"

    class RemoteContext:
        """Avoid touching any external remote."""
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    repo.create_remote("origin", str(tmp_path / "offline.git"))
    config = {
        "trusted_repos": ["owner/repo"], "direct_push_enabled": True,
        "feature_boundaries": [
            {"id": "generated", "paths": ["generated.txt" if final_check != "clear"
                                         else "unrelated.txt"], "enabled": True}],
    }
    logger = logging.getLogger(__name__)
    namespace = {
        "config": config, "feature_boundary": feature_boundary, "logger": logger,
        "repo_name": "owner/repo", "path": tmp_path,
        "repo_git": SimpleNamespace(
            git=SimpleNamespace(add=repo.git.add, diff=diff),
            head=repo.head, index=repo.index, remotes=repo.remotes),
        "gh_current": SimpleNamespace(get_user=lambda: SimpleNamespace(login="bot")),
        "repo_obj": SimpleNamespace(owner=SimpleNamespace(login="bot"), clone_url="unused"),
        "resolve_self_diagnosis_repo": lambda _config: "other/repo",
        "may_direct_push": lambda *args, **kwargs: (True, "test"),
        "base_branch": "integration", "final_verdict": "Approve", "token": "",
        "bump_repo_version": bump, "issue": SimpleNamespace(number=1, title="fix", body="body"),
        "_opened_at": lambda issue: None, "fixes": {"report.py": "value = 2"},
        "publication": {},
        "_authenticated_remote": lambda *args: RemoteContext(),
        "push_or_raise": lambda remote, branch, **kwargs: pushes.append(branch),
    }
    # Load the pure gate helper if present; never import the worker-starting module.
    helpers = [node for node in ast.parse(source).body
               if isinstance(node, ast.FunctionDef)
               and node.name in ("_publication_boundary_requirement", "_push_fix_with_evidence")]
    exec(compile(ast.Module(body=helpers, type_ignores=[]), "fix_engine.py", "exec"), namespace)
    snippet = "".join(line for line in source[start:end].splitlines(keepends=True)
                      if not line.lstrip().startswith("#"))
    exec(textwrap.dedent(snippet), namespace)
    assert "generated.txt" in repo.head.commit.stats.files
    if final_check == "clear":
        assert pushes == ["HEAD:integration"]
        assert namespace["can_actually_direct_push"] is True
    else:
        assert pushes == []
        assert namespace["can_direct_push"] is False
        assert namespace["boundary_forced_pr"] is True
        assert namespace["can_actually_direct_push"] is False


def _publish(confidence, can_direct_push, boundary_result, existing=False, checkout=None):
    """Execute the actual boundary gate and fallback publication with offline fakes."""
    source = (Path(__file__).resolve().parents[1] / "fix_engine.py").read_text()
    gate_start = source.index("            boundary_forced_pr, boundary_pr_reason = ")
    gate_end = source.index("            # Log the direct-push decision", gate_start)
    publish_start = source.index("                target_branch = ", gate_end)
    publish_end = source.index("                # Report the PR.", publish_start)
    pushed, created = [], []
    prior = SimpleNamespace(html_url="https://example.test/pr/previous")

    def diff(*args):
        """Return a staged path or simulate an unavailable diff."""
        if boundary_result == "error":
            raise RuntimeError("synthetic staged diff failure")
        return "auth.py" if boundary_result == "hit" else "report.py"

    def create_pull(**kwargs):
        """Capture a PR without contacting GitHub."""
        created.append(kwargs)
        return SimpleNamespace(html_url="https://example.test/pr/new")

    class RemoteContext:
        """Stand in for authenticated remote configuration without credentials."""
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    config = {
        "dev_branch": "integration",
        "feature_boundaries": [{"id": "auth", "paths": ["auth.py"], "enabled": True}],
    }
    namespace = {
        "can_direct_push": can_direct_push,
        "final_confidence": confidence, "confidence_threshold": 0.95,
        "config": config, "feature_boundary": feature_boundary,
        "logger": logging.getLogger(__name__), "repo_name": "owner/repo",
        "repo_git": SimpleNamespace(
            git=SimpleNamespace(diff=diff, checkout=checkout or (lambda *args: None)),
            head=SimpleNamespace(commit=SimpleNamespace(hexsha="synthetic-fix-sha")),
            remotes=SimpleNamespace(origin=object())),
        "repo_obj": SimpleNamespace(
            clone_url="https://example.test/repo", default_branch="production",
            create_pull=create_pull),
        "issue": SimpleNamespace(number=1), "token": "synthetic",
        "auto_branch_name": lambda *args, **kwargs: "bug/1-fix",
        "may_force_push": lambda *args, **kwargs: (False, "test"),
        "_authenticated_remote": lambda *args: RemoteContext(),
        "push_or_raise": lambda remote, branch, **kwargs: pushed.append(branch),
        "integration_branch": lambda *args: ("integration", None),
        "find_existing_pull_request": lambda *args: prior if existing else None,
        "_fix_pr_body": lambda *args: "test intent",
        "publication": {},
    }
    helpers = [node for node in ast.parse(source).body
               if isinstance(node, ast.FunctionDef)
               and node.name in ("_publication_boundary_requirement", "_push_fix_with_evidence")]
    exec(compile(ast.Module(body=helpers, type_ignores=[]), "fix_engine.py", "exec"), namespace)
    exec(textwrap.dedent(source[gate_start:gate_end]), namespace)
    exec(textwrap.dedent(source[publish_start:publish_end]), namespace)
    return namespace, pushed, created, prior


@pytest.mark.parametrize("confidence", [0.90, 0.9499, 0.95, 1.0])
@pytest.mark.parametrize("can_direct_push", [False, True])
@pytest.mark.parametrize("boundary_result", ["hit", "error"])
def test_boundary_or_failed_check_always_opens_topic_pr(
        confidence, can_direct_push, boundary_result):
    """Neither confidence nor direct-push eligibility bypasses a required human PR."""
    state, pushed, created, _ = _publish(confidence, can_direct_push, boundary_result)
    assert state["boundary_forced_pr"] is True
    assert state["can_direct_push"] is False
    assert pushed == ["bug/1-fix"]
    assert created and created[0]["head"] == "bug/1-fix"
    assert created[0]["base"] == "integration"
    assert state["pr"] is not None


def test_boundary_publication_reuses_existing_topic_pr():
    """Retrying a boundary-forced publication reuses its open PR."""
    state, pushed, created, prior = _publish(0.90, True, "hit", existing=True)
    assert pushed == ["bug/1-fix"]
    assert created == []
    assert state["pr"] is prior


def test_existing_topic_branch_receives_current_fix_commit(tmp_path):
    """Reusing a real local topic branch must not leave the new fix on integration."""
    repo = git.Repo.init(tmp_path, initial_branch="integration")
    with repo.config_writer() as config:
        config.set_value("user", "name", "Test")
        config.set_value("user", "email", "test@example.test")
    source = tmp_path / "source.py"
    source.write_text("value = 1\n")
    repo.index.add(["source.py"])
    repo.index.commit("base")
    repo.git.checkout("-b", "bug/1-fix")
    source.write_text("value = 2\n")
    repo.index.add(["source.py"])
    repo.index.commit("prior attempt")
    repo.git.checkout("integration")
    source.write_text("value = 3\n")
    repo.index.add(["source.py"])
    fixed_head = repo.index.commit("new reviewed fix").hexsha
    _, pushed, created, _ = _publish(
        0.90, True, "hit", existing=True, checkout=repo.git.checkout)
    assert pushed == ["bug/1-fix"] and not created
    assert repo.active_branch.name == "bug/1-fix"
    assert repo.head.commit.hexsha == fixed_head


@pytest.mark.parametrize("confidence,expected", [(0.90, "integration"), (0.95, "bug/1-fix")])
def test_unbounded_fallback_keeps_existing_confidence_behavior(confidence, expected):
    """An ordinary non-boundary fix preserves the intentional integration fallback."""
    state, pushed, created, _ = _publish(confidence, False, "miss")
    assert state["boundary_forced_pr"] is False
    assert pushed == [expected]
    assert bool(created) is (expected != "integration")
    assert (state["pr"] is None) is (expected == "integration")
