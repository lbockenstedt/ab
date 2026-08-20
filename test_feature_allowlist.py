#!/usr/bin/env python3
"""Self-test for feature_allowlist — the DEFAULT-DENY positive allowlist that,
together with feature_boundary's deny-list, gates AppBuilder auto-merge.

Run:  python3 ab/test_feature_allowlist.py

feature_allowlist is a pure, import-light module (like feature_boundary), so
this imports it directly. The safety-critical direction is FALSE POSITIVES
(auto-approving something behaviour-changing), so most cases assert
auto_approvable is False; the few True cases are the deliberately-narrow
additive shapes."""
import sys

import feature_allowlist as fa


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _f(path, status="modified", patch="", additions=0, deletions=0):
    return {"path": path, "status": status, "patch": patch,
            "additions": additions, "deletions": deletions}


def main():
    ok = True

    # ── fail-closed basics ──────────────────────────────────────────────────
    ok &= _check("empty file list -> not auto-approvable",
                 fa.classify([])["auto_approvable"] is False)

    # ── docs-only ───────────────────────────────────────────────────────────
    ok &= _check("all-markdown diff -> docs-only auto-approvable",
                 fa.classify([_f("ab/docs/x.md", patch="@@\n+hi\n"),
                              _f("README.md", patch="@@\n-a\n+b\n")]) == {
                     "category": "docs-only", "auto_approvable": True,
                     "reason": "additive allowlist category 'docs-only' (auto-approvable)"})
    ok &= _check("docs + one code file -> NOT docs-only (mixed) -> blocked",
                 fa.classify([_f("ab/docs/x.md", patch="@@\n+hi\n"),
                              _f("ab/routes.py", patch="@@\n+code()\n")])["auto_approvable"] is False)

    # ── log-only ────────────────────────────────────────────────────────────
    ok &= _check("pure added logger.* line -> log-only auto-approvable",
                 fa.classify([_f("ab/routes.py",
                                 patch="@@ -1 +1,2 @@\n context\n+    logger.info('x')\n")])["category"] == "log-only")
    ok &= _check("added logging.debug across two files -> log-only",
                 fa.classify([_f("a.py", patch="@@\n+logger.debug('a')\n"),
                              _f("b.py", patch="@@\n+logging.warning('b')\n")])["auto_approvable"] is True)
    ok &= _check("added log line PLUS a real code line -> NOT log-only -> blocked",
                 fa.classify([_f("a.py", patch="@@\n+logger.info('x')\n+x = compute()\n")])["auto_approvable"] is False)
    ok &= _check("a DELETION present -> NOT log-only (not purely additive) -> blocked",
                 fa.classify([_f("a.py", patch="@@\n-old_call()\n+logger.info('x')\n")])["auto_approvable"] is False)
    ok &= _check("logger CONFIG (setLevel/addHandler) is not a log CALL -> blocked",
                 fa.classify([_f("a.py", patch="@@\n+logger.setLevel(10)\n")])["auto_approvable"] is False)
    ok &= _check("missing patch text -> fail closed (not log-only)",
                 fa.classify([_f("a.py", patch=None)])["auto_approvable"] is False)
    ok &= _check("removed/renamed file -> never log-only",
                 fa.classify([_f("a.py", status="removed", patch="@@\n-logger.info('x')\n")])["auto_approvable"] is False)

    # ── tooltip-only ────────────────────────────────────────────────────────
    ok &= _check("only a title= attribute changed -> tooltip-only",
                 fa.classify([_f("t.html", patch="@@\n-<i title=\"Old\">\n+<i title=\"New\">\n")])["category"] == "tooltip-only")
    ok &= _check("aria-label copy change -> tooltip-only auto-approvable",
                 fa.classify([_f("t.html", patch="@@\n+<button aria-label=\"Close dialog\">\n")])["auto_approvable"] is True)
    ok &= _check("tooltip token but real logic on the line -> blocked",
                 fa.classify([_f("t.py", patch="@@\n+if title==x: return os.system('rm')\n")])["auto_approvable"] is False)
    ok &= _check("empty/no-op diff can't masquerade as tooltip-only",
                 fa.classify([_f("t.html", patch="@@\n context only\n")])["auto_approvable"] is False)

    # ── config-driven narrowing ─────────────────────────────────────────────
    ok &= _check("category matched but disabled in allowlist subset -> blocked",
                 fa.classify([_f("README.md", patch="@@\n+x\n")], allowlist=["log-only"])["auto_approvable"] is False)
    ok &= _check("category matched AND enabled in subset -> auto-approvable",
                 fa.classify([_f("README.md", patch="@@\n+x\n")], allowlist=["docs-only"])["auto_approvable"] is True)

    # ── behaviour-changing shapes are NEVER a category ──────────────────────
    ok &= _check("a route-handler diff -> no category -> human approval",
                 fa.classify([_f("ab/routes.py", patch="@@\n+@app.route('/x')\n+def x(): return go()\n")])["auto_approvable"] is False)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature-allowlist self-test...")
    sys.exit(main())
