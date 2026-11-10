"""Pins _merge_refusal + merge_pr's structured handling of GitHub policy refusals.

WHY: pr_actions.merge_pr had exactly one escape hatch -- `if not
_is_merge_conflict(e): raise`. Everything GitHub refused for a NON-conflict
reason propagated out as an exception, so both callers logged it at ERROR with
a stack trace:

    pr_review merge failed for lbockenstedt/ab#165: Repository rule violations found
    pr_review merge failed for lbockenstedt/kvm#7: refusing to allow a Personal
        Access Token to create or update workflow ... without `workflow` scope: 403

Neither is an AppBuilder defect. A ruleset rejecting a merge, or a token that
was deliberately issued without `workflow` scope, is the repository behaving
exactly as configured -- the fix is an operator changing a setting, not a code
change. But because they were ERROR, AppBuilder's own log scanner harvested
them and filed them as bugs against AppBuilder: ab#107, #170, #192.

ab#192 adds the second half of the problem. It records the SAME refusal for the
same PR at 23:27:41, :46, :53, 23:28:00 and :28:17 -- five times in 36 seconds.
Auto-drive re-attempts the merge every poll, and nothing about a rejected PR
changes between polls, so a single unchanged condition produced five identical
ERROR lines and its own escalation.

So there are two things to pin, and the second is the subtle one: the
suppression state must NOT live in `auto_merge_blocked_reason`, because
_maybe_auto_merge clears that field immediately before every merge attempt. Any
dedupe keyed on it would be reset each poll and never actually suppress
anything -- it would look correct and do nothing.
"""
import ast
import sys

SRC = "pr_actions.py"


class GithubException(Exception):
    def __init__(self, status, data):
        super().__init__(status, data)
        self.status = status
        self.data = data


def _load_refusal():
    tree = ast.parse(open(SRC).read())
    node = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "_merge_refusal"), None)
    assert node is not None, f"_merge_refusal missing from {SRC}"
    ns = {"GithubException": GithubException}
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    return ns["_merge_refusal"]


def main():
    refusal = _load_refusal()
    fails = []

    def check(label, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    print("Running merge-refusal classification self-test...")

    # ── the three filed issues, verbatim ───────────────────────────────────
    rule = refusal(GithubException(405, {"message": "Repository rule violations found"}))
    check("ab#170/#192 rule violations classified", rule is not None and rule[0] == 409)
    check("rule-violation message is preserved with its original casing",
          rule is not None and "Repository rule violations found" in rule[1])

    wf = refusal(GithubException(403, {
        "message": "refusing to allow a Personal Access Token to create or update "
                   "workflow `.github/workflows/promote.yml` without `workflow` scope"}))
    check("ab#107 missing workflow scope classified", wf is not None and wf[0] == 403)
    check("workflow-scope message names the scope", wf is not None and "`workflow` scope" in wf[1])

    # ── the conflict path must stay untouched ──────────────────────────────
    # merge_pr's auto-resolve recovery only runs when _merge_refusal declines to
    # claim the exception. If this ever returns a tuple, conflict auto-resolution
    # silently stops happening and every stale branch needs a human instead.
    check("a merge CONFLICT is not claimed (auto-resolve must still run)",
          refusal(GithubException(405, {"message": "Pull Request has merge conflicts"})) is None)
    check("conflict detection is case-insensitive",
          refusal(GithubException(405, {"message": "Pull Request has Merge Conflicts"})) is None)

    # ── generic / non-refusals ─────────────────────────────────────────────
    other405 = refusal(GithubException(405, {"message": "Required status check is failing"}))
    check("another non-mergeable 405 is a refusal, not a crash",
          other405 is not None and other405[0] == 409)
    check("a 404 is NOT a refusal (genuine error, must still raise)",
          refusal(GithubException(404, {"message": "Not Found"})) is None)
    check("a 500 is NOT a refusal (genuine error, must still raise)",
          refusal(GithubException(500, {"message": "Server Error"})) is None)
    check("a 403 unrelated to workflow scope is NOT claimed",
          refusal(GithubException(403, {"message": "Resource not accessible"})) is None)
    check("a non-GithubException is NOT claimed",
          refusal(ValueError("boom")) is None)
    check("a non-dict .data does not raise",
          refusal(GithubException(405, "raw body text")) is not None)
    check("a None message does not raise",
          refusal(GithubException(405, {"message": None})) is not None)

    # ── merge_pr returns instead of raising ────────────────────────────────
    src = open(SRC).read()
    tree = ast.parse(src)
    mp = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "merge_pr")
    body = ast.get_source_segment(src, mp)
    # NB: match the bare `raise` STATEMENT, not the substring -- merge_pr's
    # docstring contains the word "raises".
    bare_raise = [i for i, ln in enumerate(body.split("\n")) if ln.strip() == "raise"]
    refusal_line = [i for i, ln in enumerate(body.split("\n")) if "_merge_refusal(e)" in ln]
    check("merge_pr consults _merge_refusal before re-raising",
          len(bare_raise) == 1 and len(refusal_line) == 1 and refusal_line[0] < bare_raise[0])
    check("merge_pr returns retryable=False for a refusal", '"retryable": False' in body)
    check("a refusal is logged at WARNING, not ERROR",
          "logger.warning" in body and "merge refused by GitHub" in body)
    check("merge_pr clears merge_blocked_reason on a successful merge",
          "merged=True, merge_blocked_reason=None" in body)

    # ── the cross-poll suppression (ab#192) ────────────────────────────────
    psrc = open("pr_review.py").read()
    ptree = ast.parse(psrc)
    am = next(n for n in ast.walk(ptree)
              if isinstance(n, ast.FunctionDef) and n.name == "_maybe_auto_merge")
    ambody = ast.get_source_segment(psrc, am)
    check("auto-merge branches on retryable is False", 'result.get("retryable") is False' in ambody)
    check("the refusal is compared against a prior value before logging",
          "prior_refusal" in ambody and "why != prior_refusal" in ambody)
    # The load-bearing assertion. auto_merge_blocked_reason is set to None on the
    # line before merge_pr is called, so reading it back afterwards ALWAYS yields
    # None and a dedupe keyed on it can never suppress anything.
    check("prior_refusal is read BEFORE the update that clears blocked_reason",
          ambody.index("prior_refusal = rec.get") < ambody.index("auto_merge_blocked_reason=None"))
    check("suppression uses its own field, not auto_merge_blocked_reason",
          'rec.get("auto_merge_refusal")' in ambody)
    check("a successful auto-merge clears the refusal", "auto_merge_refusal=None" in ambody)

    print(f"RESULT: {'PASS' if not fails else 'FAIL — ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
