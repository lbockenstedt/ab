#!/usr/bin/env python3
"""Self-test for fix_engine's targeted-edit apply path (search/replace) and the
large-file windowing that feeds the model the buggy region.

Run:  python3 ab/test_fix_apply.py

fix_engine.py cannot be imported directly (its ``from main import …`` runs
FastAPI/logging init), so we extract the SOURCE of the pure helpers via ast and
exec them with a stub logger. This exercises the real code text.

Regression guard for the failure that made AppBuilder unable to fix a one-char
typo in a 22k-line file: the model was asked to reproduce the WHOLE file (it
truncated → the truncated-rewrite guard aborted every attempt) and the buggy
line lived past the head-truncation cutoff so the model never saw it. The fix:
window the file around issue identifiers, and accept targeted search/replace
edits that scale to any file size.
"""
import ast
import base64
import json
import os
import re
import sys
import tempfile
import threading


def _load_funcs():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_engine.py")
    src = open(path).read()
    tree = ast.parse(src)
    want = {"_safe_repo_target", "_issue_identifiers", "_targeted_file_context",
            "parse_and_apply", "_claim_issue", "_release_issue",
            "_robust_json_loads", "_sanitize_json_string_newlines",
            "_relax_json_fix_strings",
            "_fetch_repo_file_for_review", "_repo_file_text",
            "_snippet_language_mismatch_hint",
            "_relaxed_edit_span"}
    want_assign = {"_ISSUE_STOP_TOKENS", "_inflight_lock", "_inflight_issues",
                    "_JSON_BAD_ESCAPE_RE", "_JS_ONLY_TOKENS_RE", "_PY_ONLY_TOKENS_RE",
                    "_JS_EXTS", "_PY_EXTS",
                    "_FIX_JSON_KEYS", "_JSON_NEXT_MEMBER_RE", "_FIX_CODE_KEY_RE"}
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in want_assign:
                    segs.append(ast.get_source_segment(src, node))

    captured_errors = []

    class _L:
        def error(self, msg, *a, **k):
            captured_errors.append(msg)

        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {"os": os, "json": json, "re": re, "threading": threading,
          "base64": base64, "logger": _L()}
    exec("\n\n".join(segs), ns)
    ns["_captured_errors"] = captured_errors
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ns = _load_funcs()
    ok = True

    # A 22k-line file with the typo at line 20357 and the real fn at 20307 —
    # both far past a 12k-char head-truncation. The top 2000 lines are FLOODED
    # with the common identifier "ldap" (also an issue identifier): a naive
    # file-order window would exhaust its budget on this noise and never reach the
    # buggy region — which is exactly how AppBuilder once "fixed" the crash with a
    # no-op stub instead of the typo. The rare-identifier-first ranking must still
    # surface the buggy call AND its correctly-spelled twin.
    lines = [f"// filler line {i}" for i in range(1, 22823)]
    for j in range(0, 2000):
        lines[j] = f"// ldap helper reference {j}"
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

    # Regression guard for GitHub issue #755 ("Edit search snippet not found in
    # '...'; skipping this edit" — non-actionable, no context): the failing
    # snippet was computed (for the retry loop's last_failures) but never
    # logged, so the self-scanner's single-line capture had nothing to go on.
    # It must now appear directly in the logged ERROR line itself.
    ok &= _check("missing-snippet ERROR line embeds the actual failing snippet",
                 any("nonexistent snippet zzz" in m for m in ns["_captured_errors"]))

    esc = json.dumps({"confidence": 0.9, "edits": [
        {"file": "../escape.txt", "search": "a", "replace": "b"}]})
    esc_ok, esc_applied, _ = ns["parse_and_apply"](esc, d)
    ok &= _check("path traversal in an edit is rejected", esc_ok is False and not esc_applied)

    # Regression guard for GitHub issue #760 ("No fixes could be applied
    # (core/src/simulations/routes.py: search snippet not found (starts with:
    # 'await r.json().catch(() => ({}))')))" — non-actionable): the search
    # snippet is JAVASCRIPT, applied against a .py file — it can never match,
    # because the model crossed the file/search pairing between a JS edit and
    # a Python edit in the same response. A plain "not found" gives no signal
    # toward THAT diagnosis; the language-mismatch hint should.
    routes_py = os.path.join(d, "routes.py")
    open(routes_py, "w").write("def handler(request):\n    return {}\n")
    js_in_py = json.dumps({"confidence": 0.9, "edits": [
        {"file": "routes.py", "search": "await r.json().catch(() => ({}))", "replace": "x"}]})
    ns["_captured_errors"].clear()
    mismatch_ok, mismatch_applied, _ = ns["parse_and_apply"](js_in_py, d)
    ok &= _check("JS-in-.py mismatched edit does NOT report success",
                 mismatch_ok is False and not mismatch_applied)
    ok &= _check("cross-language hint appears in the ERROR line",
                 any("looks like JavaScript, not Python" in m for m in ns["_captured_errors"]))

    # Regression guard for GitHub issue #735 (recurring "unterminated string
    # literal" / "invalid syntax" self-diagnosis failures): an LLM response with
    # a multi-line code snippet that has a LITERAL (unescaped) newline inside a
    # JSON string value — json.dumps() never produces this, so build the raw
    # string by hand the way a model's free-text output does.
    raw_newline_resp = (
        '{"confidence": 0.95, "edits": [{"file": "WebUI/main.js", '
        # search targets the ALREADY-FIXED line ("targeted edit applied" above
        # already replaced the typo in main_js) — a distinct sibling edit, not
        # a re-application of the first one.
        '"search": "    await ensureLDAPTenants();", '
        '"replace": "    await ensureLDAPTenants();\n    logAudit();"}]}'
    )
    nl_ok, nl_applied, _ = ns["parse_and_apply"](raw_newline_resp, d)
    nl_result = open(main_js).read()
    ok &= _check("literal-newline-in-string response still parses and applies",
                 nl_ok and "WebUI/main.js" in nl_applied)
    ok &= _check("literal-newline replacement landed correctly",
                 "logAudit();" in nl_result and "ensureLDAPTennants" not in nl_result)

    # Regression guard for the recurring "invalid character '—' (U+2014)" /
    # "Expecting ',' delimiter" fix failures (blocked lm#210): the model's
    # search/replace CODE contains its OWN string literals with UNESCAPED
    # double-quotes (logger.error("…")), so json.loads reads the first inner
    # quote as the end of the value and everything after it (an em-dash, a
    # paren) as broken structure — the newline-repair and ast.literal_eval
    # fallbacks then choke. json.dumps can never produce this, so build the raw
    # response by hand the way a model's free-text output does: literal newlines
    # AND unescaped inner quotes AND a non-ASCII em-dash, all inside the value.
    code_old = ('        logger.error("TLS cert load failed: %s" — e)\n'
                '        raise')
    code_new = ('        logger.error("TLS cert load failed")\n'
                '        _ctx.verify_mode = ssl.CERT_REQUIRED\n'
                '        raise')
    srv_src = "def start(self):\n    if self.tls_enabled:\n" + code_old + "\n"
    srv_file = os.path.join(d, "core", "srv.py")
    os.makedirs(os.path.dirname(srv_file), exist_ok=True)
    open(srv_file, "w").write(srv_src)
    unescaped_quotes_resp = (
        '{"confidence": 0.95, "edits": [{"file": "core/srv.py", '
        '"search": "' + code_old + '", '
        '"replace": "' + code_new + '"}]}'
    )
    # It must not parse under strict JSON — otherwise the test proves nothing.
    strict_failed = False
    try:
        json.loads(unescaped_quotes_resp)
    except json.JSONDecodeError:
        strict_failed = True
    ok &= _check("malformed (unescaped-quote) response is NOT valid strict JSON",
                 strict_failed)
    uq_ok, uq_applied, _ = ns["parse_and_apply"](unescaped_quotes_resp, d)
    uq_result = open(srv_file).read()
    ok &= _check("unescaped-inner-quote+em-dash response parses and applies",
                 uq_ok and "core/srv.py" in uq_applied)
    ok &= _check("unescaped-quote replacement landed (code preserved verbatim)",
                 "_ctx.verify_mode = ssl.CERT_REQUIRED" in uq_result
                 and '"TLS cert load failed"' in uq_result)

    # The repair must be a NO-OP on already-valid JSON — it must never corrupt a
    # well-formed response (other keys, arrays of short strings, escaped quotes).
    relax = ns["_relax_json_fix_strings"]
    valid_json = '{"a": "b", "c": ["d","e"], "search": "x", "f": {"g":"h"}}'
    ok &= _check("relax is a no-op on already-valid JSON",
                 relax(valid_json) == valid_json)
    already_escaped = '{"search": "a \\"q\\" b", "replace": "c"}'
    ok &= _check("relax preserves an already-escaped code value",
                 json.loads(relax(already_escaped))["search"] == 'a "q" b')

    # Regression guard for GitHub issue #753 ("Reviewer N (copilot) failed:
    # unsupported encoding: none"): PyGithub's ContentFile.decoded_content
    # raises AssertionError("unsupported encoding: none") when GitHub's
    # Contents API doesn't inline a file's content (e.g. >1MB), and that
    # escaped getattr(c, "decoded_content", None) — which only catches
    # AttributeError — breaking this function's documented "never raises"
    # contract and crashing the whole reviewer turn.
    class _OversizedContentFile:
        path = "big.bin"

        @property
        def decoded_content(self):
            assert False, "unsupported encoding: none"

    class _FakeRepo:
        def get_contents(self, path, ref=None):
            return _OversizedContentFile()

    oversized_out = ns["_fetch_repo_file_for_review"](_FakeRepo(), "deadbeef", "big.bin")
    ok &= _check("oversized-file AssertionError degrades to an error dict, not a raise",
                 isinstance(oversized_out, dict) and "error" in oversized_out)

    # In-flight claim: two workers can't process the same issue concurrently
    # (the reopen re-queue + a scan both grabbing an issue → duplicate commits).
    claim, release = ns["_claim_issue"], ns["_release_issue"]
    ok &= _check("first claim of an issue succeeds", claim("lm:102") is True)
    ok &= _check("second concurrent claim is rejected", claim("lm:102") is False)
    ok &= _check("a different issue is unaffected", claim("lm:103") is True)
    release("lm:102")
    ok &= _check("claim succeeds again after release (re-trigger)", claim("lm:102") is True)
    release("lm:999")  # releasing a non-held id must not raise
    ok &= _check("releasing a non-held id is a safe no-op", True)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running fix_engine apply/windowing self-test...")
    sys.exit(main())
