import main  # noqa: F401  (loads before llm_client: circular import order)
import llm_client
import stage_models as sm


def _cands(*models):
    return [{"key": ("p", "", m), "model": m} for m in models]


def test_diagnostic_marker_detection():
    assert sm.is_diagnostic("**Escalated** (verdict: `health-check`, module: `ipam`)")
    assert not sm.is_diagnostic("**Escalated** (verdict: `escalate`, module: `ipam`)")


def test_patterns_default_override_and_blank(monkeypatch):
    assert sm.patterns({}, "diag_fix") == ("claude-opus-5*", "claude-opus-6*")
    assert sm.patterns({"diagnostic_fix_models": "gpt-7*, Claude-Opus-7*"}, "diag_fix") == (
        "gpt-7*", "claude-opus-7*")
    assert sm.patterns({"diagnostic_fix_models": []}, "diag_fix") == ("claude-opus-5*", "claude-opus-6*")


def test_exclusions_keep_only_matching_models(monkeypatch):
    monkeypatch.setattr(llm_client, "_enumerate_candidates",
                        lambda cfg: _cands("claude-haiku-4-5", "claude-sonnet-5.5", "claude-opus-5.1"))
    ex = sm.exclusions({}, "diag_fix", ())
    assert set(ex) == {("p", "", "claude-haiku-4-5"), ("p", "", "claude-sonnet-5.5")}
    # A newly released model is admitted by editing the setting, no code change.
    monkeypatch.setattr(llm_client, "_enumerate_candidates",
                        lambda cfg: _cands("claude-haiku-4-5", "claude-opus-7"))
    assert set(sm.exclusions({"diagnostic_fix_models": "claude-opus-7*"}, "diag_fix")) == {
        ("p", "", "claude-haiku-4-5")}


def test_exclusions_never_empty_the_pool(monkeypatch):
    monkeypatch.setattr(llm_client, "_enumerate_candidates", lambda cfg: _cands("claude-haiku-4-5"))
    assert sm.exclusions({}, "diag_fix", (("x", "", "y"),)) == (("x", "", "y"),)


def test_settings_rows_and_save_clear_default():
    import routes
    rows = routes._model_stage_rows({}, ["claude-opus-5"])
    assert [r["stage"] for r in rows] == ["diag_identify", "diag_fix"] and rows[1]["value"]


def test_used_up_stage_falls_back_to_the_given_floor(monkeypatch):
    # Both opus models already tried: the fix step must keep the write floor,
    # not open the pool to a model such as gemini flash.
    monkeypatch.setattr(llm_client, "_enumerate_candidates",
                        lambda cfg: _cands("claude-opus-5.1", "claude-sonnet-5.5", "gemini-3.8-flash"))
    tried = (("p", "", "claude-opus-5.1"),)
    floor = tried + (("p", "", "gemini-3.8-flash"),)
    assert sm.exclusions({}, "diag_fix", tried, fallback=floor) == floor
    assert sm.exclusions({}, "diag_fix", tried) == tried
