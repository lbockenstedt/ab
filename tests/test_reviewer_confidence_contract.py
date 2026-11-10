"""The reviewer prompt's verdict/confidence contract must be symmetric.

The prompt has always forbidden one contradiction -- a high confidence score
alongside a `Reject`. It said nothing about the mirror image, `Approve` with a
confidence below the 0.90 auto-merge bar, which is equally contradictory and is
what actually stalled the promotion queue.

Production, after reviewers regained file access (so these are NOT
"can't verify" verdicts -- every point below was checked against real files):

    kvm#26  p1 Approve 0.90   p2 Approve 0.875
            "... same -- harmless belt-and-braces."
    cs#152  p1 Approve 0.89   p2 Approve 0.875
            "Minor, non-blocking: the [ -z \"$TGT\" ] guard in backmerge.yml is
             unreachable in practice -- harmless defensive code, not a defect."

Both panels Approve, both describe their only caveat as harmless, and both then
report a number below the bar -- so the PR is held. That is a calibration
defect, not a safety signal.

This does NOT lower the 0.90 threshold, which is unchanged and must stay. It
tells the reviewer what the number is supposed to measure.
"""
import ast
import re

import pytest


@pytest.fixture(scope="module")
def prompt_text():
    """The reviewer prompt as the MODEL sees it.

    Assembled from the literals, not read off the source: adjacent-string
    concatenation means a sentence the reviewer reads as one phrase is split
    across lines in the file, so source-text matching gives false failures.
    """
    src = open("fix_engine.py", encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "review_fix")
    doc = ast.get_docstring(fn, clean=False)
    parts = [n.value for n in ast.walk(fn)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    # The docstring documents caller-facing policy (e.g. the degraded-panel
    # ">= 0.80 with a reviewer offline" rule) and is never sent to the model.
    return "".join(x for x in parts if x != doc)


def test_the_original_reject_side_rule_survives(prompt_text):
    assert "Do NOT give a high confidence score alongside a 'Reject'" in prompt_text


def test_approve_below_threshold_is_forbidden(prompt_text):
    """The missing half of the contract."""
    assert "do NOT return 'Approve' with a confidence below 0.90" in prompt_text


def test_confidence_is_defined_as_safe_to_merge(prompt_text):
    assert "correct and safe to merge" in prompt_text


def test_non_blocking_caveats_must_not_depress_the_score(prompt_text):
    """Pins the exact vocabulary the held reviews used on themselves."""
    for word in ("minor", "non-blocking", "cosmetic", "stylistic", "harmless"):
        assert word in prompt_text, "reviewers describe caveats as %r" % word
    assert "do NOT justify a sub-0.90 score" in prompt_text


def test_a_real_blocking_doubt_must_still_reject(prompt_text):
    """The rule must not read as 'always score high' -- genuine doubt still
    has a home, otherwise this would be threshold-lowering by prompt."""
    assert "would genuinely make you stop the merge" in prompt_text
    assert "return 'Reject' and name it explicitly" in prompt_text


def test_threshold_is_not_lowered_anywhere(prompt_text):
    """Standing constraint: 0.90 is the bar. This change is about what the
    number MEANS, never about moving it -- so the prompt must not state any
    approval bar other than 0.90."""
    bars = set(re.findall(r'(?:>=|at least|above|below)\s*(0\.\d+)', prompt_text))
    assert bars <= {"0.90"}, "prompt states a non-0.90 approval bar: %s" % sorted(bars)
    assert "0.90" in prompt_text


def test_automerge_threshold_constant_is_still_090():
    """Belt-and-braces: the gate itself, not just the prompt."""
    src = open("pr_review.py", encoding="utf-8").read()
    assert re.search(r'0\.90|0\.9\b', src), "auto-merge threshold constant missing"
