"""A merge-only PR must not be reviewed as though it authored its diff.

WHY THIS EXISTS
kvm#33 was a `backmerge/main-to-qa` PR opened to clear a real
`CONFLICT (content): Merge conflict in .github/workflows/promote.yml` that had
frozen kvm's qa->main promotion. It was mergeable and correct. Both skeptical
reviewers rejected it three times, in as many words:

    "the diff still contains no merge, no conflict resolution ... it is still
     purely a coalescing-engine change ... rename/re-scope it"

That critique cannot be satisfied by any merge commit. GitHub renders a PR's
diff as base...head, so a back-merge of main into qa shows exactly main's
unique content -- which here was the coalescing engine AppBuilder had committed
onto promotion branches (and therefore onto main only). The reviewers read
INHERITED content as AUTHORED change and demanded a diff shape no merge has.

The cost was not just a stuck PR: remediation dutifully spent all three
attempts re-editing promote.sh/promote.yml -- the very files that were
conflicting -- deepening the qa/main drift the PR existed to close.

pr_review.py cannot be imported (circular import via github_ops -> main), so
the function under test is extracted with ast and exec'd against a stub
namespace, the same approach test_twin_parity.py uses.
"""
import ast
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(ROOT, "pr_review.py")).read()


def _extract(name, src=SRC):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in pr_review.py")


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _ctx():
    ns = {"logger": _NoLog()}
    exec(_extract("_merge_only_context"), ns)
    return ns["_merge_only_context"]


class _Commit:
    def __init__(self, nparents):
        self.parents = [object()] * nparents


class _Commits:
    """Stands in for PyGithub's PaginatedList, which supports slicing."""

    def __init__(self, commits):
        self._c = commits

    def __getitem__(self, k):
        return self._c[k]


class _PR:
    def __init__(self, commits=None, boom=False):
        self._commits = commits
        self.boom = boom

    def get_commits(self):
        if self.boom:
            raise RuntimeError("API is down")
        return _Commits(self._commits)


def test_all_merge_commits_gets_the_context_block():
    out = _ctx()(_PR([_Commit(2)]))
    assert "MERGE-ONLY PR" in out
    assert "1 commit(s)" in out


def test_the_block_names_the_exact_unsatisfiable_critique():
    """The reviewers' rejection wording is the thing being pre-empted, so the
    block has to address it head-on rather than vaguely describe merges."""
    out = _ctx()(_PR([_Commit(2), _Commit(3)]))
    low = out.lower()
    assert "no merge" in low and "conflict resolution" in low
    assert "re-scoped" in low or "renamed" in low
    assert "safe and correct to bring this content into the base" in low


def test_a_single_ordinary_commit_gets_no_block():
    assert _ctx()(_PR([_Commit(1)])) == ""


def test_one_authored_commit_among_merges_disqualifies_the_pr():
    """A promotion PR (promote/dev-to-qa) carries the feature commits
    themselves, so it authors real change and MUST keep the full
    intent-vs-diff critique. Only a PR that authors nothing is exempt."""
    assert _ctx()(_PR([_Commit(2), _Commit(1), _Commit(2)])) == ""


def test_no_commits_gets_no_block():
    """An empty list must not vacuously satisfy 'every commit is a merge'."""
    assert _ctx()(_PR([])) == ""


def test_an_api_failure_degrades_to_no_block():
    """This only ever ADDS context; it must never be able to fail a review."""
    assert _ctx()(_PR(boom=True)) == ""


# -- wiring ------------------------------------------------------------------

def test_the_block_is_actually_fed_to_the_broad_panel():
    """Extracted-and-tested but never called is the failure mode this pins."""
    body = _extract("_skeptical_review")
    assert "_merge_only_context(pr)" in body


def test_the_state_logic_panel_is_left_alone():
    """Panel 2 judges state coverage and reachability only -- it makes no
    intent-vs-diff critique, so it needs no exemption and must not be given
    one."""
    assert "_merge_only_context" not in _extract("_state_logic_review")
