"""One-shot config migration for the LLM Selection Redesign (Phase 7).

`migrate(config)` upgrades a pre-redesign config in place to schema version 3
and returns ``(config, changed)``. It is:

  * **guarded** — a no-op when ``config.get("llm_config_version") == 3``;
  * **idempotent** — running it twice produces the same result as once (the
    test relies on this);
  * **pure of I/O** — it never loads or saves; the caller (main startup)
    persists the result only when ``changed`` is True.

What it does (see the plan's §7):

  1. ``llm_entries`` already IS the endpoint list. Each entry gains an
     ``enabled`` flag (True if it was assigned to a slot, else False so the
     operator can turn it on in Settings) and loses ``escalation_models``
     (candidate enumeration expands ollama endpoints live, strictly fresher
     than a frozen list).
  2. ``llm_slots`` is consumed to seed those ``enabled`` flags, then deleted.
  3. ``chat_slot`` becomes ``chat_pin`` — a ``provider|base_url|model``
     ModelKey string resolved through the slot→entry lookup — then deleted.
  4. ``llm_credentials`` is left untouched.
  5. Legacy ``LLM_PROVIDER_N``/... env vars are NOT converted: candidate
     enumeration re-reads them live every pass, so a purely env-configured
     container keeps working across restarts.
  6. Capability seeding: every configured endpoint is resolved through the
     model registry; unmatched (provider, model) pairs get an auto-discovery
     stub appended to ``model_registry_auto`` so they surface in Settings for
     review. One summary line is logged.
  7. (v3) Registry top-up of known-capable self-hosted model rules
     (qwen3-coder, llama3.3, gemma4) so a frozen config gains them without a
     manual Settings edit. Append-only/idempotent; see
     model_registry.upgrade_capable_local_rules.
  8. (v4) Reclassify a frozen claude_cli rule from free -> frontier: it is
     session-auth (no key) but bills the operator's Anthropic account, so the
     picker must RESERVE it rather than tie it with the truly-free GPU. See
     model_registry.reclassify_claude_cli_paid.
  9. (v5) Add per-model claude_cli capability rules (Haiku=medium,
     Sonnet/Opus=large) so the light/cheap Haiku is not treated like Opus.
     Append-only/idempotent; see model_registry.upgrade_claude_cli_model_rules.
     Bumping the version re-triggers these one-shot top-ups on an already-
     migrated config.
"""

import logging

import model_registry

logger = logging.getLogger("AppBuilder")

CONFIG_VERSION = 6


def _slot_entry_id(config, slot):
    """The entry id assigned to a legacy slot number (str or int), or None."""
    slots = config.get("llm_slots") or {}
    return slots.get(str(slot))


def _model_key_str(provider, base_url, model):
    """A stable ``provider|base_url|model`` key string (base_url normalised the
    same way llm_client._model_key normalises its tuple)."""
    return "|".join([
        (provider or "").lower().strip(),
        (base_url or "").strip().rstrip("/"),
        (model or "").strip(),
    ])


def _chat_pin_from_slot(config):
    """Resolve the old ``chat_slot`` pin to a ModelKey string via its entry."""
    slot = config.get("chat_slot")
    if slot in (None, ""):
        return None
    entry_id = _slot_entry_id(config, slot)
    if not entry_id:
        return None
    credentials = config.get("llm_credentials") or {}
    for entry in (config.get("llm_entries") or []):
        if entry.get("id") != entry_id:
            continue
        provider = (entry.get("provider") or "").lower().strip()
        model = (entry.get("model") or "").strip()
        cred = credentials.get(provider) or {}
        base_url = (entry.get("base_url") or cred.get("base_url") or "").strip()
        if provider and model:
            return _model_key_str(provider, base_url, model)
    return None


def _seed_registry_stubs(config):
    """Append auto-discovery stubs for every configured (provider, model) the
    curated registry doesn't already cover. Returns (n_endpoints, n_classified,
    n_unclassified). Reads llm_entries + legacy env slots the same way
    llm_client._iter_configured_endpoints does, without importing it (llm_client
    pulls in `main`, which we must not trigger at migration time)."""
    import os

    seen = []
    seen_keys = set()

    def _add(provider, model):
        provider = (provider or "").lower().strip()
        model = (model or "").strip()
        if not provider or not model:
            return
        key = (provider, model)
        if key in seen_keys:
            return
        seen_keys.add(key)
        seen.append(key)

    for entry in (config.get("llm_entries") or []):
        _add(entry.get("provider"), entry.get("model"))
    for n in range(1, 9):
        _add(config.get(f"LLM_PROVIDER_{n}") or os.getenv(f"LLM_PROVIDER_{n}", ""),
             config.get(f"LLM_MODEL_{n}") or os.getenv(f"LLM_MODEL_{n}", ""))

    classified = sum(1 for p, m in seen if model_registry.find_matching_rules(p, m, config))
    stubs = model_registry.registry_sync(seen, config)
    if stubs:
        config.setdefault("model_registry_auto", []).extend(stubs)
    return len(seen), classified, len(seen) - classified


def migrate(config):
    """Upgrade *config* in place to schema version 2. Returns (config, changed)."""
    if not isinstance(config, dict):
        return config, False
    if config.get("llm_config_version") == CONFIG_VERSION:
        return config, False

    slots = config.get("llm_slots") or {}
    slotted_entry_ids = {v for v in slots.values() if v}

    # 1 + 2 + 3: entries gain `enabled`, lose `escalation_models`; slots seed it.
    for entry in (config.get("llm_entries") or []):
        if "enabled" not in entry:
            entry["enabled"] = entry.get("id") in slotted_entry_ids
        entry.pop("escalation_models", None)

    # 4: chat_slot -> chat_pin.
    if "chat_pin" not in config:
        pin = _chat_pin_from_slot(config)
        if pin:
            config["chat_pin"] = pin
    config.pop("chat_slot", None)

    # llm_slots is fully consumed now.
    config.pop("llm_slots", None)

    # Local/free registry top-up: a config["model_registry"] frozen before a
    # local/free provider gained its DEFAULT rule (the `ollama2` GPU-endpoint
    # gap) would classify that endpoint as `unknown` instead of `free`.
    # Idempotent + append-only; see model_registry.upgrade_local_free_rules.
    if isinstance(config.get("model_registry"), list):
        _topped, _added = model_registry.upgrade_local_free_rules(config["model_registry"])
        if _added:
            config["model_registry"] = _topped
            logger.info("LLM registry: added missing local/free rules %s during migration.", _added)
        _topped, _added_cap = model_registry.upgrade_capable_local_rules(config["model_registry"])
        if _added_cap:
            config["model_registry"] = _topped
            logger.info("LLM registry: added capable self-hosted model rules %s during migration.", _added_cap)
        _repaired, _reclassified = model_registry.reclassify_claude_cli_paid(config["model_registry"])
        if _reclassified:
            config["model_registry"] = _repaired
            logger.info("LLM registry: reclassified claude_cli free -> frontier (bills the "
                        "operator's Anthropic account) during migration.")
        _topped, _added_cc = model_registry.upgrade_claude_cli_model_rules(config["model_registry"])
        if _added_cc:
            config["model_registry"] = _topped
            logger.info("LLM registry: added per-model claude_cli capability rules %s "
                        "(Haiku=medium, Sonnet/Opus=large) during migration.", _added_cc)
        _topped, _added_copilot = model_registry.upgrade_copilot_model_rules(config["model_registry"])
        if _added_copilot:
            config["model_registry"] = _topped
            logger.info("LLM registry: added per-model copilot capability rules %s "
                        "(Opus/Sonnet/GPT-5=frontier, Haiku/GPT-4/mini=cheap) during migration.",
                        _added_copilot)
        _topped, _added_router = model_registry.upgrade_openrouter_free_router_rule(config["model_registry"])
        if _added_router:
            config["model_registry"] = _topped
            logger.info("LLM registry: added the OpenRouter Free Models Router rule "
                        "(openrouter/free -> free tier) during migration.")

    # 6: capability seeding + one summary line.
    try:
        n_ep, n_class, n_unclass = _seed_registry_stubs(config)
        logger.info(
            "LLM config migrated to v2: %d endpoints, %d classified, %d unclassified "
            "— review in Settings \u2192 Model Registry.", n_ep, n_class, n_unclass)
    except Exception as e:  # noqa: BLE001 — seeding is best-effort, never blocks boot
        logger.warning("LLM registry capability seeding skipped during migration: %s", e)

    config["llm_config_version"] = CONFIG_VERSION
    return config, True
