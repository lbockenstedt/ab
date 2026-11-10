#!/usr/bin/env python3
"""Self-test for fix_engine.py's truncated-response salvage (fallback 4).

Run:  python3 test_truncated_fix_fence.py

fix_engine.py cannot be imported directly (it transitively pulls in main.py's
circular import chain), so the functions under test are extracted with ast and
exec'd -- the established convention in this repo (see
test_fix_engine_retry_triggers.py, test_fix_engine_reviewer_panel.py).

Background -- the defect this pins
---------------------------------
parse_and_apply() first locates the JSON object inside the LLM response and
binds it to `raw`, which strips any surrounding ```json code fence. Fallbacks
1, 1b, 1c, 2 and 3 all repair `raw`. Fallback 4 -- _close_truncated_json, the
salvage for a response that simply STOPPED early -- was handed `content`, the
undstripped response, instead.

_close_truncated_json re-parses whatever it builds before returning it, so a
leading "```json\n" made BOTH of its passes fail json.loads and the whole
repair return None. The complete edits the response did contain were discarded
and the failure was reported as "truncated_json", asking the model for smaller
edits it had already produced. Observed in production on 2026-09-26 as two
"Error parsing or applying JSON fix: response truncated mid-object" errors
whose raw content began with '```json' and carried one fully-formed edit.

Every provider that wraps JSON in a markdown fence hit this; the salvage only
ever worked for unfenced responses, which is why it appeared to work at all.
"""
import ast
import json
import re
import sys

_WANTED = {
    "_close_truncated_json",
    "_json_string_spans",
    "_robust_json_loads",
    "_looks_truncated_json",
    "_sanitize_json_string_newlines",
}


def _load():
    src = open("fix_engine.py").read()
    ns = {"json": json, "re": re, "_re": re, "sys": sys}
    found = set()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            exec(compile(ast.Module([node], []), "<extract>", "exec"), ns)
            found.add(node.name)
    missing = _WANTED - found
    assert not missing, f"could not extract {sorted(missing)} from fix_engine.py"
    return ns


NS = _load()


def _extract_raw(content):
    """Reproduce parse_and_apply's `raw` binding exactly."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        return match.group()
    brace = content.find("{")
    return None if brace == -1 else content[brace:]


def _fixture(fence=True, indent=2):
    """One COMPLETE edit followed by a second cut mid-"search" string.

    The strings carry shell process substitution and escaped newlines, matching
    the real promote.sh/promote.yml payloads that triggered this.
    """
    complete = {
        "file": ".github/scripts/promote.sh",
        "search": (
            '    unit_files="$(git diff --name-only "${units[$j]}^...${units[$j]}")"\n'
            '    if [ -n "$unit_files" ] && [ -n "$changed" ] \\\n'
            "       && printf '%s\\n' \"$unit_files\" \\\n"
            "          | comm -12 - <(printf '%s\\n' \"$changed\") | grep -q .; then\n"
            '      ext_idx="$j"\n    fi'
        ),
        "replace": (
            '    unit_files="$(git diff --name-only "${units[$j]}^...${units[$j]}")"\n'
            "    overlap=\"$(comm -12 <(printf '%s\\n' \"$a\") <(printf '%s\\n' \"$b\"))\"\n"
            '    if [ -n "$overlap" ]; then\n      ext_idx="$j"\n    fi'
        ),
    }
    partial = {
        "file": ".github/workflows/promote.yml",
        "search": '          intent=""\n          intent_state="no-unit"\n          unit_body="',
        "replace": "unreachable",
    }
    body = json.dumps({"confidence": 0.98, "edits": [complete, partial]},
                      indent=indent)
    full = f"```json\n{body}\n```" if fence else body
    marker = "unit_body="
    return full[: full.index(marker) + len(marker)], complete


def _salvaged_edits(text):
    repaired = NS["_close_truncated_json"](text)
    if repaired is None:
        return None
    return json.loads(repaired).get("edits") or []


def test_fenced_truncation_is_salvaged_via_raw():
    """The regression: a FENCED truncated response must still salvage."""
    content, complete = _fixture(fence=True)
    assert NS["_looks_truncated_json"](content), "fixture is not truncated"

    # The old call site passed `content`; the fence defeated both repair passes.
    assert _salvaged_edits(content) is None, (
        "fixture no longer reproduces the defect -- _close_truncated_json now "
        "tolerates a fence, so this test would pass for the wrong reason"
    )

    raw = _extract_raw(content)
    assert raw is not None and raw.startswith("{"), "raw extraction failed"
    edits = _salvaged_edits(raw)
    assert edits is not None, "fenced+truncated response was not salvaged"
    assert len(edits) == 1, f"expected the 1 complete edit, got {len(edits)}"
    assert edits[0] == complete, "salvaged edit is not byte-identical to the original"


def test_unfenced_truncation_still_salvaged():
    """The path that already worked must keep working (no regression)."""
    content, complete = _fixture(fence=False)
    edits = _salvaged_edits(_extract_raw(content))
    assert edits is not None and edits == [complete]


def test_salvage_survives_fence_with_any_indent():
    """Indentation is a formatting choice; salvage must not depend on it."""
    for indent in (None, 0, 2, 4):
        content, complete = _fixture(fence=True, indent=indent)
        edits = _salvaged_edits(_extract_raw(content))
        assert edits == [complete], f"indent={indent!r} failed to salvage"


def test_partial_edit_is_never_applied():
    """A half-written edit must be dropped, not closed into a deletion."""
    content, _ = _fixture(fence=True)
    edits = _salvaged_edits(_extract_raw(content))
    files = [e["file"] for e in edits]
    assert ".github/workflows/promote.yml" not in files, (
        "the truncated edit was salvaged; applying it would replace its search "
        "anchor with nothing"
    )
    for e in edits:
        assert e.get("file") and e.get("search") is not None and e.get("replace") is not None


def test_nothing_complete_yields_none():
    """Cut before the FIRST edit closes -- there is nothing to salvage."""
    content, _ = _fixture(fence=True)
    cut = content[: content.index("comm -12")]
    assert _salvaged_edits(_extract_raw(cut) or cut) is None


def test_complete_response_is_left_alone():
    """Not truncated -- a different repair applies; must return None."""
    body = json.dumps({"confidence": 0.9,
                       "edits": [{"file": "a", "search": "s", "replace": "r"}]})
    assert NS["_close_truncated_json"](body) is None


def test_call_site_passes_raw_not_content():
    """Pin the call site itself, so the bug cannot be reintroduced."""
    src = open("fix_engine.py").read()
    calls = re.findall(r"(?<!def )_close_truncated_json\((\w+)\)", src)
    assert calls, "call site not found -- did _close_truncated_json get renamed?"
    assert set(calls) == {"raw"}, (
        f"_close_truncated_json must be given the fence-stripped object; "
        f"found call(s) with {sorted(set(calls))}"
    )


def test_repair_never_raises():
    """Contract: a repair failure degrades, it does not raise."""
    for junk in ("", "{", "```json\n{", '{"edits": [', "not json at all", "[]", "{}"):
        NS["_close_truncated_json"](junk)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("OK" if not failures else f"{failures} FAILURE(S)")
    sys.exit(1 if failures else 0)
