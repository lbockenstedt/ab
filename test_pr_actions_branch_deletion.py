"""Regression test: merging a PR must never delete a shared branch.

AppBuilder deleted the live `dev` branch of lbockenstedt/ab. The chain was:

  1. fix_engine opens a low-confidence fix PR using `dev` itself as the HEAD
     branch (`target_branch = config.get("dev_branch", "dev")`).
  2. merge_pr merges it and calls _delete_pr_branch on the head.
  3. _delete_pr_branch's only guard was `ref == repo.default_branch`, which is
     `main` -- so `dev` was deleted.

pr_actions imports main/app_state at module scope, and importing it boots the
live app (see test_pr_actions_conflict_resolve.py), so _delete_pr_branch is
extracted by source with ast and run against stubs -- no network, no GitHub.
"""
import ast

import pytest

from branch_policy import may_delete


class _Ref:
    def __init__(self, holder, name):
        self.holder, self.name = holder, name

    def delete(self):
        self.holder.deleted.append(self.name)


class _Repo:
    def __init__(self, full_name="lbockenstedt/ab", default_branch="main"):
        self.full_name, self.default_branch = full_name, default_branch
        self.deleted = []

    def get_git_ref(self, ref):
        return _Ref(self, ref)


class _Head:
    def __init__(self, ref, repo):
        self.ref, self.repo = ref, repo


class _PR:
    def __init__(self, ref, repo):
        self.head = _Head(ref, repo)


def _load_delete_pr_branch(config):
    """Extract _delete_pr_branch from source, bound to stub collaborators."""
    tree = ast.parse(open("pr_actions.py").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_delete_pr_branch")

    logged = []

    class _Logger:
        def info(self, m): logged.append(m)
        def warning(self, m): logged.append(m)

    ns = {
        "logger": _Logger(),
        "may_delete": may_delete,
        "load_config": lambda: config,
        "GithubException": Exception,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<pr_actions>", "exec"), ns)
    return ns["_delete_pr_branch"], logged


@pytest.mark.parametrize("ref", ["dev", "qa", "main"])
def test_merging_a_pr_from_a_shared_branch_does_not_delete_it(ref):
    repo = _Repo()
    delete_pr_branch, logged = _load_delete_pr_branch({"dev_branch": "dev"})
    delete_pr_branch(repo, _PR(ref, repo))
    assert repo.deleted == [], f"{ref} was deleted"
    assert any("kept branch" in m for m in logged), "refusal must be logged"


@pytest.mark.parametrize("ref", ["ai-fix-issue-25", "ai-feature-issue-7"])
def test_appbuilder_branches_are_still_cleaned_up(ref):
    repo = _Repo()
    delete_pr_branch, _ = _load_delete_pr_branch({})
    delete_pr_branch(repo, _PR(ref, repo))
    assert repo.deleted == ["heads/%s" % ref]


def test_unrecognised_branch_is_kept():
    repo = _Repo()
    delete_pr_branch, _ = _load_delete_pr_branch({})
    delete_pr_branch(repo, _PR("feature/new-ui", repo))
    assert repo.deleted == []


def test_fork_head_is_never_touched():
    repo = _Repo()
    fork = _Repo(full_name="someone-else/ab")
    delete_pr_branch, _ = _load_delete_pr_branch({})
    delete_pr_branch(repo, _PR("ai-fix-issue-25", fork))
    assert repo.deleted == [] and fork.deleted == []


def test_cleanup_disabled_by_config_keeps_everything():
    repo = _Repo()
    delete_pr_branch, _ = _load_delete_pr_branch({"delete_merged_branches": False})
    delete_pr_branch(repo, _PR("ai-fix-issue-25", repo))
    assert repo.deleted == []


def test_delete_failure_is_swallowed_not_raised():
    """Cleanup must never turn a successful merge into an error."""
    class _Boom(_Repo):
        def get_git_ref(self, ref):
            raise Exception("410 gone")

    repo = _Boom()
    delete_pr_branch, logged = _load_delete_pr_branch({})
    delete_pr_branch(repo, _PR("ai-fix-issue-25", repo))
    assert any("could not delete" in m for m in logged)
