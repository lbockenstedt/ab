"""backmerge.yml — main is carried back down to qa and dev.

The promotion flow is one-way (dev -> qa -> main), so anything reaching main
outside it leaves the lower branches permanently behind: a direct commit by the
owner, and — routinely — the merge commit a promotion PR creates on main. This
pins the workflow that closes that gap.

The important tests here EXECUTE `.github/scripts/promote.sh` against real
throwaway git repos. A previous change to these workflows shipped a bash SYNTAX
error that `yaml.safe_load` accepted happily and that would have broken every
promotion at runtime, so parsing the YAML is explicitly not considered
sufficient evidence that this works.
"""

import os
import subprocess
import textwrap

import pytest
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
WF = os.path.join(ROOT, ".github", "workflows", "backmerge.yml")
SCRIPT = os.path.join(ROOT, ".github", "scripts", "promote.sh")


def _wf():
    with open(WF) as fh:
        return yaml.safe_load(fh)


def _jobs():
    return _wf()["jobs"]["backmerge"]


# --------------------------------------------------------------------------
# Trigger and direction
# --------------------------------------------------------------------------
def test_triggered_by_a_push_to_main_or_qa():
    """qa is a source too, otherwise a fix committed onto `promote/dev-to-qa`
    while that promotion was under review lands on qa and NOTHING carries it
    back to dev — dev then re-promotes the unfixed code every cycle."""
    # yaml parses a bare `on:` key as the boolean True.
    on = _wf().get("on", _wf().get(True))
    assert list(on) == ["push"]
    assert sorted(on["push"]["branches"]) == ["main", "qa"]


def _edges():
    return {(p["source"], p["target"]) for p in _jobs()["strategy"]["matrix"]["include"]}


def test_back_merge_edges_are_exactly_the_downward_ones():
    assert _edges() == {("main", "qa"), ("main", "dev"), ("qa", "dev")}


def test_every_edge_points_downward_so_the_graph_cannot_cycle():
    """THE loop guard. The flow is dev -> qa -> main; a back-merge must always
    move down that order. An upward edge here would feed a branch that is
    itself a trigger source and the workflow would re-trigger itself forever."""
    rank = {"dev": 0, "qa": 1, "main": 2}
    for src, tgt in _edges():
        assert rank[src] > rank[tgt], f"{src} -> {tgt} is not a downward back-merge"


def test_dev_is_never_a_source_so_the_chain_terminates():
    """dev is the terminal node: main -> qa -> dev is the longest possible
    chain. Making dev a source would close the cycle."""
    assert not any(src == "dev" for src, _ in _edges())


def test_every_trigger_branch_is_also_a_source():
    """A branch that triggers the workflow but is not a source would spawn
    three no-op jobs on every push and carry nothing."""
    on = _wf().get("on", _wf().get(True))
    assert sorted({src for src, _ in _edges()}) == sorted(on["push"]["branches"])


def test_pairs_are_filtered_by_a_step_not_a_job_level_if():
    """The `matrix` context is NOT available in a job-level `if` (GitHub
    evaluates it before the matrix expands), so filtering there would silently
    drop every pair. The filter has to be the first step."""
    assert "if" not in _jobs(), "a job-level `if` cannot see matrix.source"
    first = next(s for s in _jobs()["steps"] if "run" in s)
    assert "github.ref_name" in first["run"], \
        "the first step does not filter the matrix down to the pushed branch"


def test_never_writes_to_main():
    """The loop guard. The only trigger is a push to main, so anything here
    that pushed to main would re-trigger this workflow forever."""
    text = open(WF).read()
    for bad in ("origin main", "push -f -q origin main", "--base main", "--base \"main\""):
        assert bad not in text, f"backmerge.yml appears to write to main ({bad!r})"


def test_opens_a_pr_and_merges_it_without_bypassing_anything():
    """The gap only closes when the PR lands, so this workflow does merge --
    but it must never do so by overriding the checks. `--admin` bypasses
    branch protection outright, and `--squash`/`--rebase` would rewrite main's
    commits onto the target and destroy the shared history that makes the next
    promotion diff readable."""
    text = open(WF).read()
    assert "gh pr create" in text
    assert "gh pr merge" in text, "the back-merge PR is never merged"
    for bad in ("--admin", "--squash", "--rebase"):
        assert bad not in text, f"backmerge.yml must not merge with {bad!r}"


def test_merge_is_gated_on_checks_rather_than_unconditional():
    """qa is check-gated so auto-merge covers it, but dev has no required
    checks -- GitHub then refuses to arm auto-merge and the PR is mergeable
    the instant it opens. Merging straight away would land it while its own
    CI was still running, so the fallback path must wait and must bail out on
    a failure."""
    text = open(WF).read()
    assert "--auto" in text, "native auto-merge is not used"
    assert "statusCheckRollup" in text, "the fallback merge does not consult the checks"
    for state in ("IN_PROGRESS", "QUEUED", "FAILURE"):
        assert state in text, f"the fallback merge ignores {state} checks"


def test_merge_step_runs_after_the_parked_run_is_released():
    """A PR opened with GITHUB_TOKEN has its checks parked as action_required.
    Arming auto-merge before releasing them would leave the PR waiting on a
    check that never starts."""
    names = [s.get("name", "") for s in _jobs()["steps"]]
    release = next(i for i, n in enumerate(names) if "Release" in n)
    merge = next(i for i, n in enumerate(names) if n.startswith("Merge"))
    assert merge > release, "the merge step must come after the CI release step"


def test_concurrency_is_job_level_so_the_matrix_is_visible():
    """Workflow-level `concurrency` is evaluated before the matrix expands, so
    `matrix.target` would be empty there and the qa run would cancel the dev
    run. It must be declared on the job."""
    assert "concurrency" not in _wf(), "concurrency belongs on the job, not the workflow"
    group = _jobs()["concurrency"]["group"]
    assert "matrix.target" in group


def test_one_target_failing_still_offers_the_other():
    assert _jobs()["strategy"]["fail-fast"] is False


def test_parked_ci_run_is_released():
    """qa requires check-direction; a bot-authored PR's checks are parked as
    action_required, so without this the back-merge PR is blocked forever."""
    text = open(WF).read()
    assert "action_required" in text and "/approve" in text


# --------------------------------------------------------------------------
# The shared script actually runs (executed, not read)
# --------------------------------------------------------------------------
def _git(cwd, *args):
    return subprocess.run(("git",) + args, cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _repo(tmp_path):
    """A throwaway repo with main/qa/dev and a VERSION file, plus an `origin`
    remote pointing at itself so `origin/<branch>` refs resolve."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "VERSION").write_text("1.00\n")
    (r / "app.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    for b in ("qa", "dev"):
        _git(r, "branch", b)
    _git(r, "remote", "add", "origin", str(r))
    _git(r, "fetch", "-q", "origin")
    return r


def _run_script(repo, src, tgt, label, out_file):
    env = dict(os.environ, SRC=src, TGT=tgt, BR=f"{label}/{src}-to-{tgt}",
               LABEL=label, GITHUB_OUTPUT=str(out_file))
    return subprocess.run(["bash", SCRIPT], cwd=repo, env=env,
                          capture_output=True, text=True)


def test_backmerge_script_carries_a_direct_main_commit_down_to_qa(tmp_path):
    """The end-to-end behaviour the user asked for: commit straight to main,
    and the change becomes available to qa."""
    r = _repo(tmp_path)
    (r / "hotfix.py").write_text("urgent = True\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "direct commit to main")
    _git(r, "fetch", "-q", "origin")

    out = tmp_path / "out"
    res = _run_script(r, "main", "qa", "backmerge", out)
    assert res.returncode == 0, res.stderr
    assert "changed=true" in out.read_text()

    listing = _git(r, "ls-tree", "-r", "--name-only", "HEAD")
    assert "hotfix.py" in listing, "the direct commit did not reach the qa branch"
    assert "backmerge: main -> qa" in _git(r, "log", "-1", "--pretty=%s")


def test_backmerge_keeps_the_targets_own_version(tmp_path):
    """main's VERSION must not leak backwards -- qa stays on its own lineage."""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "qa")
    (r / "VERSION").write_text("1.45\n")
    _git(r, "commit", "-q", "-am", "qa version")
    _git(r, "checkout", "-q", "main")
    (r / "VERSION").write_text("9.99\n")
    (r / "hotfix.py").write_text("urgent = True\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "main version + hotfix")
    _git(r, "fetch", "-q", "origin")

    res = _run_script(r, "main", "qa", "backmerge", tmp_path / "out")
    assert res.returncode == 0, res.stderr
    version = (r / "VERSION").read_text().strip()
    assert not version.startswith("9."), f"main's VERSION leaked into qa ({version})"


def test_backmerge_is_a_noop_when_the_target_is_already_current(tmp_path):
    """Every promotion merge pushes main, so this runs constantly. It must not
    open an empty PR each time."""
    r = _repo(tmp_path)
    out = tmp_path / "out"
    res = _run_script(r, "main", "qa", "backmerge", out)
    assert res.returncode == 0, res.stderr
    assert "changed=false" in out.read_text()
    assert "Nothing to backmerge" in res.stdout


def test_backmerge_refuses_to_auto_resolve_a_real_conflict(tmp_path):
    """A conflicting back-merge must surface for a human, not be force-resolved
    by a bot pushing at qa."""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "qa")
    (r / "app.py").write_text("x = 'qa'\n")
    _git(r, "commit", "-q", "-am", "qa edit")
    _git(r, "checkout", "-q", "main")
    (r / "app.py").write_text("x = 'main'\n")
    _git(r, "commit", "-q", "-am", "main edit")
    _git(r, "fetch", "-q", "origin")

    res = _run_script(r, "main", "qa", "backmerge", tmp_path / "out")
    assert res.returncode != 0
    assert "merge conflict outside VERSION" in res.stdout + res.stderr


def test_a_fix_made_during_a_qa_promotion_is_back_ported_to_dev(tmp_path):
    """THE case this edge exists for, end to end.

    dev is promoted to qa; the promotion PR is reviewed and a defect is found,
    so the repair is committed onto the promotion branch and lands on qa. That
    repair now exists ONLY on qa. Without qa -> dev, dev still carries the
    unfixed code and re-promotes it next cycle — the same defect is found
    again, forever.
    """
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "qa")
    (r / "app.py").write_text("x = 1\nfixed_during_promotion = True\n")
    _git(r, "commit", "-q", "-am", "fix found while reviewing promote/dev-to-qa")
    _git(r, "checkout", "-q", "dev")
    _git(r, "fetch", "-q", "origin")

    out = tmp_path / "out"
    res = _run_script(r, "qa", "dev", "backmerge", out)
    assert res.returncode == 0, res.stderr
    assert "changed=true" in out.read_text()
    assert "fixed_during_promotion" in (r / "app.py").read_text(), \
        "the fix made on qa never reached dev"
    assert "backmerge: qa -> dev" in _git(r, "log", "-1", "--pretty=%s")


def test_qa_to_dev_keeps_devs_own_version(tmp_path):
    """qa's VERSION must not leak backwards any more than main's does."""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "dev")
    (r / "VERSION").write_text("10.00\n")
    _git(r, "commit", "-q", "-am", "dev version")
    _git(r, "checkout", "-q", "qa")
    (r / "VERSION").write_text("1.45\n")
    (r / "app.py").write_text("x = 'fixed on qa'\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "qa version + fix")
    _git(r, "fetch", "-q", "origin")

    res = _run_script(r, "qa", "dev", "backmerge", tmp_path / "out")
    assert res.returncode == 0, res.stderr
    version = (r / "VERSION").read_text().strip()
    assert version.startswith("10."), f"qa's VERSION leaked into dev ({version})"


def test_qa_to_dev_is_a_noop_when_dev_is_already_current(tmp_path):
    """Every dev -> qa promotion pushes qa, so this edge runs constantly and
    usually has nothing to carry. It must not open an empty PR each time."""
    r = _repo(tmp_path)
    out = tmp_path / "out"
    res = _run_script(r, "qa", "dev", "backmerge", out)
    assert res.returncode == 0, res.stderr
    assert "changed=false" in out.read_text()
    assert "Nothing to backmerge" in res.stdout


# --------------------------------------------------------------------------
# The matrix filter step, actually executed
# --------------------------------------------------------------------------
def _run_filter_step(tmp_path, source, target, pushed, make_target=True):
    """Run the first step's real shell with the ${{ }} expressions substituted,
    the same way GitHub would before handing it to bash."""
    step = next(s for s in _jobs()["steps"] if "run" in s)
    script = (step["run"]
              .replace("${{ matrix.source }}", source)
              .replace("${{ matrix.target }}", target)
              .replace("${{ github.ref_name }}", pushed))
    r = _repo(tmp_path)
    if not make_target:
        _git(r, "branch", "-D", target)
        _git(r, "fetch", "-q", "--prune", "origin")
        subprocess.run(["git", "update-ref", "-d", f"refs/remotes/origin/{target}"],
                       cwd=r, check=True)
    out = tmp_path / "gh_out"
    out.write_text("")
    res = subprocess.run(["bash", "-c", script], cwd=r,
                         env=dict(os.environ, GITHUB_OUTPUT=str(out)),
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return out.read_text(), res.stdout


@pytest.mark.parametrize("source,target,pushed,expected", [
    ("main", "qa", "main", "ok=true"),
    ("main", "dev", "main", "ok=true"),
    ("qa", "dev", "qa", "ok=true"),
    # The pairs that must stand down so a push to qa does not re-run main's.
    ("main", "qa", "qa", "ok=false"),
    ("main", "dev", "qa", "ok=false"),
    ("qa", "dev", "main", "ok=false"),
])
def test_filter_step_runs_only_the_pairs_for_the_pushed_branch(
        tmp_path, source, target, pushed, expected):
    written, _ = _run_filter_step(tmp_path, source, target, pushed)
    assert written.strip() == expected


def test_filter_step_stands_down_when_the_target_branch_is_missing(tmp_path):
    """Not every fleet repo has all three branches."""
    written, stdout = _run_filter_step(tmp_path, "qa", "dev", "qa", make_target=False)
    assert written.strip() == "ok=false"
    assert "nothing to back-merge" in stdout


def test_promote_direction_still_works_after_the_label_change(tmp_path):
    """promote.sh is shared with promote.yml; LABEL defaults to 'promote' and
    the forward direction must be untouched."""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "dev")
    (r / "feature.py").write_text("f = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "dev feature")
    _git(r, "checkout", "-q", "main")
    _git(r, "fetch", "-q", "origin")

    env = dict(os.environ, SRC="dev", TGT="qa", BR="promote/dev-to-qa",
               GITHUB_OUTPUT=str(tmp_path / "out"))  # no LABEL -> default
    res = subprocess.run(["bash", SCRIPT], cwd=r, env=env,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "promote: dev -> qa" in _git(r, "log", "-1", "--pretty=%s")
    assert "feature.py" in _git(r, "ls-tree", "-r", "--name-only", "HEAD")


def test_noop_message_keeps_the_string_promotion_selftest_matches(tmp_path):
    """All 16 repos ship a promotion_selftest.sh that matches on the literal
    "Nothing to promote". Renaming this message to a label-independent phrase
    broke nw's CI on main, dev and a back-merge branch simultaneously. The
    forward direction must keep emitting that exact string."""
    r = _repo(tmp_path)
    env = dict(os.environ, SRC="dev", TGT="qa", BR="promote/dev-to-qa",
               GITHUB_OUTPUT=str(tmp_path / "out"))  # no LABEL -> 'promote'
    res = subprocess.run(["bash", SCRIPT], cwd=r, env=env,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "Nothing to promote" in res.stdout


# --------------------------------------------------------------------------
# The merge step's shell, actually executed
#
# A grep over the YAML proves nothing about behaviour -- an earlier change in
# this workflow shipped a bash syntax error that yaml.safe_load accepted
# happily. These run the real step against a stub `gh` and assert what it does.
# --------------------------------------------------------------------------
_GH_STUB = r'''#!/usr/bin/env bash
echo "$@" >> "$GH_LOG"
case "$*" in
  *"--auto"*)
      [ "$AUTO_OK" = "1" ] && exit 0
      echo "Pull request is in clean status" >&2; exit 1 ;;
  *"--json state"*)
      echo "${PR_STATE:-OPEN}" ;;
  *"--json statusCheckRollup"*)
      # Serve one rollup per call so a pending check can settle.
      n=$(cat "$TICK" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$TICK"
      eval "echo \"\${ROLLUP_$n:-\$ROLLUP_LAST}\"" ;;
  *"pr merge"*)
      [ "$MERGE_OK" = "1" ] && exit 0
      echo "conflict" >&2; exit 1 ;;
esac
exit 0
'''


def _merge_step_script():
    step = next(s for s in _jobs()["steps"] if s.get("name", "").startswith("Merge"))
    return step["run"]


def _run_merge_step(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(_GH_STUB)
    (bin_dir / "gh").chmod(0o755)
    # Neutralise the poll delay so the wait loop is instant.
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "sleep").chmod(0o755)
    log = tmp_path / "gh.log"
    log.write_text("")
    e = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
             GH_LOG=str(log), TICK=str(tmp_path / "tick"),
             NUM="7", TGT="qa", GH_TOKEN="t",
             AUTO_OK="0", MERGE_OK="1", ROLLUP_LAST="SUCCESS")
    e.update({k: str(v) for k, v in env.items()})
    res = subprocess.run(["bash", "-c", _merge_step_script()],
                         cwd=tmp_path, env=e, capture_output=True, text=True)
    return res, log.read_text()


def test_merge_step_stops_at_auto_merge_when_github_arms_it(tmp_path):
    """The qa path. Auto-merge is the whole mechanism -- the step must then do
    nothing else, and must not fall through to a direct merge."""
    res, log = _run_merge_step(tmp_path, AUTO_OK="1")
    assert res.returncode == 0, res.stderr
    assert "--auto" in log
    assert log.count("pr merge") == 1, f"merged again after arming auto-merge:\n{log}"


def test_merge_step_waits_for_running_checks_then_merges(tmp_path):
    """The dev path. The PR is mergeable the moment it opens, so without the
    wait this would land while its own CI was still running."""
    res, log = _run_merge_step(tmp_path, ROLLUP_1="IN_PROGRESS", ROLLUP_2="QUEUED",
                               ROLLUP_LAST="SUCCESS")
    assert res.returncode == 0, res.stderr
    assert log.count("statusCheckRollup") >= 3, "it did not wait for the checks"
    assert "pr merge 7 --merge --delete-branch" in log


def test_merge_step_refuses_to_merge_a_pr_with_failing_checks(tmp_path):
    res, log = _run_merge_step(tmp_path, ROLLUP_LAST="SUCCESS,FAILURE")
    assert res.returncode == 0, res.stderr
    assert "pr merge 7 --merge" not in log, "merged a PR whose checks failed"
    assert "failing checks" in res.stdout


def test_merge_step_does_not_touch_an_already_merged_pr(tmp_path):
    res, log = _run_merge_step(tmp_path, PR_STATE="MERGED")
    assert res.returncode == 0, res.stderr
    assert "pr merge 7 --merge --delete-branch" not in log


def test_merge_step_leaves_a_conflicted_pr_open_instead_of_failing_the_run(tmp_path):
    """A conflicting back-merge is a human's job. It must not fail the
    workflow either -- the other target still needs its own PR handled."""
    res, log = _run_merge_step(tmp_path, MERGE_OK="0")
    assert res.returncode == 0, res.stderr
    assert "stays open for a human" in res.stdout
