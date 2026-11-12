"""Tests for pr_review.scan_promotion_prs, the promotion-PR fast review lane.

Promotion PRs used to wait for the hourly scan_open_prs before AB reviewed (and
could auto-merge) them. The fast lane reviews only promotion PRs whose current
head has not been reviewed yet, through the same _review_one.
"""
import main  # noqa: F401  (pr_review imports main; import it first to break the cycle)
import pr_review
import pytest


class _Ref:
    """A PR head/base: a branch name and a commit SHA."""

    def __init__(self, ref, sha="sha-new"):
        """Store the branch name and SHA."""
        self.ref = ref
        self.sha = sha


class _PR:
    """The few PyGithub PullRequest attributes the fast lane reads."""

    def __init__(self, number, head_ref, sha="sha-new", draft=False):
        """Build a fake open PR."""
        self.number = number
        self.head = _Ref(head_ref, sha)
        self.draft = draft


class _Repo:
    """A fake repo that lists its open PRs."""

    full_name = "owner/app"

    def __init__(self, prs):
        """Hold the open PRs."""
        self._prs = prs

    def get_pulls(self, state):
        """Return the open PRs."""
        return list(self._prs)


class _GH:
    """A fake Github client with one repo."""

    def __init__(self, repo):
        """Hold the repo."""
        self._repo = repo

    def get_repo(self, name):
        """Return the one repo."""
        return self._repo


@pytest.fixture
def lane(monkeypatch):
    """Stub the fast lane's collaborators and record which PRs get reviewed."""
    reviewed = []
    monkeypatch.setattr(pr_review, "get_monitored_repos", lambda cfg: ["owner/app"])
    monkeypatch.setattr(pr_review, "_review_one",
                        lambda gh, repo, pr, cfg: reviewed.append(pr.number))
    import gh_actions_health
    monkeypatch.setattr(gh_actions_health, "outage", lambda cfg=None: (False, ""))
    monkeypatch.setitem(pr_review.state, "pr_reviews", {})
    monkeypatch.setitem(pr_review.state, "paused", False)
    monkeypatch.setitem(pr_review.state, "blackout", False)
    return reviewed


CONFIG = {"pr_review_enabled": True, "pr_review_fast_interval_s": 300}


def test_unreviewed_promotion_head_is_reviewed(lane):
    """A promotion PR whose head has no review yet goes through _review_one."""
    gh = _GH(_Repo([_PR(7, "promote/dev-to-qa"), _PR(8, "promote/qa-to-main-2")]))
    assert pr_review.scan_promotion_prs(gh, CONFIG) == 2
    assert lane == [7, 8]


def test_already_reviewed_head_is_skipped(lane):
    """A promotion PR whose current head was already reviewed costs nothing."""
    pr_review.state["pr_reviews"]["owner/app#7"] = {"head": "sha-new"}
    pr_review.state["pr_reviews"]["owner/app#8"] = {"head": "sha-old"}
    gh = _GH(_Repo([_PR(7, "promote/dev-to-qa"), _PR(8, "promote/dev-to-qa")]))
    assert pr_review.scan_promotion_prs(gh, CONFIG) == 1
    assert lane == [8]


def test_non_promotion_pr_is_ignored(lane):
    """Feature, fix and back-merge PRs are left to the hourly scan."""
    gh = _GH(_Repo([_PR(1, "feature/x"), _PR(2, "backmerge/main-to-qa"), _PR(3, "dev")]))
    assert pr_review.scan_promotion_prs(gh, CONFIG) == 0
    assert lane == []


def test_interval_zero_disables_the_lane(lane):
    """pr_review_fast_interval_s = 0 turns the fast lane off."""
    gh = _GH(_Repo([_PR(7, "promote/dev-to-qa")]))
    assert pr_review.scan_promotion_prs(gh, dict(CONFIG, pr_review_fast_interval_s=0)) == 0
    assert lane == []


def test_skips_while_the_hourly_scan_holds_the_lock(lane):
    """The fast lane never reviews alongside a running scan_open_prs."""
    gh = _GH(_Repo([_PR(7, "promote/dev-to-qa")]))
    with pr_review._scan_lock:
        assert pr_review.scan_promotion_prs(gh, CONFIG) == 0
    assert lane == []
