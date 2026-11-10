"""Selftest for fix_engine._prior_failure_context.

WHY: error_context (the attempt-to-attempt feedback inside one run) is reset to
None at the start of every run, and the stored failure_detail was written to the
record for the UI but never read back. A retry of a terminally-failed issue
therefore began completely blind — the builder re-derived the same wrong fix and
the reviewers re-rejected it for the same reason, burning all attempts again.

Observed on lbockenstedt/lm#452 and #487: both rejected 3/3 because the fix
edited the wrong file/function, with the reviewer stating exactly where the bug
actually lived.

fix_engine imports the app (circular at import time), so the pure helper is
extracted with ast — the same harness pattern the other selftests use.
"""
import ast
import re

_WANT_FUNCS = {"_prior_failure_context"}
_WANT_ASSIGNS = {"_REPLAYABLE_FAILURE_KINDS"}


def _load():
    tree = ast.parse(open("fix_engine.py").read())
    ns = {"re": re}
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
    ctx = ns["_prior_failure_context"]
    ok = True
    print("prior-failure replay into retries:")

    # ── the real lm#487 record ─────────────────────────────────────────────
    rec487 = {
        "status": "failed",
        "attempts": 3,
        "failure_kind": "review_rejected",
        "failure_confidence": 0.08333333333333333,
        "failure_detail": ("[Reviewer (copilot)] The issue specifically occurs on the "
                           "credvault view ('currentView: credvault') when clicking 'edit'. "
                           "The proposed fix modifies the z-index in securityEventDetail(i)."),
    }
    out = ctx(rec487)
    ok &= _check("a rejected run produces context", bool(out))
    ok &= _check("the reviewer's finding is replayed verbatim",
                 "securityEventDetail(i)" in out and "credvault" in out)
    ok &= _check("attempt count is stated", "3 attempt(s)" in out)
    ok &= _check("confidence is rendered as a percentage", "8%" in out)
    ok &= _check("names the reviewer panel as the cause", "REJECTED" in out)
    ok &= _check("instructs against repeating the change",
                 "Do not re-propose the same change" in out)

    # ── the real lm#486 record (a different failure kind) ───────────────────
    rec486 = {
        "status": "failed", "attempts": 3, "failure_kind": "no_edits",
        "failure_detail": "The JSON parsed but contained no applicable changes.",
    }
    out486 = ctx(rec486)
    ok &= _check("a no_edits run produces context", bool(out486))
    ok &= _check("no_edits gets its own explanation",
                 "no applicable edits" in out486)

    # ── first runs and non-replayable states produce NOTHING ────────────────
    ok &= _check("a fresh issue (no record) is unchanged", ctx({}) == "")
    ok &= _check("None is handled", ctx(None) == "")
    ok &= _check("a non-dict is handled", ctx("failed") == "")
    ok &= _check("a fixed issue is unchanged",
                 ctx({"status": "fixed", "failure_kind": "review_rejected",
                      "failure_detail": "x"}) == "")
    ok &= _check("awaiting_review is not replayed (the fix is still pending)",
                 ctx({"status": "awaiting_review", "failure_kind": "review_rejected",
                      "failure_detail": "x"}) == "")
    ok &= _check("a failure with no detail is unchanged",
                 ctx({"status": "failed", "failure_kind": "review_rejected",
                      "failure_detail": ""}) == "")
    ok &= _check("a whitespace-only detail is unchanged",
                 ctx({"status": "failed", "failure_kind": "review_rejected",
                      "failure_detail": "   \n "}) == "")
    ok &= _check("an unknown failure kind is not replayed",
                 ctx({"status": "failed", "failure_kind": "mystery",
                      "failure_detail": "something"}) == "")

    # ── robustness on malformed values ─────────────────────────────────────
    weird = {"status": "failed", "failure_kind": "review_rejected",
             "failure_detail": "d", "attempts": "lots", "failure_confidence": "nope"}
    out_w = ctx(weird)
    ok &= _check("non-numeric attempts/confidence do not raise", bool(out_w))
    ok &= _check("non-numeric confidence is omitted rather than printed raw",
                 "nope" not in out_w)
    ok &= _check("non-numeric attempts falls back to prose", "several attempt(s)" in out_w)

    # ── detail is bounded (records store up to 1000 chars) ─────────────────
    big = {"status": "failed", "failure_kind": "review_rejected", "attempts": 1,
           "failure_detail": "z" * 5000}
    ok &= _check("an over-long detail is truncated", ctx(big).count("z") == 1000)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
