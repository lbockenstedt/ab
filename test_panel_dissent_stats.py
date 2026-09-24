#!/usr/bin/env python3
"""Self-test: per-reviewer dissent inside an approving panel (app_state).

A panel reports ONE aggregate verdict, so it can come back "Approve · 91%" while
an individual reviewer rejected the change outright. Only the aggregate was ever
persisted, so those residual concerns were invisible downstream and AppBuilder
merged straight over the top of them. _panel_dissent_stats extracts the
per-reviewer outcomes that pr_remediate.should_remediate now acts on
(panel_dissents / panel_min_reviewer_confidence).

app_state.py cannot be imported (it pulls in the whole web app), so the function
is extracted by source via ast and exec'd — same technique as the sibling
harnesses.

Generated on the remote Ollama GPU (qwen2.5-coder:14b), reviewed and pinned by hand.
"""
import pytest

import ast

def _load(path, name):
    src = open(path, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns = {}
            exec(ast.get_source_segment(src, node), ns)
            return ns[name]
    raise AssertionError("%s not found in %s" % (name, path))

_panel_dissent_stats = _load("app_state.py", "_panel_dissent_stats")

def test_all_reviewers_approve():
    """All reviewers approve with high confidences."""
    review = {
        "reviews": [
            {"verdict": "Approve", "confidence": 0.95},
            {"verdict": "Approve", "confidence": 0.99}
        ]
    }
    assert _panel_dissent_stats(review) == (0, 2, 0.95, 0)

def test_one_reviewer_dissents_inside_an_approving_panel():
    """One reviewer dissents while others approve."""
    review = {
        "reviews": [
            {"verdict": "Approve", "confidence": 0.99},
            {"verdict": "Deny", "confidence": 0.30}
        ]
    }
    assert _panel_dissent_stats(review) == (1, 2, 0.30, 0)

def test_request_changes_counts_as_a_dissent():
    """A 'Request changes' verdict counts as a dissent."""
    review = {
        "reviews": [
            {"verdict": "Approve", "confidence": 0.99},
            {"verdict": "Request changes", "confidence": 0.30}
        ]
    }
    assert _panel_dissent_stats(review) == (1, 2, 0.30, 0)

def test_verdict_matching_is_case_and_whitespace_insensitive():
    """Verdict matching is case and whitespace insensitive."""
    review = {
        "reviews": [
            {"verdict": "  approve  ", "confidence": 0.95},
            {"verdict": "APPROVE", "confidence": 0.99}
        ]
    }
    assert _panel_dissent_stats(review) == (0, 2, 0.95, 0)

def test_percentage_confidences_are_normalised():
    """Confidences given as percentages are normalised to 0-1."""
    review = {
        "reviews": [
            {"verdict": "Approve", "confidence": 95},
            {"verdict": "Approve", "confidence": 40}
        ]
    }
    assert _panel_dissent_stats(review) == (0, 2, 0.40, 0)

def test_confidence_is_clamped_to_one():
    """Confidences greater than 1 are clamped to 1, and less than 0 to 0."""
    review1 = {
        "reviews": [
            {"verdict": "Approve", "confidence": 150}
        ]
    }
    assert _panel_dissent_stats(review1) == (0, 1, 1.0, 0)
    
    review2 = {
        "reviews": [
            {"verdict": "Approve", "confidence": -5}
        ]
    }
    assert _panel_dissent_stats(review2) == (0, 1, 0.0, 0)

def test_missing_confidence_is_ignored():
    """Missing confidence is ignored, but the verdict is still counted."""
    review = {
        "reviews": [
            {"verdict": "Approve"},
            {"verdict": "Approve", "confidence": 0.8}
        ]
    }
    assert _panel_dissent_stats(review) == (0, 2, 0.8, 0)

def test_missing_verdict_is_not_rated():
    """Missing verdict is not counted as rated."""
    review = {
        "reviews": [
            {"confidence": 0.9},
            {"verdict": "Approve", "confidence": 0.8}
        ]
    }
    assert _panel_dissent_stats(review) == (0, 1, 0.8, 1)

def test_unparseable_confidence_is_skipped():
    """Unparseable confidence is skipped without raising an exception."""
    review = {
        "reviews": [
            {"verdict": "Approve", "confidence": "n/a"},
            {"verdict": "Approve", "confidence": 0.7}
        ]
    }
    assert _panel_dissent_stats(review) == (0, 2, 0.7, 0)

def test_non_dict_entries_are_skipped():
    """Non-dict entries in the reviews list are skipped."""
    review = {
        "reviews": [
            None,
            "junk",
            42,
            {"verdict": "Approve", "confidence": 0.9}
        ]
    }
    assert _panel_dissent_stats(review) == (0, 1, 0.9, 3)

@pytest.mark.parametrize("review", [None, {}, {"reviews": []}, {"reviews": None}, {"reviews": "notalist"}])
def test_empty_or_missing_inputs(review):
    """Empty or missing inputs return (0, 0, None)."""
    assert _panel_dissent_stats(review) == (0, 0, None, 0)

def test_a_panel_that_failed_to_run_reports_nothing():
    """A panel that failed to run reports nothing."""
    review = {
        "status": "timeout",
        "reviews": [{"verdict": "Deny", "confidence": 0.1}]
    }
    assert _panel_dissent_stats(review) == (0, 0, None, 0)


# ---------------------------------------------------------------------------
# ab#275 state-logic panel: an unfinished reviewer must not read as an
# approving one. These pin the `unrated` count that finding added.
# ---------------------------------------------------------------------------

def test_a_failed_seat_is_not_silently_unanimous():
    """The headline defect: a two-seat panel where one seat errored and one
    approved used to be byte-identical to unanimous approval (dissents=0,
    rated=1) with nothing recording that a seat was missing."""
    failed_then_approved = {"reviews": [{}, {"verdict": "Approve", "confidence": 0.99}]}
    unanimous = {"reviews": [{"verdict": "Approve", "confidence": 0.99},
                             {"verdict": "Approve", "confidence": 0.99}]}
    assert _panel_dissent_stats(failed_then_approved) != _panel_dissent_stats(unanimous)
    assert _panel_dissent_stats(failed_then_approved)[3] == 1
    assert _panel_dissent_stats(unanimous)[3] == 0


def test_blank_and_whitespace_verdicts_count_as_unrated():
    """A verdict key that is present but empty is still nothing we can score."""
    review = {"reviews": [{"verdict": ""}, {"verdict": None},
                          {"verdict": "Approve", "confidence": 0.9}]}
    dissents, rated, _min_conf, unrated = _panel_dissent_stats(review)
    assert (dissents, rated, unrated) == (0, 1, 2)


def test_unrated_seat_still_contributes_its_confidence():
    """A seat that reported a number but no parseable verdict still told us how
    sure it was — dropping that would hide a low rating behind an unusable
    verdict."""
    review = {"reviews": [{"confidence": 0.10},
                          {"verdict": "Approve", "confidence": 0.95}]}
    dissents, rated, min_conf, unrated = _panel_dissent_stats(review)
    assert (dissents, rated, unrated) == (0, 1, 1)
    assert min_conf == 0.10


def test_dissent_still_outranks_unrated():
    """An explicit dissent and an unreadable seat are counted separately."""
    review = {"reviews": [{"verdict": "Reject", "confidence": 0.2},
                          {"junk": True},
                          {"verdict": "Approve", "confidence": 0.9}]}
    dissents, rated, _min_conf, unrated = _panel_dissent_stats(review)
    assert (dissents, rated, unrated) == (1, 2, 1)
