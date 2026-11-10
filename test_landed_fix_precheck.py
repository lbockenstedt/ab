"""Selftest for the LANDED-FIX pre-check.

WHY: verify_already_resolved exists (test_already_resolved.py) but could only
run AFTER a run had already failed, and it judges from file CONTENT. Both limits
bit at once on lbockenstedt/lm #441, #453, #486, #487 and #488: every one was
already fixed on dev, yet each burned all three attempts, escalated through
three models and collected reviewer rejections for diffs the reviewers correctly
called no-ops. Its file-read budget is 24 000 chars, so for a 1.99 MB
WebUI/main.js it showed the model roughly the first 1% of the file -- never the
region holding the fix -- so it could not have succeeded even when run.

git answers the question outright: `git log --grep` finds the commit that
references the issue, in milliseconds, for free. This suite covers the pure,
deterministic parts of that pre-check:

  * _landed_fix_commits  -- finding them, and NOT matching #44 for #440
  * _added_lines/_diff_files -- reading the landed diff
  * _landed_fix_still_applied -- the regression guard: a commit exists but its
    change was reverted, so the bug IS back and must still be fixed
  * _should_precheck_landed_fix -- the config gate, including that it honours
    the verify_already_resolved off switch it depends on

The git-backed cases build a real throwaway repo, so they exercise the actual
`git log`/`git show` invocations rather than a mock of them.
"""
import ast
import os
import re
import subprocess
import tempfile

_WANT_FUNCS = {"_landed_fix_commits", "_added_lines", "_diff_files",
               "_landed_fix_still_applied", "_should_precheck_landed_fix",
               "_landed_fix_context", "_safe_repo_target"}
_WANT_ASSIGNS = {"_LANDED_FIX_MAX_COMMITS", "_LANDED_FIX_DIFF_BUDGET"}


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _load():
    tree = ast.parse(open("fix_engine.py").read())
    ns = {"re": re, "os": os, "logger": _Logger()}
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


class _Repo:
    """The slice of GitPython's Repo that the pre-check actually uses."""

    def __init__(self, path):
        self.working_dir = path
        self.git = self

    def _run(self, *args):
        return subprocess.run(["git"] + list(args), cwd=self.working_dir,
                              capture_output=True, text=True, check=False).stdout

    def log(self, *args):
        return self._run("log", *args)

    def show(self, *args):
        return self._run("show", *args)


def _mkrepo(tmp):
    def git(*a):
        subprocess.run(["git"] + list(a), cwd=tmp, check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    return git


def _check(label, cond):
    print("  %s  %s" % ("PASS" if cond else "FAIL", label))
    return bool(cond)


def main():
    ns = _load()
    ok = True

    # ── _added_lines / _diff_files ──────────────────────────────────────────
    diff = ("--- a/WebUI/main.js\n+++ b/WebUI/main.js\n"
            "@@ -1 +1,3 @@\n"
            "+    if (modal.id) document.getElementById(modal.id)?.remove();\n"
            "+}\n"                       # too short -- noise that matches anywhere
            "+\n"
            "-    removed_line_should_be_ignored()\n")
    added = ns["_added_lines"](diff)
    ok &= _check("added lines exclude the +++ header, blanks and stub braces",
                 added == ["if (modal.id) document.getElementById(modal.id)?.remove();"])
    ok &= _check("diff file paths are read from the +++ b/ headers",
                 ns["_diff_files"](diff) == ["WebUI/main.js"])

    # ── the config gate ─────────────────────────────────────────────────────
    gate = ns["_should_precheck_landed_fix"]
    ok &= _check("on by default", gate({}) is True)
    ok &= _check("explicit opt-out is honoured",
                 gate({"precheck_landed_fix": False}) is False)
    ok &= _check("disabling verify_already_resolved also disables the pre-check "
                 "that depends on it",
                 gate({"verify_already_resolved": False}) is False)

    # ── git-backed: finding the landed commit ───────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        git = _mkrepo(tmp)
        path = os.path.join(tmp, "app.js")
        with open(path, "w") as fh:
            fh.write("function stale() { return 1; }\n")
        git("add", "-A")
        git("commit", "-qm", "initial import")
        with open(path, "w") as fh:
            fh.write("function stale() { return 1; }\n"
                     "const FIXED_SENTINEL_VALUE = computeTheCorrectThing();\n")
        git("add", "-A")
        git("commit", "-qm", "fix(webui): let the vault edit dialog reveal values (#488)")
        repo = _Repo(tmp)

        found = ns["_landed_fix_commits"](repo, 488)
        ok &= _check("the commit referencing #488 is found",
                     len(found) == 1 and "#488" in found[0]["subject"])
        ok &= _check("its sha is captured", bool(found and len(found[0]["sha"]) == 40))

        # The number-boundary cases are the whole reason for the anchored regex.
        ok &= _check("#48 does NOT match the #488 commit",
                     ns["_landed_fix_commits"](repo, 48) == [])
        ok &= _check("#4880 does NOT match the #488 commit",
                     ns["_landed_fix_commits"](repo, 4880) == [])
        ok &= _check("an unreferenced issue finds nothing",
                     ns["_landed_fix_commits"](repo, 999) == [])

        applied, ratio = ns["_landed_fix_still_applied"](repo, found)
        ok &= _check("the landed change is still in the tree -> already fixed",
                     applied is True and ratio == 1.0)

        ctx = ns["_landed_fix_context"](repo, found)
        ok &= _check("the evidence blob names the commit and carries the diff",
                     "#488" in ctx and "FIXED_SENTINEL_VALUE" in ctx)

    # ── git-backed: the REGRESSION guard ────────────────────────────────────
    # A commit referencing the issue exists, but its change was later reverted.
    # The bug is genuinely back, so this must NOT read as already-fixed.
    with tempfile.TemporaryDirectory() as tmp:
        git = _mkrepo(tmp)
        path = os.path.join(tmp, "app.js")
        with open(path, "w") as fh:
            fh.write("base\n")
        git("add", "-A")
        git("commit", "-qm", "initial import")
        with open(path, "w") as fh:
            fh.write("base\nconst FIXED_SENTINEL_VALUE = computeTheCorrectThing();\n")
        git("add", "-A")
        git("commit", "-qm", "AI Fix #441: refresh the page on tenant switch")
        with open(path, "w") as fh:
            fh.write("base\n")          # reverted
        git("add", "-A")
        git("commit", "-qm", "revert that change for unrelated reasons")
        repo = _Repo(tmp)

        found = ns["_landed_fix_commits"](repo, 441)
        ok &= _check("the commit is still found in history after a revert",
                     len(found) == 1)
        applied, ratio = ns["_landed_fix_still_applied"](repo, found)
        ok &= _check("a REVERTED fix does not count as already-fixed "
                     "(the regression still gets fixed)",
                     applied is False and ratio == 0.0)

    # ── git-backed: a partially-surviving change ────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        git = _mkrepo(tmp)
        path = os.path.join(tmp, "app.js")
        with open(path, "w") as fh:
            fh.write("base\n")
        git("add", "-A")
        git("commit", "-qm", "initial import")
        with open(path, "w") as fh:
            fh.write("base\n"
                     "const FIRST_SENTINEL_LINE = alpha_value_here();\n"
                     "const SECOND_SENTINEL_LINE = beta_value_here();\n"
                     "const THIRD_SENTINEL_LINE = gamma_value_here();\n")
        git("add", "-A")
        git("commit", "-qm", "AI Fix #452: handle the stale lock")
        with open(path, "w") as fh:
            fh.write("base\n"
                     "const FIRST_SENTINEL_LINE = alpha_value_here();\n"
                     "const SECOND_SENTINEL_LINE = beta_value_here();\n")
        git("add", "-A")
        git("commit", "-qm", "drop one line")
        repo = _Repo(tmp)
        found = ns["_landed_fix_commits"](repo, 452)
        applied, ratio = ns["_landed_fix_still_applied"](repo, found)
        ok &= _check("2 of 3 added lines surviving clears the 60%% bar",
                     applied is True and 0.6 <= ratio < 1.0)
        applied_strict, _ = ns["_landed_fix_still_applied"](repo, found, min_ratio=0.9)
        ok &= _check("the same tree fails a stricter ratio (the knob works)",
                     applied_strict is False)

    # ── git-backed: checks ALL referencing commits, not just the newest ─────
    # A newer commit mentions the issue (e.g. a reopen/triage note) but does
    # not carry the fix; the actual fix landed in an OLDER commit that is
    # still fully applied. Only checking commits[0] would wrongly conclude
    # "not applied" and skip the already-fixed short-circuit.
    with tempfile.TemporaryDirectory() as tmp:
        git = _mkrepo(tmp)
        path = os.path.join(tmp, "app.js")
        with open(path, "w") as fh:
            fh.write("base\n")
        git("add", "-A")
        git("commit", "-qm", "initial import")
        with open(path, "w") as fh:
            fh.write("base\nconst OLDER_FIX_SENTINEL = computeTheCorrectThing();\n")
        git("add", "-A")
        git("commit", "-qm", "AI Fix #470: handle the stale lock")
        other_path = os.path.join(tmp, "notes.md")
        with open(other_path, "w") as fh:
            fh.write("unrelated triage note about #470\n")
        git("add", "-A")
        git("commit", "-qm", "chore: triage notes for #470")
        repo = _Repo(tmp)

        found = ns["_landed_fix_commits"](repo, 470)
        ok &= _check("both commits referencing #470 are found", len(found) == 2)
        applied, ratio = ns["_landed_fix_still_applied"](repo, found)
        ok &= _check("the older commit's still-applied fix is found even "
                     "though it is not commits[0]",
                     applied is True and ratio == 1.0)

    # ── robustness: the pre-check must never break a real fix run ───────────
    class _Broken:
        working_dir = "/nonexistent"

        def __init__(self):
            self.git = self

        def log(self, *a):
            raise RuntimeError("git exploded")

        def show(self, *a):
            raise RuntimeError("git exploded")

    ok &= _check("a git failure degrades to 'no landed fix', it does not raise",
                 ns["_landed_fix_commits"](_Broken(), 1) == [])
    ok &= _check("a diff-read failure degrades to not-applied",
                 ns["_landed_fix_still_applied"](
                     _Broken(), [{"sha": "deadbeef", "subject": "x"}]) == (False, 0.0))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


def test_landed_fix_precheck():
    assert main() == 0


if __name__ == "__main__":
    print("Running landed-fix pre-check selftest...")
    raise SystemExit(main())
