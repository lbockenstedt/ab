#!/usr/bin/env python3
"""Self-test for _sanitize_gemini_schema — Gemini's function-calling
`parameters` accepts only an OpenAPI-3.0 subset, so full JSON-Schema keys like
`$schema`, `additionalProperties`, `propertyNames`, and `const` must be stripped
or translated before the request, or the API 400s ("Unknown name ... Cannot
find field") and every tool-using call routed to Gemini fails.

Run:  python3 bugfixer/test_gemini_schema_sanitize.py

llm_client.py imports `main` (app-init side effects), so this extracts the
sanitizer's source (and its drop-key constant) via ast and execs it with no
dependencies, following the established pattern in the other llm_client tests.
"""
import ast


def _load():
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    segs = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_GEMINI_SCHEMA_DROP_KEYS" for t in node.targets
        ):
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.FunctionDef) and node.name == "_sanitize_gemini_schema":
            segs.append(ast.get_source_segment(src, node))
    assert len(segs) == 2, f"expected constant + function, found {len(segs)}"
    ns = {"frozenset": frozenset}
    exec(compile("\n".join(segs), "llm_client.py", "exec"), ns)
    return ns["_sanitize_gemini_schema"]


def _check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    return cond


def _keys_deep(node):
    found = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            found |= _keys_deep(v)
    elif isinstance(node, list):
        for n in node:
            found |= _keys_deep(n)
    return found


def main():
    print("Running Gemini schema sanitizer self-test...")
    sanitize = _load()
    ok = True

    # A schema mirroring the real 400: $schema/additionalProperties at the top,
    # propertyNames + additionalProperties nested, and a const inside anyOf.
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string"},
            "tags": {
                "type": "object",
                "propertyNames": {"pattern": "^[a-z]+$"},
                "additionalProperties": {"type": "string"},
            },
            "mode": {"anyOf": [{"const": "read"}, {"type": "null"}]},
        },
        "required": ["path"],
    }
    clean = sanitize(schema)
    bad = {"$schema", "additionalProperties", "propertyNames", "const"}
    present = _keys_deep(clean)
    ok &= _check("all Gemini-unsupported keys are stripped", not (bad & present))
    ok &= _check("supported structure is preserved (type/properties/required)",
                 clean.get("type") == "object" and "path" in clean["properties"]
                 and clean.get("required") == ["path"])
    ok &= _check("const is translated to a single-value enum",
                 clean["properties"]["mode"]["anyOf"][0] == {"enum": ["read"]})
    ok &= _check("the null alternative in anyOf survives",
                 {"type": "null"} in clean["properties"]["mode"]["anyOf"])

    # oneOf/allOf are folded into anyOf (the only union Gemini understands).
    folded = sanitize({"oneOf": [{"type": "string"}, {"type": "integer"}]})
    ok &= _check("oneOf is folded into anyOf", "anyOf" in folded and "oneOf" not in folded)
    allof = sanitize({"allOf": [{"type": "string"}]})
    ok &= _check("allOf is folded into anyOf", "anyOf" in allof and "allOf" not in allof)

    # Idempotent + non-mutating: sanitising the output again is a no-op, and the
    # original input dict is not modified in place.
    original = {"type": "object", "additionalProperties": False,
                "properties": {"x": {"const": 1}}}
    once = sanitize(original)
    twice = sanitize(once)
    ok &= _check("sanitiser is idempotent", once == twice)
    ok &= _check("input is not mutated in place", "additionalProperties" in original
                 and original["properties"]["x"] == {"const": 1})

    # Non-dict inputs pass through untouched.
    ok &= _check("a scalar passes through", sanitize("x") == "x")
    ok &= _check("an empty schema stays empty", sanitize({}) == {})

    print("\n" + ("ALL CASES PASSED" if ok else "SOME CASES FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
