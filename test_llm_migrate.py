#!/usr/bin/env python3
"""Self-test for llm_migrate.migrate() (LLM Selection Redesign, Phase 7).

Run:  python3 ab/test_llm_migrate.py

llm_migrate imports only model_registry (no `main` side effects), so this can
import and call it directly.
"""
import copy

import llm_migrate


def _check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    return cond


def _base_config():
    return {
        "llm_credentials": {
            "openai": {"api_key": "sk-x", "base_url": ""},
            "ollama": {"api_key": "", "base_url": "http://localhost:11434"},
        },
        "llm_entries": [
            {"id": "e1", "provider": "openai", "model": "gpt-4o",
             "escalation_models": "gpt-4o-mini,gpt-4o"},
            {"id": "e2", "provider": "ollama", "model": "qwen2.5-coder:14b"},
            {"id": "e3", "provider": "anthropic", "model": "claude-3-5-sonnet"},
        ],
        "llm_slots": {"1": "e2", "2": "e1"},
        "chat_slot": 1,
    }


def main():
    print("Running llm_migrate self-test...")
    ok = True

    cfg = _base_config()
    out, changed = llm_migrate.migrate(cfg)

    ok &= _check("migrate reports changed=True on a v1 config", changed is True)
    ok &= _check("stamps llm_config_version=6", out.get("llm_config_version") == 6)
    ok &= _check("deletes llm_slots", "llm_slots" not in out)
    ok &= _check("deletes chat_slot", "chat_slot" not in out)

    by_id = {e["id"]: e for e in out["llm_entries"]}
    ok &= _check("slotted entry e1 enabled=True", by_id["e1"].get("enabled") is True)
    ok &= _check("slotted entry e2 enabled=True", by_id["e2"].get("enabled") is True)
    ok &= _check("unslotted entry e3 enabled=False", by_id["e3"].get("enabled") is False)
    ok &= _check("escalation_models dropped from every entry",
                 all("escalation_models" not in e for e in out["llm_entries"]))

    # chat_slot 1 -> entry e2 (ollama qwen) -> provider|base_url|model
    ok &= _check("chat_pin resolved from chat_slot via entry lookup",
                 out.get("chat_pin") == "ollama|http://localhost:11434|qwen2.5-coder:14b")

    ok &= _check("llm_credentials untouched",
                 out["llm_credentials"] == _base_config()["llm_credentials"])

    # Idempotency: running again is a no-op.
    snapshot = copy.deepcopy(out)
    out2, changed2 = llm_migrate.migrate(out)
    ok &= _check("second run reports changed=False", changed2 is False)
    ok &= _check("second run leaves config identical", out2 == snapshot)

    # A config with no legacy keys still stamps the version and reports changed.
    fresh = {"llm_entries": [{"id": "x", "provider": "openai", "model": "gpt-4o", "enabled": True}]}
    out3, changed3 = llm_migrate.migrate(fresh)
    ok &= _check("bare config gets version-stamped", out3.get("llm_config_version") == 6 and changed3)
    ok &= _check("already-enabled entry keeps its explicit flag",
                 out3["llm_entries"][0]["enabled"] is True)

    # Non-dict input is tolerated.
    _, changed4 = llm_migrate.migrate(None)
    ok &= _check("non-dict input is a safe no-op", changed4 is False)

    print("\nALL CASES PASSED" if ok else "\nONE OR MORE CASES FAILED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
