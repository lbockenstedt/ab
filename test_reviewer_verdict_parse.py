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
import fnmatch
import json
import re
import sys
import types

import pytest

_FE_FUNCS = {
    "_relax_json_fix_strings", "_parse_reviewer_json", "_robust_json_loads",
    "_sanitize_json_string_newlines", "_has_verdict_key", "_canon_verdict_keys",
    "_balanced_brace_span", "_extract_reviewer_verdict", "_norm_confidence",
    "_truncate_diff", "_reviewer_vote", "_is_local_provider", "_cloud_frontier_fallback",
    "_panel_allowlist", "_model_allowed", "_reviewer_name",
}
_FE_ASSIGNS = {
    "_JSON_NEXT_MEMBER_RE", "_FIX_CODE_KEY_RE", "_REVIEW_TEXT_KEY_RE",
    "_REVIEW_NEXT_MEMBER_RE", "_JSON_BAD_ESCAPE_RE", "_REVIEWER_RETRY_NOTE",
    "_LOCAL_PROVIDERS", "DEFAULT_PANEL_ALLOWLIST",
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


class _FakeLlmClient:
    """Default llm_client stub: no candidates configured. Tests that need
    _cloud_frontier_fallback to see specific candidates replace fe["llm_client"]."""
    def __init__(self, candidates=()):
        self._c = list(candidates)

    def _enumerate_candidates(self, config):
        return list(self._c)


@pytest.fixture
def fe():
    ns = {"re": re, "json": json, "fnmatch": fnmatch, "logger": _RecLog(),
          "llm_client": _FakeLlmClient()}
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


def _local_cand(key="local-1", model="qwen-local"):
    return {"key": key, "provider": "ollama", "model": model}


def _cloud_cand(key="cloud-1", model="claude-opus-5"):
    return {"key": key, "provider": "copilot", "model": model}


def _vote(fe, replies, reviewer=None, config=None):
    """Run _reviewer_vote with _run_reviewer_turn replaying `replies` (an Exception
    entry is raised). Returns (result, prompts seen, candidates seen)."""
    prompts = []
    candidates = []
    it = iter(replies)

    def fake_turn(prompt, system, cand, task_id, repo, head_sha, repo_checkout_path=None):
        prompts.append(prompt)
        candidates.append(cand)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    fe["_run_reviewer_turn"] = fake_turn
    result = fe["_reviewer_vote"](reviewer or _R, "PROMPT", None, None, None, None, config or {})
    return result, prompts, candidates


def test_vote_first_try(fe):
    (vote, failed), prompts, _c = _vote(fe, [JSON])
    assert failed is None and vote["reviewer"] == "rev-1" and vote["verdict"] == "Approve"
    assert len(prompts) == 1


def test_vote_normalises_percentage_confidence(fe):
    (vote, _), _p, _c = _vote(fe, ['{"confidence": 95, "verdict": "Approve"}'])
    assert vote["confidence"] == 0.95


def test_vote_retries_once_after_prose(fe):
    (vote, failed), prompts, _c = _vote(fe, ["Looks fine to me, the {...} is noise.", JSON])
    assert failed is None and vote["verdict"] == "Approve"
    assert len(prompts) == 2
    assert prompts[0] == "PROMPT"
    assert "Your previous reply was not a JSON object" in prompts[1]


def test_vote_prose_twice_is_counted_failure(fe):
    (vote, failed), prompts, _c = _vote(fe, ["no json here", "still none {...}"])
    assert vote is None and failed == "rev-1"
    assert len(prompts) == 2
    warns = [m for lvl, m in fe["logger"].records if lvl == "warning"]
    assert any("no parseable verdict after retry" in m and "rev-1" in m for m in warns)


def test_vote_cooldown_error_not_retried(fe):
    (vote, failed), prompts, _c = _vote(fe, [RuntimeError("all providers cooling down: rate_limited")])
    assert vote is None and failed == "rev-1"
    assert len(prompts) == 1


# ── _reviewer_vote: hard-failure retry / cloud escalation ──────────────────

def test_vote_local_hard_failure_escalates_to_cloud_and_succeeds(fe):
    local = _local_cand()
    cloud = _cloud_cand()
    fe["llm_client"] = _FakeLlmClient([local, cloud])
    reviewer = {"name": "rev-1", "candidate": local}
    (vote, failed), prompts, cands = _vote(
        fe, [ConnectionError("network blip"), JSON], reviewer=reviewer, config={})
    assert failed is None
    assert vote["reviewer"] == "rev-1"  # original seat's name, not the fallback's
    assert vote["escalated_to"] == fe["_reviewer_name"](cloud)
    assert len(cands) == 2
    assert cands[0] is local
    assert cands[1] is cloud  # escalated retry targets the cloud fallback


def test_vote_local_hard_failure_escalation_also_fails(fe):
    local = _local_cand()
    cloud = _cloud_cand()
    fe["llm_client"] = _FakeLlmClient([local, cloud])
    reviewer = {"name": "rev-1", "candidate": local}
    (vote, failed), prompts, cands = _vote(
        fe, [ConnectionError("network blip"), ConnectionError("still down")],
        reviewer=reviewer, config={})
    assert vote is None and failed == "rev-1"
    assert len(cands) == 2


def test_vote_local_hard_failure_no_cloud_fallback_retries_same_candidate(fe):
    local = _local_cand()
    fe["llm_client"] = _FakeLlmClient([local])  # no allowlisted cloud candidate configured
    reviewer = {"name": "rev-1", "candidate": local}
    (vote, failed), prompts, cands = _vote(
        fe, [ConnectionError("network blip"), JSON], reviewer=reviewer, config={})
    assert failed is None and vote["reviewer"] == "rev-1"
    assert "escalated_to" not in vote
    assert len(cands) == 2
    assert cands[0] is local and cands[1] is local


def test_vote_non_local_hard_failure_no_fallback_retries_same_candidate(fe):
    cloud = _cloud_cand()
    fe["llm_client"] = _FakeLlmClient([cloud])
    reviewer = {"name": "rev-1", "candidate": cloud}
    (vote, failed), prompts, cands = _vote(
        fe, [ConnectionError("network blip"), JSON], reviewer=reviewer, config={})
    assert failed is None and vote["reviewer"] == "rev-1"
    assert "escalated_to" not in vote
    assert len(cands) == 2
    assert cands[0] is cloud and cands[1] is cloud  # no fallback available, retries same candidate


def test_vote_cloud_hard_failure_escalates_to_other_cloud_candidate(fe):
    cloud1 = _cloud_cand("cloud-1", "claude-opus-5")
    cloud2 = _cloud_cand("cloud-2", "gpt-5.6-sol")
    fe["llm_client"] = _FakeLlmClient([cloud1, cloud2])
    reviewer = {"name": "rev-1", "candidate": cloud1}
    (vote, failed), prompts, cands = _vote(
        fe, [ConnectionError("network blip"), JSON], reviewer=reviewer, config={})
    assert failed is None and vote["reviewer"] == "rev-1"
    assert vote["escalated_to"] == fe["_reviewer_name"](cloud2)
    assert len(cands) == 2
    assert cands[0] is cloud1 and cands[1] is cloud2


def test_vote_cloud_hard_failure_falls_back_to_local_ollama_backfill(fe):
    cloud = _cloud_cand()
    local = _local_cand()
    fe["llm_client"] = _FakeLlmClient([cloud, local])
    reviewer = {"name": "rev-1", "candidate": cloud}
    (vote, failed), prompts, cands = _vote(
        fe, [ConnectionError("network blip"), JSON], reviewer=reviewer, config={})
    assert failed is None and vote["reviewer"] == "rev-1"
    assert vote["escalated_to"] == fe["_reviewer_name"](local)
    assert len(cands) == 2
    assert cands[0] is cloud and cands[1] is local


def test_vote_cooldown_on_local_reviewer_still_not_retried(fe):
    # Regression guard: a cooldown error must NOT trigger the new hard-failure
    # retry/escalation path, even for a local-provider reviewer.
    local = _local_cand()
    fe["llm_client"] = _FakeLlmClient([local, _cloud_cand()])
    reviewer = {"name": "rev-1", "candidate": local}
    (vote, failed), prompts, cands = _vote(
        fe, [RuntimeError("all providers cooling down: rate_limited")], reviewer=reviewer, config={})
    assert vote is None and failed == "rev-1"
    assert len(cands) == 1


# ── _cloud_frontier_fallback & panel diversity ─────────────────────────────

def test_cloud_frontier_fallback_excludes_seated_panel_keys(fe):
    cloud1 = _cloud_cand("cloud-1", "claude-opus-5")
    cloud2 = _cloud_cand("cloud-2", "gpt-5.6-sol")
    fe["llm_client"] = _FakeLlmClient([cloud1, cloud2])
    # When cloud-1 is in exclude_keys (seated on panel), fallback picks cloud-2
    c = fe["_cloud_frontier_fallback"]({"cloud-1"}, {})
    assert c is not None and c["key"] == "cloud-2"
    # When both are in exclude_keys, fallback returns None
    assert fe["_cloud_frontier_fallback"]({"cloud-1", "cloud-2"}, {}) is None


def test_vote_local_hard_failure_excludes_seated_panel_keys_from_escalation(fe):
    local = _local_cand("local-1", "qwen-local")
    cloud_seated = _cloud_cand("cloud-1", "claude-opus-5")
    cloud_other = _cloud_cand("cloud-2", "gpt-5.6-sol")
    fe["llm_client"] = _FakeLlmClient([local, cloud_seated, cloud_other])
    # cloud-1 is already seated on the panel
    reviewer = {"name": "rev-1", "candidate": local, "seated_panel_keys": {"local-1", "cloud-1"}}
    (vote, failed), _prompts, cands = _vote(
        fe, [ConnectionError("network blip"), JSON], reviewer=reviewer, config={})
    assert failed is None
    assert vote["reviewer"] == "rev-1"
    assert vote["escalated_to"] == fe["_reviewer_name"](cloud_other)
    assert cands[1] is cloud_other  # did NOT pick seated cloud-1


def test_vote_first_attempt_json_decode_error_logs_warning_with_raw_snippet(fe):
    err = json.JSONDecodeError("Expecting value", '{"bad": json...', 0)
    (vote, failed), _prompts, _cands = _vote(fe, [err, JSON])
    assert failed is None and vote["verdict"] == "Approve"
    warns = [m for lvl, m in fe["logger"].records if lvl == "warning"]
    assert any("JSON parse failed" in m and "raw response" in m and "bad" in m for m in warns)


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
