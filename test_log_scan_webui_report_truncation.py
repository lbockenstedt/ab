#!/usr/bin/env python3
"""Regression test: WebUI-filed bug/feature-request titles AND the body's
**Error:** summary line must never cut off mid-word.

Run:  python3 test_log_scan_webui_report_truncation.py

Two separate raw-slice truncations were found on the same real filed issue
(lm#444), in two different fixes:

1. The TITLE ended "...under the \"lrb\" tenant an" — a plain ``[:80]``
   slice landing mid-word ("and"). Fixed with
   ``textwrap.shorten(..., width=80, placeholder=" …")``, which only drops
   whole words.
2. The body's **Error:** line (built from the SAME explanation text, via
   scan_bugs()'s fallback branch when explanation has no "Error:"-prefixed
   line — the common WebUI case) ended "...continues to show data f" — a
   separate raw ``[:200]`` slice, found only after the title fix shipped and
   the user pointed out the body was ALSO cut off. That block's own comment
   says the Error line carries "the FULL error text" — the [:200] directly
   contradicted its own documented intent, so the fix removes the cap
   entirely (collapses to one line, no length limit) rather than widening it
   to another arbitrary number.

This test execs the ACTUAL construction lines out of log_scan.py's source
(log_scan.py can't be imported directly — see test_log_scan_requirements.py's
docstring for why) so a future edit that reintroduces a raw slice at either
site is what breaks this test, not a reimplementation of the fix."""
import ast


def _extract_exprs():
    """Pulls the _msg fallback assignment (the body's Error-line source) and
    the two title-construction lines out of scan_bugs()'s real source via
    ast."""
    src = open("log_scan.py").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "scan_bugs")
    seg = ast.get_source_segment(src, node)
    lines = seg.splitlines()
    msg_idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("_msg ="))
    # The assignment wraps onto the following continuation line (the
    # ``\`` line-continuation the fallback ternary is written on). Only the
    # FIRST line's leading whitespace needs stripping for a bare exec() (a
    # top-level statement can't start indented) — the continuation line's
    # own indentation doesn't matter since ``\`` joins it into one logical
    # line regardless.
    msg_stmt = "\n".join([lines[msg_idx].strip(), lines[msg_idx + 1]])
    feature_line = next(ln for ln in lines if ln.strip().startswith('title = f"💡 Feature Request:'))
    bug_summary_line = next(ln for ln in lines if ln.strip().startswith("_summary ="))
    return msg_stmt, feature_line.strip(), bug_summary_line.strip()


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running log_scan.py WebUI-report truncation self-test...")
    ok = True

    msg_stmt, feature_line, bug_summary_line = _extract_exprs()

    # ── site 2: the body's **Error:** line (the _msg fallback) ──────────────
    ok &= _check("the _msg fallback (body's **Error:** line) has no raw slice cap",
                "[:200]" not in msg_stmt and "[:" not in msg_stmt)

    # ── site 1: the title (regression-tested previously, kept here too) ─────
    ok &= _check("feature-request title uses textwrap.shorten, not a raw slice",
                "textwrap.shorten" in feature_line and "[:80]" not in feature_line)
    ok &= _check("bug-report summary uses textwrap.shorten, not a raw slice",
                "textwrap.shorten" in bug_summary_line and "[:80]" not in bug_summary_line)

    # ── the actual regression: the real string from lm#444 ──────────────────
    import textwrap
    real_msg = ('When I am on a specific page, for example "My Devices" under '
                'the "lrb" tenant and I switch tenants (E.G. switch from "lrb" '
                'to "DXP") the "My Devices" page does not refresh and continues '
                'to show data from the "lrb" tenant.')

    # Site 2: simulate exactly what the extracted _msg fallback computes —
    # no "Error:"-prefixed line in explanation, so the ternary's else-branch
    # (join/split, no cap) is what runs.
    _err = ""
    ns = {"_err": _err, "explanation": real_msg}
    exec(f"{msg_stmt}", ns)
    ok &= _check("lm#444-style explanation: the body's **Error:** line carries the FULL text, uncut",
                ns["_msg"] == real_msg)

    raw_slice_200 = real_msg[:200]
    ok &= _check("lm#444-style explanation: the OLD [:200] slice WOULD have cut mid-word "
                "(proves this is a real regression, not a hypothetical one)",
                len(real_msg) > 200 and raw_slice_200[-1].isalnum() and real_msg[200].isalnum())

    # Site 1: title summary still truncates correctly, word-boundary-safe.
    summary = textwrap.shorten(ns["_msg"], width=80, placeholder=" …").strip()
    ok &= _check("lm#444-style message: truncated title ends with the ellipsis marker, "
                "never with a bare partial word",
                summary.endswith("…") or summary == real_msg)
    ok &= _check("lm#444-style message: every word in the (possibly truncated) "
                "title is a whole word from the source text",
                all(w.strip(" …") in real_msg.split() or w == "…"
                    for w in summary.split()))

    # ── a short message is passed through untouched at both sites ───────────
    short_ns = {"_err": "", "explanation": "Login button is broken"}
    exec(f"{msg_stmt}", short_ns)
    ok &= _check("a short explanation is not truncated in the body's Error line either",
                short_ns["_msg"] == "Login button is broken")
    short_summary = textwrap.shorten(short_ns["_msg"], width=80, placeholder=" …").strip()
    ok &= _check("a message under 80 chars is not truncated in the title",
                short_summary == "Login button is broken")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
