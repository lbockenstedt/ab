"""Tests for branch_policy — which branches AppBuilder may delete or force-push.

These encode real damage. AppBuilder deleted the `dev` branch of a live repo
and, separately, force-pushed `dev` and discarded a merged commit. Both came
from the same gap: the only guard was `ref == repo.default_branch`, which
protects `main` and nothing else, while a low-confidence fix opens its PR with
`dev` as the HEAD branch.
"""
import pytest

from branch_policy import (
    DEFAULT_AUTO_PREFIXES,
    DEFAULT_PROTECTED,
    auto_branch_prefixes,
    is_auto_created,
    is_protected,
    may_delete,
    may_force_push,
    protected_branches,
)


# ── the branches that were actually destroyed ───────────────────────────────

@pytest.mark.parametrize("ref", ["dev", "qa", "main", "master", "staging", "release", "next"])
def test_shared_branches_are_never_deleted(ref):
    ok, why = may_delete(ref, {})
    assert not ok, f"{ref} must never be deleted"
    assert "protected" in why


@pytest.mark.parametrize("ref", ["dev", "qa", "main"])
def test_shared_branches_are_never_force_pushed(ref):
    ok, why = may_force_push(ref, {})
    assert not ok, f"{ref} must never be force-pushed"
    assert "protected" in why


def test_regression_low_confidence_fix_pr_from_dev_does_not_delete_dev():
    """fix_engine opens the PR with head=dev when confidence is low. Merging it
    must clean up nothing at all."""
    ok, _ = may_delete("dev", {"dev_branch": "dev", "default_branch": "main"})
    assert not ok


# ── the branches cleanup was actually for ───────────────────────────────────

@pytest.mark.parametrize("ref", ["ai-fix-issue-25", "ai-fix-issue-1234", "ai-feature-issue-7"])
def test_appbuilder_branches_are_deletable(ref):
    ok, why = may_delete(ref, {})
    assert ok, why
    assert is_auto_created(ref)


@pytest.mark.parametrize("ref", ["ai-fix-issue-25", "ai-feature-issue-7"])
def test_appbuilder_branches_may_be_force_pushed(ref):
    ok, _ = may_force_push(ref, {})
    assert ok


# ── allowlist, not blocklist: anything unrecognised is kept ─────────────────

@pytest.mark.parametrize("ref", [
    "feature/new-ui", "hotfix", "promote/dev-to-qa", "promote/qa-to-main",
    "copilot/fix-thing", "lrb-scratch", "v2.00", "",
])
def test_unrecognised_branches_are_kept(ref):
    ok, why = may_delete(ref, {})
    assert not ok, f"{ref!r} is not an AppBuilder branch and must be kept"
    assert why


def test_promotion_branches_survive():
    """promote/* branches are created by the promotion workflow and reused via
    force-push on every promotion; deleting them is not AppBuilder's business."""
    for ref in ("promote/dev-to-qa", "promote/qa-to-main"):
        assert not may_delete(ref, {})[0]


# ── configuration ───────────────────────────────────────────────────────────

def test_extra_protected_branches_from_config():
    cfg = {"protected_branches": ["integration", "customer-demo"]}
    assert not may_delete("integration", cfg)[0]
    assert not may_force_push("customer-demo", cfg)[0]


def test_protected_branches_accepts_a_comma_separated_string():
    cfg = {"protected_branches": "integration, customer-demo"}
    assert not may_delete("integration", cfg)[0]
    assert not may_delete("customer-demo", cfg)[0]


def test_renaming_dev_branch_in_config_protects_the_new_name():
    cfg = {"dev_branch": "development", "default_branch": "trunk"}
    assert not may_delete("development", cfg)[0]
    assert not may_delete("trunk", cfg)[0]


def test_repo_default_branch_is_protected_even_if_unusual():
    assert not may_delete("prod", {}, repo_default_branch="prod")[0]
    assert not may_force_push("prod", {}, repo_default_branch="prod")[0]


def test_cleanup_can_be_disabled_entirely():
    cfg = {"delete_merged_branches": False}
    ok, why = may_delete("ai-fix-issue-9", cfg)
    assert not ok
    assert "disabled" in why


def test_cleanup_is_enabled_by_default():
    assert may_delete("ai-fix-issue-9", {})[0]


def test_custom_auto_prefixes_replace_the_defaults():
    cfg = {"auto_branch_prefixes": ["bot/"]}
    assert may_delete("bot/thing", cfg)[0]
    assert not may_delete("ai-fix-issue-9", cfg)[0], "defaults must not leak in"
    assert auto_branch_prefixes(cfg) == ("bot/",)


def test_a_protected_name_is_never_deletable_even_if_it_matches_a_prefix():
    """Protection wins over the auto-created allowlist, in either order."""
    cfg = {"auto_branch_prefixes": ["dev"], "protected_branches": ["dev"]}
    assert not may_delete("dev", cfg)[0]


# ── matching details ────────────────────────────────────────────────────────

def test_branch_matching_is_case_insensitive_and_trims_whitespace():
    assert is_protected("DEV", {})
    assert is_protected("  Qa  ", {})
    assert not may_delete(" MAIN ", {})[0]


def test_defaults_cover_the_promotion_flow():
    for b in ("main", "dev", "qa"):
        assert b in DEFAULT_PROTECTED
    assert "ai-fix-issue-" in DEFAULT_AUTO_PREFIXES
    assert "ai-feature-issue-" in DEFAULT_AUTO_PREFIXES


def test_protected_branches_returns_lowercased_set():
    got = protected_branches({"protected_branches": ["Foo"]})
    assert "foo" in got and "dev" in got


def test_none_and_empty_inputs_are_refused_not_crashed():
    for bad in (None, "", "   "):
        assert not may_delete(bad, {})[0]
        assert not may_force_push(bad, {})[0]
    assert not is_auto_created(None)
    assert not is_protected(None)
