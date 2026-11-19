"""Exercise the actual store and its callers offline, without importing main."""
import ast
import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, get_ident

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_config_store_atomic_write import _load_ns as _store_ns
from test_feature_settings_roundtrip import _FakeRequest as _FormRequest
from test_feature_settings_roundtrip import _base_pairs, _load_ns as _form_ns
from test_llm_credential_redaction import _load_ns as _llm_routes_ns


ROOT = Path(__file__).resolve().parents[1]


def _extract_function(file, name, ns):
    """Load one real function without executing its module's startup code."""
    tree = ast.parse((ROOT / file).read_text())
    node = next(n for n in tree.body if getattr(n, "name", "") == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, "exec"), ns)
    return ns[name]


@pytest.fixture
def store(tmp_path):
    """Load the real store with isolated primary and fallback paths."""
    ns = _store_ns()
    # Redirect both the primary and relative cwd fallback without changing cwd
    # (the settings and LLM extractors read routes.py relative to the repo).
    path = tmp_path / "persistent" / "config.json"
    local = tmp_path / "config.json"
    ns["CONFIG_FILE"] = str(path)
    ns["open"] = lambda file, *a, **k: open(local if file == "config.json" else file, *a, **k)
    write = ns["_atomic_write_json"]
    ns["_atomic_write_json"] = lambda file, *a, **k: write(local if file == "config.json" else file, *a, **k)
    return ns, path, local


def _simultaneous_transactions(store):
    """Make both callers arrive at their transaction boundary before writing."""
    ready = Barrier(2, timeout=5)
    event_loop_thread = get_ident()

    def update(edit):
        """Wait for both worker threads before delegating to the store."""
        assert get_ident() != event_loop_thread
        ready.wait()
        return store["update_config"](edit)

    return update


def test_concurrent_worker_scalar_intents_compose(store):
    """Keep both worker edits when they arrive at the transaction together."""
    ns, _, _ = store
    ns["save_config"]({"initial": True})
    worker_ns = dict(logger=ns["logger"], update_config=_simultaneous_transactions(ns))
    edit = _extract_function("workers.py", "_persist_config_key", worker_ns)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(edit, k, k) for k in ("worker_a", "worker_b")]
        for future in futures:
            future.result(timeout=10)
    config = ns["load_config"]()
    assert (config["worker_a"], config["worker_b"], config["initial"]) == ("worker_a", "worker_b", True)


def test_startup_migration_can_replace_snapshot_and_logs_after_commit(store):
    """Persist a migration's replacement dict before replaying its logs."""
    ns, path, _ = store
    ns["save_config"]({"legacy": True})
    emitted = []

    class Log:
        def info(self, message):
            """Record the log only after the new config is visible on disk."""
            assert json.loads(path.read_text())["current"] is True
            emitted.append(message)

    run = _extract_function("main.py", "_persist_llm_migration",
                            {"logger": Log(), "update_config": ns["update_config"]})

    def migrate(config, *, log):
        """Simulate a migration that returns a new config object."""
        assert config.pop("legacy") is True
        log.info("migrated")
        assert not emitted
        return {**config, "current": True}, True

    assert run(migrate)
    assert emitted == ["migrated"]
    assert "legacy" not in ns["load_config"]()


def test_concurrent_endpoint_and_credential_intents_compose(store):
    """Keep independent route edits and make absent endpoint operations write-free."""
    ns, _, _ = store
    ns["save_config"]({"llm_credentials": {"old": {"api_key": "test-key"}, "keep": {}}})
    routes = _llm_routes_ns()
    request = routes["_FakeRequest"]

    async def race(*operations):
        """Run two routes with synchronized entry to their transactions."""
        routes["update_config"] = _simultaneous_transactions(ns)
        return await asyncio.gather(*operations)

    async def exercise_routes():
        """Exercise concurrent adds and edits, then update an absent entry."""
        first, second = await race(
            routes["create_llm_entry"](request({"model": "first", "provider": "old"})),
            routes["create_llm_entry"](request({"model": "second"})),
        )
        first_id, second_id = first["entry"]["id"], second["entry"]["id"]
        assert {e["id"] for e in ns["load_config"]()["llm_entries"]} == {first_id, second_id}
        added, removed = await race(
            routes["save_llm_credential"](request({"provider": "new", "base_url": "https://example.invalid"})),
            routes["delete_llm_credential"](request({"provider": "old"})),
        )
        assert added["status"] == "ok" and removed["removed"] and removed["entries_using"] == 1
        assert set(ns["load_config"]()["llm_credentials"]) == {"keep", "new"}
        updated, deleted = await race(
            routes["update_llm_entry"](second_id, request({"label": "edited", "rpm": 0})),
            routes["delete_llm_entry"](first_id),
        )
        assert updated["status"] == deleted["status"] == "ok"
        assert [(e["id"], e["model"], e["label"], e["rpm"]) for e in ns["load_config"]()["llm_entries"]] == [
            (second_id, "second", "edited", 0)
        ]

        def unexpected(*args, **kwargs):
            """Fail if an absent endpoint causes any write or re-probe."""
            pytest.fail("missing entry must not write or refresh endpoints")

        routes.update(update_config=ns["update_config"], _refresh_llm_endpoints=unexpected)
        ns["_atomic_write_json"] = unexpected
        assert (await routes["update_llm_entry"](first_id, request({"label": "too late"}))).status_code == 404
        return first_id

    missing_id = asyncio.run(exercise_routes())
    app = FastAPI()
    app.delete("/api/llm/entries/{entry_id}")(routes["delete_llm_entry"])
    with TestClient(app) as client:
        response = client.delete(f"/api/llm/entries/{missing_id}")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.parametrize("accept", ["application/json", "text/html"])
def test_stale_settings_form_does_not_save_or_run_side_effects(store, tmp_path, accept, monkeypatch):
    """Return a JSON or HTML conflict response without running post-save work."""
    ns, path, _ = store
    ns["save_config"]({"monitored_repos": ["old/repo"], "pr_review_lm_summary_repos": ["old/repo"]})
    env_file = tmp_path / ".env"
    form = _form_ns(env_path=str(env_file))
    env_file.write_text("LLM_TIMEOUT=15\n")
    monkeypatch.setenv("LLM_TIMEOUT", "15")

    def unexpected(*args, **kwargs):
        """Fail if the stale form attempts a commit or post-save work."""
        pytest.fail("stale form must not run its mutator, write, reset or revalidate")

    def interleave(_edit, *, expected):
        """Commit a competing edit before the stale form checks its baseline."""
        def concurrent_edit(current):
            """Change both linked repository settings in the same commit."""
            current["monitored_repos"] = ["new/repo"]
            current["pr_review_lm_summary_repos"] = ["new/repo"]

        ns["update_config"](concurrent_edit)
        ns["_atomic_write_json"] = unexpected
        return ns["update_config"](unexpected, expected=expected)

    form.update(load_config=ns["load_config"], CHAT_CONFIG_DEFAULTS=ns["CHAT_CONFIG_DEFAULTS"],
                ConfigConflictError=ns["ConfigConflictError"], ENV_FILE=str(env_file),
                update_config=interleave, _reset_llm_semaphore=unexpected,
                validate_llm_config_on_startup=unexpected)
    request = _FormRequest(_base_pairs(LLM_TIMEOUT="60", monitored_repos_extra="form/repo"), accept)
    response = asyncio.run(form["save_settings"](request))
    assert response.status_code == 409
    if accept == "application/json":
        assert response.headers["content-type"] == "application/json"
        body = json.loads(response.body)
        assert body["status"] == "error" and "Reload" in body["message"]
    else:
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        body = response.body.decode("utf-8")
        assert body.startswith("<!DOCTYPE html>")
        assert "Your changes were not saved." in body
        assert '<a href="/settings">Reload the Settings page</a> and retry.' in body
    persisted = json.loads(path.read_text())
    assert persisted["monitored_repos"] == persisted["pr_review_lm_summary_repos"] == ["new/repo"]
    assert env_file.read_text() == "LLM_TIMEOUT=15\n"
    assert os.environ["LLM_TIMEOUT"] == "15"


def test_current_settings_form_replaces_snapshot_including_removed_key(store, tmp_path):
    """Allow a fresh form to remove a previously stored override."""
    ns, path, _ = store
    ns["save_config"]({"diagnostic_fix_models": ["test-override"]})
    form = _form_ns(env_path=str(tmp_path / ".env"))
    form.update(load_config=ns["load_config"], CHAT_CONFIG_DEFAULTS=ns["CHAT_CONFIG_DEFAULTS"],
                update_config=ns["update_config"], ConfigConflictError=ns["ConfigConflictError"])
    response = asyncio.run(form["save_settings"](_FormRequest(_base_pairs(model_stage_diag_fix=""))))
    assert response["status"] == "ok"
    assert "diagnostic_fix_models" not in json.loads(path.read_text())


def test_defaulted_baselines_and_unshared_defaults(store):
    """Match materialized defaults without sharing them with callbacks or responses."""
    ns, path, local = store
    baseline = ns["load_config"]()
    assert not path.exists() and not local.exists()
    baseline["console_accounts"].append("local-only")
    assert ns["load_config"]()["console_accounts"] == []
    assert ns["CHAT_CONFIG_DEFAULTS"]["console_accounts"] == []

    baseline = ns["load_config"]()
    ns["save_config"](baseline)  # materialize the same defaulted view

    def edit(draft):
        """Remove one key and append to a list default in the draft."""
        del draft["GITHUB_TOKEN"]
        draft["console_accounts"].append("operator")
        return draft["console_accounts"]

    result = ns["update_config"](edit, expected=baseline)
    assert result == ["operator"]
    result.append("mutated response")
    persisted = json.loads(path.read_text())
    assert "GITHUB_TOKEN" not in persisted and persisted["console_accounts"] == ["operator"]
    assert baseline["console_accounts"] == ns["CHAT_CONFIG_DEFAULTS"]["console_accounts"] == []
    path.write_text("{bad")
    assert ns["load_config"]()["console_accounts"] == ["operator"]


def test_first_boot_cwd_fallback_and_noop(store):
    """Skip a no-op and allow cwd persistence when no primary exists."""
    ns, path, local = store
    write = ns["_atomic_write_json"]
    writes = []

    def fail_primary(file, *args, **kwargs):
        """Simulate unavailable primary storage but a writable cwd."""
        writes.append(file)
        if file == str(path):
            raise PermissionError("test persistent failure")
        return write(file, *args, **kwargs)

    ns["_atomic_write_json"] = fail_primary
    assert ns["update_config"](lambda cfg: cfg["enabled_models"]) == []
    assert not writes
    ns["update_config"](lambda cfg: cfg["enabled_models"].append("offline-model"),
                        expected=ns["load_config"]())
    assert writes == [str(path), "config.json"]
    assert json.loads(local.read_text())["enabled_models"] == ["offline-model"]
    assert ns["load_config"]()["enabled_models"] == ["offline-model"]


def test_unreadable_primary_leaves_cache_and_disk_untouched(store):
    """Reject an unreadable primary without editing the cache or files."""
    ns, path, local = store
    ns["save_config"]({"enabled_models": ["saved"]})
    cache = ns["_last_good_config"]
    path.write_text("{bad")
    local.write_text('{"enabled_models":["fallback"]}')

    def unexpected(*args, **kwargs):
        """Fail if the mutator or writer runs against unreadable config."""
        pytest.fail("unreadable primary must not invoke the editor or writer")

    ns["_atomic_write_json"] = unexpected
    with pytest.raises(ns["ConfigUnreadableError"]):
        ns["update_config"](unexpected)
    assert ns["_last_good_config"] is cache
    assert ns["load_config"]()["enabled_models"] == ["saved"]
    assert path.read_text() == "{bad" and json.loads(local.read_text())["enabled_models"] == ["fallback"]


def test_existing_primary_write_failure_does_not_fall_back(store):
    """Surface a failed write when the primary would hide a cwd fallback."""
    ns, path, local = store
    ns["save_config"]({"enabled_models": ["old"]})

    def fail_primary(file, *args, **kwargs):
        """Fail the primary write and reject any fallback attempt."""
        assert file == str(path), "an existing primary would hide a cwd write"
        raise PermissionError("test persistent failure")

    ns["_atomic_write_json"] = fail_primary
    with pytest.raises(PermissionError, match="test persistent failure"):
        ns["update_config"](lambda cfg: cfg["enabled_models"].append("new"))
    assert json.loads(path.read_text()) == {"enabled_models": ["old"]}
    assert not local.exists()


def test_no_production_raw_config_saves():
    """Reject direct uses of the raw writer outside the store and tests."""
    violations = []
    # CI also checks out dependency repos here; they do not belong to AB.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"], cwd=ROOT, check=True,
        capture_output=True, text=True).stdout
    for filename in filter(None, tracked.split("\0")):
        relative = Path(filename)
        if (relative.name.startswith("test_") or relative == Path("config_store.py")
                or any(p.startswith(".") or p in {"tests", "__pycache__", "venv"}
                       for p in relative.parts[:-1])):
            continue
        path = ROOT / relative
        for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
            # Include callback references and aliased imports, not just calls.
            if ((isinstance(node, ast.Name) and node.id == "save_config")
                    or (isinstance(node, ast.Attribute) and node.attr == "save_config")
                    or (isinstance(node, ast.alias) and node.name == "save_config")):
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, f"Use update_config for production writes: {violations}"


def test_source_guard_ignores_foreign_checkout_but_checks_owned_files(tmp_path, monkeypatch):
    """CI dependency checkouts are not AB sources; tracked AB files remain guarded."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    owned = tmp_path / "owned.py"
    owned.write_text("update_config(lambda config: None)\n")
    subprocess.run(["git", "add", "owned.py"], cwd=tmp_path, check=True)
    dependency = tmp_path / "foreign_checkout"
    dependency.mkdir()
    (dependency / "module.py").write_text("save_config({})\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    test_no_production_raw_config_saves()

    owned.write_text("save_config({})\n")
    with pytest.raises(AssertionError, match=r"owned\.py:1"):
        test_no_production_raw_config_saves()
