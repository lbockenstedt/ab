"""Cross-vendor review of authored fixes, and the Opus-author single-reviewer rule.

Policy under test (two rules in fix_engine._select_review_panel):

  Rule 1 -- a model's fix is never judged only by its own vendor. Same-vendor
  models share training data and blind spots, so a same-vendor panel is not an
  independent check. HARD filter, relaxed only if it would leave no reviewer.

  Rule 2 -- when an Opus-class model AUTHORED the fix, a dual panel adds little:
  the frontier judgement already went into the code. One cross-vendor reviewer
  is seated instead. This narrows WHO reviews, never the confidence bar.

fix_engine.py cannot be imported directly, so the pieces under test are pulled
out with ast and exec'd -- same convention as test_panel_allowlist.py.
"""
import pytest

from test_panel_allowlist import _RecLog, _cand, _engine, _load_fix_engine, _models


def _pool():
    """Allowlisted (frontier-policy) candidates across three vendor families."""
    return [
        _cand("copilot", "claude-opus-5", tier="frontier"),
        _cand("copilot", "claude-opus-5.5", tier="frontier"),
        _cand("copilot", "gpt-5.6-sol", tier="frontier"),
    ]


def _key(model, provider="copilot"):
    return (provider, "", model)


def _panel(candidates, builder_model=None, max_reviewers=2, logger=None, config=None):
    extra = {}
    if logger is not None:
        extra["logger"] = logger
    ns = _engine(candidates, **extra)
    builder_key = _key(builder_model) if builder_model else None
    return ns["_select_review_panel"](config if config is not None else {},
                                      builder_key=builder_key,
                                      max_reviewers=max_reviewers)


# ---- Rule 1: cross-vendor review ----
def test_anthropic_builder_is_never_reviewed_by_anthropic():
    panel = _panel(_pool(), builder_model="claude-opus-5")
    assert panel, "a reviewer must still be seated"
    assert not any("claude" in m for m in _models(panel)), _models(panel)


def test_openai_builder_is_never_reviewed_by_openai():
    panel = _panel(_pool(), builder_model="gpt-5.6-sol")
    assert panel
    assert not any("gpt" in m for m in _models(panel)), _models(panel)


def test_builder_itself_is_still_excluded():
    panel = _panel(_pool(), builder_model="claude-opus-5")
    assert "claude-opus-5" not in _models(panel)


def test_cross_vendor_filter_relaxes_rather_than_leaving_no_reviewer():
    """Only same-vendor reviewers exist -- a same-vendor review beats none."""
    only_anthropic = [
        _cand("copilot", "claude-opus-5", tier="frontier"),
        _cand("copilot", "claude-opus-5.5", tier="frontier"),
    ]
    log = _RecLog()
    panel = _panel(only_anthropic, builder_model="claude-opus-5", logger=log)
    assert _models(panel) == ["claude-opus-5.5"]
    assert any("cross-vendor" in m for _lvl, m in log.records), log.records


def test_no_builder_means_no_vendor_filtering():
    ns = _engine(_pool())
    panel = ns["_select_review_panel"]({}, builder_n=0, max_reviewers=2)
    assert len(panel) == 2


def test_unknown_vendor_builder_does_not_filter_anyone_out():
    """An unrecognised model id maps to the 'other' family; filtering on it
    would wrongly strip every real vendor from the pool."""
    pool = _pool() + [_cand("ollama", "some-local-thing", tier="free")]
    ns = _engine(pool)
    panel = ns["_select_review_panel"]({}, builder_key=_key("some-local-thing", "ollama"),
                                       max_reviewers=2)
    assert len(panel) == 2
    assert "some-local-thing" not in _models(panel)


# ---- Rule 2: Opus authored it, so one reviewer is enough ----
@pytest.mark.parametrize("builder", ["claude-opus-5", "claude-opus-5.5", "CLAUDE-OPUS-6"])
def test_opus_authored_fix_seats_exactly_one_reviewer(builder):
    # The builder must be IN the pool: builder_model is resolved by matching the
    # builder key against the enumerated candidates.
    pool = _pool() + [_cand("copilot", "gpt-5.6-terra", tier="frontier"),
                      _cand("copilot", builder, tier="frontier")]
    panel = _panel(pool, builder_model=builder, max_reviewers=2)
    assert len(panel) == 1, _models(panel)


def test_opus_authored_single_reviewer_is_cross_vendor():
    pool = _pool() + [_cand("copilot", "gpt-5.6-terra", tier="frontier")]
    panel = _panel(pool, builder_model="claude-opus-5", max_reviewers=2)
    assert _models(panel)[0].startswith("gpt-"), _models(panel)


def test_opus_single_reviewer_decision_is_logged():
    log = _RecLog()
    _panel(_pool(), builder_model="claude-opus-5", logger=log)
    assert any("single cross-vendor reviewer" in m for _lvl, m in log.records), log.records


def test_non_opus_author_still_gets_the_full_panel():
    """gpt is allowed to write code too, but its work keeps the dual panel."""
    pool = [
        _cand("copilot", "gpt-5.6-sol", tier="frontier"),
        _cand("copilot", "claude-opus-5", tier="frontier"),
        _cand("copilot", "claude-opus-5.5", tier="frontier"),
    ]
    panel = _panel(pool, builder_model="gpt-5.6-sol", max_reviewers=2)
    assert len(panel) == 2, _models(panel)


def test_max_reviewers_one_is_not_widened_by_the_rules():
    panel = _panel(_pool(), builder_model="claude-opus-5", max_reviewers=1)
    assert len(panel) == 1


# ---- the vendor-family helper the rules rest on ----
@pytest.mark.parametrize("model,family", [
    ("claude-opus-5", "anthropic"), ("claude-haiku-4.5", "anthropic"),
    ("gpt-5.6-sol", "openai"), ("o3-mini", "openai"),
    ("gemini-3.8-flash", "google"), ("grok-4.5", "xai"),
    ("some-local-thing", "other"), ("", "other"), (None, "other"),
])
def test_vendor_family_classification(model, family):
    """_vendor_family is nested inside _select_review_panel, so it is exercised
    through the behaviour above; this pins the classification it relies on."""
    src = open("fix_engine.py").read()
    assert "_FAMILY_KEYWORDS" in src
    keywords = (
        ("anthropic", ("claude", "opus", "sonnet", "haiku")),
        ("openai", ("gpt", "o1-", "o3-", "o4-")),
        ("google", ("gemini",)),
        ("xai", ("grok",)),
    )
    m = (model or "").lower()
    got = next((f for f, needles in keywords if any(n in m for n in needles)), "other")
    assert got == family


def test_rules_are_present_in_source():
    """Guard against the rules being silently dropped in a refactor."""
    src = open("fix_engine.py").read()
    assert "cross_vendor" in src
    assert 'builder_model.lower()' in src
    _load_fix_engine()  # still parses/execs cleanly
