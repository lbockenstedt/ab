import pytest

import main  # noqa: F401  (boots the app first; llm_client imports it circularly)
import llm_client
import model_registry


def test_lm_pool_is_a_keyless_local_free_provider():
    assert llm_client._provider_is_nokey("lm_pool")
    assert llm_client._provider_configured("lm_pool", "", "review")
    assert model_registry.is_nokey_provider("lm_pool") and model_registry.is_local_provider("lm_pool")
    rule = next(r for r in model_registry.DEFAULT_MODEL_RULES if r["provider"] == "lm_pool")
    assert rule["cost_tier"] == "free" and not rule["supports_tools"]
    added = model_registry.upgrade_lm_pool_rule([])[1]
    assert list(added) == ["lm-pool"] and model_registry.upgrade_lm_pool_rule(
        model_registry.upgrade_lm_pool_rule([])[0])[1] == []


def test_call_provider_sends_the_task_as_model_and_raises_on_a_miss(monkeypatch):
    sent = []

    def fake(task, prompt):
        sent.append((task, prompt))
        return {"status": "SUCCESS", "text": "ok"}
    monkeypatch.setattr(llm_client, "_lm_hub_request", fake)
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert llm_client._call_provider("lm_pool", "review", "", "", msgs, None, False, None, {}) == "ok"
    assert sent == [("review", "S\n\nU")]
    monkeypatch.setattr(llm_client, "_lm_hub_request", lambda t, p: {"status": "FAILED", "code": "no_capacity"})
    with pytest.raises(Exception, match="no_capacity"):
        llm_client._call_provider("lm_pool", "review", "", "", msgs, None, False, None, {})
    monkeypatch.setattr(llm_client, "_lm_hub_request", lambda t, p: None)
    with pytest.raises(Exception, match="no hub connection"):
        llm_client._call_provider("lm_pool", "scout", "", "", msgs, None, False, None, {})
