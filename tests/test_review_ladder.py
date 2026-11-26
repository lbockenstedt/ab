"""Self-test: review ladder runs cheap filters first, then one frontier reviewer.

``.github/model-policy.json`` contains a ``review`` section with ladder,
pass_confidence and frontier model ids.  The review ladder:
  1. Each cheap ``ladder`` model reviews in order as a filter.
     A filter ``Reject`` ends the panel immediately (frontier is not called).
  2. Then ONE frontier reviewer gives the final verdict: Approve only when
     its verdict is "Approve" AND its confidence >= pass_confidence (0.90),
     otherwise Reject.

fix_engine cannot be imported directly (circular main.py chain), so the
pieces under test are ast-extracted and exec'd with stubbed dependencies.
"""
import ast
import json
import os
import types

import pytest

# Names of the helpers we extract from fix_engine.py.
_FIX_NAMES = {
    "_model_allowed",
    "_review_policy",
    "_resolve_builder",
    "_review_ladder",
    "_lm_review_filters",
    "_build_critique_reviews",
    "_ladder_result",
    "_run_review_ladder",
    "_frontier_rework_local",
}

# Assignments we need from fix_engine.py.
_FIX_ASSIGNS = {"MODEL_POLICY_FILE", "_REVIEW_PASS_FLOOR", "_LM_REVIEW_PASSES"}


class _NoLog:
    """Silent logger stub."""
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load(enumerate_fn, policy_data):
    """Extract ladder helpers into a namespace with a fake enumerator and policy."""
    src = open("fix_engine.py", encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in _FIX_NAMES:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in _FIX_ASSIGNS for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    llm = types.SimpleNamespace(_enumerate_candidates=enumerate_fn)
    ns = {"json": json, "os": os, "fnmatch": __import__("fnmatch"),
          "logger": _NoLog(), "llm_client": llm,
          "__file__": os.path.abspath("fix_engine.py")}
    exec("\n\n".join(segs), ns)
    # Patch MODEL_POLICY_FILE to point at our tmp file.
    return ns


def _cand(model, available=True):
    """Create a fake candidate dict."""
    return {"model": model, "key": ("provider", "", model), "available": available}


# Policy data used by most tests.
REVIEW_POLICY = {
    "review": {
        "ladder": ["mai-code-2-flash"],
        "pass_confidence": 0.9,
        "frontier": {
            "anthropic_author": "gpt-5.6-sol",
            "other_author": "claude-opus-5.5",
        },
    },
}


@pytest.fixture
def ns(tmp_path, monkeypatch):
    """Namespace with the review policy and standard candidates."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(REVIEW_POLICY), encoding="utf-8")
    cands = [
        _cand("mai-code-2-flash"),
        _cand("gpt-5.6-sol"),
        _cand("claude-opus-5.5"),
        _cand("claude-sonnet-5.5"),
    ]
    ns = _load(lambda cfg: cands, REVIEW_POLICY)
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    return ns


def _seats():
    """One cheap filter seat followed by the frontier seat."""
    return [{"name": "mai", "candidate": _cand("mai-code-2-flash")},
            {"name": "sol", "candidate": _cand("gpt-5.6-sol")}]


def _runner(answers):
    """A fake review_one that answers per seat name and records who was asked."""
    asked = []

    def review_one(r):
        """Return the scripted (vote, failed_name) for this seat."""
        asked.append(r["name"])
        return answers[r["name"]]
    return review_one, asked


def test_filter_reject_skips_frontier(ns):
    """A filter Reject ends the panel; the frontier model is never called."""
    review_one, asked = _runner({"mai": ({"reviewer": "mai", "verdict": "Reject", "confidence": 0.7}, None)})
    result = ns["_run_review_ladder"](_seats(), review_one, 0.9)
    assert asked == ["mai"]
    assert (result["verdict"], result["ladder_stage"], result["confidence"]) == ("Reject", "filter", 0.7)


def test_frontier_below_threshold_rejects(ns):
    """A filter Approve at 0.99 cannot pass; a frontier Approve at 0.85 is a Reject."""
    review_one, asked = _runner({
        "mai": ({"reviewer": "mai", "verdict": "Approve", "confidence": 0.99}, None),
        "sol": ({"reviewer": "sol", "verdict": "Approve", "confidence": 0.85}, None)})
    result = ns["_run_review_ladder"](_seats(), review_one, 0.9)
    assert asked == ["mai", "sol"]
    assert (result["verdict"], result["confidence"], result["ladder_stage"]) == ("Reject", 0.85, "frontier")
    assert len(result["reviews"]) == 2


def test_frontier_approve_passes(ns):
    """A frontier Approve at 0.92 passes with the frontier's own confidence."""
    review_one, _ = _runner({
        "mai": ({"reviewer": "mai", "verdict": "Approve", "confidence": 0.6}, None),
        "sol": ({"reviewer": "sol", "verdict": "Approve", "confidence": 0.92}, None)})
    result = ns["_run_review_ladder"](_seats(), review_one, 0.9)
    assert (result["verdict"], result["confidence"], result["ladder_stage"]) == ("Approve", 0.92, "frontier")
    assert result["panel_models"] == ["mai-code-2-flash", "gpt-5.6-sol"]


def test_failed_filter_is_skipped(ns):
    """A filter that gives no vote is listed as failed and the frontier still decides."""
    review_one, asked = _runner({
        "mai": (None, "mai"),
        "sol": ({"reviewer": "sol", "verdict": "Approve", "confidence": 0.95}, None)})
    result = ns["_run_review_ladder"](_seats(), review_one, 0.9)
    assert asked == ["mai", "sol"]
    assert result["verdict"] == "Approve"
    assert result["failed_reviewers"] == ["mai"]


@pytest.mark.parametrize("verdict", ["Approve", "Reject"])
def test_escalated_filter_is_skipped(ns, verdict):
    """A filter seat answered by an escalated model neither rejects nor counts; the frontier decides."""
    review_one, asked = _runner({
        "mai": ({"reviewer": "mai", "verdict": verdict, "confidence": 0.9, "escalated_to": "x"}, None),
        "sol": ({"reviewer": "sol", "verdict": "Approve", "confidence": 0.93}, None)})
    result = ns["_run_review_ladder"](_seats(), review_one, 0.9)
    assert asked == ["mai", "sol"]
    assert (result["verdict"], result["failed_reviewers"]) == ("Approve", ["mai"])
    assert result["pass_confidence"] == 0.9


@pytest.mark.parametrize("frontier_answer", [
    (None, "sol"),
    ({"reviewer": "sol", "verdict": "Approve", "confidence": 0.95, "escalated_to": "other"}, None),
])
def test_frontier_without_own_verdict_retries(ns, frontier_answer):
    """No frontier vote, or one answered by an escalated model, is queued for retry, not judged."""
    review_one, _ = _runner({
        "mai": ({"reviewer": "mai", "verdict": "Approve", "confidence": 0.9}, None),
        "sol": frontier_answer})
    result = ns["_run_review_ladder"](_seats(), review_one, 0.9)
    assert result["status"] == "queue_for_retry"
    assert result["seats"] == ["mai", "sol"]


# ---------------------------------------------------------------------------
# Test 5: REVIEW_MODEL_LADDER False -> _review_ladder returns None.
# ---------------------------------------------------------------------------
def test_knob_off_returns_none(ns, monkeypatch):
    """REVIEW_MODEL_LADDER=False -> _review_ladder returns None."""
    result = ns["_review_ladder"]({"REVIEW_MODEL_LADDER": False})
    assert result is None


# ---------------------------------------------------------------------------
# Test 6: builder model "claude-sonnet-5.5" -> frontier gpt-5.6-sol;
#         builder "gpt-5.6-sol" -> frontier claude-opus-5.5.
# ---------------------------------------------------------------------------
def test_frontier_claude_builder(ns, monkeypatch):
    """Builder contains 'claude' -> use anthropic_author (gpt-5.6-sol)."""
    ladder = ns["_review_ladder"](
        {}, builder_key="claude-sonnet-5.5",
    )
    assert ladder is not None
    assert ladder["frontier"]["model"] == "gpt-5.6-sol"


def test_frontier_other_builder(ns, monkeypatch):
    """Builder NOT containing 'claude' -> use other_author (claude-opus-5.5)."""
    ladder = ns["_review_ladder"](
        {}, builder_key="gpt-5.6-sol",
    )
    assert ladder is not None
    assert ladder["frontier"]["model"] == "claude-opus-5.5"


# ---------------------------------------------------------------------------
# Test 7: frontier model not among candidates -> _review_ladder returns None.
# ---------------------------------------------------------------------------
def test_frontier_not_available(ns, monkeypatch, tmp_path):
    """When no candidate matches the frontier model, _review_ladder returns None."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps({
        "review": {
            "ladder": ["mai-code-2-flash"],
            "pass_confidence": 0.9,
            "frontier": {
                "anthropic_author": "nonexistent-model-x",
                "other_author": "also-missing",
            },
        },
    }), encoding="utf-8")
    cands = [_cand("mai-code-2-flash"), _cand("other-model")]
    ns2 = _load(lambda cfg: cands, {"review": {
        "ladder": ["mai-code-2-flash"],
        "pass_confidence": 0.9,
        "frontier": {
            "anthropic_author": "nonexistent-model-x",
            "other_author": "also-missing",
        },
    }})
    monkeypatch.setitem(ns2, "MODEL_POLICY_FILE", str(policy))
    result = ns2["_review_ladder"]({})
    assert result is None


# ---------------------------------------------------------------------------
# Additional: _review_policy returns the review dict.
# ---------------------------------------------------------------------------
def test_review_policy_returns_dict(ns, monkeypatch):
    """_review_policy returns the review dict from the policy file."""
    policy = ns["_review_policy"]()
    assert "ladder" in policy
    assert "frontier" in policy
    assert "pass_confidence" in policy


# ---------------------------------------------------------------------------
# Additional: _resolve_builder returns (excluded, builder_model).
# ---------------------------------------------------------------------------
def test_resolve_builder(ns, monkeypatch):
    """_resolve_builder returns (excluded_set, builder_model)."""
    excluded, builder_model = ns["_resolve_builder"](
        {}, [_cand("mai-code-2-flash"), _cand("other")],
        builder_n=1, builder_key=None,
    )
    assert isinstance(excluded, set)
    assert isinstance(builder_model, str) or builder_model is None


@pytest.mark.parametrize("value,expected", [(0.5, 0.9), (0.95, 0.95), ("abc", None), (None, None)])
def test_pass_confidence_never_below_floor(ns, tmp_path, monkeypatch, value, expected):
    """A policy pass_confidence below 0.90 is raised to 0.90; a malformed one disables the ladder."""
    policy = json.loads(json.dumps(REVIEW_POLICY))
    policy["review"]["pass_confidence"] = value
    path = tmp_path / "p.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(path))
    ladder = ns["_review_ladder"]({})
    assert (ladder and ladder["pass_confidence"]) == expected


@pytest.mark.parametrize("builder, cands", [
    # Policy maps the builder to itself: no other-vendor handover.
    ("claude-opus-5.5", ["claude-opus-5.5", "gpt-5.6-sol"]),
    (("provider", "", "claude-opus-5.5"), ["claude-opus-5.5", "gpt-5.6-sol"]),
    # Claude builder, chosen frontier (GPT) unavailable, Opus available.
    ("claude-sonnet-5.5", ["claude-opus-5.5", "claude-sonnet-5.5"]),
])
def test_frontier_unusable_falls_back_to_panel(tmp_path, monkeypatch, builder, cands):
    """The builder never reviews itself and the other vendor is never swapped in."""
    anth = "claude-opus-5.5" if builder in ("claude-opus-5.5", ("provider", "", "claude-opus-5.5")) else "gpt-5.6-sol"
    data = {"review": {"ladder": ["mai-code-2-flash"], "pass_confidence": 0.9,
                       "frontier": {"anthropic_author": anth, "other_author": "claude-opus-5.5"}}}
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(data), encoding="utf-8")
    allc = [_cand("mai-code-2-flash")] + [_cand(m) for m in cands]
    ns2 = _load(lambda cfg: allc, data)
    monkeypatch.setitem(ns2, "MODEL_POLICY_FILE", str(policy))
    assert ns2["_review_ladder"]({}, builder_key=builder) is None


@pytest.mark.parametrize("builder, frontier", [("gpt-5.6-sol", "claude-opus-5.5"),
                                                ("claude-sonnet-5.5", "gpt-5.6-sol")])
def test_pr_fix_names_its_builder_to_the_ladder(ns, builder, frontier):
    """fix_one_pr passes the fix's model key, so the other vendor reviews it."""
    tree = ast.parse(open("pr_review.py", encoding="utf-8").read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fix_one_pr")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "review_fix"]
    assert calls and all(any(k.arg == "builder_key" for k in c.keywords) for c in calls)
    assert ns["_review_ladder"]({}, builder_key=("provider", "", builder))["frontier"]["model"] == frontier


def test_lm_passes_seat_before_cloud_filters_and_can_be_turned_off(ns):
    """Qwen then DeepSeek seat ahead of the cloud filter; an empty lm_llm_tasks removes them."""
    seats = ns["_review_ladder"]({})["filters"]
    assert [c["model"] for c in seats] == ["lm:review", "lm:review_deep", "mai-code-2-flash"]
    assert ns["_review_ladder"]({"lm_llm_tasks": {}})["filters"][0]["model"] == "mai-code-2-flash"
    only_qwen = ns["_review_ladder"]({"lm_llm_tasks": {"review": "review"}})["filters"]
    assert [c["model"] for c in only_qwen][0] == "lm:review"
    assert len(only_qwen) == 2


def test_lm_filter_reject_never_reaches_the_frontier(ns):
    """A Qwen Reject goes back to the coder without paying for DeepSeek or the frontier."""
    seats = [{"name": "qwen", "candidate": {"provider": "lm"}},
             {"name": "deep", "candidate": {"provider": "lm"}},
             {"name": "sol", "candidate": _cand("gpt-5.6-sol")}]
    review_one, asked = _runner({"qwen": ({"verdict": "Reject", "confidence": 0.8}, None)})
    result = ns["_run_review_ladder"](seats, review_one, 0.9)
    assert asked == ["qwen"] and result["ladder_stage"] == "filter"


def test_frontier_findings_go_to_the_local_coder_first(ns):
    """A frontier reject sends the findings to the local coder once; a filter
    reject, a first attempt, or an already-local builder does not."""
    f = ns["_frontier_rework_local"]
    frontier = {"kind": "review_rejected", "stage": "frontier"}
    assert f(frontier, {"provider": "claude"}) is True
    assert f(frontier, {}) is True
    # Already written by the local coder: the next try climbs to the cloud.
    assert f(frontier, {"provider": "lm"}) is False
    # A local filter seat rejected it, or it failed for another reason.
    assert f({"kind": "review_rejected", "stage": "filter"}, {}) is False
    assert f({"kind": "qa_failed"}, {}) is False
    assert f({}, None) is False


def test_apply_ai_fix_tries_lm_before_the_cloud_when_local_first():
    """apply_ai_fix asks lm_complete('fix') first only when local_first is set."""
    src = open("fix_engine.py", encoding="utf-8").read()
    body = src[src.index("def apply_ai_fix("):]
    body = body[:body.index("\ndef ", 10)]
    assert "local_first=False" in body
    assert body.index("if local_first:") < body.index("_reply = call_llm(")
    assert 'llm_client.lm_complete("fix"' in body
