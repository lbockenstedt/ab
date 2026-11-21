import pytest

from model_selection import LlmRequirements, explain_selection, safety_floor, select_model


def _candidate(provider, tier, *, available=True):
    """Build a candidate without configured endpoints or live provider calls."""
    return {
        "key": (provider, "", provider + "-model"),
        "provider": provider,
        "model": provider + "-model",
        "available": available,
        "caps": {"cost_tier": tier, "max_complexity": "large"},
    }


@pytest.mark.parametrize("provider", ["ollama", "ollama2", " Ollama-local ", "lmstudio"])
@pytest.mark.parametrize("tier", ["free", "unknown"])
def test_automatic_selection_excludes_local_in_both_passes(provider, tier):
    """Neither strict nor permissive automatic selection admits local models."""
    local = _candidate(provider, tier)
    assert select_model(LlmRequirements(), [local]) is None


@pytest.mark.parametrize("provider", ["claude_cli", "ollama_cloud"])
@pytest.mark.parametrize("tier", ["frontier", "unknown"])
def test_automatic_selection_chooses_cloud_over_cheaper_gpu(provider, tier):
    """Cloud session and hosted providers remain eligible ahead of local GPUs."""
    local = _candidate("ollama", "free")
    cloud = _candidate(provider, tier)
    selection = select_model(LlmRequirements(restrict="cloud"), [local, cloud])
    assert selection.key == cloud["key"]
    assert ("permissive" in selection.reason) == (tier == "unknown")
    assert not selection.alternatives
    report = explain_selection(LlmRequirements(), [local, cloud])
    assert report["selected"]["key"] == cloud["key"]
    assert report["rows"][1]["status"] == "excluded"
    assert "explicit" in report["rows"][1]["reason"]


def test_local_gpu_is_not_a_retry_alternative():
    """Automatic retries do not add a local inference candidate."""
    cloud = _candidate("anthropic", "cheap")
    local = _candidate("lmstudio", "frontier")
    assert select_model(LlmRequirements(), [cloud, local]).alternatives == []


def test_local_pin_and_restriction_do_not_confuse_cloud_session_auth():
    """Explicit local routing works without treating cloud CLI auth as local."""
    local = _candidate("ollama2", "unknown")
    cli = _candidate("claude_cli", "free")
    assert select_model(LlmRequirements(pin_key=local["key"]), [cli, local]).key == local["key"]
    assert select_model(LlmRequirements(restrict="local"), [cli, local]).key == local["key"]
    assert select_model(LlmRequirements(restrict="local"), [cli]) is None


def test_safety_floor_keeps_routing_exclusions():
    """Capability fallback still respects exclusions and explicit routing."""
    cloud = _candidate("anthropic", "cheap")
    local = _candidate("lmstudio", "free")
    assert safety_floor([cloud, local], LlmRequirements(exclude_models=(cloud["key"],))) is None
    assert safety_floor([cloud, local], LlmRequirements(restrict="local")) is local
