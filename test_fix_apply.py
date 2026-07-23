#!/usr/bin/env python3
"""Self-test for fix_engine's targeted-edit apply path (search/replace) and the
large-file windowing that feeds the model the buggy region.

Run:  python3 bugfixer/test_fix_apply.py

fix_engine.py cannot be imported directly (its ``from main import …`` runs
FastAPI/logging init), so we extract the SOURCE of the pure helpers via ast and
exec them with a stub logger. This exercises the real code text.

Regression guard for the failure that made BugFixer unable to fix a one-char
typo in a 22k-line file: the model was asked to reproduce the WHOLE file (it
truncated → the truncated-rewrite guard aborted every attempt) and the buggy
line lived past the head-truncation cutoff so the model never saw it. The fix:
window the file around issue identifiers, and accept targeted search/replace
edits that scale to any file size.
"""
import ast
import json
import os
import re
import sys
import tempfile


def _load_funcs():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_engine.py")
    src = open(path).read()
    tree = ast.parse(src)
    want = {"_safe_repo_target", "_issue_identifiers",
            "_targeted_file_context", "parse_and_apply"}
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_ISSUE_STOP_TOKENS":
                    segs.append(ast.get_source_segment(src, node))

    class _L:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {"os": os, "json": json, "re": re, "logger": _L()}
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ns = _load_funcs()
    ok = True

    # A 22k-line file with the typo at line 20357 and the real fn at 20307 —
    # both far past a 12k-char head-truncation.
    lines = [f"// filler line {i}" for i in range(1, 22823)]
    lines[20306] = "async function ensureLDAPTenants(force) {"
    lines[20356] = "    await ensureLDAPTennants();"
    big = "\n".join(lines)
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "WebUI"))
    main_js = os.path.join(d, "WebUI", "main.js")
    open(main_js, "w").write(big)

    issue = "**Error:** Can't find variable: ensureLDAPTennants\nView: ldap / Users"

    ids = ns["_issue_identifiers"](issue)
    ok &= _check("identifiers include the mistyped symbol", "ensureLDAPTennants" in ids)

    win = ns["_targeted_file_context"](big, ids, max_chars=12000, window=60)
    ok &= _check("windowing returns a region (not head-truncation)", win is not None)
    ok &= _check("window contains the buggy line", "await ensureLDAPTennants();" in (win or ""))
    ok &= _check("window contains the real function", "async function ensureLDAPTenants(force)" in (win or ""))
    ok &= _check("window is tiny vs the whole file", win is not None and len(win) < 20000)

    edit_resp = json.dumps({"confidence": 0.97, "edits": [
        {"file": "WebUI/main.js",
         "search": "    await ensureLDAPTennants();",
         "replace": "    await ensureLDAPTenants();"}]})
    applied_ok, applied, conf = ns["parse_and_apply"](edit_resp, d)
    result = open(main_js).read()
    ok &= _check("targeted edit applied", applied_ok and "WebUI/main.js" in applied)
    ok &= _check("typo removed", "ensureLDAPTennants" not in result)
    ok &= _check("file size preserved (no truncation)", result.count("\n") + 1 == 22822)

    bad = json.dumps({"confidence": 0.9, "edits": [
        {"file": "WebUI/main.js", "search": "nonexistent snippet zzz", "replace": "x"}]})
    bad_ok, bad_applied, _ = ns["parse_and_apply"](bad, d)
    ok &= _check("non-matching edit does NOT report success", bad_ok is False and not bad_applied)

    esc = json.dumps({"confidence": 0.9, "edits": [
        {"file": "../escape.txt", "search": "a", "replace": "b"}]})
    esc_ok, esc_applied, _ = ns["parse_and_apply"](esc, d)
    ok &= _check("path traversal in an edit is rejected", esc_ok is False and not esc_applied)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running fix_engine apply/windowing self-test...")
    sys.exit(main())
