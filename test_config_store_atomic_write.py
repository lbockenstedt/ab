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
import json
import os
import shutil
import tempfile


def _load_ns():
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_store.py")
    src = open(src_path).read()
    tree = ast.parse(src)

    want_funcs = {
        "_atomic_write_json",
        "save_config", "load_config",
        "save_processed", "load_processed",
        "save_pr_reviews", "load_pr_reviews",
        "save_update_state", "load_update_state",
    }
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                name = getattr(t, "id", "")
                if name in ("CONFIG_DIR", "CONFIG_FILE", "STATE_FILE",
                            "PR_REVIEWS_FILE", "UPDATE_STATE_FILE",
                            "CHAT_CONFIG_DEFAULTS"):
                    segs.append(ast.get_source_segment(src, node))

    module_src = "\n\n".join(segs)

    class _FakeLogger:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass

    ns = {"os": os, "json": json, "logger": _FakeLogger()}
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


if __name__ == "__main__":
    raise SystemExit(main())
