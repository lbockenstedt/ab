"""The promote/back-merge push must survive a workflow-file change.

GitHub forbids the Actions GITHUB_TOKEN from creating or updating ANY file
under `.github/workflows/`, and no `permissions:` block can grant it - it is a
platform rule, not a configurable scope. Observed fleet-wide on 2026-09-26:

    ! [remote rejected] promote/dev-to-qa -> promote/dev-to-qa
      (refusing to allow a GitHub App to create or update workflow
       `.github/workflows/promote.yml` without `workflows` permission)

    ! [remote rejected] backmerge/main-to-dev -> backmerge/main-to-dev
      (refusing to allow a GitHub App to create or update workflow ...)

The promote failure stalled the promotion queue. The back-merge failure was
worse: back-merge is the mechanism that keeps dev and qa converged, so with it
broken the branches drifted until they genuinely conflicted, which then blocked
promotion for an unrelated reason ("merge conflict outside VERSION").

These tests pin both halves of the fix: the checkout uses a workflow-scoped
token when one is configured, and the push preflights the credential so the
failure is an instruction rather than an opaque git error.
"""

import os
import re
import subprocess
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PROMOTE = os.path.join(HERE, ".github", "workflows", "promote.yml")
BACKMERGE = os.path.join(HERE, ".github", "workflows", "backmerge.yml")
WORKFLOWS = (PROMOTE, BACKMERGE)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps(path):
    for job in _load(path)["jobs"].values():
        for step in job.get("steps", []):
            yield step


def _push_step(path):
    """The step that actually pushes.

    Matched on a leading "Push" rather than a substring: backmerge.yml also has
    a "Skip pairs that do not apply to this push..." step, and matching that one
    made these tests assert against the wrong shell.
    """
    for step in _steps(path):
        if (step.get("name") or "").strip().lower().startswith("push"):
            return step
    raise AssertionError("no push step found in %s" % path)


def test_checkout_prefers_a_workflow_scoped_token():
    """Without this the push is rejected the moment a unit touches a workflow."""
    for path in WORKFLOWS:
        found = False
        for step in _steps(path):
            if str(step.get("uses", "")).startswith("actions/checkout"):
                token = (step.get("with") or {}).get("token") or ""
                assert "WORKFLOW_TOKEN" in token, (path, token)
                # The fallback matters: repos without the secret must still run.
                assert "GITHUB_TOKEN" in token, (path, token)
                found = True
        assert found, "no checkout step in %s" % path


def test_push_step_preflights_the_credential():
    for path in WORKFLOWS:
        run = _push_step(path)["run"]
        assert ".github/workflows/" in run, path
        assert "HAS_WORKFLOW_TOKEN" in run, path
        assert "::error::" in run, path
        # The message must tell the operator what to actually do.
        assert "WORKFLOW_TOKEN" in run and "workflow" in run, path


def test_push_step_does_not_mask_a_failed_diff_as_no_workflow_files():
    """`git diff | grep` prints nothing when the DIFF failed, which would read
    as "no workflow files touched" and skip the check for the wrong reason."""
    for path in WORKFLOWS:
        run = _push_step(path)["run"]
        body = "\n".join(l for l in run.splitlines() if not l.strip().startswith("#"))
        assert "if wf=" in body, path
        assert "::warning::" in body, path


def _run_preflight(run_body, tmpdir, env):
    """Execute the push step's shell with the GitHub expressions stubbed out."""
    script = re.sub(r"\$\{\{[^}]*\}\}", "X", run_body)
    script = script.replace("git push -f -q origin", "echo PUSHED")
    path = os.path.join(tmpdir, "preflight.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("set -e\n" + script)
    full = dict(os.environ)
    full.update(env)
    return subprocess.run(["bash", path], capture_output=True, text=True,
                          cwd=tmpdir, env=full)


def _repo_with_workflow_change(tmpdir):
    """A real git repo whose branch changes a file under .github/workflows/."""
    def git(*a):
        subprocess.run(["git"] + list(a), cwd=tmpdir, check=True,
                       capture_output=True)
    git("init", "-q", "-b", "qa")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    os.makedirs(os.path.join(tmpdir, ".github", "workflows"))
    wf = os.path.join(tmpdir, ".github", "workflows", "promote.yml")
    with open(wf, "w") as fh:
        fh.write("name: promote\n")
    with open(os.path.join(tmpdir, "code.py"), "w") as fh:
        fh.write("x = 1\n")
    git("add", "-A"); git("commit", "-qm", "base")
    # `origin/qa` must resolve: the workflow diffs against origin/<target>.
    git("update-ref", "refs/remotes/origin/qa", "HEAD")
    git("checkout", "-q", "-b", "promote/dev-to-qa")
    with open(wf, "w") as fh:
        fh.write("name: promote\n# changed\n")
    git("add", "-A"); git("commit", "-qm", "touch the workflow")
    return "promote/dev-to-qa"


def test_preflight_blocks_and_explains_when_the_token_is_missing():
    with tempfile.TemporaryDirectory() as td:
        br = _repo_with_workflow_change(td)
        run = _push_step(PROMOTE)["run"]
        r = _run_preflight(run, td, {"TGT": "qa", "BR": br,
                                     "HAS_WORKFLOW_TOKEN": "false"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert "::error::" in r.stdout
        assert "WORKFLOW_TOKEN" in r.stdout
        assert ".github/workflows/promote.yml" in r.stdout
        assert "PUSHED" not in r.stdout


def test_preflight_allows_the_push_when_the_token_is_present():
    with tempfile.TemporaryDirectory() as td:
        br = _repo_with_workflow_change(td)
        run = _push_step(PROMOTE)["run"]
        r = _run_preflight(run, td, {"TGT": "qa", "BR": br,
                                     "HAS_WORKFLOW_TOKEN": "true"})
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PUSHED" in r.stdout


def test_preflight_is_transparent_when_no_workflow_file_is_touched():
    """The common case: an ordinary code promotion must be unaffected."""
    with tempfile.TemporaryDirectory() as td:
        _repo_with_workflow_change(td)
        subprocess.run(["git", "checkout", "-q", "-b", "code-only",
                        "refs/remotes/origin/qa"], cwd=td, check=True)
        with open(os.path.join(td, "code.py"), "w") as fh:
            fh.write("x = 2\n")
        subprocess.run(["git", "commit", "-aqm", "code"], cwd=td, check=True)
        run = _push_step(PROMOTE)["run"]
        r = _run_preflight(run, td, {"TGT": "qa", "BR": "code-only",
                                     "HAS_WORKFLOW_TOKEN": "false"})
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PUSHED" in r.stdout
        assert "::error::" not in r.stdout


def test_preflight_warns_rather_than_silently_skipping_on_a_bad_ref():
    with tempfile.TemporaryDirectory() as td:
        br = _repo_with_workflow_change(td)
        run = _push_step(PROMOTE)["run"]
        r = _run_preflight(run, td, {"TGT": "nosuchbranch", "BR": br,
                                     "HAS_WORKFLOW_TOKEN": "false"})
        assert "::warning::" in r.stdout, r.stdout + r.stderr


# --- Panel-requested hardening (nw#137 skeptical review) ---------------------
#
# The dissenting reviewer could not verify from the diff that steps.route emits
# a `tgt` output, and correctly identified the consequence: an empty TGT makes
# the preflight diff `origin/...$BR`, which FAILS, and a failed diff takes the
# warn-and-push branch -- silently disabling the credential preflight. route
# does emit tgt (promote.yml "Resolve source and target"), but "currently true"
# is not a guarantee, so the guard below turns that into a hard error.

def test_route_step_actually_emits_tgt():
    """The premise the preflight depends on, pinned in the file itself."""
    route = next(s for s in _steps(PROMOTE)
                 if s.get("id") == "route")
    assert 'echo "tgt=$tgt" >> "$GITHUB_OUTPUT"' in route["run"], (
        "steps.route no longer emits a 'tgt' output; the push preflight "
        "depends on it")


def test_push_step_refuses_an_empty_target():
    """An empty TGT must fail loudly, not degrade into warn-and-push."""
    for wf, label in ((PROMOTE, "promote"), (BACKMERGE, "backmerge")):
        with tempfile.TemporaryDirectory() as td:
            br = _repo_with_workflow_change(td)
            run = _push_step(wf)["run"]
            r = _run_preflight(run, td, {"TGT": "", "BR": br,
                                         "HAS_WORKFLOW_TOKEN": "false"})
            assert r.returncode == 1, f"{label}: {r.stdout}{r.stderr}"
            assert "::error::" in r.stdout, label
            assert "TGT" in r.stdout, label
            assert "PUSHED" not in r.stdout, (
                f"{label}: pushed with no target, so the preflight was "
                f"silently disabled")
            assert "::warning::" not in r.stdout, (
                f"{label}: degraded to warn-and-push instead of failing")


def test_empty_target_is_rejected_even_for_a_code_only_push():
    """The guard is about a broken route, so it must not depend on the diff."""
    with tempfile.TemporaryDirectory() as td:
        _repo_with_workflow_change(td)
        subprocess.run(["git", "checkout", "-q", "-b", "code-only",
                        "refs/remotes/origin/qa"], cwd=td, check=True)
        with open(os.path.join(td, "code.py"), "w") as fh:
            fh.write("x = 3\n")
        subprocess.run(["git", "commit", "-aqm", "code"], cwd=td, check=True)
        run = _push_step(PROMOTE)["run"]
        r = _run_preflight(run, td, {"TGT": "", "BR": "code-only",
                                     "HAS_WORKFLOW_TOKEN": "true"})
        assert r.returncode == 1, r.stdout + r.stderr
        assert "PUSHED" not in r.stdout


def test_credential_scope_and_trigger_behaviour_are_documented():
    """The reviewer's other two findings were 'not called out' -- call them out.

    A PAT persisted by actions/checkout widens the scope of every later step,
    and a PAT push (unlike a GITHUB_TOKEN push) starts downstream workflows.
    Both are accepted and bounded by the trigger filters; the requirement is
    that the file says so, next to the token.
    """
    for path, trigger in ((PROMOTE, "[qa]"), (BACKMERGE, "[main, qa]")):
        text = open(path, encoding="utf-8").read()
        head = text.split("token: ${{ secrets.WORKFLOW_TOKEN")[0]
        note = head[-1600:]
        low = note.lower()
        assert "persist" in low or "later step" in low, (
            f"{path}: credential-scope expansion is not called out at the "
            f"checkout step")
        assert "downstream" in low or "trigger" in low, (
            f"{path}: PAT push trigger behaviour is not called out at the "
            f"checkout step")


def test_push_targets_a_branch_that_triggers_nothing():
    """The claim above must hold: promote/* and backmerge/* start no run.

    This is what actually closes the recursion the reviewer worried about --
    not the token, the trigger filter.
    """
    for path in (PROMOTE, BACKMERGE):
        wf = yaml.safe_load(open(path, encoding="utf-8"))
        on = wf.get(True, wf.get("on"))
        branches = (on.get("push") or {}).get("branches") or []
        for b in branches:
            assert not b.startswith("promote/"), f"{path}: {b}"
            assert not b.startswith("backmerge/"), f"{path}: {b}"
            assert b in ("dev", "qa", "main"), (
                f"{path}: unexpected push trigger {b!r} -- a promote/* or "
                f"backmerge/* trigger would make PAT pushes recurse")
