#!/usr/bin/env python3
"""Self-test for process_single_issue's exception-logging fix (#796).

Run:  python3 ab/test_process_issue_error_logging.py

process_single_issue is a ~600-line function with heavy dependencies
(GitHub API, LLM providers, sandboxed git clones) that can't be reasonably
extracted/mocked whole for a unit test. Instead this exercises the actual
transformation logic added to its except-block against a REAL
git.GitCommandError (reproduced against a genuine empty git repo, not a
hand-built fake), since that's the one part of the fix worth verifying
precisely: GitPython's error string format.

Regression guard: "Error in process_single_issue: Cmd('git') failed due to:
exit code(128)" was reported non-actionable — "please provide the git command
being executed... and the full error output". GitCommandError.__str__ DOES
contain exactly that (cmdline + stderr), but as MULTIPLE physical lines; the
self-log scanner captures single ERROR-level lines verbatim with no
surrounding context, so the useful part was there in the log file but
invisible to it. The fix flattens embedded newlines into the single logged
line and adds the traceback's originating frame (file:line) so a triager
doesn't have to guess which of the many git/GitHub calls in this large
function actually raised.
"""
import tempfile
import traceback

import git


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _format_like_the_fix(e, repo_name, issue_num):
    """Mirrors fix_engine.py's process_single_issue except-block exactly."""
    _tb_frames = traceback.extract_tb(e.__traceback__)
    _origin = (f" (at {_tb_frames[-1].filename.split('/')[-1]}:"
              f"{_tb_frames[-1].lineno} in {_tb_frames[-1].name})") if _tb_frames else ""
    _flat = str(e).replace("\r", "").replace("\n", " | ")
    return (f"Error in process_single_issue for {repo_name}#{issue_num}: "
           f"{type(e).__name__}: {_flat}{_origin}")


def main():
    ok = True

    with tempfile.TemporaryDirectory() as tmpd:
        git.Repo.init(tmpd)
        try:
            git.Repo(tmpd).git.diff("HEAD")  # empty repo -> real GitCommandError
            raise AssertionError("expected GitCommandError, git.diff succeeded unexpectedly")
        except git.GitCommandError as e:
            raw = str(e)
            ok &= _check("sanity: the real error IS multi-line before the fix",
                         "\n" in raw)

            line = _format_like_the_fix(e, "lbockenstedt/lm", 202)

            ok &= _check("flattened line has NO embedded newlines",
                         "\n" not in line and "\r" not in line)
            ok &= _check("flattened line contains the exception type",
                         "GitCommandError" in line)
            ok &= _check("flattened line contains the actual git command",
                         "git diff HEAD" in line)
            ok &= _check("flattened line contains the real stderr detail",
                         "ambiguous argument" in line)
            ok &= _check("flattened line contains the repo/issue identity",
                         "lbockenstedt/lm#202" in line)
            ok &= _check("flattened line contains a file:line origin marker",
                         ".py:" in line and " in " in line)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running process_single_issue error-logging self-test...")
    import sys
    sys.exit(main())
