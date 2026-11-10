"""Selftest for _parse_reviewer_json — the reviewer-side JSON repair ladder.

WHY: a reviewer's "critique" quotes the code it is reviewing, so it contains
unescaped double-quotes (settings["KEY"], logger.error("…")). The reviewer path
only ever called _robust_json_loads, so ONE such quote discarded an otherwise
complete review and counted the reviewer as failed — burning a panel slot.
Confirmed in production 2026-08-30 00:47:34 (copilot, confidence 0.93/Approve,
killed by "Expecting ',' delimiter: line 1 column 248").

fix_engine imports the app (circular at import time), so — like the other
test_llm_client_* harnesses — the pure helpers are extracted with ast and
exec'd into a synthetic namespace.
"""
import ast
import json
import re

_WANT_FUNCS = {"_relax_json_fix_strings", "_parse_reviewer_json",
               "_robust_json_loads", "_sanitize_json_string_newlines"}
_WANT_ASSIGNS = {"_JSON_NEXT_MEMBER_RE", "_FIX_CODE_KEY_RE", "_REVIEW_TEXT_KEY_RE",
                 "_REVIEW_NEXT_MEMBER_RE", "_JSON_BAD_ESCAPE_RE"}


def _load():
    tree = ast.parse(open("fix_engine.py").read())
    ns = {"re": re, "json": json}
    mod = ast.Module(body=[], type_ignores=[])
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            mod.body.append(node)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) in _WANT_ASSIGNS for t in node.targets):
            mod.body.append(node)
    exec(compile(mod, "fix_engine_extract", "exec"), ns)
    missing = sorted((_WANT_FUNCS | _WANT_ASSIGNS) - set(ns))
    if missing:
        raise AssertionError("extraction incomplete, missing: %s" % ", ".join(missing))
    return ns


def _check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    return bool(cond)


def main():
    ns = _load()
    parse = ns["_parse_reviewer_json"]
    relax = ns["_relax_json_fix_strings"]
    ok = True

    print("reviewer JSON repair:")

    # ── the exact production failure ────────────────────────────────────────
    prod = ('{"confidence": 0.93, "verdict": "Approve", "critique": "Reachability: '
            'the settings["GITHUB_TOKEN_configured"]/from_env lines are computed."}')
    try:
        json.loads(prod)
        ok &= _check("production sample really is invalid JSON (guards the premise)", False)
    except json.JSONDecodeError:
        ok &= _check("production sample really is invalid JSON (guards the premise)", True)
    v = parse(prod)
    ok &= _check("production sample now parses", isinstance(v, dict))
    ok &= _check("verdict recovered", v.get("verdict") == "Approve")
    ok &= _check("confidence recovered", v.get("confidence") == 0.93)
    ok &= _check("critique text preserved verbatim, inner quotes intact",
                 'settings["GITHUB_TOKEN_configured"]' in v.get("critique", ""))

    # ── the `]` trap: why bracket_closes=False is required ───────────────────
    # The fix path treats a quote followed by `]` as a structural close. In prose
    # that is an inner quote, and honouring it truncates the critique mid-word.
    bracketed = relax(prod, key_re=ns["_REVIEW_TEXT_KEY_RE"],
                      next_member_re=ns["_REVIEW_NEXT_MEMBER_RE"], bracket_closes=True)
    bad = True
    try:
        json.loads(bracketed)
        bad = False
    except json.JSONDecodeError:
        pass
    ok &= _check("bracket_closes=True would still fail (proves the param is load-bearing)", bad)

    # ── other real critique shapes ──────────────────────────────────────────
    quoted_call = ('{"confidence": 0.8, "verdict": "Reject", "critique": '
                   '"logger.error("boom") should be logger.warning("boom")."}')
    v2 = parse(quoted_call)
    ok &= _check("critique containing a quoted call parses", v2.get("verdict") == "Reject")
    ok &= _check("quoted call preserved", 'logger.error("boom")' in v2.get("critique", ""))

    multiline = ('{"confidence": 0.5, "verdict": "Reject", "critique": "line one\n'
                 'line two with "quoted" text"}')
    v3 = parse(multiline)
    ok &= _check("critique with literal newlines parses", v3.get("verdict") == "Reject")

    # critique is NOT the last member -> the next-member anchor must be used
    reordered = ('{"critique": "uses settings["A"] here", "confidence": 0.7, '
                 '"verdict": "Approve"}')
    v4 = parse(reordered)
    ok &= _check("critique before other members still parses", v4.get("verdict") == "Approve")
    ok &= _check("reordered critique preserved", 'settings["A"]' in v4.get("critique", ""))

    # ── no-ops and non-regression ───────────────────────────────────────────
    clean = '{"confidence": 1.0, "verdict": "Approve", "critique": "All good."}'
    ok &= _check("already-valid JSON is unchanged", parse(clean)["critique"] == "All good.")
    ok &= _check("relax() is a no-op on valid JSON",
                 relax(clean, key_re=ns["_REVIEW_TEXT_KEY_RE"],
                       next_member_re=ns["_REVIEW_NEXT_MEMBER_RE"],
                       bracket_closes=False) == clean)

    esc = '{"confidence": 0.9, "verdict": "Approve", "critique": "he said \\"hi\\" ok"}'
    ok &= _check("correctly-escaped quotes survive",
                 parse(esc)["critique"] == 'he said "hi" ok')

    # the fix path must be untouched by the new parameters
    fix_raw = '{"search": "logger.error("x")", "replace": "logger.warning("x")"}'
    fixed = relax(fix_raw)
    try:
        fv = json.loads(fixed)
        ok &= _check("fix-path default behaviour unchanged (search/replace repaired)",
                     fv.get("search") == 'logger.error("x")')
    except json.JSONDecodeError as e:
        ok &= _check("fix-path default behaviour unchanged (search/replace repaired): %s" % e,
                     False)

    # unrepairable input must raise the ORIGINAL error for the caller's logging
    try:
        parse('{"confidence": 0.5, "verdict": ')
        ok &= _check("unrepairable input raises JSONDecodeError", False)
    except json.JSONDecodeError:
        ok &= _check("unrepairable input raises JSONDecodeError", True)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
