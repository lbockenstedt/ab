"""Self-test: only frontier models may WRITE code, not just judge it.

The review panel has been restricted to an Opus-class allowlist for a long time
(`DEFAULT_PANEL_ALLOWLIST`). The model that authors the FIX was never held to
the same bar: `apply_ai_fix` built `LlmRequirements(complexity="large", ...)`
and handed it to a COST-FIRST picker, so the cheapest model clearing "large"
won the job.

In production that was a flash-class model, which answered with reasoning prose
("Wait! In promote.sh:") instead of the required JSON. Every such reply burned a
remediation attempt and got the model excluded for that PR, one PR at a time,
until it was blacklisted on 24 of 31 open review records while the real defects
went un-attempted.

A model trusted to author a change that lands across the fleet should be at
least as strong as one trusted to reject it, so the fix path now reuses the
panel's allowlist.
"""
import ast
import fnmatch
import json
import os
import re
import types

import pytest

_FIX_NAMES = {"_model_allowed", "_panel_allowlist", "_fix_allowlist",
              "_fix_model_exclusions", "_premium_allowlist"}
_FIX_ASSIGNS = {"DEFAULT_PANEL_ALLOWLIST", "DEFAULT_FIX_ALLOWLIST",
                "DEFAULT_FIX_EXTRA_ALLOWLIST", "DEFAULT_PREMIUM_FIX_ALLOWLIST"}


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load(enumerate_fn=None):
    """fix_engine cannot be imported directly (circular main.py chain), so the
    pieces under test are ast-extracted -- the convention in test_panel_allowlist."""
    src = open("fix_engine.py").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in _FIX_NAMES:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in _FIX_ASSIGNS for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    llm = types.SimpleNamespace(
        _enumerate_candidates=enumerate_fn or (lambda cfg: []))
    ns = {"re": re, "json": json, "os": os, "fnmatch": fnmatch,
          "logger": _NoLog(), "llm_client": llm}
    exec("\n\n".join(segs), ns)
    return ns


fix_engine = _load()


def _cand(model, key=None):
    return {"model": model, "key": key or ("k:" + model)}


# -- the bar itself --------------------------------------------------------

def test_writing_code_is_held_to_the_same_bar_as_judging_it():
    """The write bar is the panel bar, plus the premium escalation tier, plus the
    explicit write-only additions -- and nothing else.

    Premium sits ABOVE the panel bar (every premium model outranks Opus in
    model_registry), so that union widens the bar upward, never downward. It is
    required, not cosmetic: without it the premium narrowing and this floor
    intersect to an empty pool on the escalation turn and the tier is silently
    inert.

    DEFAULT_FIX_EXTRA_ALLOWLIST is the deliberate exception to "the write bar IS
    the judge bar": a model can be better at WRITING code than at judging it
    (Sonnet 5.5 outranks Opus 5 in model_registry for code work at a third of the
    multiplier). Admitting it by widening the PANEL instead would change who
    reviews every PR in the fleet, which is a different and much larger policy
    change -- so it is enumerated here and asserted below to stay out of the panel.
    """
    fix = fix_engine["DEFAULT_FIX_ALLOWLIST"]
    panel = fix_engine["DEFAULT_PANEL_ALLOWLIST"]
    premium = fix_engine["DEFAULT_PREMIUM_FIX_ALLOWLIST"]
    extra = fix_engine["DEFAULT_FIX_EXTRA_ALLOWLIST"]
    assert set(panel) <= set(fix)
    assert set(fix) - set(panel) == (set(premium) | set(extra)) - set(panel)
    # The panel bar itself is untouched: the panel runs on EVERY PR, so
    # admitting premium there would seat the most expensive models on the
    # roster for routine reviews.
    assert not (set(premium) & set(panel))
    # ...and the write-only additions must never leak into the panel either.
    assert not (set(extra) & set(panel))
    # A write-only addition is an ordinary-turn model, so it must NOT be
    # reserved premium as well -- that would make the premium turn re-seat a
    # model the ordinary turns already tried and burn the reserved attempt.
    assert not (set(extra) & set(premium))


def test_the_write_only_additions_are_version_pinned():
    """DEFAULT_FIX_EXTRA_ALLOWLIST widens the bar DOWNWARD in family terms (Sonnet
    sits below Opus for the generic "*sonnet*" registry rule), so each pattern must
    name an exact version. "claude-sonnet-5*" would silently re-admit plain Sonnet
    5 -- the model DEFAULT_LAST_TURN_FIX_ALLOWLIST documents as unable to resolve
    findings Opus resolved."""
    extra = fix_engine["DEFAULT_FIX_EXTRA_ALLOWLIST"]
    assert extra, "the write-only additions must not be empty"
    for pat in extra:
        stem = pat.rstrip("*")
        assert re.search(r"\d+\.\d+$", stem), (
            "%r must be pinned to a major.minor version, not a whole family" % pat)


def test_sonnet_5_5_may_write_but_older_sonnets_may_not():
    """The point of the whole change: Sonnet 5.5 is admitted to write code, and
    every earlier Sonnet stays out."""
    patterns = fix_engine["DEFAULT_FIX_ALLOWLIST"]
    assert fix_engine["_model_allowed"]("claude-sonnet-5.5", patterns) is True
    assert fix_engine["_model_allowed"]("claude-sonnet-5.5-20260101", patterns) is True
    assert fix_engine["_model_allowed"]("anthropic/claude-sonnet-5.5", patterns) is True
    for old in ("claude-sonnet-5", "claude-sonnet-4.5", "claude-sonnet-5.1"):
        assert fix_engine["_model_allowed"](old, patterns) is False, (
            "%s must still be barred from writing code" % old)


def test_sonnet_5_5_is_not_admitted_to_the_review_panel():
    """Widening the write bar must not change who REVIEWS the fleet's PRs."""
    panel = fix_engine["_panel_allowlist"]({})
    assert fix_engine["_model_allowed"]("claude-sonnet-5.5", panel) is False
    assert fix_engine["_model_allowed"]("claude-opus-5", panel) is True


def test_the_default_bar_is_opus_class_or_better():
    """The rule is "Opus or better" -- pin it so a later edit cannot quietly
    admit a cheaper family."""
    patterns = fix_engine["DEFAULT_FIX_ALLOWLIST"]
    assert patterns, "the fix allowlist must not default to disabled"
    for weak in ("gemini-3.8-flash", "gemini-2.0-flash", "claude-haiku-4.5",
                 "claude-sonnet-5", "gpt-5-mini", "qwen3-coder"):
        assert not fix_engine["_model_allowed"](weak, patterns), (
            "%s must not be allowed to write code" % weak)
    for strong in ("claude-opus-5", "claude-opus-5.5", "claude-opus-6"):
        assert fix_engine["_model_allowed"](strong, patterns), (
            "%s is Opus class and must be allowed" % strong)


def test_vendor_prefixes_do_not_smuggle_a_weak_model_through():
    patterns = fix_engine["DEFAULT_FIX_ALLOWLIST"]
    assert fix_engine["_model_allowed"]("anthropic/claude-opus-5", patterns) is True
    assert fix_engine["_model_allowed"]("google/gemini-3.8-flash", patterns) is False


# -- the allowlist resolver ------------------------------------------------

def test_absent_config_uses_the_default():
    assert fix_engine["_fix_allowlist"]({}) == fix_engine["DEFAULT_FIX_ALLOWLIST"]
    assert fix_engine["_fix_allowlist"](None) == fix_engine["DEFAULT_FIX_ALLOWLIST"]


def test_config_can_override_with_a_list_or_a_string():
    # The operator's override replaces the DEFAULT bar; the premium tier is
    # still unioned on, because a premium model the fix floor rejects could
    # never write the escalation fix it was picked for.
    premium = fix_engine["DEFAULT_PREMIUM_FIX_ALLOWLIST"]
    assert fix_engine["_fix_allowlist"](
        {"pr_fix_model_allowlist": ["Claude-Opus-9*"]}) == ("claude-opus-9*",) + premium
    assert fix_engine["_fix_allowlist"](
        {"pr_fix_model_allowlist": "a*, b*"}) == ("a*", "b*") + premium
    # An operator-named premium list overrides the default one too.
    assert fix_engine["_fix_allowlist"]({
        "pr_fix_model_allowlist": ["opus*"],
        "pr_remediate_premium_model_allowlist": ["zeta*"]}) == ("opus*", "zeta*")
    # An explicit empty fix list still DISABLES the policy outright.
    assert fix_engine["_fix_allowlist"]({"pr_fix_model_allowlist": []}) == ()


def test_an_explicit_empty_list_disables_the_policy():
    assert fix_engine["_fix_allowlist"]({"pr_fix_model_allowlist": []}) == ()


# -- the exclusions --------------------------------------------------------

def test_non_allowlisted_models_are_excluded():
    ns = _load(lambda cfg: [_cand("claude-opus-5"),
                            _cand("gemini-3.8-flash"),
                            _cand("claude-haiku-4.5")])
    excl = ns["_fix_model_exclusions"]({})
    assert "k:gemini-3.8-flash" in excl
    assert "k:claude-haiku-4.5" in excl
    assert "k:claude-opus-5" not in excl, "an Opus model must stay eligible"


def test_existing_exclusions_are_preserved():
    ns = _load(lambda cfg: [_cand("claude-opus-5")])
    excl = ns["_fix_model_exclusions"]({}, existing=("k:already-tried",))
    assert "k:already-tried" in excl, (
        "the per-PR failed-model exclusions must survive the floor")


def test_disabled_policy_excludes_nothing():
    ns = _load(lambda cfg: [_cand("gemini-3.8-flash")])
    assert ns["_fix_model_exclusions"]({"pr_fix_model_allowlist": []}) == ()


def test_enumeration_failure_never_blocks_fixing():
    """Degrade to the old behaviour rather than excluding everything, which
    would leave the picker with no candidate at all."""
    def _boom(cfg):
        raise RuntimeError("registry down")
    ns = _load(_boom)
    assert ns["_fix_model_exclusions"]({}, existing=("k:x",)) == ("k:x",)


# -- the wiring ------------------------------------------------------------

def test_apply_ai_fix_applies_the_floor():
    """Pin the call site: the floor is worthless if the generator never uses it."""
    src = open("fix_engine.py", encoding="utf-8").read()
    head = src.index("def apply_ai_fix")
    body = src[head:head + 12000]
    assert "_fix_model_exclusions(" in body, (
        "apply_ai_fix must apply the fix-model floor")
    assert "exclude_models=_fix_excl" in body
