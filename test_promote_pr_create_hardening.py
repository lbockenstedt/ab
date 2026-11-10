"""The promotion PR-create step must survive a transient GraphQL failure.

WHY THIS EXISTS
ab's own dev -> qa promotion failed with

    pull request create failed: GraphQL: Something went wrong while executing
    your query on 2026-09-29T03:12:00Z. Please include
    `3C02:14AA9F:1DE640:27DC3F:6ABB2C7F` when reporting this issue.

(run 36516057861, step "Open or update the promotion PR"). That is a GitHub
-side flake, not a defect in the promotion logic -- but the step had no retry,
so the promotion branch was left PUSHED with no PR behind it. Nothing reopens
it: the next scheduled run is the only recovery, and until then the queue looks
empty while the work is actually stranded.

Three properties are pinned here, each of which was got wrong on the way to the
shipped version:

1. NO eval. `$title` is a git commit subject -- arbitrary text that can carry
   backticks, $(...) and semicolons. The first drafted fix built the gh call as
   a string and ran it through eval, which is a command-injection hole: a
   commit subject of `x $(touch /tmp/PWNED) y` executes. Arguments must be
   passed as separate words, so the subject stays inert data.

2. `gh pr create` is NOT retried. A create that succeeded server-side but
   returned a transport error would be performed twice by a blind retry,
   opening duplicate promotion PRs. The step instead re-queries for the PR and
   treats "error, but it exists" as success.

3. The create is tested with `if !`, never `$?`. The step runs under
   `bash -e` (GitHub's default `shell: /usr/bin/bash -e {0}`), where a bare
   failing command aborts the step immediately -- so a `cmd; if [ $? -ne 0 ]`
   shape can never reach its own error handling.

These are source-level assertions rather than an execution harness because the
step only exists inside the workflow YAML; the behaviour itself was verified by
stubbing `gh` against the extracted step.
"""
import os
import re
import subprocess
import tempfile

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_WF = os.path.join(_HERE, ".github", "workflows", "promote.yml")


def _pr_step():
    with open(_WF) as fh:
        doc = yaml.safe_load(fh)
    for step in doc["jobs"]["promote"]["steps"]:
        if "Open or update the promotion PR" in (step.get("name") or ""):
            return step["run"]
    raise AssertionError("the 'Open or update the promotion PR' step is gone from promote.yml")


def _code_lines():
    """The step's shell, minus comment and blank lines -- so a rule about what
    the CODE does is never satisfied merely by prose in a comment."""
    out = []
    for line in _pr_step().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(line)
    return out


def test_the_step_still_exists_and_is_valid_shell():
    body = _pr_step()
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(body)
        path = fh.name
    try:
        proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert proc.returncode == 0, "step is not valid bash:\n%s" % proc.stderr
    finally:
        os.unlink(path)


def test_no_eval_anywhere_in_the_step():
    """$title is an attacker-influencable commit subject; eval would execute it."""
    for line in _code_lines():
        assert not re.search(r"\beval\b", line), (
            "eval reintroduced into the PR-create step -- a commit subject "
            "containing $(...) would execute: %s" % line.strip())


def test_gh_calls_pass_arguments_as_separate_words():
    code = "\n".join(_code_lines())
    # The retry helper must invoke "$@", which is what keeps $title inert.
    assert '"$@"' in code, 'gh_retry must invoke "$@" directly, not a built command string'


def test_there_is_a_bounded_retry():
    code = "\n".join(_code_lines())
    assert "gh_retry" in code, "the retry helper is gone"
    # Bounded, not infinite: some attempt ceiling must be compared.
    assert re.search(r"-ge\s+\d+", code), "the retry loop has no attempt ceiling"
    assert "sleep" in code, "the retry loop does not back off at all"


def test_pr_create_is_not_blindly_retried():
    """A create that landed server-side but flaked in transit must not run twice."""
    code = "\n".join(_code_lines())
    assert not re.search(r"gh_retry\s+gh\s+pr\s+create", code), (
        "gh pr create is wrapped in the retry helper -- a create that succeeded "
        "server-side but returned a transport error would open a duplicate PR")


def test_pr_create_failure_is_tested_with_if_not_dollar_question():
    """The step runs under `bash -e`, where `$?` is never reached."""
    code = "\n".join(_code_lines())
    assert re.search(r"if\s+!\s+gh\s+pr\s+create", code), (
        "gh pr create's failure must be tested with `if !` -- under bash -e a "
        "bare failing command aborts the step before `$?` can be inspected")
    assert "$?" not in code, (
        "`$?` is unreliable under bash -e in this step; use `if !` instead")


def test_a_flaked_create_rechecks_before_failing():
    code = "\n".join(_code_lines())
    # find_existing must be called at least twice: once up front, once after a
    # failed create (the "error, but it landed" case).
    assert code.count("find_existing") >= 3, (
        "after a failed create the step must re-query for the PR -- a GraphQL "
        "error can be returned for a create that actually succeeded")


def test_command_substitutions_tolerate_failure_under_bash_e():
    """`existing=$(find_existing)` aborts the whole step under bash -e when the
    listing fails, which is exactly the transient case this step exists to
    survive."""
    for line in _code_lines():
        if "=$(find_existing" in line:
            assert "|| true" in line, (
                "under bash -e a failing command substitution aborts the step; "
                "this assignment must tolerate failure: %s" % line.strip())


def test_a_genuine_failure_is_still_a_loud_failure():
    """Retrying must not turn a real, persistent failure into a silent pass."""
    code = "\n".join(_code_lines())
    assert "::error::" in code, "a genuine create failure must still annotate the run"
    assert "exit 1" in code, "a genuine create failure must still fail the step"
