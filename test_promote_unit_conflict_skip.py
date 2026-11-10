"""A promotion must not be frozen by a unit that conflicts only in isolation.

WHY THIS EXISTS
tsa's qa -> main promotion failed on every single run with

    CONFLICT (content): Merge conflict in .github/workflows/promote.yml
    ##[error]merge conflict outside VERSION -- resolve qa -> main by hand

while `git merge origin/qa` into main succeeded cleanly by hand. kvm and pxmx
showed the same shape.

The cause was the split-promotion unit selector. Units come from
`rev-list --reverse --first-parent $TGT..$SRC`, and stage_to builds "$TGT plus
everything UP TO <endpoint>" -- so the units are CUMULATIVE PREFIXES, each a
superset of the last. The selector nonetheless treated a conflict on the
oldest outstanding unit as fatal, reasoning that "promoting a later unit ahead
of it would reorder the branch". For cumulative prefixes that is not so:
advancing to units[i+1] batches unit i in with it. Nothing is reordered and
nothing is dropped.

The practical trap: once AppBuilder committed a repair onto a promotion branch,
$TGT gained a change the OLD units predate. The oldest outstanding unit then
conflicted against $TGT permanently -- so promotion stayed frozen even after a
back-merge had made the full $SRC -> $TGT merge clean, which is precisely the
state tsa/kvm/pxmx were left in.

These tests execute the real .github/scripts/promote.sh against real git
repositories, in the style of test_backmerge.py -- the behaviour only appears
when git actually performs the merges.
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "promote.sh")


def _git(cwd, *args):
    return subprocess.run(("git",) + args, cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "VERSION").write_text("1.00\n")
    (r / "workflow.yml").write_text("step: original\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "branch", "qa")
    _git(r, "remote", "add", "origin", str(r))
    _git(r, "fetch", "-q", "origin")
    return r


def _run(repo, src, tgt, out_file, split="1"):
    env = dict(os.environ, SRC=src, TGT=tgt, BR=f"promote/{src}-to-{tgt}",
               LABEL="promote", GITHUB_OUTPUT=str(out_file),
               PROMOTE_SPLIT=split)
    return subprocess.run(["bash", SCRIPT], cwd=repo, env=env,
                          capture_output=True, text=True)


def _tsa_shape(tmp_path):
    """Reproduce the exact live state of tsa/kvm/pxmx.

    1. qa gets a unit that edits workflow.yml   (an old, outstanding unit)
    2. main gets a CONFLICTING edit to workflow.yml -- this stands for
       "AppBuilder: fix PR #N review findings", committed onto a promotion
       branch and therefore landing on main and nowhere else
    3. a back-merge carries main into qa, so the FULL qa -> main merge is clean
    4. qa gets one more ordinary unit afterwards

    The oldest outstanding unit (step 1) predates main's repair and conflicts
    against it in isolation, even though qa as a whole now merges cleanly.
    """
    r = _repo(tmp_path)

    _git(r, "checkout", "-q", "qa")
    (r / "workflow.yml").write_text("step: qa-edit\n")
    _git(r, "commit", "-q", "-am", "unit 1: edit the workflow on qa")

    _git(r, "checkout", "-q", "main")
    (r / "workflow.yml").write_text("step: appbuilder-repair\n")
    _git(r, "commit", "-q", "-am", "AppBuilder: fix PR #15 review findings")

    # The back-merge that reconciles the drift: main -> qa, main's copy wins.
    _git(r, "checkout", "-q", "qa")
    subprocess.run(["git", "merge", "--no-commit", "--no-ff", "main"],
                   cwd=r, capture_output=True, text=True)
    (r / "workflow.yml").write_text("step: appbuilder-repair\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "ci: back-merge main into qa")

    (r / "feature.py").write_text("feature = True\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "unit 2: an ordinary later change")
    _git(r, "fetch", "-q", "origin")
    return r


def test_the_full_merge_really_is_clean(tmp_path):
    """Anchors the premise: the fixture is NOT a genuine divergence. If this
    ever fails, the rest of the file is testing the wrong thing."""
    r = _tsa_shape(tmp_path)
    _git(r, "checkout", "-q", "-B", "probe", "origin/main")
    res = subprocess.run(["git", "merge", "--no-commit", "--no-ff", "origin/qa"],
                         cwd=r, capture_output=True, text=True)
    assert res.returncode == 0, "fixture is a real conflict, not the tsa shape"


def test_promotion_survives_a_unit_that_conflicts_in_isolation(tmp_path):
    """THE regression. This exited 1 on every run before the fix."""
    r = _tsa_shape(tmp_path)
    out = tmp_path / "out"
    res = _run(r, "qa", "main", out)
    assert res.returncode == 0, (
        "promotion still frozen by an isolated unit conflict:\n%s%s"
        % (res.stdout, res.stderr))
    assert "changed=true" in out.read_text()


def test_the_skipped_unit_is_still_carried(tmp_path):
    """Skipping must BATCH, never DROP. Advancing to a later endpoint has to
    bring the skipped unit's content along, or the fix would silently lose
    commits -- far worse than the freeze it replaces."""
    r = _tsa_shape(tmp_path)
    out = tmp_path / "out"
    res = _run(r, "qa", "main", out)
    assert res.returncode == 0, res.stderr
    listing = _git(r, "ls-tree", "-r", "--name-only", "HEAD")
    assert "feature.py" in listing, "the later unit was not promoted"
    # main's repair is what qa's back-merge settled on, so it must survive.
    assert (r / "workflow.yml").read_text() == "step: appbuilder-repair\n"


def test_the_skip_is_reported_as_a_warning_not_an_error(tmp_path):
    """A recoverable skip annotated ::error:: marks a green run red, which is
    what sent us hunting for a promotion failure that had already resolved."""
    r = _tsa_shape(tmp_path)
    res = _run(r, "qa", "main", tmp_path / "out")
    assert "::warning::" in res.stdout
    assert "::error::" not in res.stdout


def test_a_genuine_divergence_still_fails_loudly(tmp_path):
    """The guard must not become a way to paper over a real conflict: when
    EVERY endpoint conflicts -- including the tip of $SRC -- promotion has to
    stop and say so."""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "qa")
    (r / "workflow.yml").write_text("step: qa-wins\n")
    _git(r, "commit", "-q", "-am", "qa edit")
    _git(r, "checkout", "-q", "main")
    (r / "workflow.yml").write_text("step: main-wins\n")
    _git(r, "commit", "-q", "-am", "main edit")
    _git(r, "fetch", "-q", "origin")

    res = _run(r, "qa", "main", tmp_path / "out")
    assert res.returncode == 1
    assert "resolve qa -> main by hand" in res.stdout


def test_a_real_conflict_is_not_reported_as_nothing_to_promote(tmp_path):
    """The failure mode the `conflicted` flag exists to prevent: falling
    through to the no-op branch would report success and silently promote
    nothing, for a divergence a human must reconcile."""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "qa")
    (r / "workflow.yml").write_text("step: qa-wins\n")
    _git(r, "commit", "-q", "-am", "qa edit")
    _git(r, "checkout", "-q", "main")
    (r / "workflow.yml").write_text("step: main-wins\n")
    _git(r, "commit", "-q", "-am", "main edit")
    _git(r, "fetch", "-q", "origin")

    res = _run(r, "qa", "main", tmp_path / "out")
    assert "Nothing to promote" not in res.stdout


def test_isolated_conflict_then_noop_tip_is_nothing_to_promote(tmp_path):
    """An early unit conflicts in isolation, but the tip of $SRC is a clean
    content no-op against $TGT -- there is no divergence, so this must be the
    quiet no-op, not a red ::error:: run."""
    r = _repo(tmp_path)

    _git(r, "checkout", "-q", "qa")
    (r / "workflow.yml").write_text("step: qa-edit\n")
    _git(r, "commit", "-q", "-am", "unit 1: edit the workflow on qa")

    _git(r, "checkout", "-q", "main")
    (r / "workflow.yml").write_text("step: appbuilder-repair\n")
    _git(r, "commit", "-q", "-am", "AppBuilder: fix PR #15 review findings")

    # Back-merge settles on main's copy, and NO later unit follows.
    _git(r, "checkout", "-q", "qa")
    subprocess.run(["git", "merge", "--no-commit", "--no-ff", "main"],
                   cwd=r, capture_output=True, text=True)
    (r / "workflow.yml").write_text("step: appbuilder-repair\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "ci: back-merge main into qa")
    _git(r, "fetch", "-q", "origin")

    res = _run(r, "qa", "main", tmp_path / "out")
    assert res.returncode == 0, (
        "a no-op tip after an isolated conflict was reported as a conflict:\n%s%s"
        % (res.stdout, res.stderr))
    assert "Nothing to promote" in res.stdout
    assert "::error::" not in res.stdout


def test_nothing_to_promote_is_unaffected(tmp_path):
    """No units at all must still be the quiet no-op, not a conflict."""
    r = _repo(tmp_path)
    res = _run(r, "qa", "main", tmp_path / "out")
    assert res.returncode == 0
    assert "Nothing to promote" in res.stdout


def test_batched_mode_is_unaffected(tmp_path):
    """PROMOTE_SPLIT=0 has a single endpoint (all of $SRC); the new skip path
    must not change its behaviour."""
    r = _tsa_shape(tmp_path)
    out = tmp_path / "out"
    res = _run(r, "qa", "main", out, split="0")
    assert res.returncode == 0, res.stderr
    assert "changed=true" in out.read_text()
