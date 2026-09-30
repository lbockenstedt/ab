"""Self-test: Sonnet 5.5 is the model the fix ladder seats FIRST.

Sonnet 5.5 outperforms Opus 5 on code generation and repair at roughly a third of
the Copilot request multiplier, so it should author the first remediation attempt
on every PR, with Opus kept in reserve for the turns where the cheaper-and-better
model has already failed.

Two independent things had to change for that to actually happen, and a test that
only checked one of them would pass while the feature stayed inert:

  1. **The write bar** (`fix_engine.DEFAULT_FIX_ALLOWLIST`) previously WAS the
     review-panel bar, which is Opus-class only, so Sonnet could not write code at
     all.
  2. **The ranking.** `model_selection._rank_tier` sorts a `complexity="large"`
     request by `capability_rank` FIRST, and Sonnet matched only the generic
     `*sonnet*` registry rule at rank 78 -- below Opus at 95. Widening the
     allowlist alone would have admitted Sonnet 5.5 to the pool and then never
     picked it.

These tests therefore drive the REAL `model_registry.resolve` and
`model_selection.select_model`, not a restatement of the intended ordering.
"""
import ast
import fnmatch
import json
import os
import re
import types

import pytest

import model_registry as reg
from model_selection import LlmRequirements, select_model


# --------------------------------------------------------------------------
# Load the real allowlist policy out of fix_engine (which cannot be imported
# directly -- circular main.py chain), exactly as test_fix_model_floor does.
# --------------------------------------------------------------------------
_FNS = {"_model_allowed", "_panel_allowlist", "_fix_allowlist", "_premium_allowlist"}
_ASSIGNS = {"DEFAULT_PANEL_ALLOWLIST", "DEFAULT_FIX_ALLOWLIST",
            "DEFAULT_FIX_EXTRA_ALLOWLIST", "DEFAULT_PREMIUM_FIX_ALLOWLIST",
            "DEFAULT_LAST_TURN_FIX_ALLOWLIST"}


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load():
    src = open("fix_engine.py").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in _FNS:
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in _ASSIGNS for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    ns = {"re": re, "json": json, "os": os, "fnmatch": fnmatch, "logger": _NoLog(),
          "llm_client": types.SimpleNamespace(_enumerate_candidates=lambda cfg: [])}
    exec("\n\n".join(segs), ns)
    missing = (_FNS | _ASSIGNS) - set(ns)
    assert not missing, "fix_engine no longer defines: %s" % sorted(missing)
    return ns


FIX = _load()

SONNET_55 = "claude-sonnet-5.5"
OPUS_5 = "claude-opus-5"

#: A realistic Copilot roster: the models AppBuilder has actually been observed
#: routing to on LM-AB, plus the older Sonnet that must stay out.
FLEET = [OPUS_5, "claude-opus-5.5", "claude-sonnet-5", SONNET_55,
         "gpt-5.6-terra", "claude-fable-5.1", "gpt-6-astra", "claude-haiku-4.5"]


def _candidates(config=None):
    return [{"key": ("copilot", None, m), "provider": "copilot", "model": m,
             "api_key": "k", "base_url": None, "rpm": 0, "available": True,
             "caps": reg.resolve("copilot", m, config or {})}
            for m in FLEET]


def _allowed(model, patterns):
    return FIX["_model_allowed"](model, patterns)


def _pick(exclude_keys, config=None):
    """What the picker seats for a fix request with `exclude_keys` barred."""
    cands = _candidates(config)
    sel = select_model(
        LlmRequirements(complexity="large", needs_structured_output=True,
                        exclude_models=tuple(exclude_keys)),
        cands)
    return None if sel is None else sel.model


def _keys_failing(patterns, config=None):
    """Keys to exclude so that ONLY models matching `patterns` remain -- the shape
    both _fix_model_exclusions and _last_turn_fix_exclusions produce."""
    return [c["key"] for c in _candidates(config) if not _allowed(c["model"], patterns)]


def _ordinary_turn_exclusions(config=None):
    """The exclusion set an ORDINARY remediation turn builds: the write bar, minus
    the premium tier (which _premium_fix_exclusions(want_premium=False) bars)."""
    fix = FIX["_fix_allowlist"](config or {})
    premium = FIX["_premium_allowlist"](config or {})
    return [c["key"] for c in _candidates(config)
            if not _allowed(c["model"], fix) or _allowed(c["model"], premium)]


# -- the registry rating ---------------------------------------------------

def test_sonnet_5_5_outranks_opus_in_the_registry():
    """The ranking is the half of the change that actually decides the pick."""
    s = reg.resolve("copilot", SONNET_55, {})
    o = reg.resolve("copilot", OPUS_5, {})
    assert reg.capability_rank(s) > reg.capability_rank(o), (
        "Sonnet 5.5 must outrank Opus or the picker will never seat it first")
    # Same cost tier, or the tier grouping would decide it instead of capability
    # and this would silently become a cost change rather than a quality one.
    assert s["cost_tier"] == o["cost_tier"] == "frontier"
    assert s["max_complexity"] == "large"


def test_the_pin_does_not_promote_older_sonnets():
    """`claude-sonnet-5.5*` is more specific than `*sonnet*` (specificity 17 vs 6),
    so only 5.5 is re-rated; Sonnet 5 must keep its rank-78 rating BELOW Opus."""
    old = reg.resolve("copilot", "claude-sonnet-5", {})
    assert reg.capability_rank(old) < reg.capability_rank(reg.resolve("copilot", OPUS_5, {}))
    assert reg.capability_rank(old) < reg.capability_rank(reg.resolve("copilot", SONNET_55, {}))


def test_the_pinned_rule_wins_on_specificity_not_list_order():
    """Pin the resolution mechanism: if this ever relied on list order, appending an
    operator rule could silently flip Sonnet 5.5 back to the generic rating."""
    rules = reg.find_matching_rules("copilot", SONNET_55, {})
    ids = {r["id"] for r in rules}
    assert {"copilot-sonnet-5.5", "copilot-sonnet"} <= ids, (
        "both rules should MATCH; specificity decides which one wins")
    assert reg.resolve("copilot", SONNET_55, {})["capability_rank"] == 96


def test_sonnet_5_5_is_seeded_into_an_already_persisted_registry():
    """The bug this guards is silent and total: an install that already has a
    "model_registry" key never reads DEFAULT_MODEL_RULES, so a rule that is only
    added to the defaults never reaches production. LM-AB is exactly that install."""
    persisted = [r for r in reg.DEFAULT_MODEL_RULES
                 if r.get("id") not in ("copilot-sonnet-5.5",)]
    topped_up, added = reg.upgrade_copilot_model_rules(persisted)
    assert "copilot-sonnet-5.5" in added, (
        "copilot-sonnet-5.5 must be in _COPILOT_MODEL_RULE_IDS or it will never "
        "be seeded onto a host that already persisted a registry")
    got = reg.resolve("copilot", SONNET_55, {"model_registry": topped_up})
    assert reg.capability_rank(got) == 96


# -- the write bar ---------------------------------------------------------

def test_sonnet_5_5_is_allowed_to_write_code():
    assert _allowed(SONNET_55, FIX["_fix_allowlist"]({})) is True


def test_sonnet_5_5_is_not_allowed_to_review():
    """Widening the write bar must not change who judges the fleet's PRs."""
    assert _allowed(SONNET_55, FIX["_panel_allowlist"]({})) is False


def test_sonnet_5_5_is_not_reserved_as_premium():
    """It is an ORDINARY-turn model. If it were also premium, the reserved
    escalation turn would re-seat a model the ordinary turns already tried."""
    assert _allowed(SONNET_55, FIX["_premium_allowlist"]({})) is False


def test_sonnet_5_5_is_not_admitted_to_the_opus_last_turn():
    """DEFAULT_LAST_TURN_FIX_ALLOWLIST documents that Sonnet could not resolve
    findings Opus resolved. The last turn stays Opus-only."""
    assert _allowed(SONNET_55, FIX["DEFAULT_LAST_TURN_FIX_ALLOWLIST"]) is False
    assert _allowed(OPUS_5, FIX["DEFAULT_LAST_TURN_FIX_ALLOWLIST"]) is True


# -- the ladder, end to end ------------------------------------------------

def test_first_fix_attempt_seats_sonnet_5_5():
    """The headline behaviour."""
    assert _pick(_ordinary_turn_exclusions()) == SONNET_55


def test_second_attempt_falls_back_to_opus():
    """`next_remediation_requirements` adds the tried model to `exclude`, so the
    turn after Sonnet 5.5 must land on the next-strongest ordinary model."""
    first = _pick(_ordinary_turn_exclusions())
    tried = [c["key"] for c in _candidates() if c["model"] == first]
    assert _pick(_ordinary_turn_exclusions() + tried) == OPUS_5


def test_the_opus_last_turn_is_unchanged():
    assert _pick(_keys_failing(FIX["DEFAULT_LAST_TURN_FIX_ALLOWLIST"])) == OPUS_5


def test_the_premium_turn_is_unchanged():
    assert _pick(_keys_failing(FIX["_premium_allowlist"]({}))) == "gpt-6-astra"


def test_the_review_panel_still_seats_opus():
    assert _pick(_keys_failing(FIX["_panel_allowlist"]({}))) == OPUS_5


def test_older_sonnet_is_never_seated_to_write():
    """Even with every stronger model excluded, Sonnet 5 must not win the fix job."""
    fix = FIX["_fix_allowlist"]({})
    assert _allowed("claude-sonnet-5", fix) is False
    stronger = [c["key"] for c in _candidates()
                if c["model"] in (SONNET_55, OPUS_5, "claude-opus-5.5")]
    assert _pick(_ordinary_turn_exclusions() + stronger) != "claude-sonnet-5"


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-sonnet-4.5",
                                   "claude-sonnet-5.1", "claude-haiku-4.5"])
def test_only_5_5_slipped_through_the_widened_bar(model):
    assert _allowed(model, FIX["_fix_allowlist"]({})) is False


def test_an_operator_override_still_replaces_the_default_bar():
    """The widening is a DEFAULT, not a hardcode: an operator who names their own
    fix allowlist must still be able to keep Sonnet out entirely."""
    fix = FIX["_fix_allowlist"]({"pr_fix_model_allowlist": ["claude-opus-5*"]})
    assert _allowed(SONNET_55, fix) is False
