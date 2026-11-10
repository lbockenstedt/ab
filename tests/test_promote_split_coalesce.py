"""Split promotion must not promote a version of a file that a LATER unit
has already corrected.

THE DEADLOCK THIS PREVENTS
--------------------------
Promoting one unit of work at a time is what makes a promotion reviewable.
But the first time it met a self-correcting change it stalled the whole fleet:

  unit 1  shipped a defect
  unit 2  fixed that defect

Unit 2 cannot be promoted until unit 1 merges, and unit 1 cannot merge because
the review panel correctly rejects the defect -- the one unit 2 already fixed.
Every repo sat on the same rejected unit-1 PR and nothing moved.

The mistake is asking the panel to approve code that is known to be
superseded. So a picked unit is extended forward over any later unit touching
the same file(s), and they are carried together. Units with nothing in common
are still left behind, so promotions stay small.
"""

import os
import subprocess

from test_backmerge import SCRIPT, _git, _repo


def _split(repo, out_file, src="dev", tgt="qa"):
    env = dict(os.environ, SRC=src, TGT=tgt, BR=f"promote/{src}-to-{tgt}",
               LABEL="promote", PROMOTE_SPLIT="1", GITHUB_OUTPUT=str(out_file))
    return subprocess.run(["bash", SCRIPT], cwd=repo, env=env,
                          capture_output=True, text=True)


def _units(r):
    """dev gains three units: two touching shared.py, one touching app.py.

    app.py already exists in the base commit, so unit3 shares NO file with
    unit1 -- otherwise unit1 would legitimately pull unit3 in and the
    "left behind" assertions would be testing the wrong thing.
    """
    _git(r, "checkout", "-q", "dev")
    (r / "shared.py").write_text("value = 'defect'\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "unit1: add feature (#101)")
    (r / "shared.py").write_text("value = 'fixed'\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "unit2: fix the defect (#102)")
    (r / "app.py").write_text("x = 2\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "unit3: unrelated (#103)")
    _git(r, "fetch", "-q", "origin")


def test_a_later_unit_fixing_the_same_file_is_carried_with_it(tmp_path):
    """The deadlock itself: unit 1's defect must never reach the review panel
    on its own when unit 2 already fixed it."""
    r = _repo(tmp_path)
    _units(r)
    res = _split(r, tmp_path / "out")
    assert res.returncode == 0, res.stderr

    shared = _git(r, "show", "promote/dev-to-qa:shared.py")
    assert "fixed" in shared, \
        "promoted the superseded version of shared.py -- the panel will reject it"
    assert "defect" not in shared


def test_an_unrelated_unit_is_still_left_behind(tmp_path):
    """Coalescing must not silently turn split promotion back into a batch."""
    r = _repo(tmp_path)
    _units(r)
    res = _split(r, tmp_path / "out")
    assert res.returncode == 0, res.stderr

    assert "x = 1" in _git(r, "show", "promote/dev-to-qa:app.py"), \
        "unit3 touches no file in common and should not have been carried"
    assert "remaining=1" in (tmp_path / "out").read_text()


def test_the_extension_is_reported_so_the_promotion_is_explainable(tmp_path):
    r = _repo(tmp_path)
    _units(r)
    res = _split(r, tmp_path / "out")
    assert "extending unit" in res.stdout, res.stdout


def test_the_leftover_unit_promotes_on_the_next_run(tmp_path):
    """After the coalesced promotion merges, the untouched unit is next --
    proving nothing was dropped."""
    r = _repo(tmp_path)
    _units(r)
    assert _split(r, tmp_path / "out").returncode == 0

    _git(r, "checkout", "-q", "qa")
    _git(r, "merge", "-q", "--no-ff", "-m", "merge promotion", "promote/dev-to-qa")
    _git(r, "checkout", "-q", "dev")
    _git(r, "branch", "-q", "-D", "promote/dev-to-qa")
    _git(r, "fetch", "-q", "origin")

    out2 = tmp_path / "out2"
    assert _split(r, out2).returncode == 0
    assert "x = 2" in _git(r, "show", "promote/dev-to-qa:app.py")
    assert "remaining=0" in out2.read_text()


def test_split_still_promotes_one_unit_when_nothing_overlaps(tmp_path):
    """Two units touching different files stay separate promotions."""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "dev")
    (r / "a.py").write_text("a = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "unitA (#201)")
    (r / "b.py").write_text("b = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "unitB (#202)")
    _git(r, "fetch", "-q", "origin")

    out = tmp_path / "out"
    assert _split(r, out).returncode == 0
    listing = _git(r, "ls-tree", "-r", "--name-only", "promote/dev-to-qa")
    assert "a.py" in listing
    assert "b.py" not in listing, "unrelated unitB was swept into unitA's promotion"
