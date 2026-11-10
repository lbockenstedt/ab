"""Tests for branch_policy — which branches AppBuilder may delete or force-push.

These encode real damage. AppBuilder deleted the `dev` branch of a live repo
and, separately, force-pushed `dev` and discarded a merged commit. Both came
from the same gap: the only guard was `ref == repo.default_branch`, which
protects `main` and nothing else, while a low-confidence fix opens its PR with
`dev` as the HEAD branch.
"""
import pytest

from branch_policy import (
    AUTO_BRANCH_PREFIXES_BY_KIND,
    DEFAULT_AUTO_PREFIXES,
    DEFAULT_PROTECTED,
    auto_branch_name,
    auto_branch_prefixes,
    is_auto_created,
    is_protected,
    is_release_locked,
    may_delete,
    may_force_push,
    protected_branches,
    release_locked_branches,
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

@pytest.mark.parametrize("ref", ["bug/25-null-pointer", "bug/1234-typo", "ai-feature/7-new-dashboard"])
def test_appbuilder_branches_are_deletable(ref):
    ok, why = may_delete(ref, {})
    assert ok, why
    assert is_auto_created(ref)


@pytest.mark.parametrize("ref", ["bug/25-null-pointer", "ai-feature/7-new-dashboard"])
def test_appbuilder_branches_may_be_force_pushed(ref):
    ok, _ = may_force_push(ref, {})
    assert ok


# ── allowlist, not blocklist: anything unrecognised is kept ─────────────────

@pytest.mark.parametrize("ref", [
    "feature/new-ui", "hotfix", "promote/dev-to-qa", "promote/qa-to-main",
    "copilot/fix-thing", "lrb-scratch", "v2.00", "", "fix/some-bug",
])
def test_unrecognised_branches_are_kept(ref):
    ok, why = may_delete(ref, {})
    assert not ok, f"{ref!r} is not an AppBuilder branch and must be kept"
    assert why


# ── THE reason bug/ and ai-feature/ were chosen over plain feature/ ─────────

@pytest.mark.parametrize("ref", [
    "feature/agentic-llm-router", "feature/router-capable-model", "feature/x",
])
def test_human_feature_branches_are_never_touched(ref):
    """37+ existing human branches use plain feature/ in this repo. If
    AppBuilder's own prefix were ever changed back to "feature/", every one
    of these becomes deletable — this is the exact class of bug
    branch_policy.py exists to prevent. Pinned here so a future change can't
    reintroduce it silently.

    NOTE: may_force_push is deliberately NOT asserted here — unlike
    may_delete, it only checks is_protected, not is_auto_created (a
    pre-existing, separate design gap: it's a blocklist, not an allowlist,
    for this one function). Not exploitable via either of its two real call
    sites today since both only ever pass a protected name or a freshly
    auto_branch_name()-constructed one — but worth knowing this function
    alone would currently say True for an arbitrary unprotected human branch."""
    assert not is_auto_created(ref)
    assert not may_delete(ref, {})[0]


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
    ok, why = may_delete("bug/9-thing", cfg)
    assert not ok
    assert "disabled" in why


def test_cleanup_is_enabled_by_default():
    assert may_delete("bug/9-thing", {})[0]


def test_custom_auto_prefixes_replace_the_defaults():
    cfg = {"auto_branch_prefixes": ["bot/"]}
    assert may_delete("bot/thing", cfg)[0]
    assert not may_delete("bug/9-thing", cfg)[0], "defaults must not leak in"
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
    assert "bug/" in DEFAULT_AUTO_PREFIXES
    assert "ai-feature/" in DEFAULT_AUTO_PREFIXES
    assert "feature/" not in DEFAULT_AUTO_PREFIXES, \
        "feature/ is the human convention; it must never become auto-deletable"


def test_protected_branches_returns_lowercased_set():
    got = protected_branches({"protected_branches": ["Foo"]})
    assert "foo" in got and "dev" in got


def test_none_and_empty_inputs_are_refused_not_crashed():
    for bad in (None, "", "   "):
        assert not may_delete(bad, {})[0]
        assert not may_force_push(bad, {})[0]
    assert not is_auto_created(None)
    assert not is_protected(None)


# ── auto_branch_name — construction, not just recognition ───────────────────

class _FakeIssue:
    def __init__(self, number, title):
        self.number = number
        self.title = title


def test_auto_branch_name_bug_with_issue():
    name = auto_branch_name("bug", issue=_FakeIssue(123, "Null pointer in the parser!"))
    assert name == "bug/123-null-pointer-in-the-parser"
    assert is_auto_created(name)


def test_auto_branch_name_feature_with_issue():
    name = auto_branch_name("feature", issue=_FakeIssue(7, "New dashboard"))
    assert name == "ai-feature/7-new-dashboard"
    assert is_auto_created(name)


def test_auto_branch_name_without_an_issue_number_omits_it():
    """The user's own spec: include the issue number IF one exists. A
    chat-triggered fix with no filed issue yet still needs a valid name."""
    name = auto_branch_name("bug", description="stale cache on the settings page")
    assert name == "bug/stale-cache-on-the-settings-page"
    assert "None" not in name
    assert is_auto_created(name)


def test_auto_branch_name_issue_number_zero_is_falsy_but_valid():
    """GitHub issue numbers are never 0, but guard the falsy-vs-missing
    distinction explicitly rather than relying on that being true forever."""
    name = auto_branch_name("bug", issue=_FakeIssue(0, "edge case"))
    assert name == "bug/edge-case"  # 0 treated as "no number", not "0-edge-case"


def test_auto_branch_name_slug_strips_unsafe_characters():
    name = auto_branch_name("bug", issue=_FakeIssue(1, "Fix: crash on `~^:?*[` chars!!!"))
    assert name == "bug/1-fix-crash-on-chars"


def test_auto_branch_name_empty_title_falls_back_not_crashes():
    name = auto_branch_name("bug", issue=_FakeIssue(5, ""))
    assert name == "bug/5-untitled"


def test_auto_branch_name_unknown_kind_raises_rather_than_silently_unprefixed():
    with pytest.raises(KeyError):
        auto_branch_name("chore", description="something")


def test_auto_branch_name_prefixes_match_the_recognition_table():
    """The construction side and the recognition side must be the same
    source, not two literals that happen to agree today."""
    for kind, prefix in AUTO_BRANCH_PREFIXES_BY_KIND.items():
        name = auto_branch_name(kind, description="x")
        assert name.startswith(prefix)
        assert is_auto_created(name)


# ── release lock: hold auto-merge into a branch without blocking review ──────

def test_no_release_lock_by_default():
    assert release_locked_branches({}) == set()
    assert release_locked_branches(None) == set()
    assert not is_release_locked("dev", {})
    assert not is_release_locked("qa", {})


@pytest.mark.parametrize("ref", ["dev", "qa"])
def test_locked_branches_report_locked(ref):
    cfg = {"release_locked_branches": ["dev", "qa"]}
    assert is_release_locked(ref, cfg)
    assert release_locked_branches(cfg) == {"dev", "qa"}


def test_release_lock_is_case_insensitive_and_string_parsed():
    # Settings supplies a comma-separated string; matching ignores case/space.
    cfg = {"release_locked_branches": "Dev, QA"}
    assert is_release_locked("dev", cfg)
    assert is_release_locked("qa", cfg)
    assert not is_release_locked("main", cfg)


def test_unlisted_branch_is_not_locked():
    cfg = {"release_locked_branches": ["dev"]}
    assert is_release_locked("dev", cfg)
    assert not is_release_locked("qa", cfg)


def test_release_lock_does_not_affect_delete_or_protection():
    # The lock gates auto-merge only — it must not change cleanup/force-push
    # policy. A locked non-auto branch is still just "not AppBuilder's".
    cfg = {"release_locked_branches": ["feature/x"]}
    ok, _ = may_delete("feature/x", cfg)
    assert not ok  # unchanged: not an auto-created branch, still undeletable
