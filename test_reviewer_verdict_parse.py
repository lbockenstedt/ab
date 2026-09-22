"""Tests for the reviewer-verdict extraction/retry path in fix_engine.review_fix.

WHY: the old greedy `\\{.*\\}` regex spanned the first `{` to the last `}` of a
reply, so a prose answer mentioning `{...}` discarded a correct verdict, and a
brace-less reply returned (None, None) -- neither a vote nor a counted failure --
which surfaced as `all_reviewers_failed: reviewer(s)` with no cause. Diffs cut
mid-hunk also made reviewers report phantom syntax errors at the cut.

fix_engine imports the app (circular at import time), so -- like the other
reviewer tests -- the pure helpers are extracted with ast and exec'd.
"""
import ast
import json
import re
import sys
import types

import pytest

_FE_FUNCS = {
    "_relax_json_fix_strings", "_parse_reviewer_json", "_robust_json_loads",
    "_sanitize_json_string_newlines", "_has_verdict_key", "_canon_verdict_keys",
    "_balanced_brace_span", "_extract_reviewer_verdict", "_norm_confidence",
    "_truncate_diff", "_reviewer_vote",
}
_FE_ASSIGNS = {
    "_JSON_NEXT_MEMBER_RE", "_FIX_CODE_KEY_RE", "_REVIEW_TEXT_KEY_RE",
    "_REVIEW_NEXT_MEMBER_RE", "_JSON_BAD_ESCAPE_RE", "_REVIEWER_RETRY_NOTE",
}


class _RecLog:
    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.records.append((level, str(msg)))


def _extract(path, funcs, assigns=()):
    src = open(path).read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in assigns for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    return segs


@pytest.fixture
def fe():
    ns = {"re": re, "json": json, "logger": _RecLog()}
    exec("\n\n".join(_extract("llm_client.py", {"is_llm_cooldown_error"})), ns)
    exec("\n\n".join(_extract("fix_engine.py", _FE_FUNCS, _FE_ASSIGNS)), ns)
    missing = sorted((_FE_FUNCS | _FE_ASSIGNS) - set(ns))
    assert not missing, "extraction incomplete: %s" % missing
    return ns


VERDICT = {"confidence": 0.93, "verdict": "Approve", "critique": "Looks right."}
JSON = json.dumps(VERDICT)


# ── _extract_reviewer_verdict ───────────────────────────────────────────────

def test_plain_json(fe):
    assert fe["_extract_reviewer_verdict"](JSON) == VERDICT


def test_fenced_json(fe):
    assert fe["_extract_reviewer_verdict"]("```json\n%s\n```" % JSON) == VERDICT


def test_prose_then_json(fe):
    assert fe["_extract_reviewer_verdict"]("My verdict follows.\n" + JSON) == VERDICT


def test_prose_braces_before_json(fe):
    text = "The `response = {...}` block and {a: b} were diff noise.\n" + JSON
    assert fe["_extract_reviewer_verdict"](text) == VERDICT


def test_critique_with_braces_and_quotes(fe):
    v = {"confidence": 0.9, "verdict": "Reject",
         "critique": 'The dict {"a": 1} is built with a "quoted" key and a } stray brace.'}
    assert fe["_extract_reviewer_verdict"]("Here: " + json.dumps(v) + " done") == v


def test_first_object_without_verdict_is_skipped(fe):
    text = '{"note": "context"} then ' + JSON
    assert fe["_extract_reviewer_verdict"](text) == VERDICT


def test_repair_path_unescaped_quote(fe):
    prod = ('{"confidence": 0.93, "verdict": "Approve", "critique": "Reachability: '
            'the settings["GITHUB_TOKEN_configured"]/from_env lines are computed."}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(prod)
    v = fe["_extract_reviewer_verdict"]("Answer:\n" + prod)
    assert v["verdict"] == "Approve" and v["confidence"] == 0.93
    assert 'settings["GITHUB_TOKEN_configured"]' in v["critique"]


def test_prose_only_braces_is_none(fe):
    assert fe["_extract_reviewer_verdict"]("The `response = {...}` block is fine.") is None


def test_empty_and_none(fe):
    assert fe["_extract_reviewer_verdict"]("") is None
    assert fe["_extract_reviewer_verdict"](None) is None


def test_verdict_without_confidence_is_returned(fe):
    assert fe["_extract_reviewer_verdict"]('{"verdict": "Approve"}') == {"verdict": "Approve"}


def test_verdict_key_case_insensitive(fe):
    v = fe["_extract_reviewer_verdict"]('{"Verdict": "Approve", "Confidence": 0.9}')
    assert v == {"verdict": "Approve", "confidence": 0.9}


# ── _reviewer_vote ──────────────────────────────────────────────────────────

_R = {"name": "rev-1", "candidate": None}


def _vote(fe, replies):
    """Run _reviewer_vote with _run_reviewer_turn replaying `replies` (an Exception
    entry is raised). Returns (result, prompts seen)."""
    prompts = []
    it = iter(replies)

    def fake_turn(prompt, system, cand, task_id, repo, head_sha, repo_checkout_path=None):
        prompts.append(prompt)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    fe["_run_reviewer_turn"] = fake_turn
    return fe["_reviewer_vote"](_R, "PROMPT", None, None, None, None), prompts


def test_vote_first_try(fe):
    (vote, failed), prompts = _vote(fe, [JSON])
    assert failed is None and vote["reviewer"] == "rev-1" and vote["verdict"] == "Approve"
    assert len(prompts) == 1


def test_vote_normalises_percentage_confidence(fe):
    (vote, _), _p = _vote(fe, ['{"confidence": 95, "verdict": "Approve"}'])
    assert vote["confidence"] == 0.95


def test_vote_retries_once_after_prose(fe):
    (vote, failed), prompts = _vote(fe, ["Looks fine to me, the {...} is noise.", JSON])
    assert failed is None and vote["verdict"] == "Approve"
    assert len(prompts) == 2
    assert prompts[0] == "PROMPT"
    assert "Your previous reply was not a JSON object" in prompts[1]


def test_vote_prose_twice_is_counted_failure(fe):
    (vote, failed), prompts = _vote(fe, ["no json here", "still none {...}"])
    assert vote is None and failed == "rev-1"
    assert len(prompts) == 2
    warns = [m for lvl, m in fe["logger"].records if lvl == "warning"]
    assert any("no parseable verdict after retry" in m and "rev-1" in m for m in warns)


def test_vote_cooldown_error_not_retried(fe):
    (vote, failed), prompts = _vote(fe, [RuntimeError("all providers cooling down: rate_limited")])
    assert vote is None and failed == "rev-1"
    assert len(prompts) == 1


# ── _truncate_diff ──────────────────────────────────────────────────────────

def test_truncate_diff_under_limit_unchanged(fe):
    assert fe["_truncate_diff"]("a\nb\nc", 100) == "a\nb\nc"


def test_truncate_diff_cuts_on_line_boundary(fe):
    text = "\n".join("line %03d" % i for i in range(100))
    out = fe["_truncate_diff"](text, 50)
    kept, marker = out.split("\n… [DIFF TRUNCATED", 1)
    assert text.startswith(kept) and len(kept) <= 50
    assert text[len(kept)] == "\n"            # cut lands on a line boundary
    assert "NOT a syntax error" in marker
    assert "at %d chars: %d more chars omitted" % (len(kept), len(text) - len(kept)) in marker


def test_truncate_diff_single_long_line(fe):
    out = fe["_truncate_diff"]("x" * 100, 10)
    assert out.startswith("x" * 10 + "\n… [DIFF TRUNCATED at 10 chars: 90 more")


# ── pr_review._pr_diff_text ─────────────────────────────────────────────────

def test_pr_diff_text_uses_new_marker(fe, monkeypatch):
    stub = types.ModuleType("fix_engine")
    stub._truncate_diff = fe["_truncate_diff"]
    monkeypatch.setitem(sys.modules, "fix_engine", stub)
    ns = {}
    exec("\n\n".join(_extract("pr_review.py", {"_pr_diff_text"},
                              {"_PANEL_PATCH_CHARS", "_PANEL_DIFF_CHARS", "_PANEL_MAX_FILES"})), ns)

    class _F:
        filename = "big.py"
        patch = "\n".join("+line %05d" % i for i in range(5000))

    assert len(_F.patch) > ns["_PANEL_PATCH_CHARS"]
    out = ns["_pr_diff_text"]([_F()])
    assert out.startswith("--- big.py\n")
    assert "NOT a syntax error" in out
    assert "(patch truncated)" not in out
