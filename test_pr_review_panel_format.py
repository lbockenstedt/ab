#!/usr/bin/env python3
"""Self-test: the advisory review panels render as readable, structured markdown.

The panels used to dump every reviewer's prose as one whitespace-collapsed
paragraph, with each reviewer's own verdict/confidence thrown away. These cases
pin the replacement: recommendation first, concerns next, per-reviewer detail
below — and pin that highlighting follows actual verdicts rather than
keyword-guessing the prose.

pr_review.py can't be imported (circular: pr_review -> github_ops -> main ->
log_scan -> main), so the renderers are ast-extracted, matching the other
pr_review self-tests.
"""
import ast
import re
import sys

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def _load_ns():
    src = open("pr_review.py").read()
    tree = ast.parse(src)
    want_fn = {"_norm_conf_pct", "_reviewer_blocks", "_structure_critique",
               "_render_review_body", "_render_panel", "_render_state_panel"}
    want_as = {"_REVIEWER_TAG_RE", "_REVIEWER_SPLIT_RE", "_ENUM_MARK_RE",
               "_SENTENCE_SPLIT_RE", "_PANEL_HEADER", "_STATE_PANEL_HEADER"}
    segs = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in want_fn:
            segs.append(ast.get_source_segment(src, n))
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if getattr(t, "id", "") in want_as:
                    segs.append(ast.get_source_segment(src, n))
    ns = {"re": re}
    exec("\n\n".join(segs), ns)
    missing = (want_fn | want_as) - set(ns)
    if missing:
        raise SystemExit("could not extract from pr_review.py: %s" % sorted(missing))
    return ns


NS = _load_ns()


def render(review):
    return "\n".join(NS["_render_panel"](review))


MIXED = {
    "verdict": "Reject", "confidence": 0.58,
    "reviews": [
        {"reviewer": "gpt-5", "verdict": "Reject", "confidence": 0.41,
         "critique": "Mostly reasonable but has problems. 1) The loop mutates "
                     "the list it iterates. 2) No test covers the empty branch."},
        {"reviewer": "claude", "verdict": "Reject", "confidence": 0.55,
         "critique": "Existing configs lack the new key and this raises KeyError."},
        {"reviewer": "gemini", "verdict": "Approve", "confidence": 0.78,
         "critique": "Additive and well-scoped."},
    ],
}


def main():
    print("== recommendation header ==")
    out = render(MIXED)
    check("deny recommendation is at the top",
          out.index("Recommendation: DENY") < out.index("Reviewer detail"))
    check("deny is marked red", "\U0001F534 **Recommendation: DENY**" in out)
    check("panel confidence shown as pct", "panel confidence **58%**" in out)

    approve = dict(MIXED, verdict="Approve", confidence=0.93,
                   reviews=[dict(MIXED["reviews"][2])])
    aout = render(approve)
    check("approve is marked green", "\U0001F7E2 **Recommendation: APPROVE**" in aout)
    check("approve shows no-objections block", "No objections" in aout)
    check("approve shows no concerns block", "Concerns" not in aout)

    print("== concerns block ==")
    check("concerns precede detail", out.index("Concerns") < out.index("Reviewer detail"))
    check("counts only dissenters", "2 of 3 reviewers did not approve" in out)
    check("names each dissenter", "**gpt-5**" in out and "**claude**" in out)
    check("dissenter confidence shown", "(41%)" in out and "(55%)" in out)
    approving = [ln for ln in out.split("\n")
                 if ln.startswith("- \U0001F534") and "gemini" in ln]
    check("approving reviewer is not listed as a concern", not approving, approving)

    print("== per-reviewer detail ==")
    check("each reviewer's own verdict is kept",
          out.count("\U0001F534 Reject") == 2 and "\U0001F7E2 Approve" in out)
    check("each reviewer's own confidence is kept", "confidence 78%" in out)
    check("enumerated points become bullets",
          "- The loop mutates the list it iterates." in out)
    check("enumeration markers are stripped", "1) The loop" not in out)

    print("== legacy blob fallback ==")
    legacy = {"verdict": "Reject", "confidence": 0.62,
              "critique": "[gpt-5] Two issues. 1) Mutates while iterating. "
                          "2) Swallows the exception. | [gemini] Small and additive."}
    lout = render(legacy)
    check("legacy splits per reviewer", "**gpt-5**" in lout and "**gemini**" in lout)
    check("legacy strips the name tag", "[gpt-5]" not in lout)
    check("legacy bullets the points", "- Mutates while iterating." in lout)
    check("legacy deny still states a concern", "Concerns" in lout)
    check("legacy invents no verdicts", "Reject ·" not in lout)

    # A " | " inside one reviewer's prose must not be read as a reviewer break.
    piped = {"verdict": "Approve", "confidence": 0.9,
             "critique": "[gpt-5] Use `ps aux | grep foo` here | [gemini] Fine."}
    pout = render(piped)
    check("pipe inside prose is not a reviewer split",
          pout.count("**gpt-5**") == 1 and "grep foo" in pout
          and pout.count("Reviewer**") == 0, pout)

    print("== degenerate inputs ==")
    check("no review renders nothing", NS["_render_panel"](None) == [])
    check("empty review renders nothing", NS["_render_panel"]({}) == [])
    unavail = render({"status": "error", "reason": "provider timeout"})
    check("status path explains itself", "Panel unavailable" in unavail
          and "provider timeout" in unavail)
    check("status path emits no verdict", "Recommendation" not in unavail)

    blank = render({"verdict": "Reject", "confidence": 0.5,
                    "reviews": [{"reviewer": "gpt-5", "verdict": "Reject",
                                 "confidence": 0.5, "critique": ""}]})
    check("empty critique says so", "No critique text returned" in blank)

    bad = render({"verdict": "Reject", "confidence": "n/a",
                  "reviews": [{"reviewer": "x", "verdict": "Reject", "critique": "y."}]})
    check("unparseable confidence degrades to n/a", "confidence **n/a**" in bad)
    check("missing reviewer confidence is omitted, not zeroed",
          "confidence 0%" not in bad)

    long_crit = "Sentence one is here. " * 12
    lng = render({"verdict": "Reject", "confidence": 0.5,
                  "reviews": [{"reviewer": "x", "verdict": "Reject",
                               "confidence": 0.5, "critique": long_crit}]})
    concern = [l for l in lng.split("\n") if l.startswith("- \U0001F534")][0]
    check("long headline is truncated in concerns", len(concern) < 300, len(concern))
    check("unenumerated prose is broken into paragraphs",
          lng.count("Sentence one is here. Sentence one is here.") > 1)

    print("== state panel shares the format ==")
    sout = "\n".join(NS["_render_state_panel"](MIXED))
    check("state panel uses its own header", NS["_STATE_PANEL_HEADER"] in sout)
    check("state panel is structured too",
          "Recommendation: DENY" in sout and "Reviewer detail" in sout)

    print("\n%s" % ("FAILED: %s" % FAILURES if FAILURES else "all checks passed"))
    return 1 if FAILURES else 0


def test_pr_review_panel_format():
    """pytest wrapper so this runs in CI as well as standalone."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
