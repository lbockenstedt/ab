#!/usr/bin/env python3
"""Self-test pinning that pr_review._review_one's "skip BugFixer's own AI Fix
PR" check does NOT match feature-auto-drive PRs.

Run:  python3 bugfixer/test_pr_review_own_pr_skip.py

Standalone: the predicate under test is two string literals + startswith
checks (pr_review.py:692), extracted verbatim rather than reimplemented so a
future edit to the real check is what this test actually exercises.

Regression guard: _review_one skips a PR when its title starts "AI Fix #" or
its head branch starts "ai-fix-issue-" (BugFixer's own bug-fix PRs, already
vetted by the fix panel when opened — re-reviewing them is redundant).
feature_build.py deliberately titles feature PRs "AI Feature #N" on branch
"ai-feature-issue-N" — SIMILAR but NOT matching those prefixes — specifically
so the pre-review panel DOES run on them (feature builds need the same
scrutiny a human PR gets, unlike an already-panel-reviewed bug fix). If a
future edit widened the skip prefix (e.g. to "AI " or "ai-"), feature PRs
would silently stop being reviewed with no error anywhere — this test is
the only thing standing between that and a bug-fix-only change quietly
disabling review for the entire feature auto-drive pipeline."""
import re
import sys


def _extract_skip_predicate():
    """Pulls the literal prefixes out of pr_review.py's own source (not
    reimplemented by hand) so this test tracks the REAL check, not a
    memorized copy of it that could drift silently."""
    src = open("pr_review.py").read()
    m = re.search(
        r'_title\.startswith\("([^"]+)"\)\s+or\s+_head_ref\.startswith\("([^"]+)"\)',
        src,
    )
    assert m, "pr_review.py's own-PR skip predicate not found — did its shape change?"
    return m.group(1), m.group(2)


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running pr_review own-PR-skip / feature-PR-naming self-test...")
    ok = True

    title_prefix, branch_prefix = _extract_skip_predicate()
    ok &= _check("extracted title prefix is 'AI Fix #'", title_prefix == "AI Fix #")
    ok &= _check("extracted branch prefix is 'ai-fix-issue-'", branch_prefix == "ai-fix-issue-")

    def is_skipped(title, branch):
        return title.startswith(title_prefix) or branch.startswith(branch_prefix)

    # ── bug-fix PRs (feature_build.py's sibling, fix_engine.py) ARE skipped ─
    ok &= _check("a real bug-fix PR title is skipped",
                is_skipped("AI Fix #42", "ai-fix-issue-42"))
    ok &= _check("a bug-fix PR is skipped even if only the branch matches",
                is_skipped("something else", "ai-fix-issue-42"))

    # ── feature-auto-drive PRs (feature_build.py) are NOT skipped ───────────
    feature_title = "AI Feature #42: Add a clear-dongles button"
    feature_branch = "ai-feature-issue-42"
    ok &= _check("a feature-build PR title does NOT match the skip prefix",
                not feature_title.startswith(title_prefix))
    ok &= _check("a feature-build PR branch does NOT match the skip prefix",
                not feature_branch.startswith(branch_prefix))
    ok &= _check("a feature-build PR is therefore NOT skipped by _review_one — the panel runs on it",
                not is_skipped(feature_title, feature_branch))

    # ── exact strings feature_build.py actually produces ────────────────────
    # (mirrors the naming asserted in test_feature_build.py's "docs present
    # from the start" case — kept in sync deliberately, not imported, since
    # this file's whole point is testing the STRING SHAPE independently.)
    ok &= _check("feature_build.py's real PR-title template does not match the skip",
                not f"AI Feature #{7}: some title"[:len(title_prefix)] == title_prefix)
    ok &= _check("feature_build.py's real branch template does not match the skip",
                not f"ai-feature-issue-{7}".startswith(branch_prefix))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
