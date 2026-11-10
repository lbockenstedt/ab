"""Pins the 410-Gone handling on the Reopen path.

WHY: ab#197 records AppBuilder "repeatedly" logging

    Reopen failed for lbockenstedt/lm:211: This issue was deleted: 410
        {"message": "This issue was deleted", ..., "status": "410"}

Two separate problems, and the repetition is the interesting one.

/reopen_issue has exactly one caller -- the Reopen button in index.html -- so
the repeats were an operator clicking it again and again. They did that because
the failure path only raised an error toast: it never reloaded, so the dead row
stayed on screen looking reopenable, and AppBuilder's stored record still
counted it. Clicking again was the reasonable thing to do.

And 410 is not a transient failure. Unlike a 404, which can be a permissions
artefact or a lookup race, GitHub returns 410 Gone for a *deleted* issue
permanently; no retry can ever succeed. Logging it at ERROR meant AppBuilder's
own log scanner harvested it and filed it as a bug against AppBuilder, which
AppBuilder cannot fix, because nothing is wrong with AppBuilder.

The classifier is duck-typed deliberately: routes.py imports Github but NOT
GithubException, so an isinstance check would be a NameError on the very path
that is supposed to stop an error from escaping.
"""
import ast
import re
import sys

SRC = "routes.py"


def _load():
    tree = ast.parse(open(SRC).read())
    node = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "_issue_is_deleted"), None)
    assert node is not None, f"_issue_is_deleted missing from {SRC}"
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    return ns["_issue_is_deleted"]


class _Exc(Exception):
    def __init__(self, status, data):
        super().__init__(status, data)
        self.status = status
        self.data = data


def main():
    deleted = _load()
    fails = []

    def check(label, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    print("Running reopen-deleted-issue self-test...")

    # ── classification ─────────────────────────────────────────────────────
    check("ab#197's exact payload is recognised",
          deleted(_Exc(410, {"message": "This issue was deleted",
                             "documentation_url": "https://docs.github.com/rest/issues/issues"
                                                  "#get-an-issue",
                             "status": "410"})) is True)
    check("410 with no body is still recognised", deleted(_Exc(410, None)) is True)
    check("the message alone is enough when no numeric status is surfaced",
          deleted(_Exc(None, {"message": "This issue was deleted"})) is True)
    check("message matching is case-insensitive",
          deleted(_Exc(403, "Issue Was Deleted")) is True)

    # 404 must NOT be treated as deleted: it can be a transient lookup or a
    # token-permissions artefact, and dropping the record on one would lose a
    # live issue.
    check("a 404 is NOT treated as deleted", deleted(_Exc(404, {"message": "Not Found"})) is False)
    check("a 500 is NOT treated as deleted", deleted(_Exc(500, {"message": "Server Error"})) is False)
    check("a plain exception is not deleted", deleted(ValueError("boom")) is False)
    check("None does not raise", deleted(None) is False)
    check("a list .data does not raise", deleted(_Exc(200, [1, 2])) is False)
    check("an int .data does not raise", deleted(_Exc(200, 5)) is False)
    check("a None message does not raise", deleted(_Exc(None, {"message": None})) is False)

    # ── the route ──────────────────────────────────────────────────────────
    src = open(SRC).read()
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "reopen_issue")
    body = ast.get_source_segment(src, node)

    check("the reopen handler consults _issue_is_deleted", "_issue_is_deleted(e)" in body)
    check("a deleted issue is logged at WARNING, not ERROR",
          "logger.warning(f\"Reopen skipped" in body)
    check("the generic ERROR log is still there for real failures",
          "logger.error(f\"Reopen failed for" in body)
    check("the deleted check runs BEFORE the generic error log",
          body.index("_issue_is_deleted(e)") < body.index('logger.error(f"Reopen failed for'))
    check("the local record is dropped so the row cannot be clicked again",
          "processed.pop(issue_id, None)" in body)
    check("counters are recomputed after dropping the record",
          "recompute_issue_counters(processed)" in body)
    check("dropping the record cannot itself break the response",
          "could not drop record for deleted" in body)
    check("the response is 410, not 500", "status_code=410" in body)
    check("the response is flagged so the UI can distinguish it",
          '"deleted": True' in body)

    # ── the UI ─────────────────────────────────────────────────────────────
    html = open("templates/index.html").read()
    fn = re.search(r"async function reopenIssue\(issueId\)\s*\{.*?\n        \}",
                   html, re.DOTALL)
    check("reopenIssue() was found in the template", fn is not None)
    if fn:
        js = fn.group()
        check("the UI reloads on a deleted issue instead of only toasting",
              "data.deleted" in js and js.index("data.deleted") < js.index("showToast"))
        check("the success path still reloads", "reloadWithToast" in js)

    print(f"RESULT: {'PASS' if not fails else 'FAIL — ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
