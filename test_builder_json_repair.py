"""Selftest for the BUILDER-side JSON robustness helpers.

WHY: test_reviewer_json_repair covers the *reviewer* path. These are three
distinct, separately-confirmed failures on the *builder* path, all observed in
production on 2026-08-30 while retrying lm#452 / lm#486 / lm#487 — every one of
which ended the retry with the misleading "AI generated invalid JSON format":

  02:21:29  unterminated string literal (detected at line 7) — raw content
            (1076 chars) beginning '```json\\n{\\n  "confidence": 0.95, …'
            → the response simply STOPPED mid-value. Well-formed as far as it
              got; telling the model its syntax was invalid is wrong advice.

  02:23:23  ':' expected after dictionary key — raw content (482 chars):
            '{"confidence": 0.55, "edits": [{"file": "WebUI/index.html",
             "class=\\"relative z-[60] …\\">", "replace": …}]}'
            → the model emitted the search VALUE but omitted the "search" KEY.
              Purely structural, so no quote/newline repair in the ladder can
              touch it, and the whole (correct) z-index fix was discarded.

  02:23:19  Error identifying files: Expecting value: line 1 column 2 (char 1)
            → the greedy `\\[.*\\]` DOTALL match ran from the first `[` anywhere
              in the reply to the LAST `]` anywhere in it. A Tailwind class like
              z-[60] in the preamble is enough to swallow the whole response.

fix_engine imports the app (circular at import time), so — like the sibling
harnesses — the pure helpers are extracted with ast and exec'd into a synthetic
namespace.
"""
import ast
import json
import re

_WANT_FUNCS = {"_json_string_spans", "_looks_truncated_json",
               "_enclosing_flat_object", "_flat_object_keys",
               "_repair_missing_object_keys", "_first_json_array_of_strings"}
_WANT_ASSIGNS = {"_EDIT_OBJECT_KEYS"}


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


# The real 02:23:23 payload, transcribed from /var/log/ab.log. The doubled
# backslashes in the log line are its repr(); this is the actual text.
PROD_MISSING_KEY = (
    '{"confidence": 0.55, "edits": [{"file": "WebUI/index.html", '
    '"class=\\"relative z-[60] h-10 w-full flex items-center px-6 text-white '
    'text-xs font-medium transition-all shrink-0\\" style=\\"background-color: '
    'var(--hpe-navy); border-top: 3px solid var(--hpe-green);\\">", '
    '"replace": "class=\\"relative z-[70] h-10 w-full flex items-center px-6 '
    'text-white text-xs font-medium transition-all shrink-0\\" '
    'style=\\"background-color: var(--hpe-navy); border-top: 3px solid '
    'var(--hpe-green);\\">"}]}')

# The tail of the real 02:21:29 payload — cut off inside the "replace" value.
PROD_TRUNCATED = (
    '{\n  "confidence": 0.95,\n  "edits": [\n    {\n      '
    '"file": "WebUI/main.js",\n      '
    '"search": "async function setTenant(tenant) {\\n    currentTenant = tenant;\\n}",\n'
    '      "replace": "async function setTenant(tenant) {\\n    currentTenant = tenant;'
    '\\n    try {\\n        await fetch(\'/setup/tenant\', {')


def main():
    ns = _load()
    spans = ns["_json_string_spans"]
    truncated = ns["_looks_truncated_json"]
    repair = ns["_repair_missing_object_keys"]
    first_array = ns["_first_json_array_of_strings"]
    ok = True

    # ── _json_string_spans ─────────────────────────────────────────────────
    print("_json_string_spans:")
    s = spans('{"a": "b"}')
    ok &= _check("finds both strings", s == [(1, 3), (6, 8)])
    s = spans(r'{"a": "x\"y"}')
    ok &= _check("an escaped quote does not close the span", len(s) == 2 and s[1] == (6, 11))
    s = spans('"unterminated')
    ok &= _check("unterminated span ends at len(text) (the truncation signal)",
                 s == [(0, 13)] and s[0][1] == len('"unterminated'))
    ok &= _check("no strings → no spans", spans("{}") == [])

    # ── _looks_truncated_json ──────────────────────────────────────────────
    print("_looks_truncated_json:")
    ok &= _check("the real 02:21:29 payload is detected as truncated",
                 truncated(PROD_TRUNCATED) is True)
    ok &= _check("complete object is NOT truncated",
                 truncated('{"a": 1}') is False)
    ok &= _check("unclosed container is truncated",
                 truncated('{"edits": [{"file": "a.py"}') is True)
    ok &= _check("ends inside a string is truncated",
                 truncated('{"search": "def f(') is True)
    ok &= _check("a brace INSIDE a string does not fake completeness",
                 truncated('{"search": "if (x) {}"') is True)
    ok &= _check("empty text is not truncated (it is a different failure)",
                 truncated("") is False)
    # Discriminates the unterminated-STRING signal from the unbalanced-container
    # one: here the containers are balanced (the `{` is inside the string and is
    # skipped), so only the string check can catch it.
    ok &= _check("truncated inside a string with balanced containers is detected",
                 truncated('"async function f() {') is True)
    # The whole point of separating this reason: the missing-key payload is
    # complete, so it must NOT be blamed on truncation.
    ok &= _check("the missing-KEY payload is complete, not truncated",
                 truncated(PROD_MISSING_KEY) is False)

    # ── _repair_missing_object_keys ────────────────────────────────────────
    print("_repair_missing_object_keys:")
    try:
        json.loads(PROD_MISSING_KEY)
        ok &= _check("production sample really is invalid JSON (guards the premise)", False)
    except json.JSONDecodeError as e:
        ok &= _check("production sample fails with \"Expecting ':' delimiter\"",
                     "':'" in str(e))

    fixed = repair(PROD_MISSING_KEY)
    try:
        data = json.loads(fixed)
    except json.JSONDecodeError:
        data = None
    ok &= _check("repaired production sample parses", data is not None)
    if data:
        edit = data["edits"][0]
        ok &= _check("the missing key is restored as \"search\"", "search" in edit)
        ok &= _check("file is untouched", edit["file"] == "WebUI/index.html")
        ok &= _check("search keeps the ORIGINAL z-[60] value",
                     "z-[60]" in edit["search"] and "z-[70]" not in edit["search"])
        ok &= _check("replace keeps the NEW z-[70] value",
                     "z-[70]" in edit["replace"] and "z-[60]" not in edit["replace"])
        ok &= _check("search/replace were not swapped",
                     edit["search"] != edit["replace"])
        ok &= _check("confidence survives", data["confidence"] == 0.55)

    # Refusals — the repair must decline rather than guess.
    two_missing = '{"edits": [{"file": "a.py", "old text", "new text"}]}'
    ok &= _check("declines when TWO schema keys are missing (order is ambiguous)",
                 repair(two_missing) == two_missing)

    good = '{"edits": [{"file": "a.py", "search": "x", "replace": "y"}]}'
    ok &= _check("valid JSON passes through byte-for-byte", repair(good) == good)

    other_err = '{"edits": [{"file": "a.py" "search": "x"}]}'
    ok &= _check("a key that merely lost its colon is NOT treated as a stray value",
                 repair(other_err) == other_err)

    # Same class, but crafted so EXACTLY ONE schema key is absent — otherwise the
    # "exactly one missing" guard would mask this one. Here json really does say
    # "Expecting ':'", and only the ,/} proof distinguishes a key that lost its
    # colon from a value that lost its key.
    lost_colon = '{"file": "a.py", "replace": "y", "search" "stray"}'
    ok &= _check("\"Expecting ':'\" alone is not enough — the ,/} proof is required",
                 repair(lost_colon) == lost_colon)

    nested = '{"file": "a.py", "meta stray", "search": "x", "replace": {"k": 1}}'
    ok &= _check("declines inside a non-flat object", repair(nested) == nested)

    # As above: leave exactly one key missing so flatness is the only thing that
    # can decline it.
    nested_one = '{"file": "a.py", "old text", "replace": {"k": 1}}'
    ok &= _check("declines on a nested object even when one key is missing",
                 repair(nested_one) == nested_one)

    unrelated = '{"a": 1,}'
    ok &= _check("an unrelated JSON error is returned unchanged",
                 repair(unrelated) == unrelated)

    # ── _first_json_array_of_strings ───────────────────────────────────────
    print("_first_json_array_of_strings:")
    ok &= _check("plain array",
                 first_array('["a.py", "b.js"]') == ["a.py", "b.js"])
    ok &= _check("array embedded in prose",
                 first_array('Sure! Here you go:\n["a.py"]\nHope that helps.') == ["a.py"])
    # The exact 02:23:19 shape: a Tailwind class before the real answer.
    prose = 'The banner uses z-[60] so check these: ["WebUI/index.html", "WebUI/main.js"]'
    ok &= _check("a z-[60] in the preamble no longer swallows the response",
                 first_array(prose) == ["WebUI/index.html", "WebUI/main.js"])
    ok &= _check("a numeric array is rejected, not returned as the file list",
                 first_array("z-[60]") is None)
    ok &= _check("brackets inside a STRING are not mistaken for the array",
                 first_array('["a[0].py"]') == ["a[0].py"])
    ok &= _check("no array at all → None", first_array("I could not determine this.") is None)
    ok &= _check("empty array is rejected (nothing to merge)", first_array("[]") is None)
    ok &= _check("mixed-type array is rejected",
                 first_array('[1, "a.py"]') is None)
    ok &= _check("fenced array is found",
                 first_array('```json\n["a.py"]\n```') == ["a.py"])

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
