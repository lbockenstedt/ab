"""Reviewer-panel model allowlist (frontier-only policy).

fix_engine.py / pr_review.py cannot be imported directly (circular main.py chain /
heavy imports), so the pieces under test are extracted via ast and exec'd with
stubbed dependencies -- the convention used by test_fix_engine_reviewer_panel.py.

Covers: _model_allowed, _panel_allowlist, _select_review_panel (allowlist mode and
the legacy escape hatch), review_fix's no-frontier-reviewer refusal, reviewer
naming, and pr_review's single-reviewer-panel notice.
"""
import ast
import fnmatch
import json
import os
import re

import pytest

_PANEL_NAMES = {
    "_select_review_panel", "review_fix", "_panel_allowlist", "_model_allowed",
    "_reviewer_name", "_reviewer_vote", "_extract_reviewer_verdict",
    "_has_verdict_key", "_canon_verdict_keys", "_balanced_brace_span",
    "_is_local_provider", "_cloud_frontier_fallback",
}
_PANEL_ASSIGNS = {
    "_REVIEW_PANEL_MAX", "_REVIEW_PANEL_MIN", "DEFAULT_PANEL_ALLOWLIST",
    "_REVIEWER_RETRY_NOTE", "_LOCAL_PROVIDERS",
}


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


class _RecLog:
    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def _log(msg, *a, **k):
            formatted = (str(msg) % a) if a else str(msg)
            self.records.append((level, formatted))
        return _log


def _load_fix_engine(extra_ns=None):
    src = open("fix_engine.py").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in _PANEL_NAMES:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in _PANEL_ASSIGNS for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    ns = {"re": re, "json": json, "os": os, "fnmatch": fnmatch, "logger": _NoLog()}
    ns.update(extra_ns or {})
    exec("\n\n".join(segs), ns)
    return ns


def _cand(provider, model, tier="cheap", max_complexity="large"):
    return {
        "key": (provider, "", model), "provider": provider, "model": model,
        "base_url": "", "api_key": "", "rpm": 0, "available": True, "unavailable_reason": None,
        "caps": {"supports_tools": True, "native_agentic_tools": False,
                 "supports_mutating_agent": False, "supports_structured_output": True,
                 "cost_tier": tier, "max_complexity": max_complexity, "context_window": 200000},
    }


def _production():
    return [
        _cand("copilot", "claude-haiku-4.5", max_complexity="medium"),
        _cand("copilot", "claude-sonnet-5", tier="frontier"),
        _cand("copilot", "claude-opus-5", tier="frontier"),
        _cand("copilot", "gpt-4o-2024-08-06", max_complexity="medium"),
        _cand("copilot", "gemini-3.7-flash"),
        _cand("copilot", "gpt-5-mini", max_complexity="small"),
    ]


class _FakeLlm:
    def __init__(self, candidates):
        self._c = candidates

    def _enumerate_candidates(self, config):
        return list(self._c)

    def get_llm_perf_snapshot(self):
        return {}

    def _model_key(self, provider, base_url, model):
        return (provider, base_url, model)


def _engine(candidates, **extra):
    ns = {"llm_client": _FakeLlm(candidates), "_get_provider_config": lambda n, c: (None,) * 4,
          "load_config": lambda: {}}
    ns.update(extra)
    return _load_fix_engine(ns)


def _models(panel):
    return [c["model"] for c in panel]


# ---- 1. _model_allowed ----
@pytest.mark.parametrize("model", [
    "claude-opus-5", "claude-opus-5.1", "anthropic/claude-opus-6", "gpt-5.6-sol", "GPT-5.6-SOL",
    "gpt-5.6-sol-preview"])
def test_model_allowed_true(model):
    ns = _load_fix_engine()
    assert ns["_model_allowed"](model, ns["DEFAULT_PANEL_ALLOWLIST"])


@pytest.mark.parametrize("model", [
    "claude-sonnet-5", "claude-haiku-4.5", "gemini-3.7-flash", "gpt-5-mini", "gpt-4o-2024-08-06",
    "claude-opus-4", "gpt-5.5", "", None])
def test_model_allowed_false(model):
    ns = _load_fix_engine()
    assert not ns["_model_allowed"](model, ns["DEFAULT_PANEL_ALLOWLIST"])


def test_model_allowed_empty_patterns_is_false():
    assert not _load_fix_engine()["_model_allowed"]("claude-opus-5", ())


# ---- 1b. _is_local_provider ----
@pytest.mark.parametrize("provider", ["ollama", "ollama2", "Ollama", " OLLAMA2 "])
def test_is_local_provider_true(provider):
    assert _load_fix_engine()["_is_local_provider"](provider)


@pytest.mark.parametrize("provider", ["ollama_cloud", "copilot", "anthropic", "", None])
def test_is_local_provider_false(provider):
    assert not _load_fix_engine()["_is_local_provider"](provider)


# ---- 2. _panel_allowlist ----
def test_panel_allowlist_parsing():
    ns = _load_fix_engine()
    f, default = ns["_panel_allowlist"], ns["DEFAULT_PANEL_ALLOWLIST"]
    assert f({}) == default
    assert f({"pr_review_panel_allowlist": None}) == default
    assert f({"pr_review_panel_allowlist": ["Foo-1*", " ", "BAR"]}) == ("foo-1*", "bar")
    assert f({"pr_review_panel_allowlist": []}) == ()
    assert f({"pr_review_panel_allowlist": "Claude-Opus-5*, gpt-5.6-sol*"}) == (
        "claude-opus-5*", "gpt-5.6-sol*")


# ---- 3. _select_review_panel ----
def test_default_allowlist_seats_only_opus_no_backfill():
    ns = _engine(_production())
    assert _models(ns["_select_review_panel"]({}, builder_n=0)) == ["claude-opus-5"]


def test_second_allowlisted_model_joins_panel():
    ns = _engine(_production() + [_cand("copilot", "gpt-5.6-sol", tier="frontier")])
    assert set(_models(ns["_select_review_panel"]({}, builder_n=0))) == {
        "claude-opus-5", "gpt-5.6-sol"}


def test_empty_allowlist_keeps_legacy_picker():
    # Documents the pre-policy behaviour: weak Flash model votes alongside the frontier ones.
    ns = _engine(_production())
    panel = _models(ns["_select_review_panel"]({"pr_review_panel_allowlist": []}, builder_n=0))
    assert {"gemini-3.7-flash", "claude-opus-5", "claude-sonnet-5"} <= set(panel)


def test_custom_allowlist_seats_only_that_model():
    ns = _engine(_production())
    panel = ns["_select_review_panel"]({"pr_review_panel_allowlist": ["gemini-3.7-flash"]}, builder_n=0)
    assert _models(panel) == ["gemini-3.7-flash"]


# ---- 3b. _select_review_panel local Ollama backfill ----
def test_local_backfill_reaches_minimum_and_stops_there():
    cands = [
        _cand("copilot", "claude-opus-5", tier="frontier"),
        _cand("ollama", "qwen-something"),
        _cand("ollama2", "llama-something"),
        _cand("ollama", "third-local-model"),
    ]
    ns = _engine(cands)
    panel = ns["_select_review_panel"]({}, builder_n=0)
    assert _models(panel)[0] == "claude-opus-5"
    assert len(panel) == 2  # _REVIEW_PANEL_MIN — the 3rd local candidate is not seated
    assert panel[1]["provider"] in ("ollama", "ollama2")


def test_local_backfill_does_not_run_when_frontier_alone_reaches_minimum():
    cands = [
        _cand("copilot", "claude-opus-5", tier="frontier"),
        _cand("copilot", "claude-opus-6", tier="frontier"),
        _cand("ollama", "qwen-something"),
    ]
    ns = _engine(cands)
    panel = ns["_select_review_panel"]({}, builder_n=0)
    assert set(_models(panel)) == {"claude-opus-5", "claude-opus-6"}
    assert all(c["provider"] != "ollama" for c in panel)


def test_no_local_candidates_leaves_panel_short_no_crash():
    log = _RecLog()
    ns = _engine(_production(), logger=log)  # single frontier candidate, no local providers configured
    panel = ns["_select_review_panel"]({}, builder_n=0)
    assert _models(panel) == ["claude-opus-5"]
    warns = [m for lvl, m in log.records if lvl == "warning"]
    assert any("only 1 of 2 reviewer seat(s) filled (frontier allowlist):" in m for m in warns)
    assert not any("local Ollama backfill" in m for m in warns)


def test_short_panel_with_local_backfill_logs_both_clauses():
    # 0 frontier candidates, but 1 local candidate available: panel has 1 local reviewer (< 2 minimum)
    cands = [_cand("ollama", "qwen-local")]
    log = _RecLog()
    ns = _engine(cands, logger=log)
    panel = ns["_select_review_panel"]({}, builder_n=0)
    assert _models(panel) == ["qwen-local"]
    warns = [m for lvl, m in log.records if lvl == "warning"]
    assert any("only 1 of 2 reviewer seat(s) filled (frontier allowlist + local Ollama backfill):" in m for m in warns)


# ---- 3c. _cloud_frontier_fallback ----
def _fallback_mix():
    return [
        _cand("ollama", "local-model"),
        _cand("copilot", "claude-opus-5", tier="frontier"),
        _cand("copilot", "claude-sonnet-5", tier="frontier"),
    ]


def test_cloud_frontier_fallback_returns_non_local_allowlisted_candidate():
    ns = _engine(_fallback_mix())
    c = ns["_cloud_frontier_fallback"](set(), {})
    assert c is not None and c["model"] == "claude-opus-5" and c["provider"] != "ollama"


def test_cloud_frontier_fallback_none_when_all_allowlisted_excluded():
    ns = _engine(_fallback_mix())
    excluded = {c["key"] for c in _fallback_mix() if c["model"] == "claude-opus-5"}
    assert ns["_cloud_frontier_fallback"](excluded, {}) is None


def test_cloud_frontier_fallback_none_when_no_allowlisted_candidate_exists():
    ns = _engine([_cand("ollama", "local-model"), _cand("copilot", "gpt-4o-2024-08-06")])
    assert ns["_cloud_frontier_fallback"](set(), {}) is None


def test_cloud_frontier_fallback_none_when_allowlist_empty():
    ns = _engine(_fallback_mix())
    assert ns["_cloud_frontier_fallback"](set(), {"pr_review_panel_allowlist": []}) is None


# ---- 4. review_fix ----
def _boom(*a, **k):
    raise AssertionError("reviewer LLM must not be invoked")


def test_review_fix_no_allowlisted_model_queues_for_retry():
    cands = [_cand("copilot", "claude-sonnet-5", tier="frontier"), _cand("copilot", "gemini-3.7-flash")]
    ns = _engine(cands, _run_reviewer_turn=_boom)
    res = ns["review_fix"](None, "issue", {"a.py": "x"}, builder_n=0, diff_override="diff")
    assert res["status"] == "queue_for_retry"
    assert res["reason"].startswith("no_frontier_reviewer")


def test_review_fix_empty_allowlist_uses_default_reviewer():
    ns = _engine([], load_config=lambda: {"pr_review_panel_allowlist": []},
                 _run_reviewer_turn=lambda *a, **k: '{"confidence": 0.9, "verdict": "Approve", "critique": "ok"}',
                 _parse_reviewer_json=json.loads, _norm_confidence=float,
                 is_llm_cooldown_error=lambda e: False)
    res = ns["review_fix"](None, "issue", {"a.py": "x"}, builder_n=0, diff_override="diff")
    assert res.get("status") != "queue_for_retry"
    assert res["panel_size"] == 1


def test_review_fix_success_reports_panel_models():
    ns = _engine(_production(),
                 _run_reviewer_turn=lambda *a, **k: '{"confidence": 0.9, "verdict": "Approve", "critique": "ok"}',
                 _parse_reviewer_json=json.loads, _norm_confidence=float,
                 is_llm_cooldown_error=lambda e: False)
    res = ns["review_fix"](None, "issue", {"a.py": "x"}, builder_n=0, diff_override="diff")
    assert res["panel_size"] == 1 and res["panel_models"] == ["claude-opus-5"]
    assert res["reviews"][0]["reviewer"] == "Reviewer (copilot/claude-opus-5)"


# ---- 5. reviewer naming ----
def test_reviewer_name_includes_model_and_round_trips_tag_regex():
    name = _load_fix_engine()["_reviewer_name"](_cand("copilot", "claude-opus-5"))
    assert name == "Reviewer (copilot/claude-opus-5)"
    tag_re = re.compile(r"^\[([^\]]{1,60})\]\s*")  # mirrors pr_review._REVIEWER_TAG_RE
    assert tag_re.match("[%s] critique" % name).group(1) == name
    long_name = _load_fix_engine()["_reviewer_name"](_cand("p" * 30, "m" * 80))
    assert tag_re.match("[%s] x" % long_name).group(1) == long_name


def test_reviewer_name_without_model_uses_provider():
    assert _load_fix_engine()["_reviewer_name"]({"provider": "copilot", "model": ""}) == "Reviewer (copilot)"


# ---- 6. pr_review renderer ----
def _load_pr_review():
    src = open("pr_review.py").read()
    ns = {"re": re, "os": os, "json": json, "logger": _NoLog()}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.Assign)):
            try:
                exec(compile(ast.Module([node], []), "pr_review.py", "exec"), ns)
            except Exception:  # noqa: BLE001 - only the renderer's own helpers matter here
                pass
    return ns


def _review(**extra):
    r = {"confidence": 0.9, "verdict": "Approve", "critique": "[Reviewer (copilot/claude-opus-5)] fine",
         "reviews": [{"reviewer": "Reviewer (copilot/claude-opus-5)", "verdict": "Approve",
                      "confidence": 0.9, "critique": "fine"}]}
    r.update(extra)
    return r


def test_single_reviewer_notice_rendered_only_for_panel_of_one():
    render = _load_pr_review()["_render_panel"]
    one = "\n".join(render(_review(panel_size=1, panel_models=["claude-opus-5"])))
    assert ("_Single-reviewer panel: only 1 of 2 required frontier models was available "
            "(claude-opus-5)._") in one
    assert "Single-reviewer" not in "\n".join(render(_review(panel_size=2)))
    assert "Single-reviewer" not in "\n".join(render(_review()))
