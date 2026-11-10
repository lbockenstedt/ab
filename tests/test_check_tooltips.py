#!/usr/bin/env python3
"""Self-test for check_tooltips.py — Tier-1 tooltip-completeness scan for PR
pre-review (wired into pr_review.py's findings list) and, by extension,
feature auto-drive's built PRs.

Run:  python3 ab/test_check_tooltips.py

Standalone: imports only check_tooltips (no app/main init).
"""
import sys

import check_tooltips as ct


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


class _FakeFile:
    def __init__(self, filename, patch):
        self.filename = filename
        self.patch = patch


def main():
    print("Running ab check_tooltips self-test...")
    ok = True

    # --- find_missing_tooltips (single-file patch) --------------------------

    patch_missing = (
        "@@ -1,3 +1,4 @@\n"
        " <div>\n"
        '+<button onclick="doIt()" class="btn">Go</button>\n'
        " </div>\n"
    )
    hits = ct.find_missing_tooltips(patch_missing)
    ok &= _check("a button with no title= is flagged", len(hits) == 1 and hits[0][0] == "button")

    patch_with_title = (
        "@@ -1,3 +1,4 @@\n"
        " <div>\n"
        '+<button onclick="doIt()" title="Does the thing" class="btn">Go</button>\n'
        " </div>\n"
    )
    ok &= _check("a button WITH title= is not flagged", ct.find_missing_tooltips(patch_with_title) == [])

    patch_wrapped = (
        "@@ -1,3 +1,4 @@\n"
        " <div>\n"
        '+<button onclick="doIt()"\n'
        '+        title="Does the thing" class="btn">Go</button>\n'
        " </div>\n"
    )
    ok &= _check("title= on the FOLLOWING added line (wrapped attrs) still counts",
                ct.find_missing_tooltips(patch_wrapped) == [])

    patch_hidden = (
        "@@ -1,3 +1,4 @@\n"
        " <div>\n"
        '+<input type="hidden" name="csrf" value="x">\n'
        " </div>\n"
    )
    ok &= _check("a hidden <input> is never flagged (not user-visible)",
                ct.find_missing_tooltips(patch_hidden) == [])

    patch_select = (
        "@@ -1,3 +1,4 @@\n"
        " <div>\n"
        '+<select name="mode"><option>a</option></select>\n'
        " </div>\n"
    )
    ok &= _check("a <select> with no title= is flagged",
                len(ct.find_missing_tooltips(patch_select)) == 1)

    patch_no_tag = (
        "@@ -1,3 +1,4 @@\n"
        " <div>\n"
        "+<span>just text, not a control</span>\n"
        " </div>\n"
    )
    ok &= _check("a line with no button/input/select tag yields nothing",
                ct.find_missing_tooltips(patch_no_tag) == [])

    ok &= _check("empty patch yields nothing, no crash", ct.find_missing_tooltips("") == [])
    ok &= _check("None patch yields nothing, no crash", ct.find_missing_tooltips(None) == [])

    # --- find_missing_tooltips_in_files (PyGithub File list shape) ---------

    files = [
        _FakeFile("templates/index.html", patch_missing),
        _FakeFile("routes.py", '+x = 1\n'),  # non-.html file — never scanned
        _FakeFile("lm/WebUI/main.js", '+<button onclick="x()">Go</button>\n'),  # .js — never scanned
    ]
    findings = ct.find_missing_tooltips_in_files(files)
    ok &= _check("only .html files are scanned (routes.py and .js ignored)",
                len(findings) == 1)
    ok &= _check("finding uses level='advisory' (a nudge, not an error)",
                findings[0]["level"] == "advisory")
    ok &= _check("finding names the file", "templates/index.html" in findings[0]["title"])

    ok &= _check("empty file list yields no findings, no crash",
                ct.find_missing_tooltips_in_files([]) == [])
    ok &= _check("None file list yields no findings, no crash",
                ct.find_missing_tooltips_in_files(None) == [])

    # --- cap behavior --------------------------------------------------------
    many_files = [_FakeFile(f"templates/f{i}.html", patch_missing) for i in range(ct._MAX_FINDINGS + 5)]
    capped = ct.find_missing_tooltips_in_files(many_files)
    ok &= _check(f"findings are capped at {ct._MAX_FINDINGS} (+1 cap-notice entry)",
                len(capped) == ct._MAX_FINDINGS + 1)
    ok &= _check("the cap-notice entry is itself level='advisory'",
                capped[-1]["level"] == "advisory" and "capped" in capped[-1]["title"].lower())

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
