#!/usr/bin/env python3
"""Self-test for config_store's persistent-file writers being crash-safe.

Run:  python3 ab/test_config_store_atomic_write.py

config_store.py cannot be imported directly (it does `from main import
logger`, and main.py's own import chain circularly re-imports config_store —
see test_dismiss_background_retry.py's docstring for the same problem with
routes.py), so this extracts the pure save/load functions via ast and execs
them with a stubbed logger/os/json, exactly like that file does for
delete_issue.

Regression guard: AppBuilder Hub logged
  "Error reading persistent config /etc/ab/config.json: Expecting
  value: line 1 column 1 (char 0)"
which is exactly what json.load raises against a zero-byte file. The old
save_config/save_processed/save_pr_reviews/save_update_state each opened
their target path directly with `open(path, "w")` — that call TRUNCATES the
file immediately, before a single byte of the new JSON is written. A process
death (crash, OOM-kill, forced restart) in the window between the truncate
and the json.dump completing leaves a permanently empty/partial file, so the
very next load silently discards the persisted state (falling back to
defaults or stale local data) instead of surfacing the loss. save_llm_tps
already avoided this via a temp-file + os.replace swap; this test pins that
save_config/save_processed/save_pr_reviews/save_update_state now do the
same: the on-disk file only ever reflects a fully-written old or new
version, never a half-written one.
"""
import ast
import copy
import json
import os
import shutil
import tempfile
import threading


def _load_ns():
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config_store.py")
    src = open(src_path).read()
    tree = ast.parse(src)

    want_funcs = {
        "_atomic_write_json", "_read_persistent_config", "_remember_good_config",
        "_apply_config_defaults", "_config_view", "_save_config",
        "save_config", "load_config", "update_config",
        "ConfigUnreadableError", "ConfigConflictError",
        "save_processed", "load_processed",
        "save_pr_reviews", "load_pr_reviews",
        "save_update_state", "load_update_state",
    }
    segs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                name = getattr(t, "id", "")
                if name in ("CONFIG_DIR", "CONFIG_FILE", "STATE_FILE",
                            "PR_REVIEWS_FILE", "UPDATE_STATE_FILE",
                            "CHAT_CONFIG_DEFAULTS",
                            "_config_lock", "_last_good_config", "_unreadable_logged"):
                    segs.append(ast.get_source_segment(src, node))

    module_src = "\n\n".join(segs)

    class _FakeLogger:
        """Records info() and error() lines so tests can check what was logged."""
        def __init__(self):
            self.errors = []
            self.infos = []
        def info(self, msg, *a, **k): self.infos.append(msg)
        def warning(self, *a, **k): pass
        def error(self, msg, *a, **k): self.errors.append(msg)

    ns = {"os": os, "json": json, "copy": copy, "threading": threading, "logger": _FakeLogger()}
    exec(compile(module_src, "config_store_extract", "exec"), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running config_store atomic-write self-test...")
    ok = True

    tmpdir = tempfile.mkdtemp(prefix="bf_config_store_test_")
    orig_cwd = os.getcwd()
    try:
        os.chdir(tmpdir)
        ns = _load_ns()

        # Point every persistent path at a real, writable dir inside tmpdir so
        # save_config's `os.access(CONFIG_DIR, os.W_OK)` primary-storage branch
        # is actually exercised (matches production; "config directory not
        # writable" would just silently redirect to the local-fallback branch
        # and never touch the temp-file/os.replace code path under test).
        persistent_dir = os.path.join(tmpdir, "etc_ab")
        os.makedirs(persistent_dir, exist_ok=True)
        ns["CONFIG_DIR"] = persistent_dir
        ns["CONFIG_FILE"] = os.path.join(persistent_dir, "config.json")
        ns["STATE_FILE"] = os.path.join(persistent_dir, "processed_issues.json")
        ns["PR_REVIEWS_FILE"] = os.path.join(persistent_dir, "pr_reviews.json")
        ns["UPDATE_STATE_FILE"] = os.path.join(persistent_dir, "update_state.json")

        # --- save_config: normal round trip still works -----------------
        cfg1 = {"monitored_repos": ["a/b"], "enabled_models": ["x"]}
        ns["save_config"](cfg1)
        ok &= _check("save_config writes a file readable back",
                      ns["load_config"]()["monitored_repos"] == ["a/b"])
        ok &= _check("save_config leaves no leftover .tmp file",
                      not os.path.exists(ns["CONFIG_FILE"] + ".tmp"))

        # --- save_config: simulated crash mid-write must not corrupt -----
        # json.dump raising partway through (e.g. an unserializable value
        # slipped in, or the process was killed) must not leave CONFIG_FILE
        # truncated -- the OLD fully-written content must still be there.
        real_dump = json.dump

        def _boom(*a, **k):
            raise RuntimeError("simulated crash mid-write")

        ns["json"] = type("J", (), {"dump": staticmethod(_boom),
                                     "load": staticmethod(json.load)})()
        try:
            ns["save_config"]({"monitored_repos": ["should-not-land"]})
        except Exception:
            pass
        ns["json"] = json  # restore
        with open(ns["CONFIG_FILE"]) as f:
            surviving = json.load(f)
        ok &= _check(
            "a failed save_config write leaves the OLD config intact, not truncated",
            surviving.get("monitored_repos") == ["a/b"],
        )

        # --- load_config: a genuinely empty/corrupt file must not crash --
        with open(ns["CONFIG_FILE"], "w") as f:
            f.write("")  # zero-byte file, reproduces the reported log line
        try:
            cfg = ns["load_config"]()
            ok &= _check("load_config against a zero-byte file returns a dict, doesn't raise",
                         isinstance(cfg, dict))
        except Exception as e:
            ok &= _check(f"load_config against a zero-byte file doesn't raise (raised {e!r})", False)

        # --- save_processed / save_pr_reviews / save_update_state --------
        # Reset config.json to something valid for the remaining checks.
        ns["save_config"](cfg1)

        ns["save_processed"]({"1": {"status": "fixed"}})
        ok &= _check("save_processed round-trips",
                      ns["load_processed"]() == {"1": {"status": "fixed"}})
        ok &= _check("save_processed leaves no leftover .tmp file",
                      not os.path.exists(ns["STATE_FILE"] + ".tmp"))

        ns["save_pr_reviews"]({"o/r#1": {"status": "approved"}})
        ok &= _check("save_pr_reviews round-trips",
                      ns["load_pr_reviews"]() == {"o/r#1": {"status": "approved"}})
        ok &= _check("save_pr_reviews leaves no leftover .tmp file",
                      not os.path.exists(ns["PR_REVIEWS_FILE"] + ".tmp"))

        ns["save_update_state"]({"last_known_good_commit": "abc123"})
        ok &= _check("save_update_state round-trips",
                      ns["load_update_state"]()["last_known_good_commit"] == "abc123")
        ok &= _check("save_update_state leaves no leftover .tmp file",
                      not os.path.exists(ns["UPDATE_STATE_FILE"] + ".tmp"))

        # --- ENOENT: the writers must CREATE the persistent dir ----------
        # Regression guard. Only save_config ever called os.makedirs(CONFIG_DIR);
        # save_processed / save_pr_reviews / save_llm_tps / save_update_state and
        # the chat/self-scan/startup-stamp writers opened their paths under
        # /etc/ab directly. On any host where that directory did not exist, every
        # one of them failed with "[Errno 2] No such file or directory" -- and
        # since those failures log at ERROR, the self-log scanner filed a fresh
        # duplicate issue per occurrence (twelve before the cause was found).
        # Point the paths at a directory that does NOT exist and require the
        # writes to succeed anyway.
        missing_dir = os.path.join(tmpdir, "no_such_dir", "etc_ab")
        ok &= _check("test precondition: target dir absent",
                      not os.path.exists(missing_dir))
        ns["CONFIG_DIR"] = missing_dir
        ns["CONFIG_FILE"] = os.path.join(missing_dir, "config.json")
        ns["STATE_FILE"] = os.path.join(missing_dir, "processed_issues.json")
        ns["PR_REVIEWS_FILE"] = os.path.join(missing_dir, "pr_reviews.json")
        ns["UPDATE_STATE_FILE"] = os.path.join(missing_dir, "update_state.json")

        ns["save_processed"]({"o/r": [7]})
        ok &= _check("save_processed creates missing dir (no ENOENT)",
                      os.path.exists(ns["STATE_FILE"]))
        ok &= _check("save_processed round-trips into created dir",
                      ns["load_processed"]() == {"o/r": [7]})
        ok &= _check("save_processed did NOT fall back to cwd",
                      not os.path.exists(os.path.join(tmpdir, "processed_issues.json")))

        ns["save_pr_reviews"]({"o/r#2": {"status": "denied"}})
        ok &= _check("save_pr_reviews creates missing dir (no ENOENT)",
                      ns["load_pr_reviews"]() == {"o/r#2": {"status": "denied"}})

        ns["save_update_state"]({"last_known_good_commit": "def456"})
        ok &= _check("save_update_state creates missing dir (no ENOENT)",
                      ns["load_update_state"]()["last_known_good_commit"] == "def456")

        deep = os.path.join(tmpdir, "a", "b", "c", "cfg.json")
        ns["save_config"].__globals__  # noqa: B018  (ns funcs share `ns` as globals)
        ns["CONFIG_DIR"] = os.path.dirname(deep)
        ns["CONFIG_FILE"] = deep
        ns["save_config"]({"monitored_repos": ["deep/repo"]})
        ok &= _check("save_config creates nested missing dirs",
                      os.path.exists(deep))

        # --- _atomic_write_json contract ---------------------------------
        aw = ns["_atomic_write_json"]
        p = os.path.join(tmpdir, "mk", "x.json")
        aw(p, {"k": 1})
        ok &= _check("_atomic_write_json creates parent dir",
                      json.load(open(p)) == {"k": 1})
        ok &= _check("_atomic_write_json leaves no .tmp",
                      not os.path.exists(p + ".tmp"))

        pc = os.path.join(tmpdir, "mk", "compact.json")
        aw(pc, {"k": 1}, indent=None)
        ok &= _check("_atomic_write_json indent=None writes compact JSON",
                      open(pc).read() == '{"k": 1}')

        pm = os.path.join(tmpdir, "mk", "mode.json")
        aw(pm, {"k": 1}, chmod=0o600)
        ok &= _check("_atomic_write_json applies chmod before replace",
                      (os.stat(pm).st_mode & 0o777) == 0o600)

        # Unserialisable payload: must re-raise (callers own the fallback) and
        # must not leave the temp file behind for the next reader to trip on.
        pbad = os.path.join(tmpdir, "mk", "bad.json")
        raised = False
        try:
            aw(pbad, {"k": object()})
        except Exception:
            raised = True
        ok &= _check("_atomic_write_json re-raises on failure", raised)
        ok &= _check("_atomic_write_json cleans up .tmp on failure",
                      not os.path.exists(pbad + ".tmp"))
        ok &= _check("_atomic_write_json wrote no target file on failure",
                      not os.path.exists(pbad))

    finally:
        os.chdir(orig_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)

    if ok:
        print("\nALL CASES PASSED")
        return 0
    print("\nSOME CASES FAILED")
    return 1


def test_overlapping_atomic_writes(tmp_path, monkeypatch):
    """Interleaved saves must not consume another writer's temporary file."""
    aw = _load_ns()["_atomic_write_json"]
    target = tmp_path / "missing" / "nested" / "processed_issues.json"
    replace = os.replace
    sources = []

    def interleaved_replace(src, dst):
        """Finish another save while the first is about to replace its target."""
        sources.append(src)
        if len(sources) == 1:
            aw(str(target), {"writer": "second"})
            assert json.loads(target.read_text()) == {"writer": "second"}
        replace(src, dst)

    monkeypatch.setattr(os, "replace", interleaved_replace)
    aw(str(target), {"writer": "first"})
    assert len(set(sources)) == 2
    assert json.loads(target.read_text()) == {"writer": "first"}
    assert list(target.parent.iterdir()) == [target]


def test_failed_atomic_write_preserves_target(tmp_path):
    """Serialization failure preserves old JSON and removes the unique temp."""
    aw = _load_ns()["_atomic_write_json"]
    target = tmp_path / "processed_issues.json"
    aw(str(target), {"old": True})
    try:
        aw(str(target), {"bad": object()})
    except TypeError:
        pass
    else:
        raise AssertionError("Serialization failure must reach the caller")
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [target]


def _ns_for(tmp_path, monkeypatch):
    """Extracted config_store functions pointed at a temp CONFIG_FILE, cwd isolated."""
    monkeypatch.chdir(tmp_path)
    ns = _load_ns()
    ns["CONFIG_DIR"] = str(tmp_path / "etc_ab")
    ns["CONFIG_FILE"] = str(tmp_path / "etc_ab" / "config.json")
    os.makedirs(ns["CONFIG_DIR"])
    return ns


def test_unreadable_config_keeps_last_good_and_is_never_overwritten(tmp_path, monkeypatch):
    """EACCES on config.json (root-owned after a maintenance script) must not
    turn into 'defaults' that the next save writes over the stored LLM endpoints."""
    ns = _ns_for(tmp_path, monkeypatch)
    good = {"llm_entries": [{"id": "e1"}, {"id": "e2"}], "monitored_repos": ["a/b"]}
    ns["save_config"](good)
    assert len(ns["load_config"]()["llm_entries"]) == 2
    stored = open(ns["CONFIG_FILE"], "rb").read()

    real_open = open

    def denied_open(path, *a, **k):
        """Simulate EACCES for the config file only (also holds when tests run as root)."""
        if path == ns["CONFIG_FILE"]:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, *a, **k)

    ns["open"] = denied_open
    for _ in range(3):  # the heartbeat worker polls every 5 seconds
        assert len(ns["load_config"]()["llm_entries"]) == 2
    assert len(ns["logger"].errors) == 1, "the outage is logged once, not per poll"

    try:
        ns["save_config"]({"llm_entries": []})
    except ns["ConfigUnreadableError"]:
        pass
    else:
        raise AssertionError("save_config must refuse while the config is unreadable")
    assert real_open(ns["CONFIG_FILE"], "rb").read() == stored
    assert not os.path.exists("config.json"), "no throwaway local fallback copy either"

    del ns["open"]
    assert len(ns["load_config"]()["llm_entries"]) == 2


def test_unreadable_config_on_fresh_start_is_not_saved_over(tmp_path, monkeypatch):
    """With no last-good config (a restart) a corrupt file yields defaults to
    run on, but the startup migration's save_config must not replace the file."""
    ns = _ns_for(tmp_path, monkeypatch)
    for bad in ('{"llm_entries": [', "[]"):
        with open(ns["CONFIG_FILE"], "w") as f:
            f.write(bad)
        assert isinstance(ns["load_config"](), dict)
        try:
            ns["save_config"]({"llm_entries": []})
        except ns["ConfigUnreadableError"]:
            pass
        else:
            raise AssertionError(f"save_config overwrote unreadable config {bad!r}")
        assert open(ns["CONFIG_FILE"]).read() == bad


def test_save_refreshes_the_last_good_config(tmp_path, monkeypatch):
    """Load A, save B, then the file becomes unreadable before the next load: the config
    served is B (what is on disk), not the stale A."""
    ns = _ns_for(tmp_path, monkeypatch)
    ns["save_config"]({"llm_entries": [{"id": "a"}]})
    assert ns["load_config"]()["llm_entries"] == [{"id": "a"}]
    ns["save_config"]({"llm_entries": [{"id": "b"}], "monitored_repos": ["x/y"]})

    real_open = open

    def denied_open(path, *a, **k):
        """Simulate EACCES for the config file only."""
        if path == ns["CONFIG_FILE"]:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, *a, **k)

    ns["open"] = denied_open
    served = ns["load_config"]()
    assert served["llm_entries"] == [{"id": "b"}] and served["monitored_repos"] == ["x/y"]
    assert "enabled_models" in served, "the served copy carries the same defaults as a normal load"


def test_unwritable_everywhere_raises_instead_of_a_false_success(tmp_path, monkeypatch):
    """Persistent write and local fallback both fail (full disk, read-only mount): nothing
    was stored, so save_config raises and its caller does not report success."""
    ns = _ns_for(tmp_path, monkeypatch)

    def no_space(*a, **k):
        """Stand in for _atomic_write_json failing on every target."""
        raise OSError(28, "No space left on device")

    ns["_atomic_write_json"] = no_space
    try:
        ns["save_config"]({"monitored_repos": ["x/y"]})
    except OSError:
        pass
    else:
        raise AssertionError("save_config returned normally though nothing was stored")
    assert any("Critical failure saving config" in m for m in ns["logger"].errors)


def _fail_persistent_write_only(ns):
    """Make _atomic_write_json fail for CONFIG_FILE (e.g. its directory is not writable)."""
    real = ns["_atomic_write_json"]

    def write(path, *a, **k):
        """Fail the persistent target, write any other."""
        if path == ns["CONFIG_FILE"]:
            raise PermissionError(13, "Permission denied", path)
        return real(path, *a, **k)

    ns["_atomic_write_json"] = write


def test_unwritable_persistent_dir_raises_when_a_config_exists(tmp_path, monkeypatch):
    """A readable CONFIG_FILE that cannot be replaced: a ./config.json saved instead would
    never be read back (load_config prefers CONFIG_FILE), so the save must raise, not succeed."""
    ns = _ns_for(tmp_path, monkeypatch)
    ns["save_config"]({"monitored_repos": ["old"]})
    _fail_persistent_write_only(ns)
    try:
        ns["save_config"]({"monitored_repos": ["new"]})
    except PermissionError:
        pass
    else:
        raise AssertionError("save_config reported success for a config that load_config ignores")
    assert not os.path.exists("config.json")
    assert ns["load_config"]()["monitored_repos"] == ["old"]


def test_unwritable_persistent_dir_still_falls_back_on_first_boot(tmp_path, monkeypatch):
    """With no CONFIG_FILE yet, the local fallback is the one load_config reads: keep it."""
    ns = _ns_for(tmp_path, monkeypatch)
    _fail_persistent_write_only(ns)
    ns["save_config"]({"monitored_repos": ["new"]})
    assert ns["load_config"]()["monitored_repos"] == ["new"]


def test_unreadable_after_a_deleted_file_serves_the_config_actually_in_use(tmp_path, monkeypatch):
    """Readable A -> file deleted or emptied -> unreadable. If ./config.json B took over, B is
    what AB runs on and is served. With no fallback file only the defaults are in use, which
    never replace A: a truncate-then-rewrite by a root script passes through exactly that state."""
    ns = _ns_for(tmp_path, monkeypatch)
    real_open = open

    def denied_open(path, *a, **k):
        """Simulate EACCES for the config file only."""
        if path == ns["CONFIG_FILE"]:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, *a, **k)

    ns["save_config"]({"monitored_repos": ["A"]})
    open(ns["CONFIG_FILE"], "w").close()
    assert ns["load_config"]()["monitored_repos"] == []
    monkeypatch.setitem(ns, "open", denied_open)
    assert ns["load_config"]()["monitored_repos"] == ["A"]

    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    ns["save_config"]({"monitored_repos": ["A"]})
    os.remove(ns["CONFIG_FILE"])
    with open("config.json", "w") as f:
        f.write('{"monitored_repos": ["B"]}')
    assert ns["load_config"]()["monitored_repos"] == ["B"]
    monkeypatch.setitem(ns, "open", denied_open)
    assert ns["load_config"]()["monitored_repos"] == ["B"]


def test_save_to_the_local_fallback_refreshes_the_last_good_config(tmp_path, monkeypatch):
    """No CONFIG_FILE yet: local config A is loaded, B is saved to ./config.json, then
    CONFIG_FILE appears corrupt. The config served is B (what was just saved), not A."""
    ns = _ns_for(tmp_path, monkeypatch)
    with open("config.json", "w") as f:
        f.write('{"monitored_repos": ["A"]}')
    assert ns["load_config"]()["monitored_repos"] == ["A"]
    _fail_persistent_write_only(ns)
    ns["save_config"]({"monitored_repos": ["B"]})
    with open(ns["CONFIG_FILE"], "w") as f:
        f.write("{")
    assert ns["load_config"]()["monitored_repos"] == ["B"]


def test_recovery_is_reported_only_for_a_parsed_config(tmp_path, monkeypatch):
    """An unreadable file that is later fixed is "readable again". One that was deleted or
    emptied is not a recovery: it is reported as missing/empty, and the next unreadable
    file is reported again (the one-ERROR latch is reset in both cases)."""
    ns = _ns_for(tmp_path, monkeypatch)
    ns["save_config"]({"llm_entries": [{"id": "a"}]})
    real_open = open

    def denied_open(path, *a, **k):
        """Simulate EACCES for the config file only."""
        if path == ns["CONFIG_FILE"]:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, *a, **k)

    def readable_again(infos):
        return [m for m in infos if "readable again" in m]

    ns["open"] = denied_open
    ns["load_config"]()
    del ns["open"]
    os.remove(ns["CONFIG_FILE"])
    ns["load_config"]()
    assert readable_again(ns["logger"].infos) == []
    assert any("missing or empty" in m for m in ns["logger"].infos)

    ns["save_config"]({"llm_entries": [{"id": "b"}]})
    ns["open"] = denied_open
    ns["load_config"]()
    assert len(ns["logger"].errors) == 2, "the latch was reset, so the second outage is logged"
    del ns["open"]
    ns["load_config"]()
    assert len(readable_again(ns["logger"].infos)) == 1


def test_empty_or_missing_config_can_still_be_saved(tmp_path, monkeypatch):
    """First boot (no file) and the zero-byte leftover of a crash hold nothing to lose."""
    ns = _ns_for(tmp_path, monkeypatch)
    ns["save_config"]({"monitored_repos": ["x/y"]})
    os.remove(ns["CONFIG_FILE"])
    open(ns["CONFIG_FILE"], "w").close()
    ns["save_config"]({"monitored_repos": ["x/y"]})
    assert ns["load_config"]()["monitored_repos"] == ["x/y"]


if __name__ == "__main__":
    raise SystemExit(main())
