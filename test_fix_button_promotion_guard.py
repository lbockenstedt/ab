"""The UI "Fix" button must refuse promotion / back-merge PRs.

WHY THIS EXISTS
This is the root cause of the fleet's recurring promotion breakage.

A promotion branch must carry EXACTLY what its source branch has.
pr_remediate.auto_remediate_pr enforces that (_PROMOTION_HEAD_RE) and says so
in as many words: pushing a remediation commit onto one "injects code into
qa/main that was never on dev".

fix_one_pr -- the UI's per-PR Fix button -- had no such guard. It clones the
PR's own head branch and pushes "AppBuilder: fix PR #N review findings" onto
it, whatever that branch is. So the engine's own invariant was one click away
from being broken by hand.

And that click is precisely the one an operator makes under pressure: when
promotions stack up, the stuck PRs ARE the promotion PRs. Each manual Fix
commits code onto e.g. promote/qa-to-main, which merges into main and nowhere
else. qa never receives it, so qa and main drift in exactly the files
AppBuilder keeps repairing -- promote.yml and promote.sh -- and the NEXT
qa->main promotion dies on a content conflict in them.

Confirmed live in tsa: commit e8cc55a "AppBuilder: fix PR #15 review findings"
reached main through promote/qa-to-main, and promotion runs afterwards failed
with "CONFLICT (content): Merge conflict in .github/workflows/promote.yml".
The same shape appears in kvm and pxmx. Every manual intervention deepened the
jam, which is why the problem never stayed fixed.

pr_review.py cannot be imported (circular import via github_ops -> main), so
these are source-level assertions over the extracted function.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(ROOT, "pr_review.py")).read()
REM = open(os.path.join(ROOT, "pr_remediate.py")).read()


def _extract(name, src=SRC):
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError("%s not found" % name)


FIX_ONE_PR = _extract("fix_one_pr")


def _promotion_re():
    """Use the REAL regex from pr_remediate, so this test tracks the shipped
    pattern instead of a copy that could drift away from it."""
    for node in ast.parse(REM).body:
        if isinstance(node, ast.Assign) and getattr(
                node.targets[0], "id", "") == "_PROMOTION_HEAD_RE":
            ns = {"re": re}
            exec(ast.get_source_segment(REM, node), ns)
            return ns["_PROMOTION_HEAD_RE"]
    raise AssertionError("_PROMOTION_HEAD_RE not found in pr_remediate.py")


# -- the guard is present and uses the shared pattern ------------------------

def test_fix_one_pr_refuses_promotion_branches():
    assert "_PROMOTION_HEAD_RE" in FIX_ONE_PR, (
        "the UI Fix button can still push commits onto a promotion branch")


def test_the_guard_reuses_pr_remediates_pattern_not_a_copy():
    """Two independently maintained definitions of 'is this a promotion
    branch' is how the automated path and the manual path drifted apart in the
    first place."""
    assert "pr_remediate._PROMOTION_HEAD_RE" in FIX_ONE_PR
    assert "re.compile" not in FIX_ONE_PR


def test_pr_remediate_is_imported_in_the_function():
    """pr_remediate imports pr_review, so a module-level import would be a
    circular-import crash at load time rather than a guard."""
    assert "import pr_remediate" in FIX_ONE_PR


# -- it fires before anything is generated or pushed -------------------------

def test_guard_precedes_the_push():
    """Refusing after a fix was generated would still burn a model budget, and
    refusing after the push would not refuse at all."""
    assert FIX_ONE_PR.index("_PROMOTION_HEAD_RE") < FIX_ONE_PR.index("origin.push")


def test_guard_precedes_fix_generation():
    assert FIX_ONE_PR.index("_PROMOTION_HEAD_RE") < FIX_ONE_PR.index("apply_ai_fix(")  # the call site, not the import


# -- it honours the same kill switch as the automated path -------------------

def test_guard_shares_the_automated_paths_config_flag():
    assert "pr_auto_remediate_skip_promotion" in FIX_ONE_PR


# -- the pattern actually covers the branches that caused the outage ---------

def test_the_branches_that_broke_the_fleet_are_matched():
    rx = _promotion_re()
    for ref in ("promote/qa-to-main", "promote/dev-to-qa", "promote/dev-to-main",
                "backmerge/main-to-qa", "backmerge/main-to-dev"):
        assert rx.match(ref), "%s would still be rewritten by hand" % ref


def test_ordinary_branches_are_still_fixable():
    """The Fix button must keep working for the PRs it exists for."""
    rx = _promotion_re()
    for ref in ("bug/1234-thing", "ai-feature/new-panel", "dev", "main",
                "feat/dhcpv6-twin-sync"):
        assert not rx.match(ref), "%s must remain fixable from the UI" % ref


# -- the refusal has to tell the operator what to do instead -----------------

def test_the_refusal_explains_the_fix_forward_route():
    """"Refused" with no route is what makes an operator reach for the manual
    push that caused this."""
    msg = FIX_ONE_PR[FIX_ONE_PR.index("_PROMOTION_HEAD_RE"):]
    low = msg.lower()
    assert "dev" in low and "promote" in low
    assert "let it promote forward" in low
    assert "source branch" in low


# -- description fixes are deliberately NOT blocked --------------------------

def test_description_repair_still_runs_for_promotion_prs():
    """fix_pr_description edits the PR body, never the branch, so it cannot
    cause drift -- and promotion PRs are exactly the ones whose generated
    boilerplate the panel objects to. Blocking it would strand them."""
    assert FIX_ONE_PR.index("fix_pr_description") < FIX_ONE_PR.index("_PROMOTION_HEAD_RE")
