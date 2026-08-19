#!/usr/bin/env python3
"""Self-test for save_settings' handling of the feature-auto-drive Settings
fields added in Phase 0 (skills_*) and Phase 1 (feature_drive_*,
feature_boundaries) of the feature auto-drive plan.

Run:  python3 ab/test_feature_settings_roundtrip.py

routes.py cannot be imported directly (main.py's app-init side effects — see
test_dismiss_background_retry.py's docstring), so this extracts save_settings
via ast and execs it with a stubbed FormData/Request, exactly like that file
does for delete_issue.

Regression guard: Settings is ONE <form> spanning every tab, and
saveSettingsAjax submits the WHOLE form on any Save — so a save triggered
from an unrelated tab still carries feature_boundaries_json's current value
(hidden sections' inputs aren't `disabled`, so they still submit). The real
risk this guards against is a field going missing or its parse failing
SILENTLY overwriting an operator's already-configured boundary list with an
empty one — this pins that a parse failure keeps the OLD value and that an
entirely-absent field never touches it either.
"""
import ast
import asyncio
import json
import os
import sys
import tempfile


def _load_ns():
    src = open("routes.py").read()
    tree = ast.parse(src)

    seg = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "save_settings":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "save_settings not found in routes.py"

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    _config_holder = {"config": {}}

    def load_config():
        return dict(_config_holder["config"])

    def save_config(cfg):
        _config_holder["config"] = dict(cfg)

    def clean_repo_name(r):
        return (r or "").strip()

    def parse_module_repo_map(v):
        return {}

    def _reset_llm_semaphore():
        pass

    def validate_llm_config_on_startup():
        pass

    class _RedirectResponse:
        def __init__(self, url=None, status_code=303):
            self.url = url
            self.status_code = status_code

    _env_fd, env_path = tempfile.mkstemp(prefix="bf_test_env_")
    os.close(_env_fd)

    ns = {
        "logger": _NoLog(),
        "load_config": load_config, "save_config": save_config,
        "clean_repo_name": clean_repo_name, "parse_module_repo_map": parse_module_repo_map,
        "_reset_llm_semaphore": _reset_llm_semaphore,
        "validate_llm_config_on_startup": validate_llm_config_on_startup,
        "CHAT_CONFIG_DEFAULTS": {
            "CHAT_TOOL_MAX_ITERATIONS": 5, "CHAT_TOOL_MAX_TOKENS": 4000,
            "CHAT_INDEX_ISSUE_LIMIT": 8, "CHAT_INDEX_CACHE_TTL": 60,
            "CHAT_FIX_PROPOSAL_TTL": 600, "FIX_MAX_FILES": 8,
            "FIX_MAX_FILE_CHARS": 20000, "FIX_MAX_CONTEXT_CHARS": 60000,
            "FIX_MAX_OUTPUT_TOKENS": 4000, "HEARTBEAT_STALE_S": 300,
        },
        "ENV_FILE": env_path,
        "json": json, "os": os,
        "Request": object, "RedirectResponse": _RedirectResponse,
    }
    exec(seg, ns)
    ns["_config_holder"] = _config_holder
    ns["_env_path"] = env_path
    return ns


class _FakeFormData(dict):
    """Mimics Starlette's FormData well enough for save_settings: dict(form_data)
    (last-value-wins, via the dict base) AND .getlist(key) (all values, the
    real multi-value behavior checkboxes need)."""
    def __init__(self, pairs):
        self._pairs = list(pairs)
        super().__init__(pairs)

    def getlist(self, key):
        return [v for k, v in self._pairs if k == key]


class _FakeRequest:
    def __init__(self, pairs, accept="application/json"):
        self._pairs = pairs
        self.headers = {"accept": accept}

    async def form(self):
        return _FakeFormData(self._pairs)


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# The full field set a real save always submits (one shared <form>, hidden
# sections' inputs still present) — a baseline pairs list every test case
# starts from and overrides individual fields on top of.
def _base_pairs(**overrides):
    pairs = [
        ("monitored_repos_extra", ""), ("trusted_repos_extra", ""),
        ("label_mode", "SPECIFIC"), ("custom_labels", ""),
        ("skills_repo", "lbockenstedt/lm"), ("skills_path", ".claude/skills"),
        ("skills_ttl_s", "3600"),
        ("feature_drive_label", "enhancement"),
        ("feature_drive_max_per_cycle", "1"),
        ("feature_drive_repos_extra", ""),
        ("feature_boundaries_json", json.dumps([{"id": "psk", "hard": True}])),
        ("repo_tests", ""),
    ]
    for k, v in overrides.items():
        # Replace if present, else append.
        pairs = [(pk, pv) for pk, pv in pairs if pk != k]
        pairs.append((k, v))
    return pairs


def main():
    ok = True
    ns = _load_ns()

    # ── valid feature_boundaries_json round-trips ───────────────────────────
    ns["_config_holder"]["config"] = {}
    _run(ns["save_settings"](_FakeRequest(_base_pairs())))
    saved = ns["_config_holder"]["config"]
    ok &= _check("valid feature_boundaries_json is parsed and stored",
                saved.get("feature_boundaries") == [{"id": "psk", "hard": True}])

    # ── malformed JSON keeps the previous value, doesn't crash ─────────────
    ns["_config_holder"]["config"] = {"feature_boundaries": [{"id": "existing-rule"}]}
    resp = _run(ns["save_settings"](_FakeRequest(
        _base_pairs(feature_boundaries_json="{not valid json"))))
    saved = ns["_config_holder"]["config"]
    ok &= _check("malformed JSON does NOT wipe the previously-saved boundary list",
                saved.get("feature_boundaries") == [{"id": "existing-rule"}])
    ok &= _check("malformed JSON still returns a success response (with a warning), not a hard failure",
                resp.get("status") == "ok" and "feature_boundaries" in (resp.get("message") or ""))

    # ── a boundary missing "id" is rejected the same way (shape validation) ─
    ns["_config_holder"]["config"] = {"feature_boundaries": [{"id": "existing-rule"}]}
    _run(ns["save_settings"](_FakeRequest(
        _base_pairs(feature_boundaries_json=json.dumps([{"label": "no id field"}])))))
    saved = ns["_config_holder"]["config"]
    ok &= _check("a boundary entry missing \"id\" is rejected, old value kept",
                saved.get("feature_boundaries") == [{"id": "existing-rule"}])

    # ── field entirely ABSENT from the POST never touches the stored value ──
    ns["_config_holder"]["config"] = {"feature_boundaries": [{"id": "existing-rule"}]}
    pairs_without = [(k, v) for k, v in _base_pairs() if k != "feature_boundaries_json"]
    _run(ns["save_settings"](_FakeRequest(pairs_without)))
    saved = ns["_config_holder"]["config"]
    ok &= _check("feature_boundaries_json entirely absent from the request does not wipe the stored list",
                saved.get("feature_boundaries") == [{"id": "existing-rule"}])

    # ── feature_drive_repos: checkbox list + extra text merge (getlist) ─────
    ns["_config_holder"]["config"] = {}
    pairs = _base_pairs(feature_drive_repos_extra="org/extra-repo, org/extra-repo2")
    pairs += [("feature_drive_repos", "owner/repo-a"), ("feature_drive_repos", "owner/repo-b")]
    _run(ns["save_settings"](_FakeRequest(pairs)))
    saved = ns["_config_holder"]["config"]
    ok &= _check("feature_drive_repos merges checked boxes + the extra free-text field",
                saved.get("feature_drive_repos") == ["owner/repo-a", "owner/repo-b", "org/extra-repo", "org/extra-repo2"])

    # ── unrelated-tab save still round-trips the boundary list unchanged ────
    # Simulates saving from a DIFFERENT tab (e.g. LLM) — the shared-form
    # design means feature_boundaries_json is STILL submitted with its
    # current (correctly re-rendered) value; this pins that a save that has
    # nothing to do with Feature Auto-Drive doesn't disturb it.
    ns["_config_holder"]["config"] = {}
    existing = [{"id": "rule-1", "hard": True}, {"id": "rule-2", "hard": False}]
    _run(ns["save_settings"](_FakeRequest(_base_pairs(feature_boundaries_json=json.dumps(existing)))))
    _run(ns["save_settings"](_FakeRequest(_base_pairs(feature_boundaries_json=json.dumps(existing),
                                                       skills_ttl_s="7200"))))  # "different tab" save
    saved = ns["_config_holder"]["config"]
    ok &= _check("a save from an unrelated tab (only skills_ttl_s changed) leaves the boundary list intact",
                saved.get("feature_boundaries") == existing)
    ok &= _check("...while the actually-changed field on that save DID take effect",
                saved.get("skills_ttl_s") == 7200)

    # ── skills_* fields (Phase 0) round-trip correctly ──────────────────────
    ns["_config_holder"]["config"] = {}
    _run(ns["save_settings"](_FakeRequest(_base_pairs(
        skills_enabled="on", skills_repo="  myorg/myrepo  ", skills_path=".claude/skills2",
        skills_ttl_s="120"))))
    saved = ns["_config_holder"]["config"]
    ok &= _check("skills_enabled checked -> True", saved.get("skills_enabled") is True)
    ok &= _check("skills_repo is trimmed", saved.get("skills_repo") == "myorg/myrepo")
    ok &= _check("skills_ttl_s parses to an int", saved.get("skills_ttl_s") == 120)

    ns["_config_holder"]["config"] = {}
    _run(ns["save_settings"](_FakeRequest(_base_pairs(skills_ttl_s="not-a-number"))))
    saved = ns["_config_holder"]["config"]
    ok &= _check("skills_enabled unchecked (absent from POST) -> False",
                saved.get("skills_enabled") is False)
    ok &= _check("a non-numeric skills_ttl_s falls back to the 3600 default, doesn't crash",
                saved.get("skills_ttl_s") == 3600)

    # ── none of the new keys leak into the .env file ────────────────────────
    ns["_config_holder"]["config"] = {}
    _run(ns["save_settings"](_FakeRequest(_base_pairs())))
    with open(ns["_env_path"]) as f:
        env_contents = f.read()
    for leaky_key in ("skills_enabled", "skills_repo", "skills_ttl_s",
                      "feature_drive_enabled", "feature_boundaries", "feature_drive_repos"):
        ok &= _check(f"{leaky_key} is NOT written to .env (config-only, not an env-backed key)",
                    f"{leaky_key}=" not in env_contents)

    os.unlink(ns["_env_path"])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature-drive Settings round-trip self-test...")
    sys.exit(main())
