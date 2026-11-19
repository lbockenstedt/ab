"""A promotion PR is reviewed as a promotion, with its whole description.

WHY THIS EXISTS
ab#586, ab's dev -> qa promotion, was denied at 0.86. Panel 1's brief called
it a HUMAN-authored PR, so its reviewer read a PR into qa as hand-made (fleet
rule 2.1) and the VERSION advance as a breach of rule 2.2. Both briefs also
cut the description at 4,000 characters and the intent at 2,000, so of the
units the PR carried, the reviewers saw about one unit's intent. lm's
promote-reusable.yml now quotes every carried unit's intent (up to about
16,000 characters), and the briefs carry a promotion's description whole.
Only the exact branches promote.yml builds, in the PR's own repo and into
their route's target, get the promotion brief.
"""
import types

import pytest

import main  # noqa: F401  (pr_review imports main; import it first)
import pr_review
import pr_template

_REPO = "lbockenstedt/ab"


def _pr(head_ref, base_ref, body="", head_repo=_REPO, base_repo=_REPO):
    """A stand-in for a PyGithub pull request: only what the briefs read."""
    def repo(name):
        """A repo stand-in, or None as for a deleted fork."""
        return types.SimpleNamespace(full_name=name) if name else None
    return types.SimpleNamespace(
        title="promote: dev -> qa", body=body, get_commits=lambda: [],
        head=types.SimpleNamespace(ref=head_ref, repo=repo(head_repo)),
        base=types.SimpleNamespace(ref=base_ref, repo=repo(base_repo)))


def _briefs(pr):
    """Panel 1's and panel 2's briefs for ``pr``."""
    return pr_review.skeptical_issue_body(pr), pr_review.state_logic_issue_body(pr)


@pytest.mark.parametrize("head,base,source", [
    ("promote/dev-to-qa", "qa", "dev"),
    ("promote/qa-to-main", "main", "qa"),
    ("promote/dev-to-main", "main", "dev"),
])
def test_a_promotion_is_framed_as_one(head, base, source):
    """Both briefs say where the promotion comes from and goes; panel 1's says
    the VERSION advance is expected, and names the override only for it."""
    p1, p2 = _briefs(_pr(head, base))
    framing = "This is a PROMOTION pull request from `%s` into `%s`" % (source, base)
    assert framing in p1 and framing in p2
    assert "HUMAN-authored" not in p1 + p2
    assert "(fleet rule 2.1)" in p1 and "(fleet rule 2.2)" in p1
    assert ("dev -> main override" in p1) == (head == "promote/dev-to-main")
    # The rest of each brief is unchanged.
    assert "INTENT vs DIFF FIDELITY" in p1
    assert "STATE/STATUS COVERAGE" in p2


def test_a_promotion_carries_its_whole_description():
    """An intent far past the ordinary caps reaches the reviewers whole: in
    panel 1's description and its stated intent, and in panel 2's description.
    An ordinary PR with the same body keeps the ordinary caps."""
    body = pr_template.render(intent="x" * 30000 + " SENTINEL-END", solution="Promote.")
    p1, p2 = _briefs(_pr("promote/dev-to-qa", "qa", body))
    assert p1.count("SENTINEL-END") == 2
    assert p2.count("SENTINEL-END") == 1
    p1, p2 = _briefs(_pr("feature/x", "dev", body))
    assert "SENTINEL-END" not in p1 + p2


@pytest.mark.parametrize("pr", [
    _pr("promote/dev-to-qamalicious", "qa"),
    _pr("promote/qa-to-main-2", "main"),
    _pr("promote/dev-to-qa", "main"),
    _pr("promote/dev-to-qa", "qa", head_repo="someone/ab"),
    _pr("promote/dev-to-qa", "qa", head_repo=None),
    _pr("promote/dev-to-qa", "qa", head_repo=None, base_repo=None),
    _pr("feature/x", "dev"),
    types.SimpleNamespace(title="t", body="", get_commits=lambda: []),
], ids=["suffix", "numbered", "other-base", "fork", "deleted-fork", "no-repos",
        "feature", "no-head"])
def test_a_look_alike_keeps_the_ordinary_brief(pr):
    """A branch that only resembles a promotion is reviewed as a human PR:
    the briefs must not vouch for where it came from."""
    p1, p2 = _briefs(pr)
    assert "HUMAN-authored" in p1 and "HUMAN-authored" in p2
    assert "PROMOTION" not in p1 + p2
