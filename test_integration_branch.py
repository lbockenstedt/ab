"""AppBuilder must never aim its own work at the production branch.

Changes reach production one way: dev -> qa -> main, one deliberate promotion
at a time, driven by the repo owner. AppBuilder used to take
``config["default_branch"]`` -- i.e. ``main`` -- as its base, which meant a
fix either opened a PR straight into main (skipping qa) or, on a trusted repo
with an approving review, was pushed to main directly with no PR at all.

Two layers are tested here: what ``integration_branch`` returns, and a
source-level guard that the call sites still use it. The second matters
because the failure mode is silent -- re-introducing ``default_branch`` as a
base reads like a harmless line and produces PRs that look fine right up until
they land in main.
"""

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from branch_policy import integration_branch  # noqa: E402


class _Repo:
    """A repo where every branch asked for exists."""
    full_name = "lbockenstedt/example"
    default_branch = "main"

    def get_branch(self, name):
        return object()


class _RepoWithoutDev(_Repo):
    def get_branch(self, name):
        raise Exception("404 Branch not found")


def test_defaults_to_dev():
    branch, why = integration_branch({})
    assert branch == "dev"
    assert why


def test_honours_a_renamed_dev_branch():
    assert integration_branch({"dev_branch": "develop"})[0] == "develop"


def test_default_branch_is_ignored_as_a_base():
    """The whole bug: default_branch is main, and main is not a base."""
    cfg = {"default_branch": "main", "dev_branch": "dev"}
    assert integration_branch(cfg, _Repo())[0] == "dev"


def test_never_returns_main_while_dev_exists():
    for cfg in ({}, {"default_branch": "main"}, {"default_branch": "master"},
                {"default_branch": "main", "dev_branch": "dev"}):
        assert integration_branch(cfg, _Repo())[0] != cfg.get("default_branch", "main")


def test_blank_dev_branch_config_still_yields_dev():
    """An empty string in config must not collapse the branch name to ''."""
    for blank in ("", "   ", None):
        assert integration_branch({"dev_branch": blank})[0] == "dev"


def test_missing_dev_branch_falls_back_and_says_so():
    branch, why = integration_branch({"default_branch": "main"}, _RepoWithoutDev())
    assert branch == "main", "a PR into a non-existent branch would just fail"
    assert "does not exist" in why and "create" in why, (
        "the fallback must explain itself -- it is a silent policy bypass otherwise")


# ── source guard: the call sites must keep using it ────────────────────────

def _assignments_to(path, target):
    """Every source line assigning to `target` in the file."""
    src = open(os.path.join(HERE, path)).read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            names = []
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
                elif isinstance(t, ast.Tuple):
                    names += [e.id for e in t.elts if isinstance(e, ast.Name)]
            if target in names:
                out.append(ast.get_source_segment(src, node))
    return out


def test_call_sites_derive_their_base_from_the_policy():
    """base_branch must come from integration_branch, never from default_branch."""
    for path in ("fix_engine.py", "feature_build.py"):
        assigns = _assignments_to(path, "base_branch")
        assert assigns, f"no base_branch assignment found in {path}"
        for a in assigns:
            assert "integration_branch" in a, (
                f"{path} assigns base_branch without the policy: {a!r}")
            assert "default_branch" not in a, (
                f"{path} reintroduced default_branch as a base: {a!r} -- "
                "that sends AppBuilder's work straight into main")
