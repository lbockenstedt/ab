#!/usr/bin/env python3
"""Self-test for ab/dedup.py — strengthens confidence in duplicate detection.

Run:  python3 ab/test_dedup.py

Standalone: imports only ``dedup`` (stdlib-only, no app init, no GitHub/network).

The cases below use the REAL recurring errors that previously spawned redundant
AI-fix issues + branches:

  * opnsense ``NameError: 'time' module not imported``  — filed repeatedly as
    AI Fix #25 -> #55 -> #78 -> #90 because each "fix" closed the issue and the
    next cycle's identical error was filed anew (dedup only searched OPEN issues).
  * pxmx ``Agent Error - Optional Not Defined``  — filed as #70 -> #73.

The strengthened detection must:
  (a) match the SAME error across cycles despite timestamp drift, the opns vs
      opnsense module-name variant, and the boilerplate wrapper;
  (b) NOT cross-match an opnsense error against an unrelated pxmx error.

What this test does NOT claim: catching two errors that are the same root cause
but phrased so differently by the LLM that they share almost no tokens (e.g.
"name 'time' is not defined" vs "missing import of time module"). That needs the
deferred fingerprint-registry (signature -> issue) and is out of scope here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dedup import (
    _normalize_for_dedup, _token_set, _jaccard, _is_duplicate_match,
    MODULE_ALIASES, strip_boilerplate,
)

# The exact boilerplate wrapper create_automated_issue (main.py) injects.
_BOILERPLATE_BODY = (
    "**Automated Error Detection**\n\n"
    "The AppBuilder Hub analysis detected a potential issue in the logs:\n\n"
    "### Log Evidence:\n```\n{snippet}\n```\n\n"
    "This issue has been automatically created for fixing."
)


def _opnsense_issue(title_extra, snippet):
    """An automated issue for the recurring opnsense 'time' import error."""
    title = f"🤖 Log Alert: NameError: 'time' module not imported {title_extra}"
    body = _BOILERPLATE_BODY.format(snippet=snippet)
    return title, body


def _pxmx_issue(snippet):
    title = "🤖 Log Alert: Agent Error - Optional Not Defined"
    body = _BOILERPLATE_BODY.format(snippet=snippet)
    return title, body


# --- opnsense recurrence: cycle A (module named "opns") vs cycle B ("opnsense") -
OPNS_A_TITLE, OPNS_A_BODY = _opnsense_issue(
    "in opns control_plane",
    "2026-06-10 03:53:05,214 [ERROR] opns.control_plane: NameError: "
    "name 'time' is not defined at line 82",
)
OPNS_B_TITLE, OPNS_B_BODY = _opnsense_issue(
    "in opnsense control_plane",
    "2026-06-17 00:02:06,054 [ERROR] opnsense.control_plane: NameError: "
    "'time' module not imported",
)

# --- pxmx recurrence: cycle A vs cycle B (same error, drifted timestamp) -------
PXMX_A_TITLE, PXMX_A_BODY = _pxmx_issue(
    "2026-06-12 11:14:02,001 [ERROR] pxmx.agent: NameError: name 'Optional' "
    "is not defined in agent.py:44",
)
PXMX_B_TITLE, PXMX_B_BODY = _pxmx_issue(
    "2026-06-15 19:30:55,772 [ERROR] pxmx.agent: NameError: name 'Optional' "
    "is not defined in agent.py:44",
)


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


def main():
    print("Running ab dedup self-test...")
    ok = True

    # (1) opnsense recurrence across cycles matches despite:
    #     - timestamp drift, line-number drift, phrasing drift in the body
    #     - opns vs opnsense module-name variant in title + body
    #     - the boilerplate wrapper + emoji
    ok &= _check(
        "opnsense #25-style vs #90-style recurrence matches",
        _is_duplicate_match(OPNS_B_TITLE, OPNS_B_BODY, OPNS_A_TITLE, OPNS_A_BODY),
    )

    # (2) pxmx recurrence across cycles matches despite timestamp drift.
    ok &= _check(
        "pxmx #70 vs #73 recurrence matches",
        _is_duplicate_match(PXMX_A_TITLE, PXMX_A_BODY, PXMX_B_TITLE, PXMX_B_BODY),
    )

    # (3) Cross-module: an opnsense error must NOT match an unrelated pxmx error.
    ok &= _check(
        "opnsense error does NOT cross-match pxmx error",
        not _is_duplicate_match(OPNS_B_TITLE, OPNS_B_BODY, PXMX_A_TITLE, PXMX_A_BODY),
    )

    # (4) Module alias: opns and opnsense normalize to the SAME canonical token.
    ok &= _check(
        "MODULE_ALIASES maps opns -> opnsense",
        "opns" in MODULE_ALIASES and MODULE_ALIASES["opns"] == "opnsense",
    )
    ok &= _check(
        "_normalize_for_dedup('opns') == _normalize_for_dedup('opnsense')",
        _normalize_for_dedup("opns") == _normalize_for_dedup("opnsense") == "opnsense",
    )

    # (5) Boilerplate stripping: the wrapper must not change the normalized core,
    #     so two issues differing only by the wrapper compare equal.
    wrapped = "🤖 Log Alert: NameError: 'time' module not imported"
    bare = "NameError time module not imported"
    ok &= _check(
        "boilerplate+emoji wrapper does not affect normalized title",
        _normalize_for_dedup(wrapped) == _normalize_for_dedup(bare),
    )

    # (6a) Timestamp drift alone (same line number) still compares equal — the
    #      timestamp is stripped as noise.
    snip_a = "2026-06-10 03:53:05,214 [ERROR] opnsense: NameError time not imported at line 82"
    snip_b = "2026-06-17 00:02:06,054 [ERROR] opnsense: NameError time not imported at line 82"
    ok &= _check(
        "timestamp drift does not affect normalized body",
        _normalize_for_dedup(snip_a) == _normalize_for_dedup(snip_b),
    )

    # (6b) Line-number / error-code drift is now PRESERVED as a discriminator:
    #      two snippets differing only by line number must NOT normalize equal,
    #      so a genuinely-distinct bug is no longer folded into an existing issue.
    snip_c = "2026-06-17 00:02:06,054 [ERROR] opnsense: NameError time not imported at line 901"
    ok &= _check(
        "line-number drift IS preserved (distinct errors stay distinct)",
        _normalize_for_dedup(snip_a) != _normalize_for_dedup(snip_c),
    )

    # (7) Sanity: genuinely different errors do not match.
    ok &= _check(
        "two unrelated distinct errors do not match",
        not _is_duplicate_match(
            "Connection refused to hub on port 8000",
            "2026-06-10 01:00:00,000 [ERROR] hub: ConnectionRefusedError on port 8000",
            "Disk full on /var partition",
            "2026-06-10 02:00:00,000 [ERROR] disk: OSError: No space left on device",
        ),
    )

    # (8) WebUI bug reports: title is "🤖 Bug Report - <short error summary>",
    # error also on its own body line. _normalize_for_dedup strips the "Bug
    # Report" boilerplate, so dedup compares the error summary (title) plus the
    # body signature. SAME error across two filings must match; two DISTINCT
    # errors must NOT — even though both share the "Bug Report" prefix.
    def _webui(err, view, ver):
        summary = " ".join(err.split())[:80].strip()
        title = f"🤖 Bug Report - {summary}" if summary else "🤖 Bug Report"
        body = (
            f'**Filed via the LM WebUI "Bug/Feature Request" button**\n\n'
            f"**Error:** {err}\n\n"
            f"### What's wrong\n[Auto-filed from a runtime browser error]\n\n"
            f"Error: {err}\nView: {view}\n\n### Context\n- hubVersion: {ver}\n"
        )
        return title, body
    same_at, same_a = _webui("Can't find variable: ensureLDAPTennants", "ldap / Users", ".1215")
    same_bt, same_b = _webui("Can't find variable: ensureLDAPTennants", "ldap / Users", ".1216")
    diff_bt, diff_b = _webui("Cannot read properties of undefined reading foo", "dns / Zones", ".1216")
    ok &= _check(
        "summary-title: SAME browser error across two filings matches",
        _is_duplicate_match(same_bt, same_b, same_at, same_a),
    )
    ok &= _check(
        "summary-title: DISTINCT browser errors do NOT false-match on the 'Bug Report' prefix",
        not _is_duplicate_match(diff_bt, diff_b, same_at, same_a),
    )

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())