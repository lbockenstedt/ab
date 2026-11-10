"""The review panel's file context must survive pr_review's diff header style.

`_full_file_context` exists to hand a reviewer the FULL text of every file a
diff touches, because "I can't verify this" is the single largest source of
false Rejects. It located those files with `_DIFF_FILE_HEADER_RE`, which only
matches git's `diff --git a/x b/x` form.

But `pr_review._pr_diff_text` does not produce that form. It assembles the
panel's view as `--- <filename>` followed by GitHub's per-file `patch`. So on
the PR pre-review path -- the exact path the mitigation was written for -- the
regex matched nothing, `_full_file_context` returned "", and the reviewer got
no file context at all.

For a claude_cli reviewer that is total blindness: `supports_tools` excludes it
from `fetch_repo_file`, and `pr_review` passes no checkout, so the embedded
context was its only file access. Production critiques said so directly ("I
could not fetch any files", "the tests depend on the wording in promote.sh,
which I did not check") and parked those reviews at 0.85-0.895, just under the
0.90 auto-merge threshold.
"""
import ast
import re

import pytest


def _extract(path, funcs, assigns):
    src = open(path, encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in assigns for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    return "\n\n".join(segs)


@pytest.fixture
def ns():
    src = _extract("fix_engine.py", {"_diff_context_paths"},
                   {"_DIFF_FILE_HEADER_RE", "_PLAIN_FILE_HEADER_RE"})
    n = {"re": re}
    exec(src, n)
    return n


# The literal shape pr_review._pr_diff_text builds: "--- %s\n%s" % (filename, patch)
PR_REVIEW_STYLE = """--- .github/workflows/promote.yml
@@ -1,5 +1,9 @@
-# carries CODE ONLY
+# carries code; VERSION is pinned then advanced one step

--- VERSION
@@ -1 +1 @@
-1.25
+1.26"""

GIT_STYLE = """diff --git a/pr_review.py b/pr_review.py
index 1234567..89abcde 100644
--- a/pr_review.py
+++ b/pr_review.py
@@ -1 +1 @@
-x
+y"""


def test_pr_review_header_style_is_found(ns):
    """The regression: this returned [] and blinded every PR pre-review."""
    assert ns["_diff_context_paths"](PR_REVIEW_STYLE, 8) == [
        ".github/workflows/promote.yml", "VERSION"]


def test_git_header_style_still_works(ns):
    assert ns["_diff_context_paths"](GIT_STYLE, 8) == ["pr_review.py"]


def test_unified_diff_a_prefix_is_not_captured(ns):
    """`--- a/x` is a real unified-diff header; the git pattern owns it, and it
    must not also be captured as a literal path beginning 'a/'."""
    assert "a/pr_review.py" not in ns["_diff_context_paths"](GIT_STYLE, 8)


def test_mixed_styles_are_both_found_without_duplicates(ns):
    got = ns["_diff_context_paths"](PR_REVIEW_STYLE + "\n" + GIT_STYLE, 8)
    assert set(got) == {".github/workflows/promote.yml", "VERSION", "pr_review.py"}
    assert len(got) == len(set(got))


def test_duplicate_paths_are_collapsed(ns):
    assert ns["_diff_context_paths"](PR_REVIEW_STYLE + "\n" + PR_REVIEW_STYLE, 8) == [
        ".github/workflows/promote.yml", "VERSION"]


def test_limit_is_respected(ns):
    assert len(ns["_diff_context_paths"](PR_REVIEW_STYLE, 1)) == 1


def test_empty_and_none_prompts_are_safe(ns):
    assert ns["_diff_context_paths"]("", 8) == []
    assert ns["_diff_context_paths"](None, 8) == []


def test_a_removed_line_is_not_mistaken_for_a_header(ns):
    """A deleted line renders as '-<content>'; only a real '--- path' header
    (three dashes, one space, a path with no spaces) may match."""
    assert ns["_diff_context_paths"]("@@ -1 +1 @@\n--- not a path here\n", 8) == []


def test_pr_review_diff_text_still_emits_the_matched_style():
    """Pin the producer against the consumer: if _pr_diff_text ever stops using
    the '--- <filename>' header, this fix stops working and this test says so."""
    src = open("pr_review.py", encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_pr_diff_text")
    body = ast.get_source_segment(src, fn)
    assert '"--- %s\\n%s"' in body, (
        "_pr_diff_text no longer emits '--- <filename>' headers; "
        "_PLAIN_FILE_HEADER_RE must be updated to match the new format")


def test_full_file_context_uses_the_shared_helper():
    """_full_file_context must go through _diff_context_paths, not re-implement
    path extraction with the git-only regex."""
    src = open("fix_engine.py", encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_full_file_context")
    body = ast.get_source_segment(src, fn)
    assert "_diff_context_paths" in body
    assert "_DIFF_FILE_HEADER_RE.findall" not in body
