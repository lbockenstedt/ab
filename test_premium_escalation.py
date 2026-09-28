"""The premium escalation tier (fable / astra).

These models are the most expensive on the roster, so the policy has two
halves and BOTH are load-bearing:

  1. They are reserved -- excluded from every ordinary remediation turn.
     Without this they are actively PREFERRED, because Copilot's generic
     registry fallback rates an unmatched model "cheap" and the picker is
     cost-first. That failure mode is silent and expensive, so it gets
     explicit coverage here.
  2. They are capped by a COUNT (`pr_remediate_premium_attempts`, default 1)
     that must survive app_state.record_pr_review rebuilding the record on
     every scan -- otherwise the cap resets hourly and is no cap at all.
"""
import ast
import types

import pytest

import model_registry
import pr_remediate


class _Reqs:
    def __init__(self, complexity="small", exclude_models=()):
        self.complexity = complexity
        self.needs_structured_output = True
        self.min_context_tokens = 0
        self.restrict = None
        self.must_escalate_to_human = False
        self.exclude_models = exclude_models


def _fake_fix_engine(monkeypatch, premium_models=("claude-fable-5.1", "gpt-6-astra")):
    """Stand in for fix_engine, which cannot be imported in the test env."""
    mod = types.ModuleType("fix_engine")
    mod.calls = []

    def _premium(config, existing=(), *, want_premium=False):
        mod.calls.append({"existing": tuple(existing), "want_premium": want_premium})
        keep = set(premium_models) if want_premium else {"cheap-model", "claude-opus-5"}
        drop = ({"cheap-model", "claude-opus-5"} if want_premium else set(premium_models))
        assert keep  # guard the fixture itself
        return tuple(sorted(set(existing) | drop, key=str))

    def _last_turn(config, existing=()):
        return tuple(sorted(set(existing) | {"cheap-model"}, key=str))

    mod._premium_fix_exclusions = _premium
    mod._last_turn_fix_exclusions = _last_turn
    mod.is_premium_fix_model = lambda m, config=None: m in premium_models
    monkeypatch.setitem(__import__("sys").modules, "fix_engine", mod)
    return mod


# --------------------------------------------------------------------------
# 1. The registry must stop rating these as cheap.
# --------------------------------------------------------------------------

def _resolve(model, config=None):
    rules = model_registry.find_matching_rules("copilot", model, config or {})
    assert rules, f"no rule matched {model}"
    return max(rules, key=lambda r: model_registry._specificity(r.get("match")))


@pytest.mark.parametrize("model", ["claude-fable-5.1", "claude-fable-5", "gpt-6-astra", "gpt-6-sol"])
def test_premium_models_are_not_priced_as_cheap(model):
    """The regression that matters: falling through to the copilot "*" rule
    makes the most expensive models look like the cheapest option."""
    rule = _resolve(model)
    assert rule["id"] != "copilot", f"{model} fell through to the generic copilot rule"
    assert rule["cost_tier"] == "frontier"


def test_astra_outranks_the_generic_gpt6_rule():
    assert _resolve("gpt-6-astra")["id"] == "copilot-gpt6-astra"
    assert _resolve("gpt-6-sol")["id"] == "copilot-gpt6"


def test_premium_ranks_above_opus():
    """prefer_capable must reach for these ahead of Opus, not behind it."""
    opus = _resolve("claude-opus-5")["capability_rank"]
    for model in ("claude-fable-5.1", "gpt-6-astra"):
        assert _resolve(model)["capability_rank"] > opus


def test_rules_are_seeded_into_an_already_persisted_registry():
    """A config with its own model_registry never reads DEFAULT_MODEL_RULES,
    so adding the rules there alone would be inert on every existing install."""
    old = [{"id": "copilot", "provider": "copilot", "match": "*", "cost_tier": "cheap"}]
    new, added = model_registry.upgrade_copilot_model_rules(old)
    assert {"copilot-claude-fable", "copilot-gpt6", "copilot-gpt6-astra"} <= set(added)
    assert _resolve("gpt-6-astra", {"model_registry": new})["cost_tier"] == "frontier"


def test_seeding_is_idempotent_and_never_overrides_an_operator_rule():
    once, _ = model_registry.upgrade_copilot_model_rules(
        [{"id": "copilot", "provider": "copilot", "match": "*", "cost_tier": "cheap"}])
    twice, added = model_registry.upgrade_copilot_model_rules(once)
    assert added == []
    assert len(twice) == len(once)

    curated = [{"id": "mine", "provider": "copilot", "match": "gpt-6-astra*",
                "cost_tier": "cheap", "enabled": True}]
    out, added = model_registry.upgrade_copilot_model_rules(curated)
    assert "copilot-gpt6-astra" not in added


# --------------------------------------------------------------------------
# 2. Premium is excluded from ordinary turns and narrowed to only on the
#    escalation turn.
# --------------------------------------------------------------------------

def test_ordinary_turn_excludes_premium(monkeypatch):
    fe = _fake_fix_engine(monkeypatch)
    reqs = pr_remediate.next_remediation_requirements(_Reqs(), config={}, attempts=0)
    assert fe.calls[0]["want_premium"] is False
    assert "claude-fable-5.1" in reqs.exclude_models
    assert "gpt-6-astra" in reqs.exclude_models


def test_opus_last_turn_still_excludes_premium(monkeypatch):
    """The Opus rung must not become a back door into the premium tier."""
    fe = _fake_fix_engine(monkeypatch)
    reqs = pr_remediate.next_remediation_requirements(_Reqs(), config={}, attempts=2)
    assert fe.calls[0]["want_premium"] is False
    assert "gpt-6-astra" in reqs.exclude_models


def test_premium_turn_narrows_to_premium(monkeypatch):
    fe = _fake_fix_engine(monkeypatch)
    reqs = pr_remediate.next_remediation_requirements(
        _Reqs(), config={}, attempts=3, premium=True)
    assert fe.calls[0]["want_premium"] is True
    assert "claude-opus-5" in reqs.exclude_models
    assert "claude-fable-5.1" not in reqs.exclude_models


def test_premium_turn_does_not_also_apply_the_opus_narrowing(monkeypatch):
    """Applying both would exclude every premium model and empty the pool."""
    fe = _fake_fix_engine(monkeypatch)
    reqs = pr_remediate.next_remediation_requirements(
        _Reqs(), config={}, attempts=5, premium=True)
    assert [c["want_premium"] for c in fe.calls] == [True]
    assert "claude-fable-5.1" not in reqs.exclude_models


def test_tried_model_is_still_excluded_on_a_premium_turn(monkeypatch):
    _fake_fix_engine(monkeypatch)
    reqs = pr_remediate.next_remediation_requirements(
        _Reqs(), tried_key="tried", config={}, attempts=3, premium=True)
    assert "tried" in reqs.exclude_models


def test_broken_fix_engine_does_not_stop_the_retry(monkeypatch):
    mod = types.ModuleType("fix_engine")

    def _boom(*a, **k):
        raise RuntimeError("nope")

    mod._premium_fix_exclusions = _boom
    mod._last_turn_fix_exclusions = _boom
    monkeypatch.setitem(__import__("sys").modules, "fix_engine", mod)
    reqs = pr_remediate.next_remediation_requirements(
        _Reqs(), tried_key="tried", config={}, attempts=3, premium=True)
    assert "tried" in reqs.exclude_models


# --------------------------------------------------------------------------
# 3. The budget.
# --------------------------------------------------------------------------

def test_premium_budget_defaults_to_one():
    assert pr_remediate._premium_budget({}) == 1


@pytest.mark.parametrize("raw,expected", [
    (0, 0), (1, 1), (2, 2), (99, 3), (-5, 0), ("bogus", 1), (None, 1),
])
def test_premium_budget_is_clamped(raw, expected):
    assert pr_remediate._premium_budget({"pr_remediate_premium_attempts": raw}) == expected


def test_premium_turn_available_only_until_the_budget_is_spent():
    cfg = {}
    assert pr_remediate.premium_turn_available({"premium_attempts": 0}, cfg) is True
    assert pr_remediate.premium_turn_available({"premium_attempts": 1}, cfg) is False
    assert pr_remediate.premium_turn_available({"premium_attempts": 7}, cfg) is False


def test_premium_turn_can_be_disabled():
    assert pr_remediate.premium_turn_available(
        {"premium_attempts": 0}, {"pr_remediate_premium_attempts": 0}) is False


def test_premium_turn_tolerates_a_junk_counter():
    assert pr_remediate.premium_turn_available({"premium_attempts": "x"}, {}) is True
    assert pr_remediate.premium_turn_available(None, {}) is False


def test_exhausted_gate_grants_the_premium_turn_then_stops():
    """The ordinary ceiling must yield exactly once, not indefinitely."""
    cfg = {"pr_auto_remediate_max_attempts": 3}
    spent = {"remediation_attempts": 3, "premium_attempts": 0}
    assert pr_remediate.premium_turn_available(spent, cfg) is True
    spent["premium_attempts"] = 1
    assert pr_remediate.premium_turn_available(spent, cfg) is False


# --------------------------------------------------------------------------
# 4. The counter must survive record_pr_review rebuilding the record.
# --------------------------------------------------------------------------

# NOTE: app_state cannot be imported in this test env (it pulls in main.py and
# thus tenacity/fastapi), so the carry-forward is asserted at source level
# below -- the same technique the rest of this suite uses for wiring.
def test_record_pr_review_source_carries_the_counter():
    """Source-level guard: record_pr_review REBUILDS the record from a fixed
    literal, so a counter omitted here is silently reset on every poll."""
    src = open("app_state.py").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "record_pr_review")
    body = ast.get_source_segment(src, fn)
    assert '"premium_attempts"' in body
    assert 'prev.get("premium_attempts")' in body


# --------------------------------------------------------------------------
# 5. Wiring: the charge is made by the model actually used.
# --------------------------------------------------------------------------

def _auto_remediate_source():
    src = open("pr_remediate.py").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "auto_remediate_pr")
    return ast.get_source_segment(src, fn)


def test_every_record_write_persists_the_counter():
    body = _auto_remediate_source()
    assert body.count("premium_attempts=_premium_used") == 3, (
        "a write path that omits the counter lets the premium attempt go unrecorded")


def test_charge_is_keyed_on_the_model_actually_used():
    body = _auto_remediate_source()
    assert "is_premium_fix_model(used_model, config)" in body


def test_premium_turn_is_gated_on_the_ordinary_budget_being_spent():
    body = _auto_remediate_source()
    assert "attempts >= max_attempts and premium_turn_available(rec, config)" in body
    assert "if attempts >= max_attempts and not premium_turn:" in body


def test_premium_flag_reaches_the_requirements_builder():
    body = _auto_remediate_source()
    assert "premium=premium_turn" in body


def test_can_remediate_gate_honours_the_premium_turn():
    src = open("pr_remediate.py").read()
    assert "if not premium_turn_available(rec, config):" in src


# --------------------------------------------------------------------------
# 6. Operator control: both knobs must be settable from the UI, not only by
#    hand-editing /etc/ab/config.json on the box.
# --------------------------------------------------------------------------

from test_feature_settings_roundtrip import (  # noqa: E402
    _load_ns, _FakeRequest, _base_pairs, _run,
)


def _fix_engine_ns():
    """fix_engine cannot be imported here (it pulls in main.py), so lift the
    premium-policy functions out of the source the same way the rest of this
    suite lifts save_settings."""
    import fnmatch as _fnmatch
    import re as _re
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    want = {"_model_allowed", "_premium_allowlist", "is_premium_fix_model",
            "_fix_allowlist", "_panel_allowlist"}
    ns = {"fnmatch": _fnmatch, "re": _re}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            exec(compile(ast.Module([node], []), "fix_engine.py", "exec"), ns)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") in ("DEFAULT_PREMIUM_FIX_ALLOWLIST",
                                         "DEFAULT_PANEL_ALLOWLIST",
                                         "DEFAULT_FIX_ALLOWLIST")
                for t in node.targets):
            exec(compile(ast.Module([node], []), "fix_engine.py", "exec"), ns)
    assert want <= set(ns), sorted(want - set(ns))
    return ns


def _save(pairs, base_config=None):
    ns = _load_ns()
    ns["_config_holder"]["config"] = dict(base_config or {})
    _run(ns["save_settings"](_FakeRequest(pairs)))
    return ns["_config_holder"]["config"]


def test_premium_budget_is_saved_from_the_form():
    saved = _save(_base_pairs(pr_remediate_premium_attempts="2"))
    assert saved["pr_remediate_premium_attempts"] == 2


def test_premium_budget_can_be_switched_off_from_the_form():
    saved = _save(_base_pairs(pr_remediate_premium_attempts="0"),
                  base_config={"pr_remediate_premium_attempts": 1})
    assert saved["pr_remediate_premium_attempts"] == 0


@pytest.mark.parametrize("raw,expected", [
    ("9", 3), ("-1", 0), ("", 1), ("abc", 1), ("2.7", 2),
])
def test_premium_budget_is_clamped_on_save(raw, expected):
    """A typo here is a bill, not a bug -- the form must not persist it."""
    assert _save(_base_pairs(pr_remediate_premium_attempts=raw))[
        "pr_remediate_premium_attempts"] == expected


def test_premium_allowlist_is_saved_from_the_checkboxes():
    saved = _save(_base_pairs() + [("premium_models", "claude-fable-5.1"),
                                   ("premium_models", "gpt-6-astra")])
    assert saved["pr_remediate_premium_model_allowlist"] == [
        "claude-fable-5.1", "gpt-6-astra"]


def test_premium_allowlist_accepts_globs_from_the_text_field():
    saved = _save(_base_pairs(premium_models_extra="claude-fable-*, GPT-6*"))
    assert saved["pr_remediate_premium_model_allowlist"] == ["claude-fable-*", "gpt-6*"]


def test_premium_allowlist_merges_and_dedupes_both_inputs():
    saved = _save(_base_pairs(premium_models_extra="gpt-6*, gpt-6*")
                  + [("premium_models", "gpt-6*")])
    assert saved["pr_remediate_premium_model_allowlist"] == ["gpt-6*"]


def test_clearing_every_box_disables_the_reservation_deliberately():
    """Empty is a real, reachable choice -- but only reachable deliberately,
    which is why the GET renders the effective defaults already ticked."""
    saved = _save(_base_pairs(),
                  base_config={"pr_remediate_premium_model_allowlist": ["gpt-6*"]})
    assert saved["pr_remediate_premium_model_allowlist"] == []


def test_saved_allowlist_is_what_fix_engine_reads():
    """Pin the key: a form that writes a name the policy never reads would
    look like it worked and change nothing."""
    fe = _fix_engine_ns()
    saved = _save(_base_pairs(premium_models_extra="gpt-6*"))
    assert fe["_premium_allowlist"](saved) == ("gpt-6*",)
    assert fe["is_premium_fix_model"]("gpt-6-astra", saved) is True
    assert fe["is_premium_fix_model"]("claude-fable-5.1", saved) is False


def test_defaults_cover_the_models_the_operator_called_top_tier():
    fe = _fix_engine_ns()
    for m in ("claude-fable-5.1", "claude-fable-5", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"):
        assert fe["is_premium_fix_model"](m, {}) is True
    for m in ("claude-opus-5", "claude-sonnet-5", "gpt-5.6-sol"):
        assert fe["is_premium_fix_model"](m, {}) is False


def test_settings_get_pre_ticks_the_effective_allowlist():
    """The GET must render defaults ticked. Without it, saving any unrelated
    setting would submit an empty allowlist and silently un-reserve the
    most expensive models on the roster."""
    src = open("routes.py").read()
    assert "premium_model_options" in src and "premium_models_set" in src
    assert "premium_models_extra" in src
    tpl = open("templates/index.html").read()
    assert 'name="premium_models"' in tpl and 'in premium_models_set %}checked' in tpl
    assert 'name="pr_remediate_premium_attempts"' in tpl
    assert 'value="{{ premium_models_extra }}"' in tpl, (
        "globs live only in the text field; not echoing it back drops them on save")


def test_the_fix_floor_admits_the_premium_tier():
    """Regression: _premium_fix_exclusions(want_premium=True) keeps every
    NON-premium model out, while _fix_model_exclusions keeps every non-Opus
    model out. Before the union those two intersected to an empty pool, so
    the escalation turn could seat nothing and the tier was silently inert --
    no error, no log, just a feature that never fired."""
    fe = _fix_engine_ns()
    fix_patterns = fe["_fix_allowlist"]({})
    for m in ("claude-fable-5.1", "gpt-6-astra", "gpt-6-sol"):
        assert fe["is_premium_fix_model"](m, {}) is True
        assert fe["_model_allowed"](m, fix_patterns) is True, (
            "%s is premium but the fix floor would reject it" % m)


def test_an_operator_named_premium_model_is_admitted_to_write():
    """Settings lets the operator name ANY model premium; the write bar must
    follow, or the UI silently produces an unusable configuration."""
    fe = _fix_engine_ns()
    cfg = {"pr_remediate_premium_model_allowlist": ["zeta-9*"]}
    assert fe["is_premium_fix_model"]("zeta-9-pro", cfg) is True
    assert fe["_model_allowed"]("zeta-9-pro", fe["_fix_allowlist"](cfg)) is True


def test_widening_the_write_bar_does_not_widen_the_panel():
    fe = _fix_engine_ns()
    panel = fe["_panel_allowlist"]({}) if "_panel_allowlist" in fe else None
    if panel is None:
        pytest.skip("_panel_allowlist not extracted")
    for m in ("claude-fable-5.1", "gpt-6-astra"):
        assert fe["_model_allowed"](m, panel) is False, (
            "the panel runs on every PR -- premium models must stay off it")
