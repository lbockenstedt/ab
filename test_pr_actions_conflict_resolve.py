#!/usr/bin/env python3
"""Self-test for pr_actions' merge-conflict auto-resolver.

Run:  python3 ab/test_pr_actions_conflict_resolve.py

pr_actions.py imports `main`/`app_state` at module scope (importing it boots
the live app as a side effect — see test_feature_automerge_gate.py's
docstring), so we extract the pure pieces (`_AUTO_RESOLVE_CONFLICTS`,
`_conflicted_paths`, `_resolve_tree_conflicts`) by source via ast and exercise
them against a REAL local git repo — no network, no GitHub.

What must hold:
  * a lone cosmetic (VERSION) conflict auto-resolves to the base's value
    ("theirs") and leaves a completed merge commit, and
  * ANY non-allowlisted (code) conflict aborts the merge and hands back to a
    human — the single safety-critical property (never machine-merge code).
"""
import ast
import os
import sys
import tempfile

import git


def _load_ns():
    src = open("pr_actions.py").read()
    tree = ast.parse(src)
    want_fn = {"_conflicted_paths", "_resolve_tree_conflicts"}
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_AUTO_RESOLVE_CONFLICTS" for t in node.targets):
            exec(ast.get_source_segment(src, node), ns)
        if isinstance(node, ast.FunctionDef) and node.name in want_fn:
            exec(ast.get_source_segment(src, node), ns)
    assert "_AUTO_RESOLVE_CONFLICTS" in ns and want_fn <= ns.keys(), "extraction failed"
    return ns


def _repo(tmp):
    r = git.Repo.init(tmp)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "t")
        cw.set_value("user", "email", "t@t")
    return r


def _write(tmp, name, text):
    with open(os.path.join(tmp, name), "w") as f:
        f.write(text)


def _fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def test_version_only_conflict_autoresolves_to_theirs():
    ns = _load_ns()
    with tempfile.TemporaryDirectory() as tmp:
        r = _repo(tmp)
        _write(tmp, "VERSION", "1.00")
        _write(tmp, "app.py", "v = 1\n")
        r.index.add(["VERSION", "app.py"]); r.index.commit("c0")
        base = r.active_branch.name  # 'main' or 'master'
        r.git.checkout("-b", "fix")
        _write(tmp, "VERSION", "1.02")          # branch touches VERSION line
        _write(tmp, "app.py", "v = 1\nfeat=1\n")  # non-conflicting code change
        r.index.add(["VERSION", "app.py"]); r.index.commit("c-fix")
        r.git.checkout(base)
        _write(tmp, "VERSION", "1.10")          # base advances VERSION line
        r.index.add(["VERSION"]); r.index.commit("c-base")
        r.git.checkout("fix")

        ok, detail = ns["_resolve_tree_conflicts"](r, base)
        if not ok:
            _fail("cosmetic VERSION conflict should auto-resolve, got: %s" % detail)
        if open(os.path.join(tmp, "VERSION")).read().strip() != "1.10":
            _fail("VERSION should resolve to the base's value (theirs=1.10)")
        if "feat=1" not in open(os.path.join(tmp, "app.py")).read():
            _fail("the branch's own change must be preserved through the merge")
        if os.path.exists(os.path.join(tmp, ".git", "MERGE_HEAD")):
            _fail("merge must be committed (no dangling MERGE_HEAD)")
        print("PASS: VERSION-only conflict auto-resolved to theirs +", detail)


def test_code_conflict_aborts_for_human():
    ns = _load_ns()
    with tempfile.TemporaryDirectory() as tmp:
        r = _repo(tmp)
        _write(tmp, "VERSION", "1.00")
        _write(tmp, "app.py", "v = 1\n")
        r.index.add(["VERSION", "app.py"]); r.index.commit("c0")
        base = r.active_branch.name
        r.git.checkout("-b", "fix")
        _write(tmp, "VERSION", "1.02")
        _write(tmp, "app.py", "v = 99\n")   # conflicting code edit (same line)
        r.index.add(["VERSION", "app.py"]); r.index.commit("c-fix")
        r.git.checkout(base)
        _write(tmp, "VERSION", "1.10")
        _write(tmp, "app.py", "v = 2\n")    # base edits the same code line
        r.index.add(["VERSION", "app.py"]); r.index.commit("c-base")
        r.git.checkout("fix")

        ok, detail = ns["_resolve_tree_conflicts"](r, base)
        if ok:
            _fail("a code (app.py) conflict must NOT be machine-resolved")
        if "app.py" not in detail:
            _fail("the unresolvable-conflict detail should name app.py, got: %s" % detail)
        if os.path.exists(os.path.join(tmp, ".git", "MERGE_HEAD")):
            _fail("aborted merge must leave no MERGE_HEAD")
        if open(os.path.join(tmp, "VERSION")).read().strip() != "1.02":
            _fail("abort must leave the branch untouched (VERSION still 1.02)")
        print("PASS: code conflict aborted for a human +", detail)


if __name__ == "__main__":
    test_version_only_conflict_autoresolves_to_theirs()
    test_code_conflict_aborts_for_human()
    print("all pr_actions conflict-resolver tests passed")
