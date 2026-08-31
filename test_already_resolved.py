"""Selftest for fix_engine's already-resolved gating.

WHY: "Failure" must mean "I could not fix it", not "there was nothing to fix".
When a fix run produces no accepted change AND the reported problem is already
present-and-correct in the tree (a re-filed/duplicate report, or a fix already
merged — exactly lm#486 and lm#487, which were fixed in #489/854698a8 yet kept
being retried and reported as AppBuilder Failures), the issue should be marked
RESOLVED, not failed.

The two decision points are pure and tested here:
  * _should_check_already_resolved(last_failure, config) — WHICH failure kinds are
    even eligible for the check (and the on/off gate), and
  * _resolved_gate(verifier_result, config) — whether the verifier's verdict is
    strong enough (explicit resolved=true, evidence present, confidence >= thresh)
    to auto-resolve.

fix_engine imports the app (circular at import time), so the pure helpers are
extracted with ast — the same harness pattern the other selftests use.
"""
import ast

_WANT_FUNCS = {"_should_check_already_resolved", "_resolved_gate"}
_WANT_ASSIGNS = {"_RESOLVED_CHECK_KINDS"}


def _load():
    tree = ast.parse(open("fix_engine.py").read())
    ns = {}
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
    should = ns["_should_check_already_resolved"]
    gate = ns["_resolved_gate"]
    ON = {}  # empty config → feature defaults ON
    ok = True

    print("which failure kinds are eligible for the already-resolved check:")
    ok &= _check("no_edits is eligible (model found nothing to change)",
                 should({"kind": "no_edits"}, ON) is True)
    ok &= _check("review_rejected is eligible (fix deemed unnecessary/fragile)",
                 should({"kind": "review_rejected"}, ON) is True)
    ok &= _check("low_confidence is eligible",
                 should({"kind": "low_confidence"}, ON) is True)
    ok &= _check("qa_failed is NOT eligible (a change was made and broke tests)",
                 should({"kind": "qa_failed"}, ON) is False)
    ok &= _check("invalid_json is NOT eligible (mechanical output failure)",
                 should({"kind": "invalid_json"}, ON) is False)
    ok &= _check("edit_anchor_miss is NOT eligible",
                 should({"kind": "edit_anchor_miss"}, ON) is False)
    ok &= _check("error is NOT eligible",
                 should({"kind": "error"}, ON) is False)
    ok &= _check("a missing/unknown kind is NOT eligible",
                 should({}, ON) is False and should(None, ON) is False)

    print("the feature can be turned off:")
    ok &= _check("verify_already_resolved=false disables the check entirely",
                 should({"kind": "no_edits"}, {"verify_already_resolved": False}) is False)
    ok &= _check("verify_already_resolved=true keeps it on",
                 should({"kind": "no_edits"}, {"verify_already_resolved": True}) is True)

    print("the verifier verdict gate (strict — must be confident, evidenced, explicit):")
    ok &= _check("resolved + high confidence + evidence passes",
                 gate({"resolved": True, "confidence": 0.95, "evidence": "line 1702: _mdFilter.tenant = tenant"}, ON) is True)
    ok &= _check("resolved=false never passes",
                 gate({"resolved": False, "confidence": 0.99, "evidence": "x"}, ON) is False)
    ok &= _check("resolved=true but NO evidence fails",
                 gate({"resolved": True, "confidence": 0.99, "evidence": ""}, ON) is False)
    ok &= _check("resolved=true but whitespace-only evidence fails",
                 gate({"resolved": True, "confidence": 0.99, "evidence": "   "}, ON) is False)
    ok &= _check("confidence below the default 0.85 threshold fails",
                 gate({"resolved": True, "confidence": 0.5, "evidence": "x"}, ON) is False)
    ok &= _check("confidence exactly at threshold passes",
                 gate({"resolved": True, "confidence": 0.85, "evidence": "x"}, ON) is True)
    ok &= _check("a custom higher threshold is honored",
                 gate({"resolved": True, "confidence": 0.9, "evidence": "x"},
                      {"resolved_confidence_threshold": 0.95}) is False)
    ok &= _check("non-numeric confidence fails safely",
                 gate({"resolved": True, "confidence": "very", "evidence": "x"}, ON) is False)
    ok &= _check("missing confidence fails safely",
                 gate({"resolved": True, "evidence": "x"}, ON) is False)
    ok &= _check("a non-dict verdict fails safely",
                 gate(None, ON) is False and gate("resolved", ON) is False)
    ok &= _check("an empty verdict ({}) fails safely",
                 gate({}, ON) is False)
    ok &= _check("a garbage threshold falls back to 0.85 (still passes a 0.9 verdict)",
                 gate({"resolved": True, "confidence": 0.9, "evidence": "x"},
                      {"resolved_confidence_threshold": "high"}) is True)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
