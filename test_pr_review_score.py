#!/usr/bin/env python3
"""Self-test for pr_review_score — the module AppBuilder uses to read and act on
its own PR pre-review.

Unlike most fix_engine self-tests, pr_review_score imports cleanly (no circular
main.py chain), so this imports it directly. Covers the pure parser and the
self-improvement feeder (dissent_feedback) against a rendered pre-review body in
the exact shape pr_review.py emits — including the mixed panel where one panel is
strong (APPROVE, high confidence) and the other carries a dissenting reviewer.
Run: python3 test_pr_review_score.py
"""
import sys

import pr_review_score as prs

# A trimmed but format-faithful pre-review body (see pr_review._render_panel /
# _render_state_panel). Panel 1 is clean; panel 2 has a 40% Reject → dissent.
SAMPLE = """<!-- ab-pr-review -->
<!-- head: 9f0d19f88196c460595c4434ec1b737a1d16b166 -->
## 🤖 AppBuilder PR pre-review

### 🧠 Skeptical review (panel)

🟢 **Recommendation: APPROVE** · panel confidence **94%**

#### Reviewer detail

**Reviewer (copilot)** — 🟢 Approve · confidence 95%

The changes correctly and safely address both issues.

**Reviewer (copilot)** — 🟢 Approve · confidence 92%

Logic is sound; tests are thorough.

### 🔀 State-logic / control-flow review (panel)

🟢 **Recommendation: APPROVE** · panel confidence **77%**

#### ⚠️ Concerns (1 of 3 reviewers did not approve)

- 🔴 **Reviewer (copilot)** (40%) — STATE CONFLATION in error_context.

#### Reviewer detail

**Reviewer (copilot)** — 🟢 Approve · confidence 98%

No state/status coverage defects found.

**Reviewer (copilot)** — 🔴 Reject · confidence 40%

STATE CONFLATION — error_context conflates five distinct failure states and
unconditionally asserts the last attempt edited the WRONG file.

**Reviewer (copilot)** — 🟢 Approve · confidence 92%

Well-scoped defensive change.
"""


def _check(name, cond):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
    return bool(cond)


class _FakeComment:
    def __init__(self, body):
        self.body = body


class _FakePR:
    def __init__(self, comments):
        self._c = comments

    def get_issue_comments(self):
        return list(self._c)


def main():
    ok = True
    report = prs.parse_review(SAMPLE)

    ok &= _check("parse: head sha captured",
                 report["head"] == "9f0d19f88196c460595c4434ec1b737a1d16b166")
    ok &= _check("parse: two panels found", len(report["panels"]) == 2)
    ok &= _check("parse: composite is the WEAKEST panel (77)", report["composite"] == 77)
    ok &= _check("parse: general panel 94% / APPROVE",
                 report["panels"][0]["confidence"] == 94 and report["panels"][0]["verdict"] == "APPROVE")
    ok &= _check("parse: state-logic panel has one dissenting reviewer (40%)",
                 len(report["panels"][1]["dissenting"]) == 1
                 and report["panels"][1]["dissenting"][0]["confidence"] == 40)
    ok &= _check("parse: any_dissent True", report["any_dissent"] is True)

    fb = prs.dissent_feedback(report, SAMPLE, min_pct=90)
    ok &= _check("dissent_feedback: names the below-target state-logic panel",
                 "State-logic" in fb and "77%" in fb)
    ok &= _check("dissent_feedback: quotes the dissenting reviewer's concern",
                 "STATE CONFLATION" in fb)
    ok &= _check("dissent_feedback: omits the clean 94% panel from the ask",
                 "Skeptical review (panel) — panel confidence 94%" not in fb)

    # A clean review yields no feedback (nothing to fix → loop terminates).
    clean = SAMPLE.replace("**77%**", "**96%**").replace(
        "**Reviewer (copilot)** — 🔴 Reject · confidence 40%", "**Reviewer (copilot)** — 🟢 Approve · confidence 96%")
    clean_report = prs.parse_review(clean)
    ok &= _check("dissent_feedback: empty when every panel is at/above target",
                 prs.dissent_feedback(clean_report, clean, min_pct=90) == "")

    # Programmatic reader over a PyGithub-shaped PR (latest marker comment wins).
    pr = _FakePR([_FakeComment("unrelated"), _FakeComment(SAMPLE)])
    r2, b2 = prs.read_pr_review(pr)
    ok &= _check("read_pr_review: returns the posted pre-review report+body",
                 r2 is not None and r2["composite"] == 77 and prs._marker() in (b2 or ""))
    none_pr = _FakePR([_FakeComment("no review here")])
    ok &= _check("read_pr_review: (None, None) when AB has not posted a review",
                 prs.read_pr_review(none_pr) == (None, None))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
