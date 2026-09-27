"""Last-turn escalation to an Opus-class fix model.

Two failed remediation turns mean the cheapest allowlisted model that clears
the capability floor could not do the job. Raising the complexity tier again
achieves nothing -- every allowlisted model already clears "large", so the
ladder has no rungs left and the picker re-seats the same model. The escalation
must therefore narrow WHICH models may write the fix.

The bar is a model-name allowlist, never the `frontier` cost tier: Opus and
Sonnet are both cost_tier=frontier/max_complexity=large, and Sonnet was
observed failing findings Opus resolved. A cost-tier bar would re-admit it.
"""
import ast
import re
import sys
import types

import pytest

import pr_remediate


class _RecLog:
    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def _log(msg, *a, **k):
            self.records.append((level, (str(msg) % a) if a else str(msg)))
        return _log


class _Reqs:
    def __init__(self, complexity="small", exclude_models=()):
        self.complexity = complexity
        self.needs_structured_output = True
        self.min_context_tokens = 0
        self.restrict = None
        self.must_escalate_to_human = False
        self.exclude_models = exclude_models


@pytest.fixture
def fake_fix_engine(monkeypatch):
    """Stand in for fix_engine, which cannot be imported in the test env."""
    mod = types.ModuleType("fix_engine")
    mod.calls = []

    def _excl(config, existing=()):
        mod.calls.append((config, tuple(existing)))
        return tuple(sorted(set(existing) | {("copilot", "", "cheap-model")}))

    mod._last_turn_fix_exclusions = _excl

    # The premium split runs BEFORE the Opus narrowing on every turn (see
    # pr_remediate.next_remediation_requirements). Stub it as a pass-through so
    # these tests exercise the real code path rather than the degraded one the
    # caller's except-clause would take on AttributeError.
    def _premium(config, existing=(), *, want_premium=False):
        mod.premium_calls.append({"existing": tuple(existing), "want_premium": want_premium})
        return tuple(existing)

    mod.premium_calls = []
    mod._premium_fix_exclusions = _premium
    monkeypatch.setitem(sys.modules, "fix_engine", mod)
    return mod


def _next(attempts, config=None, **kw):
    return pr_remediate.next_remediation_requirements(
        _Reqs(**kw), failure_kind="retry", tried_key=None,
        config=config if config is not None else {}, attempts=attempts)


# ---- the escalation fires at the right time ----
@pytest.mark.parametrize("attempts", [0, 1])
def test_early_turns_do_not_narrow_the_pool(attempts, fake_fix_engine):
    _next(attempts)
    assert fake_fix_engine.calls == []


@pytest.mark.parametrize("attempts", [2, 3, 7])
def test_last_turns_narrow_to_opus_class(attempts, fake_fix_engine):
    reqs = _next(attempts)
    assert fake_fix_engine.calls, "escalation did not run"
    assert ("copilot", "", "cheap-model") in reqs.exclude_models


def test_threshold_is_configurable(fake_fix_engine):
    _next(1, config={"pr_remediate_last_turn_after_attempts": 1})
    assert fake_fix_engine.calls


def test_threshold_zero_disables_escalation(fake_fix_engine):
    _next(5, config={"pr_remediate_last_turn_after_attempts": 0})
    assert fake_fix_engine.calls == []


def test_garbage_threshold_falls_back_to_two(fake_fix_engine):
    _next(1, config={"pr_remediate_last_turn_after_attempts": "nonsense"})
    assert fake_fix_engine.calls == []
    _next(2, config={"pr_remediate_last_turn_after_attempts": "nonsense"})
    assert fake_fix_engine.calls


def test_none_config_is_tolerated(fake_fix_engine):
    reqs = pr_remediate.next_remediation_requirements(_Reqs(), config=None, attempts=2)
    assert reqs is not None


# ---- it composes with, rather than replaces, existing behaviour ----
def test_tried_model_is_still_excluded_after_escalation(fake_fix_engine):
    reqs = pr_remediate.next_remediation_requirements(
        _Reqs(), tried_key=("copilot", "", "tried"), config={}, attempts=2)
    assert ("copilot", "", "tried") in reqs.exclude_models
    assert ("copilot", "", "cheap-model") in reqs.exclude_models


def test_prior_exclusions_are_carried_into_the_escalation(fake_fix_engine):
    prior = (("copilot", "", "old"),)
    pr_remediate.next_remediation_requirements(
        _Reqs(exclude_models=prior), config={}, attempts=2)
    _cfg, existing = fake_fix_engine.calls[0]
    assert ("copilot", "", "old") in existing


def test_complexity_still_steps_up(fake_fix_engine):
    assert _next(2, complexity="small").complexity == "medium"


def test_complexity_never_overflows_the_ladder(fake_fix_engine):
    assert _next(2, complexity="large").complexity == "large"


def test_broken_fix_engine_does_not_stop_the_retry(monkeypatch):
    """A failed escalation must degrade to an ordinary retry, not abort it."""
    mod = types.ModuleType("fix_engine")

    def _boom(config, existing=()):
        raise RuntimeError("nope")

    mod._last_turn_fix_exclusions = _boom
    monkeypatch.setitem(sys.modules, "fix_engine", mod)
    reqs = pr_remediate.next_remediation_requirements(
        _Reqs(), tried_key=("copilot", "", "tried"), config={}, attempts=2)
    assert ("copilot", "", "tried") in reqs.exclude_models


def test_escalation_is_logged(monkeypatch, fake_fix_engine):
    log = _RecLog()
    monkeypatch.setattr(pr_remediate, "logger", log)
    _next(2)
    assert any("Opus-class" in m for _l, m in log.records), log.records


# ---- the caller actually passes attempts through ----
def test_auto_remediate_passes_attempts_through():
    src = open("pr_remediate.py").read()
    call = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "next_remediation_requirements")
    assert "attempts" in {k.arg for k in call.keywords}, (
        "auto_remediate_pr must pass attempts= or the escalation can never fire")


# ---- the bar is a model allowlist, NOT a cost tier ----
def test_default_last_turn_allowlist_is_opus_class_only():
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    val = next(ast.literal_eval(n.value) for n in tree.body
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "DEFAULT_LAST_TURN_FIX_ALLOWLIST"
                       for t in n.targets))
    assert val, "the last-turn allowlist must not be empty by default"
    assert all("opus" in p for p in val), val


@pytest.mark.parametrize("model", [
    "claude-sonnet-5", "claude-sonnet-4.5", "claude-haiku-4.5",
    "gemini-3.8-flash", "gpt-5-mini"])
def test_sonnet_class_models_may_not_write_the_last_fix(model):
    """Sonnet is cost_tier=frontier but could not resolve findings Opus could.
    A cost-tier bar would admit it; the name allowlist must not."""
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    ns = {"fnmatch": __import__("fnmatch")}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_model_allowed":
            exec(ast.get_source_segment(src, n), ns)
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) == "DEFAULT_LAST_TURN_FIX_ALLOWLIST" for t in n.targets):
            exec(ast.get_source_segment(src, n), ns)
    assert not ns["_model_allowed"](model, ns["DEFAULT_LAST_TURN_FIX_ALLOWLIST"])


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5.5", "claude-opus-6"])
def test_opus_models_may_write_the_last_fix(model):
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    ns = {"fnmatch": __import__("fnmatch")}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_model_allowed":
            exec(ast.get_source_segment(src, n), ns)
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) == "DEFAULT_LAST_TURN_FIX_ALLOWLIST" for t in n.targets):
            exec(ast.get_source_segment(src, n), ns)
    assert ns["_model_allowed"](model, ns["DEFAULT_LAST_TURN_FIX_ALLOWLIST"])


def test_capability_bars_are_never_expressed_as_a_cost_tier():
    """Pin the rule itself: no fix/review gate may key off cost_tier=frontier,
    because Opus and Sonnet share that tier."""
    src = open("fix_engine.py").read()
    offenders = [ln.strip() for ln in src.splitlines()
                 if re.search(r'cost_tier.*==.*["\']frontier', ln)
                 or re.search(r'["\']frontier["\'].*==.*cost_tier', ln)]
    assert not offenders, offenders


# ---- exclusion sets legitimately mix strings and ModelKey tuples ----
def _load_exclusion_fns(candidates):
    """Extract the two exclusion helpers from fix_engine (not importable)."""
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    want_fn = {"_model_allowed", "_fix_allowlist", "_fix_model_exclusions",
               "_last_turn_fix_exclusions", "_premium_allowlist"}
    want_as = {"DEFAULT_PANEL_ALLOWLIST", "DEFAULT_FIX_ALLOWLIST",
               "DEFAULT_LAST_TURN_FIX_ALLOWLIST", "DEFAULT_PREMIUM_FIX_ALLOWLIST"}
    segs = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in want_fn:
            segs.append(ast.get_source_segment(src, n))
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) in want_as for t in n.targets):
            segs.append(ast.get_source_segment(src, n))

    class _Llm:
        def _enumerate_candidates(self, config):
            return list(candidates)

    ns = {"re": re, "fnmatch": __import__("fnmatch"), "logger": _RecLog(),
          "llm_client": _Llm()}
    exec("\n\n".join(segs), ns)
    return ns


def _cands():
    def c(model):
        return {"key": ("copilot", "", model), "model": model}
    return [c("claude-opus-5"), c("claude-sonnet-5"), c("gemini-3.8-flash")]


@pytest.mark.parametrize("fn", ["_fix_model_exclusions", "_last_turn_fix_exclusions"])
def test_exclusions_tolerate_mixed_str_and_tuple(fn):
    """Review records persist excluded_models as NAME STRINGS while
    _enumerate_candidates yields ModelKey TUPLES. A bare sorted() over both
    raises TypeError, which reached production as an unfixable
    "'<' not supported between instances of 'str' and 'tuple'" on every PR."""
    ns = _load_exclusion_fns(_cands())
    out = ns[fn]({}, existing=("gemini-3.8-flash", ("copilot", "", "claude-sonnet-5")))
    assert "gemini-3.8-flash" in out
    assert ("copilot", "", "gemini-3.8-flash") in out
    assert ("copilot", "", "claude-opus-5") not in out


@pytest.mark.parametrize("fn", ["_fix_model_exclusions", "_last_turn_fix_exclusions"])
def test_exclusions_are_deterministic(fn):
    ns = _load_exclusion_fns(_cands())
    existing = ("z-name", ("copilot", "", "a-model"))
    assert ns[fn]({}, existing=existing) == ns[fn]({}, existing=tuple(reversed(existing)))


def test_last_turn_exclusions_keep_opus_and_drop_the_rest():
    ns = _load_exclusion_fns(_cands())
    out = ns["_last_turn_fix_exclusions"]({})
    assert ("copilot", "", "claude-sonnet-5") in out, "Sonnet must not write the last fix"
    assert ("copilot", "", "gemini-3.8-flash") in out
    assert ("copilot", "", "claude-opus-5") not in out


# ---- the guard must count SELECTABLE Opus models, not configured ones ----
def test_sole_opus_already_excluded_does_not_empty_the_pool():
    """The escalating caller adds tried_key BEFORE calling us, so on the turn
    that matters the only Opus candidate is routinely already excluded.
    Counting it as available would exclude every candidate and hand the picker
    an empty pool -- the dead end this guard exists to prevent."""
    ns = _load_exclusion_fns(_cands())
    existing = (("copilot", "", "claude-opus-5"),)
    out = ns["_last_turn_fix_exclusions"]({}, existing=existing)
    assert out == existing, "must leave the pool unnarrowed, not exclude everything"


def test_sole_opus_already_excluded_by_NAME_also_short_circuits():
    """Records persist exclusions as name strings, so eligibility has to be
    tested in both forms."""
    ns = _load_exclusion_fns(_cands())
    existing = ("claude-opus-5",)
    assert ns["_last_turn_fix_exclusions"]({}, existing=existing) == existing


def test_a_second_selectable_opus_still_allows_narrowing():
    def c(model):
        return {"key": ("copilot", "", model), "model": model}
    cands = [c("claude-opus-5"), c("claude-opus-5.5"), c("gemini-3.8-flash")]
    ns = _load_exclusion_fns(cands)
    out = ns["_last_turn_fix_exclusions"]({}, existing=(("copilot", "", "claude-opus-5"),))
    assert ("copilot", "", "gemini-3.8-flash") in out, "narrowing should still happen"
    assert ("copilot", "", "claude-opus-5.5") not in out, "the selectable Opus must survive"


def test_never_returns_a_set_covering_every_candidate():
    """Whatever the inputs, the result must never exclude the whole pool."""
    ns = _load_exclusion_fns(_cands())
    all_keys = {("copilot", "", m)
                for m in ("claude-opus-5", "claude-sonnet-5", "gemini-3.8-flash")}
    for existing in ((), ("claude-opus-5",), (("copilot", "", "claude-opus-5"),),
                     (("copilot", "", "claude-sonnet-5"),)):
        out = set(ns["_last_turn_fix_exclusions"]({}, existing=existing))
        assert not all_keys.issubset(out), (existing, out)
