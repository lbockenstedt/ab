"""AB must be able to repair a PR DESCRIPTION, not only its code.

The skeptical panel judges the description against the diff, so "the
description contradicts the diff" is a verdict it returns. fix_one_pr clones
the branch and edits FILES, so it can never satisfy that finding — it burns
the whole fix budget producing code edits that miss the point, and the PR
stalls. Observed verbatim in production:

    Critique 1 (the description doesn't state the behaviour changes) is
    untouched. It needs a PR-body edit, not code.

These tests cover the detector that routes such findings, and the guards on
the repair path that edits the description.
"""
import ast
import pathlib
import re
import unittest

SRC = pathlib.Path(__file__).with_name("pr_review.py")
_TREE = ast.parse(SRC.read_text(encoding="utf-8"))


def _load(*names):
    """pr_review.py can't be imported (circular import via main.py, and
    fastapi/tenacity aren't installed) — lift definitions out with ast."""
    ns = {"re": re}
    wanted = set(names)
    for node in _TREE.body:
        got = None
        if isinstance(node, ast.FunctionDef):
            got = node.name
        elif isinstance(node, ast.Assign):
            got = getattr(node.targets[0], "id", None)
        if got in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC), "exec"), ns)
    missing = wanted - set(ns)
    if missing:
        raise AssertionError("not found in pr_review.py: %s" % sorted(missing))
    return [ns[n] for n in names]


class DetectorRoutesRealCritiques(unittest.TestCase):
    """Every string here is real panel output or a close paraphrase."""

    def setUp(self):
        _load("_DESC_NOUN_RE", "_DESC_COMPLAINT_RE", "_DESC_OBJECTION_WINDOW")
        # the helpers the function closes over must come from the same namespace
        ns = {"re": re}
        for node in _TREE.body:
            name = (node.name if isinstance(node, ast.FunctionDef)
                    else getattr(getattr(node, "targets", [None])[0], "id", None)
                    if isinstance(node, ast.Assign) else None)
            if name in ("_DESC_NOUN_RE", "_DESC_COMPLAINT_RE",
                        "_DESC_OBJECTION_WINDOW", "_panel_objects_to_description"):
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC), "exec"), ns)
        self.f = ns["_panel_objects_to_description"]

    # --- must be routed to the description path -----------------------------

    def test_the_version_objection_that_stalled_the_queue(self):
        self.assertTrue(self.f(
            "VERSION contradicts the description. The diff bumps VERSION from 1.35 to "
            "1.36. The description says promotion pins every VERSION file back to qa's "
            "value."))

    def test_complaint_before_the_noun(self):
        """The naive pattern required noun-then-complaint and missed this."""
        self.assertTrue(self.f("This contradicts the description."))

    def test_needs_a_pr_body_edit_not_code(self):
        self.assertTrue(self.f(
            "4. **Critique 1 (the description doesn't state the behaviour changes) is "
            "untouched.** It needs a PR-body edit, not code."))

    def test_description_claims_something_the_diff_disproves(self):
        self.assertTrue(self.f(
            "The PR description claims the change is behaviour-neutral, but the diff "
            "changes the default timeout."))

    def test_stale_pull_request_body(self):
        self.assertTrue(self.f("The pull request body is out of date with respect to the diff."))

    def test_summary_omits_something(self):
        self.assertTrue(self.f("The summary does not mention that the retry budget was raised."))

    # --- must NOT be routed here (they are ordinary code findings) ----------

    def test_a_purely_positive_critique(self):
        self.assertFalse(self.f(
            "The shell logic itself is sound: `wf` is pre-initialized to \"\" so the "
            "later grep is safe when the diff failed. No new bug found in the modified "
            "block."))

    def test_docs_files_in_the_repo_are_not_the_pr_description(self):
        """The trap: these say "docs" but mean files, and are fixable by code."""
        self.assertFalse(self.f(
            "6. **Risky docs.** The docs still encourage turning off "
            "`feature_automerge_require_allowlist` and allowing auto-merge into `main`."))
        self.assertFalse(self.f(
            "To approve: revert the empty-verdict change, restore the original "
            "chain-setup docs and add a production caveat, and correct docs item (3)."))
        self.assertFalse(self.f(
            "7. **Concern 6 (docs encouraging auto-merge into `main`) is not addressed.**"))

    def test_empty_and_none(self):
        self.assertFalse(self.f(""))
        self.assertFalse(self.f(None))

    def test_non_string_does_not_raise(self):
        self.assertFalse(self.f(12345))

    def test_a_distant_noun_and_complaint_are_not_paired(self):
        """The panel joins reviewers with ' | '; one reviewer's noun must not
        pair with another reviewer's unrelated complaint."""
        far = ("The description is fine. " + ("x" * 400) + " The retry count is wrong.")
        self.assertFalse(self.f(far))

    def test_word_boundaries_prevent_substring_matches(self):
        """'summarised' is not 'summary'; 'wrongly' is not 'wrong'."""
        self.assertFalse(self.f("The change is summarised in the commit; nothing wrongly typed."))


class RepairPathGuards(unittest.TestCase):
    """Read the source of fix_pr_description — it cannot be executed here
    (PyGithub network client), so assert its guards structurally."""

    def setUp(self):
        self.src = SRC.read_text(encoding="utf-8")
        i = self.src.find("def fix_pr_description(")
        self.assertNotEqual(i, -1, "fix_pr_description is missing")
        j = self.src.find("\ndef fix_one_pr(", i)
        self.body = self.src[i:j]
        # Strip the docstring: it legitimately contains the words "push" and
        # "merge" while PROMISING not to do them.
        fn = next(n for n in _TREE.body
                  if isinstance(n, ast.FunctionDef) and n.name == "fix_pr_description")
        stmts = fn.body[1:] if ast.get_docstring(fn) else fn.body
        lines = self.src.splitlines()
        self.code = "\n".join(lines[stmts[0].lineno - 1:fn.end_lineno])

    def test_it_never_pushes_or_merges(self):
        for forbidden in ("git.Repo", ".push(", ".merge(", "create_pull", "create_git_ref"):
            self.assertNotIn(forbidden, self.code,
                             "the description path must only edit the description")

    def test_it_does_not_clone_the_branch(self):
        for forbidden in ("clone_from", "tempfile", "import git"):
            self.assertNotIn(forbidden, self.code)

    def test_it_edits_only_the_body(self):
        self.assertIn("pr.edit(body=new_body)", self.body)

    def test_it_refuses_when_the_objection_is_not_about_the_description(self):
        self.assertIn("_panel_objects_to_description", self.body)

    def test_it_is_budgeted_per_head(self):
        self.assertIn("_MAX_DESC_FIXES", self.body)
        self.assertIn("head_sha", self.body)

    def test_it_takes_the_concurrency_lock(self):
        self.assertIn("_claim_issue", self.body)
        self.assertIn("_release_issue", self.body)
        self.assertIn("finally:", self.body)

    def test_it_refuses_an_empty_or_short_rewrite(self):
        self.assertIn("< 200", self.body)

    def test_it_refuses_an_identical_rewrite(self):
        """Otherwise it would spend budget and trigger a re-review for nothing."""
        self.assertIn('new_body.strip() == (pr.body or "").strip()', self.body)

    def test_it_protects_the_required_intent_section(self):
        self.assertIn("## Intent & Problem Statement", self.body)

    def test_the_budget_constant_is_small_and_finite(self):
        (cap,) = _load("_MAX_DESC_FIXES")
        self.assertIsInstance(cap, int)
        self.assertGreaterEqual(cap, 1)
        self.assertLessEqual(cap, 3, "a large cap lets a stubborn objection rewrite forever")


class WiredIntoTheFixLoop(unittest.TestCase):
    def setUp(self):
        src = SRC.read_text(encoding="utf-8")
        i = src.find("def fix_one_pr(")
        self.body = src[i:i + 4000]

    def test_fix_one_pr_tries_the_description_first(self):
        self.assertIn("_panel_objects_to_description", self.body,
                      "fix_one_pr still spends its whole budget on code edits for a "
                      "finding that explicitly needs a PR-body edit")
        self.assertIn("fix_pr_description", self.body)

    def test_it_reprocesses_so_the_edit_takes_effect(self):
        """The review cache keys on the description, so the re-review is what
        turns the edit into a new verdict."""
        self.assertIn("force=True", self.body)

    def test_it_falls_through_to_the_code_fix_when_the_rewrite_is_refused(self):
        self.assertIn("continuing with the code fix", self.body)


class Regenerator(unittest.TestCase):
    def setUp(self):
        src = SRC.read_text(encoding="utf-8")
        i = src.find("def _regenerate_pr_description(")
        j = src.find("\ndef _apply_batched_pr_summary(", i)
        self.body = src[i:j]

    def test_it_is_best_effort(self):
        self.assertIn('return ""', self.body)
        self.assertIn("except Exception", self.body)

    def test_it_can_be_disabled_by_config(self):
        self.assertIn("pr_review_desc_fix_enabled", self.body)

    def test_the_prompt_carries_the_objection_and_the_diff(self):
        for needed in ("CURRENT DESCRIPTION", "OBJECTION", "DIFF", "critique", "digest"):
            self.assertIn(needed, self.body)

    def test_it_instructs_the_model_to_keep_the_intent_section(self):
        self.assertIn("## Intent & Problem Statement", self.body)

    def test_it_forbids_inventing_changes(self):
        self.assertIn("Never invent", self.body)

    def test_it_strips_a_whole_answer_code_fence(self):
        """A fenced answer would be posted verbatim into the PR body."""
        self.assertIn('out.startswith("```")', self.body)


if __name__ == "__main__":
    unittest.main()
