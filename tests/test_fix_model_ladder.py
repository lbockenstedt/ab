"""Self-test: fixes climb the fleet coding ladder, one rung per failed attempt.

`.github/model-policy.json` lists the coding ladder cheapest-first. The first
ordinary fix seats the cheapest rung; each failed attempt (the caller passes the
tried models back in `existing`) seats the next one. When every rung is used,
or the knob or the policy file is absent, the existing cost-first picker runs.

fix_engine cannot be imported directly (circular main.py chain), so the pieces
under test are ast-extracted, the same convention as test_fix_model_floor.
"""
import ast
import fnmatch
import json
import os
import re
import types

import pytest

_FIX_NAMES = {"_model_allowed", "_ladder_models", "_ladder_exclusions"}
_FIX_ASSIGNS = {"MODEL_POLICY_FILE"}


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load(enumerate_fn):
    """Exec the ladder helpers in a stand-in namespace with a fake enumerator."""
    src = open("fix_engine.py", encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in _FIX_NAMES:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in _FIX_ASSIGNS for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    llm = types.SimpleNamespace(_enumerate_candidates=enumerate_fn)
    ns = {"json": json, "os": os, "re": re, "fnmatch": fnmatch, "logger": _NoLog(),
          "llm_client": llm, "__file__": os.path.abspath("fix_engine.py")}
    exec("\n\n".join(segs), ns)
    return ns


def _cand(model):
    return {"model": model, "key": ("copilot", "", model)}


HAIKU = _cand("claude-haiku-5.5")
SONNET = _cand("claude-sonnet-5.5")
OPUS = _cand("claude-opus-5.5")
GPT = _cand("gpt-5.6-sol")
LADDER = {"coding": {"ladder": [
    {"agent": "cloud-coder", "model": "claude-haiku-5.5"},
    {"agent": "cloud-coder-plus", "model": "claude-sonnet-5.5"},
    {"agent": None, "model": "claude-opus-5.5"}]}, "other": {}}


@pytest.fixture
def ns(tmp_path, monkeypatch):
    """A namespace whose policy file is a tmp file holding LADDER, with all four candidates."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    ns = _load(lambda cfg: [HAIKU, SONNET, OPUS, GPT])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    return ns


def test_first_call_seats_the_cheapest_rung(ns):
    """With nothing tried, haiku is seated: sonnet, opus and gpt are barred."""
    excl = ns["_ladder_exclusions"]({}, existing=())
    assert HAIKU["key"] not in excl
    assert SONNET["key"] in excl and OPUS["key"] in excl and GPT["key"] in excl


def test_each_failed_attempt_moves_up_one_rung(ns):
    """A tried haiku (by key) seats sonnet; a tried sonnet (by model name) seats opus."""
    excl = ns["_ladder_exclusions"]({}, existing=(HAIKU["key"],))
    assert SONNET["key"] not in excl and OPUS["key"] in excl and HAIKU["key"] in excl
    excl = ns["_ladder_exclusions"]({}, existing=(HAIKU["key"], "claude-sonnet-5.5"))
    assert OPUS["key"] not in excl and SONNET["key"] in excl


def test_every_rung_used_returns_none(ns):
    """All three rungs tried: None, so the cost-first picker takes over."""
    tried = (HAIKU["key"], SONNET["key"], OPUS["key"])
    assert ns["_ladder_exclusions"]({}, existing=tried) is None


def test_missing_policy_or_knob_off_returns_none(ns, tmp_path, monkeypatch):
    """No policy file, or FIX_MODEL_LADDER off: None, so the existing picker runs."""
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(tmp_path / "absent.json"))
    assert ns["_ladder_exclusions"]({}) is None
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(tmp_path / "model-policy.json"))
    assert ns["_ladder_exclusions"]({"FIX_MODEL_LADDER": False}) is None


def test_rung_falls_back_to_an_enabled_version_of_the_same_family(tmp_path, monkeypatch):
    """With only claude-haiku-4.5 enabled, the haiku-5.5 rung seats it instead of jumping to sonnet;
    an exact match still wins when present, and unrelated families never match."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    old_haiku = _cand("claude-haiku-4.5")
    ns = _load(lambda cfg: [old_haiku, SONNET, OPUS, GPT])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    excl = ns["_ladder_exclusions"]({}, existing=())
    assert old_haiku["key"] not in excl and SONNET["key"] in excl
    excl = ns["_ladder_exclusions"]({}, existing=(old_haiku["key"],))
    assert SONNET["key"] not in excl and old_haiku["key"] in excl
    ns = _load(lambda cfg: [old_haiku, HAIKU, SONNET])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    assert HAIKU["key"] not in ns["_ladder_exclusions"]({}, existing=())


def test_unconfigured_rung_is_skipped(tmp_path, monkeypatch):
    """A ladder rung with no configured candidate is skipped: with no haiku, sonnet is seated."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    ns = _load(lambda cfg: [SONNET, OPUS, GPT])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    excl = ns["_ladder_exclusions"]({}, existing=())
    assert SONNET["key"] not in excl
    assert OPUS["key"] in excl and GPT["key"] in excl


def test_unavailable_rung_is_skipped_not_seated(tmp_path, monkeypatch):
    """A rate-limited or dead Haiku is not seated: the ladder moves on to sonnet."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    unavailable = dict(HAIKU, available=False)
    ns = _load(lambda cfg: [unavailable, SONNET, OPUS, GPT])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    excl = ns["_ladder_exclusions"]({})
    assert SONNET["key"] not in excl
    assert HAIKU["key"] in excl


def test_a_second_endpoint_of_a_tried_model_does_not_cost_a_rung(tmp_path, monkeypatch):
    """Copilot Haiku tried: the Anthropic Haiku endpoint is barred too, and sonnet is seated."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    anthropic_haiku = {"model": "claude-haiku-5.5", "key": ("anthropic", "", "claude-haiku-5.5")}
    ns = _load(lambda cfg: [HAIKU, anthropic_haiku, SONNET, OPUS])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    excl = ns["_ladder_exclusions"]({}, existing=(HAIKU["key"],))
    assert SONNET["key"] not in excl
    assert anthropic_haiku["key"] in excl


def test_dated_model_id_matches_its_rung(tmp_path, monkeypatch):
    """A provider's dated id (claude-sonnet-5.5-20260101) counts as the sonnet rung."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    dated = _cand("claude-sonnet-5.5-20260101")
    ns = _load(lambda cfg: [dated, OPUS, GPT])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    excl = ns["_ladder_exclusions"]({}, existing=())
    assert dated["key"] not in excl and OPUS["key"] in excl


def test_malformed_ladder_entry_falls_back(ns, tmp_path, monkeypatch):
    """A ladder entry that is not an object (null, a string) means no ladder, not a crash."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"coding": {"ladder": [None, "claude-haiku-5.5"]}}), encoding="utf-8")
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(bad))
    assert ns["_ladder_exclusions"]({}) is None


def test_ladder_climbs_monotonically_past_a_recovered_lower_rung(tmp_path, monkeypatch):
    """Haiku skipped while cooling, sonnet tried and failed: opus is seated, not haiku again."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    ns = _load(lambda cfg: [HAIKU, SONNET, OPUS])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    excl = ns["_ladder_exclusions"]({}, existing=(SONNET["key"],))
    assert OPUS["key"] not in excl
    assert HAIKU["key"] in excl


def test_failed_endpoints_are_reported_by_llm_client_and_replayed_by_fix_engine():
    """A failed endpoint is recorded in llm_client and replayed into fix_engine's retry."""
    llm_src = open("llm_client.py", encoding="utf-8").read()
    fix_src = open("fix_engine.py", encoding="utf-8").read()
    assert llm_src.count('"failed_keys"') >= 2
    assert fix_src.count('"failed_keys"') >= 1


def test_tried_dated_endpoint_uses_the_whole_rung(tmp_path, monkeypatch):
    """Two providers with different dated Sonnet ids: once one is tried, the rung is used and Opus is seated."""
    policy = tmp_path / "model-policy.json"
    policy.write_text(json.dumps(LADDER), encoding="utf-8")
    s1 = {"model": "claude-sonnet-5.5-20260101", "key": ("anthropic", "", "claude-sonnet-5.5-20260101")}
    s2 = {"model": "claude-sonnet-5.5-20260301", "key": ("bedrock", "", "claude-sonnet-5.5-20260301")}
    ns = _load(lambda cfg: [HAIKU, s1, s2, OPUS])
    monkeypatch.setitem(ns, "MODEL_POLICY_FILE", str(policy))
    excl = ns["_ladder_exclusions"]({}, existing=(HAIKU["key"], s1["key"]))
    assert OPUS["key"] not in excl and s2["key"] in excl


def test_queued_review_keeps_its_builder_model():
    """A fix queued for review saves its builder's key and a resume restores it, so a rejection still climbs."""
    src = open("fix_engine.py", encoding="utf-8").read()
    assert '"builder_key": list(builder_key) if builder_key else None' in src
    assert 'used_model_out["key"] = tuple(pending_fix["builder_key"])' in src


def test_explicit_provider_restriction_skips_the_ladder():
    """'Retry with Claude' or local keeps its provider: a rung outside the restriction would leave no model."""
    src = open("fix_engine.py", encoding="utf-8").read()
    head = src.index("def apply_ai_fix")
    body = src[head:src.index("\ndef ", head + 1)]
    gate = body[body.index("_ladder = None"):body.index("_ladder_exclusions(config")]
    assert "not requirements.restrict" in gate and "not requirements.pin_key" in gate
