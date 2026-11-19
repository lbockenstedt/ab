"""Configured boundaries must force a topic PR on every publication fallback."""
import logging
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import git

import feature_boundary


def _publish(confidence, can_direct_push, boundary_result, existing=False, checkout=None):
    """Execute the actual boundary gate and fallback publication with offline fakes."""
    source = (Path(__file__).resolve().parents[1] / "fix_engine.py").read_text()
    gate_start = source.index("            boundary_forced_pr = False")
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
    }
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
