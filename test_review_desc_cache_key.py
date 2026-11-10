"""The review cache must key on the PR DESCRIPTION as well as the head SHA.

The skeptical panel reads the PR body and rejects a PR whose description
contradicts its own diff ("VERSION contradicts the description. The diff bumps
VERSION from 1.35 to 1.36..."). That makes the description a review INPUT.

Before this fix the cache key was the head SHA alone, so fixing a wrong PR
description could never cause a re-review: AB replayed the stale rejection
forever and the promotion queue stalled with no way out but a force-push or a
manual Reprocess. These tests pin the description into the key.
"""
import ast
import pathlib
import re
import unittest

SRC = pathlib.Path(__file__).with_name("pr_review.py")


def _load(*names):
    """pr_review.py can't be imported (circular import via main.py, and
    fastapi/tenacity aren't installed), so lift the functions out with ast —
    the same convention as test_fix_engine_retry_triggers.py."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    ns = {"hashlib": __import__("hashlib")}
    wanted = set(names)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, str(SRC), "exec"), ns)
    missing = wanted - set(ns)
    if missing:
        raise AssertionError("not found in pr_review.py: %s" % sorted(missing))
    return [ns[n] for n in names]


class _PR:
    def __init__(self, title="t", body="b"):
        self.title = title
        self.body = body


class DescDigest(unittest.TestCase):
    def setUp(self):
        (self.digest,) = _load("_desc_digest")

    def test_same_description_same_digest(self):
        self.assertEqual(self.digest(_PR("a", "b")), self.digest(_PR("a", "b")))

    def test_editing_the_body_changes_the_digest(self):
        """The whole point: this is what a body-only edit must move."""
        self.assertNotEqual(self.digest(_PR("a", "b")), self.digest(_PR("a", "b2")))

    def test_editing_the_title_changes_the_digest(self):
        self.assertNotEqual(self.digest(_PR("a", "b")), self.digest(_PR("a2", "b")))

    def test_title_body_boundary_is_not_ambiguous(self):
        """Without a separator, ("ab","c") and ("a","bc") would collide."""
        self.assertNotEqual(self.digest(_PR("ab", "c")), self.digest(_PR("a", "bc")))

    def test_a_none_body_does_not_raise(self):
        """PyGithub hands back None for an empty body — a TypeError here would
        abort the scan for the whole repo."""
        self.assertTrue(self.digest(_PR("t", None)))
        self.assertTrue(self.digest(_PR(None, None)))

    def test_digest_is_short_and_hex(self):
        d = self.digest(_PR("a", "b"))
        self.assertEqual(len(d), 12)
        self.assertRegex(d, r"^[0-9a-f]{12}$")

    def test_real_world_version_wording_change_is_detected(self):
        """The exact edit that unblocked the stalled promotion queue."""
        old = _PR("promote: dev -> qa", "Promotion pins every VERSION file back to qa's value.")
        new = _PR("promote: dev -> qa", "Promotion pins every VERSION file to qa's value, then "
                                        "advances qa's own counter one step (1.35 -> 1.36).")
        self.assertNotEqual(self.digest(old), self.digest(new))


class DescIsCurrent(unittest.TestCase):
    def setUp(self):
        self.cur, = _load("_desc_is_current")

    def test_matching_marker_is_current(self):
        self.assertTrue(self.cur("x <!-- desc: abc123def456 --> y", "abc123def456"))

    def test_stale_marker_is_not_current(self):
        self.assertFalse(self.cur("x <!-- desc: abc123def456 --> y", "999999999999"))

    def test_legacy_comment_without_a_marker_is_tolerated(self):
        """Deploying this must not re-run the LLM panel on every open PR in the
        fleet at once — comments predating the marker stay valid."""
        self.assertTrue(self.cur("<!-- ab-pr-review -->\n<!-- head: deadbeef -->", "abc123def456"))

    def test_empty_or_none_body_is_tolerated(self):
        self.assertTrue(self.cur("", "abc123def456"))
        self.assertTrue(self.cur(None, "abc123def456"))


class Wiring(unittest.TestCase):
    """The functions existing is not enough — they have to be *used*."""

    def setUp(self):
        self.src = SRC.read_text(encoding="utf-8")

    def test_hashlib_is_imported(self):
        self.assertRegex(self.src, r"(?m)^import hashlib$")

    def test_already_current_consults_the_description(self):
        m = re.search(r"already_current = (.+?)\n    if ", self.src, re.S)
        self.assertIsNotNone(m, "could not locate the already_current expression")
        self.assertIn("_desc_is_current", m.group(1),
                      "already_current still keys on the head SHA alone — a corrected "
                      "PR description would never trigger a re-review")

    def test_render_is_called_with_the_digest(self):
        m = re.search(r"body = _render\((.+?)\)\n", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("desc_digest=_desc", m.group(1),
                      "the comment would be rewritten without a desc marker, so the "
                      "next poll could never tell the description had changed")

    def test_render_emits_the_marker(self):
        (render,) = _load("_render")
        ns = {
            "PR_REVIEW_MARKER": "<!-- ab-pr-review -->",
            "_render_route": lambda *a, **k: [],
            "_render_panel": lambda *a, **k: [],
            "_render_state_panel": lambda *a, **k: [],
            "_SUMMARY_HEADER": "### Summary",
            "_LEVEL_ORDER": {}, "_LEVEL_ICON": {},
        }
        render.__globals__.update(ns)
        out = render([], "deadbeef", desc_digest="abc123def456")
        self.assertIn("<!-- head: deadbeef -->", out)
        self.assertIn("<!-- desc: abc123def456 -->", out)
        # Regression guard: the heading must survive alongside the new marker.
        self.assertIn("AppBuilder PR pre-review", out)

    def test_render_omits_the_marker_when_no_digest_is_given(self):
        (render,) = _load("_render")
        render.__globals__.update({
            "PR_REVIEW_MARKER": "<!-- ab-pr-review -->",
            "_render_route": lambda *a, **k: [],
            "_render_panel": lambda *a, **k: [],
            "_render_state_panel": lambda *a, **k: [],
            "_SUMMARY_HEADER": "### Summary",
            "_LEVEL_ORDER": {}, "_LEVEL_ICON": {},
        })
        out = render([], "deadbeef")
        self.assertNotIn("<!-- desc:", out)
        self.assertIn("AppBuilder PR pre-review", out)


if __name__ == "__main__":
    unittest.main()


class DeletedCommentRecovery(unittest.TestCase):
    """A review comment deleted mid-scan must not abort the PR's review.

    Observed live: `pr_review: PR #73 in lbockenstedt/netbox failed: 404 ...
    update-an-issue-comment`. _find_marker_comment() had found the comment, it
    was deleted before existing.edit() ran, and the 404 propagated out of
    _review_one — skipping record_pr_review, so the PR silently left the queue
    with neither a comment nor a state row.
    """

    def setUp(self):
        self.src = SRC.read_text(encoding="utf-8")

    def _block(self):
        m = re.search(r"\n        if existing:\n(.+?)\n            action = \"created\"\n",
                      self.src, re.S)
        self.assertIsNotNone(m, "could not locate the comment upsert block")
        return m.group(0)

    def test_edit_is_guarded(self):
        self.assertIn("try:", self._block(),
                      "existing.edit() is unguarded — a deleted comment aborts the review")

    def test_a_404_falls_back_to_posting_a_new_comment(self):
        blk = self._block()
        self.assertIn('"404" not in str(e)', blk)
        self.assertIn("create_issue_comment", blk)

    def test_non_404_errors_still_propagate(self):
        """Swallowing every exception here would hide real API failures."""
        self.assertIn("raise", self._block(),
                      "a non-404 failure must not be silently swallowed")
