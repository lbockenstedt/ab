#!/usr/bin/env python3
"""Self-test pinning that pr_review._review_one's "skip AppBuilder's own AI Fix
PR" check does NOT match feature-auto-drive PRs.

Run:  python3 ab/test_pr_review_own_pr_skip.py

Standalone: the predicate under test is a string literal (title) + a
branch_policy.AUTO_BRANCH_PREFIXES_BY_KIND["bug"] lookup (pr_review.py),
extracted/resolved from the real source rather than reimplemented so a
future edit to the real check is what this test actually exercises.

Regression guard: _review_one skips a PR when its title starts "AI Fix #" or
its head branch starts with the "bug" auto-branch prefix (AppBuilder's own
bug-fix PRs, already vetted by the fix panel when opened — re-reviewing them
is redundant). feature_build.py deliberately titles feature PRs
"AI Feature #N" on the "feature" auto-branch prefix — SIMILAR but NOT
matching the bug ones — specifically so the pre-review panel DOES run on
them (feature builds need the same scrutiny a human PR gets, unlike an
already-panel-reviewed bug fix). If a future edit widened the skip prefix
(e.g. to match both kinds), feature PRs would silently stop being reviewed
with no error anywhere — this test is the only thing standing between that
and a bug-fix-only change quietly disabling review for the entire feature
auto-drive pipeline."""
import re
import sys

import branch_policy


def _extract_skip_predicate():
    """Pulls the title literal out of pr_review.py's own source, and resolves
    the branch prefix through the SAME branch_policy lookup pr_review.py
    itself uses (not a memorized copy of the string) so this test tracks the
    REAL check, not a snapshot of it that could drift silently."""
    src = open("pr_review.py").read()
    m = re.search(
        r'_title\.startswith\("([^"]+)"\)\s+or\s+_head_ref\.startswith\('
        r'AUTO_BRANCH_PREFIXES_BY_KIND\["([^"]+)"\]\)',
        src,
    )
    assert m, "pr_review.py's own-PR skip predicate not found — did its shape change?"
    title_prefix, kind = m.group(1), m.group(2)
    return title_prefix, branch_policy.AUTO_BRANCH_PREFIXES_BY_KIND[kind]


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running pr_review own-PR-skip / feature-PR-naming self-test...")
    ok = True

    title_prefix, branch_prefix = _extract_skip_predicate()
    ok &= _check("extracted title prefix is 'AI Fix #'", title_prefix == "AI Fix #")
    ok &= _check("extracted branch prefix is 'bug/'", branch_prefix == "bug/")

    def is_skipped(title, branch):
        return title.startswith(title_prefix) or branch.startswith(branch_prefix)

    # ── real branch names, built the same way fix_engine/feature_build do ───
    bug_branch = branch_policy.auto_branch_name("bug", description="null pointer")
    feature_branch = branch_policy.auto_branch_name("feature", description="clear-dongles button")

    # ── bug-fix PRs (fix_engine.py) ARE skipped ──────────────────────────────
    ok &= _check("a real bug-fix PR title is skipped",
                is_skipped("AI Fix #42", bug_branch))
    ok &= _check("a bug-fix PR is skipped even if only the branch matches",
                is_skipped("something else", bug_branch))

    # ── feature-auto-drive PRs (feature_build.py) are NOT skipped ───────────
    feature_title = "AI Feature #42: Add a clear-dongles button"
    ok &= _check("a feature-build PR title does NOT match the skip prefix",
                not feature_title.startswith(title_prefix))
    ok &= _check("a feature-build PR branch does NOT match the skip prefix",
                not feature_branch.startswith(branch_prefix))
    ok &= _check("a feature-build PR is therefore NOT skipped by _review_one — the panel runs on it",
                not is_skipped(feature_title, feature_branch))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
