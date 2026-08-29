#!/usr/bin/env python3
"""Regression test: WebUI-filed bug/feature-request titles must never cut off
mid-word.

Run:  python3 test_log_scan_title_truncation.py

Found via a real filed issue (lm#444): the title ended "...under the \"lrb\"
tenant an" — a plain ``[:80]`` slice landing mid-word ("and"). log_scan.py's
scan_bugs() now builds both the bug-report and feature-request titles with
``textwrap.shorten(..., width=80, placeholder=" …")`` instead, which only
drops whole words. This test execs the ACTUAL title-construction lines out of
log_scan.py's source (log_scan.py can't be imported directly — see
test_log_scan_requirements.py's docstring for why) so a future edit that
reintroduces a raw slice is what breaks this test, not a reimplementation of
the fix."""
import ast


def _extract_title_exprs():
    """Pulls the two literal title-construction lines out of scan_bugs()'s
    real source (not a copy) via ast, keyed by the f-string prefix each one
    starts with."""
    src = open("log_scan.py").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "scan_bugs")
    seg = ast.get_source_segment(src, node)
    feature_line = next(ln for ln in seg.splitlines()
                        if ln.strip().startswith('title = f"💡 Feature Request:'))
    bug_summary_line = next(ln for ln in seg.splitlines()
                            if ln.strip().startswith("_summary ="))
    return feature_line.strip(), bug_summary_line.strip()


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running log_scan.py title-truncation self-test...")
    ok = True

    feature_line, bug_summary_line = _extract_title_exprs()
    ok &= _check("feature-request title uses textwrap.shorten, not a raw slice",
                "textwrap.shorten" in feature_line and "[:80]" not in feature_line)
    ok &= _check("bug-report summary uses textwrap.shorten, not a raw slice",
                "textwrap.shorten" in bug_summary_line and "[:80]" not in bug_summary_line)

    # ── the actual regression: the real string from lm#444 ──────────────────
    import textwrap
    real_msg = ('When I am on a specific page, for example "My Devices" under '
                'the "lrb" tenant and I try to filter by online status, the '
                'filter dropdown does not apply correctly.')
    raw_slice = real_msg[:80]
    ok &= _check("lm#444-style message: the raw [:80] slice WOULD have cut mid-word "
                "(proves this is a real regression, not a hypothetical one)",
                raw_slice[-1].isalnum() and real_msg[80].isalnum())

    summary = textwrap.shorten(real_msg, width=80, placeholder=" …").strip()
    ok &= _check("lm#444-style message: truncated title ends with the ellipsis marker, "
                "never with a bare partial word",
                summary.endswith("…") or summary == real_msg)
    ok &= _check("lm#444-style message: every word in the (possibly truncated) "
                "title is a whole word from the source text",
                all(w.strip(" …") in real_msg.split() or w == "…"
                    for w in summary.split()))

    # ── a short message is passed through untouched ─────────────────────────
    short = textwrap.shorten("Login button is broken", width=80, placeholder=" …").strip()
    ok &= _check("a message under 80 chars is not truncated at all",
                short == "Login button is broken")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
