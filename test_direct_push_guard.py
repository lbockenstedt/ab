#!/usr/bin/env python3
"""Self-tests for the direct-push guard: AppBuilder may commit straight to the
integration branch (dev), but never to production/promotion branches.

Run:  python3 -m pytest -q test_direct_push_guard.py

WHY THIS EXISTS
AppBuilder authenticates as the repo OWNER's personal access token, which
bypasses every GitHub ruleset and branch protection. If fix_engine's direct
push is ever aimed at main, GitHub will accept it -- silently putting
unreviewed, un-QA'd automated changes into production. Branch protection
cannot be the enforcement here, so the enforcement is
branch_policy.may_direct_push plus its wiring in fix_engine.

Committing directly to main stays possible ONLY from a CLI session using the
owner's own token by hand. Nothing in the WebUI or in AppBuilder's automation
may do it.

The distinction being pinned:
  ALLOWED   dev (the integration branch) and AppBuilder's own topic branches
  REFUSED   main/master, the repo default branch, and the promotion gates
            (qa/staging/release/next)
`is_protected` is deliberately NOT used wholesale -- it covers dev too, and
refusing dev would break the ordinary trusted-repo flow this guard is meant to
leave alone.
"""
import ast
import os

import pytest

from branch_policy import is_protected, may_direct_push

ROOT = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# Allowed
# --------------------------------------------------------------------------
def test_dev_is_allowed():
    ok, why = may_direct_push("dev", {}, repo_default_branch="main")
    assert ok is True
    assert "integration branch" in why


def test_configured_dev_branch_is_allowed():
    ok, why = may_direct_push("develop", {"dev_branch": "develop"}, repo_default_branch="main")
    assert ok is True
    assert "integration branch" in why


def test_dev_is_refused_when_the_integration_branch_was_renamed():
    """If dev_branch is 'develop', plain 'dev' is just another protected
    branch -- the exemption follows the configured integration branch, it is
    not a hardcoded blessing of the literal name 'dev'."""
    ok, _ = may_direct_push("dev", {"dev_branch": "develop"}, repo_default_branch="main")
    assert ok is False


@pytest.mark.parametrize("ref", ["bug/123-thing", "ai-feature/45-thing", "topic/whatever"])
def test_appbuilder_own_branches_are_allowed(ref):
    ok, _ = may_direct_push(ref, {}, repo_default_branch="main")
    assert ok is True


# --------------------------------------------------------------------------
# Refused
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ref", ["main", "master", "qa", "staging", "release", "next"])
def test_protected_branches_are_refused(ref):
    ok, why = may_direct_push(ref, {}, repo_default_branch="main")
    assert ok is False, f"{ref} must not accept a direct push"
    assert "promotion PR" in why or "protected" in why
    assert why, "a refusal must always carry a loggable reason"


def test_repo_default_branch_is_refused_even_when_unusually_named():
    ok, why = may_direct_push("trunk", {}, repo_default_branch="trunk")
    assert ok is False
    assert "trunk" in why


def test_configured_default_branch_is_refused():
    ok, _ = may_direct_push("production", {"default_branch": "production"},
                            repo_default_branch="main")
    assert ok is False


def test_extra_configured_protected_branches_are_refused():
    ok, _ = may_direct_push("hotfix", {"protected_branches": "hotfix"},
                            repo_default_branch="main")
    assert ok is False


def test_branch_name_matching_is_case_insensitive():
    """'MAIN' must not slip past a lowercase-only comparison."""
    for ref in ("MAIN", "Main", "mAiN"):
        ok, _ = may_direct_push(ref, {}, repo_default_branch="main")
        assert ok is False, f"{ref} must be refused"


def test_whitespace_is_stripped_before_matching():
    ok, _ = may_direct_push("  main  ", {}, repo_default_branch="main")
    assert ok is False


def test_empty_ref_is_refused():
    ok, why = may_direct_push("", {}, repo_default_branch="main")
    assert ok is False
    assert why == "no branch name"


def test_returns_the_ok_reason_tuple_convention():
    """Same (ok, reason) shape as may_delete / may_force_push."""
    for ref in ("dev", "main", ""):
        result = may_direct_push(ref, {}, repo_default_branch="main")
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], bool) and isinstance(result[1], str) and result[1]


def test_guard_is_narrower_than_is_protected():
    """The whole point of a separate helper: dev is protected against
    force-push/delete, yet is a legal direct-push target."""
    assert is_protected("dev", {}, "main") is True
    assert may_direct_push("dev", {}, "main")[0] is True


# --------------------------------------------------------------------------
# fix_engine wiring — the guard must actually be applied before the push
# --------------------------------------------------------------------------
def _fix_engine_src():
    return open(os.path.join(ROOT, "fix_engine.py")).read()


def test_fix_engine_imports_and_calls_the_guard():
    src = _fix_engine_src()
    assert "may_direct_push" in src, "fix_engine must use the direct-push guard"
    tree = ast.parse(src)
    imported = any(
        isinstance(n, ast.ImportFrom) and n.module == "branch_policy"
        and any(a.name == "may_direct_push" for a in n.names)
        for n in ast.walk(tree))
    assert imported, "may_direct_push must be imported from branch_policy"


def test_guard_runs_before_the_direct_push_and_disables_it():
    """A refusal must clear can_direct_push so control falls through to the
    existing pull-request path, rather than merely logging and pushing anyway."""
    src = _fix_engine_src()
    guard_at = src.index("may_direct_push(")
    push_at = src.index('push(f"HEAD:{base_branch}")')
    assert guard_at < push_at, "the guard must be evaluated before the push"

    window = src[guard_at:push_at]
    assert "can_direct_push = False" in window, (
        "a refused direct push must disable direct-push mode so the PR path runs")


def test_refusal_reason_is_logged_and_reaches_the_pr_decision():
    src = _fix_engine_src()
    assert "direct_push_refused_reason" in src
    assert "Direct push REFUSED" in src, "a refusal must be logged so it is never silent"
    # The reason must feed the PR-path decision, not be dropped on the floor.
    assert src.count("direct_push_refused_reason") >= 3
