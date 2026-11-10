"""Pins the JSON extraction used by analyze_logs_for_errors().

WHY: the log-analysis path used `re.search(r'\\[.*\\]', res, re.DOTALL)`. That is
greedy across the WHOLE reply, so it spans from the first `[` anywhere in the
model's output to the LAST `]` anywhere in it. One bracket in the preamble --
"Here are the findings [see note below]:" -- makes the captured text start
`[see note below]: [{...}]`, and json.loads raises exactly the errors filed as
ab#1, #15, #68, #130 ("Expecting value: line 1 column 2 (char 1)") and ab#149
("Extra data: line 23 column 1"). Both are the SAME bug: the capture is not a
single balanced array.

fix_engine.py already fixed this for the builder path with a balanced-bracket
scanner (_first_json_array_of_strings); its docstring names the identical
symptom. log_scan.py was simply never ported. These checks pin the port, and
in particular pin that a `]` inside a JSON *string* does not end the array --
the one thing a naive bracket counter gets wrong, and very easy to hit here
because the 'body' field carries a raw log snippet.
"""
import ast
import json
import sys

SRC = "log_scan.py"
_WANT = ("_json_string_spans", "_first_json_array_of_objects")


def _load():
    """Extract the two helpers without importing log_scan (it pulls in requests,
    config_store and the whole LLM stack at import time)."""
    tree = ast.parse(open(SRC).read())
    ns = {"json": json}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
            found.add(node.name)
    missing = set(_WANT) - found
    assert not missing, f"missing from {SRC}: {sorted(missing)}"
    return ns["_first_json_array_of_objects"], ns["_json_string_spans"]


def main():
    first_array, spans = _load()
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            print(f"        got  {got!r}")
            print(f"        want {want!r}")
            fails.append(label)

    print("Running log-analysis JSON extraction self-test...")

    # ── the exact production failures ──────────────────────────────────────
    entry = {"module": "lm", "title": "t", "body": "b"}
    check(
        "preamble bracket does not swallow the array (ab#1/#15/#68/#130)",
        first_array('Here are the findings [see note below]:\n' + json.dumps([entry])),
        [entry],
    )
    check(
        "trailing prose after the array is ignored (ab#149 'Extra data')",
        first_array(json.dumps([entry]) + "\n\nLet me know if you want more detail."),
        [entry],
    )
    check(
        "fenced json block is unwrapped",
        first_array("```json\n" + json.dumps([entry]) + "\n```"),
        [entry],
    )
    check(
        "both a leading bracket AND trailing prose",
        first_array("note [x] here " + json.dumps([entry]) + " thanks!"),
        [entry],
    )

    # ── the thing a naive bracket counter gets wrong ───────────────────────
    logline = "2026-08-30 - lm - ERROR - parse failed at token ] in [payload]"
    esc = {"module": "lm", "title": "t", "body": logline}
    check(
        "a ']' inside a JSON string does not terminate the array",
        first_array("Findings:\n" + json.dumps([esc])),
        [esc],
    )
    q = {"module": "lm", "title": 'he said "] done"', "body": "b"}
    check(
        "an escaped quote inside a string is handled",
        first_array(json.dumps([q])),
        [q],
    )

    # ── acceptance predicate ───────────────────────────────────────────────
    check("empty array is a valid 'nothing actionable' answer", first_array("[]"), [])
    check("array of strings is rejected", first_array('["a.py","b.py"]'), None)
    check("array of numbers is rejected", first_array("[1,2,3]"), None)
    check("mixed array is rejected", first_array('[ "s", {"a":1} ]'), None)
    check("nested arrays are rejected", first_array("[[1],[2]]"), None)
    check("no JSON at all", first_array("the model apologised and said nothing"), None)
    check("empty input", first_array(""), None)
    check("unbalanced array", first_array('[{"a":1}'), None)
    check(
        "a Tailwind-style z-[60] in the preamble is skipped, not returned",
        first_array('use z-[60] for this. ' + json.dumps([entry])),
        [entry],
    )
    check(
        "scanning continues past a non-qualifying candidate to a later one",
        first_array('["not","it"] then ' + json.dumps([entry])),
        [entry],
    )
    check(
        "multiple objects are all returned",
        first_array(json.dumps([entry, {"module": "nw", "title": "u", "body": "c"}])),
        [entry, {"module": "nw", "title": "u", "body": "c"}],
    )

    # ── _json_string_spans ─────────────────────────────────────────────────
    check("spans finds a simple string", spans('x "ab" y'), [(2, 5)])
    check("spans skips an escaped quote", spans(r'"a\"b"'), [(0, 5)])
    check("spans on text with no strings", spans("[1, 2]"), [])

    print(f"RESULT: {'PASS' if not fails else 'FAIL — ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
