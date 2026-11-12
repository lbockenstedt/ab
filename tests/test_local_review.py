"""Tests for scripts/local_review.py, the local run of AB's PR review.

The point of the script is that a review before `git push` sees the same change
AB will see after it, so it must not miss work that is not committed yet.
"""
import importlib.util
import json
import os
import subprocess
import sys

import types

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "local_review", os.path.join(os.path.dirname(__file__), "..", "scripts", "local_review.py"))
local_review = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(local_review)


def _git(repo, *args):
    """Run git quietly in ``repo``."""
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_change_covers_commits_uncommitted_edits_and_new_files(tmp_path):
    """Committed, edited-but-uncommitted and untracked files are all reviewed,
    and a path with a space in it stays one path."""
    repo = str(tmp_path)
    _git(repo, "init", "-q")
    for name in ("committed.py", "edited.py", "same.py", "my notes.py"):
        (tmp_path / name).write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    (tmp_path / "committed.py").write_text("x = 2\n")
    (tmp_path / "my notes.py").write_text("x = 4\n")
    _git(repo, "commit", "-qam", "change")
    (tmp_path / "edited.py").write_text("x = 3\n")
    (tmp_path / "new.py").write_text("y = 1\n")
    (tmp_path / "new file.py").write_text("z = 1\n")

    files = {f.filename: f.patch for f in local_review._changed_files(repo, base)}

    assert sorted(files) == ["committed.py", "edited.py", "my notes.py", "new file.py", "new.py"]
    assert "+x = 2" in files["committed.py"] and "+x = 3" in files["edited.py"]
    assert "+y = 1" in files["new.py"] and "+x = 4" in files["my notes.py"]
    assert "+z = 1" in files["new file.py"]


def test_importing_ab_leaves_no_files_in_the_repo_under_review(tmp_path):
    """AB's import writes config.json and ab.log into the working directory;
    run from inside a repo, they must not appear there (they would join the
    reviewed change as untracked files)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    out = tmp_path / "out"
    out.mkdir()
    _replies(out, ["p/m"], **{"panel1-p%2Fm": ("Approve", 0.95), "panel2-p%2Fm": ("Approve", 0.95)})

    # A passing gate proves AB was imported (gate() needs fix_engine).
    run = _script(["--gate", str(out)], repo)

    assert run.returncode == 0, run.stderr
    assert sorted(os.listdir(repo)) == [".git"]


def _script(args, cwd):
    """Run local_review.py as the skill does, from ``cwd``."""
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "local_review.py")
    return subprocess.run([sys.executable, script, *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120, env=dict(os.environ, AB_DISABLE_WORKERS="1"))


def test_nothing_to_review_has_its_own_exit_code(tmp_path):
    """"No change" (exit 3) must not read as "reviewer models unknown" (exit 4)."""
    repo = str(tmp_path)
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    run = _script([repo, "--base", "HEAD", "--out", str(tmp_path / "out")], repo)
    assert run.returncode == 3 and "no change since HEAD" in run.stderr


def test_a_merge_commit_reaches_abs_merge_only_context(tmp_path):
    """AB reads each commit's parents from GitHub; the local PR must carry them
    too, or AB's merge-only context never applies to a local back-merge."""
    import main  # noqa: F401
    import pr_review
    repo = str(tmp_path)
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "on main")
    base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "merge", "-q", "--no-ff", "-m", "back-merge main", "main")

    commits = local_review._commits(repo, base)
    pr = types.SimpleNamespace(get_commits=lambda: commits)

    assert [len(c.parents) for c in commits] == [2]
    assert "MERGE-ONLY PR" in pr_review._merge_only_context(pr)

    # A hand edit not yet committed is reviewed too, so it is not merge-only.
    (tmp_path / "f.txt").write_text("edited\n")
    commits = local_review._commits(repo, base)
    assert pr_review._merge_only_context(pr) == ""


def test_reviewer_models_come_from_abs_latest_review(monkeypatch):
    """The panel models are read from AB's most recently updated review
    comment, not hard-coded; other comments that merely quote a seat label
    are ignored, and a seat label that names no model is skipped."""
    import main  # noqa: F401  (pr_review needs main imported first)
    calls = []

    def gh(*args):
        calls.append(args)
        return [{"body": "quoting **Reviewer (copilot/old-model)**"},
                {"body": ("<!-- ab-pr-review -->\n**Reviewer (copilot/model-a)** — Approve\n"
                          "**Reviewer (copilot)** — Approve\n"
                          "**Reviewer (copilot/model-b)** — Reject\n"
                          "**Reviewer (copilot/model-a)** — Approve")},
                {"body": "<!-- ab-pr-review -->\n**Reviewer (copilot/stale-model)**"}]
    monkeypatch.setattr(local_review, "_gh_json", gh)
    seats = ["copilot/model-a", "copilot/model-b"]
    assert local_review._reviewer_seats("owner/repo") == {"panel1": seats, "panel2": seats}
    assert "sort=updated" in calls[0][1]


@pytest.mark.parametrize("reply", [None, [], [{"body": "no review here"}]])
def test_no_reviewer_models_is_an_error_not_an_empty_panel(monkeypatch, reply):
    """gh failing and AB never having reviewed are both errors: an empty list
    would launch no reviewers and let the gate pass with nothing reviewed."""
    import main  # noqa: F401
    monkeypatch.setattr(local_review, "_gh_json", lambda *a: reply)
    with pytest.raises(RuntimeError):
        local_review._reviewer_seats("owner/repo")


def test_the_newest_ab_review_wins_across_repos(monkeypatch):
    """The target repo's own last review can be older than AB's latest panel."""
    import main  # noqa: F401
    replies = {"owner/repo": [{"updated_at": "2026-01-01T00:00:00Z",
                               "body": "<!-- ab-pr-review -->\n**Reviewer (copilot/old)**"}],
               "lbockenstedt/ab": [{"updated_at": "2026-02-01T00:00:00Z",
                                    "body": "<!-- ab-pr-review -->\n**Reviewer (copilot/new)**"}]}
    monkeypatch.setattr(local_review, "_slug", lambda path: "lbockenstedt/ab")
    monkeypatch.setattr(local_review, "_gh_json", lambda _api, url: replies[url.split("/")[1] + "/" + url.split("/")[2]])
    assert local_review._reviewer_seats("owner/repo")["panel1"] == ["copilot/new"]


def test_each_panel_is_gated_on_its_own_seats(monkeypatch, tmp_path, capsys):
    """AB seats each panel separately; a seat on only one panel must not count
    as a failed reviewer on the other, which would change that panel's verdict."""
    import main  # noqa: F401
    import pr_review
    body = ("<!-- ab-pr-review -->\n**Reviewer (copilot/a)**\n**Reviewer (copilot/b)**\n"
            + pr_review._STATE_PANEL_HEADER + "\n**Reviewer (copilot/a)**\n**Reviewer (copilot/c)**")
    monkeypatch.setattr(local_review, "_gh_json", lambda *a: [{"body": body}])
    seats = local_review._reviewer_seats("owner/repo")
    assert seats == {"panel1": ["copilot/a", "copilot/b"], "panel2": ["copilot/a", "copilot/c"]}

    _replies(tmp_path, seats, **{"panel1-copilot%2Fa": ("Approve", 0.95),
                                 "panel1-copilot%2Fb": ("Approve", 0.95),
                                 "panel2-copilot%2Fa": ("Approve", 0.95),
                                 "panel2-copilot%2Fc": ("Approve", 0.95)})
    passed, out = _gate(tmp_path, capsys)
    assert passed is True
    assert out["panel1"]["failed"] == [] and out["panel2"]["failed"] == []


def test_seats_keep_provider_and_slashed_model_ids(tmp_path, capsys):
    """Two providers running the same model are two seats, as in AB, and a
    model ID with a slash still maps to one flat vote file per seat, never
    shared with another seat."""
    seats = ["copilot/vendor/m", "openrouter/vendor/m", "copilot/vendor_m"]
    files = {local_review.vote_file(str(tmp_path), p, s) for p in ("panel1", "panel2") for s in seats}
    assert len(files) == 6 and all(os.path.dirname(f) == str(tmp_path / "votes") for f in files)
    _replies(tmp_path, seats[:2], **{"panel1-copilot%2Fvendor%2Fm": ("Approve", 0.95),
                                     "panel2-copilot%2Fvendor%2Fm": ("Approve", 0.95)})
    passed, out = _gate(tmp_path, capsys)
    assert passed is True
    assert out["panel1"] == {"votes": 1, "failed": ["openrouter/vendor/m"],
                             "confidence": 0.95, "verdict": "Approve"}


def _replies(folder, models, **votes):
    """Lay out a --out folder as the skill does: reviewers.json plus
    vote_file() for each reply (a str is saved verbatim)."""
    if isinstance(models, list):
        models = {"panel1": models, "panel2": models}
    (folder / "reviewers.json").write_text(json.dumps(models))
    (folder / "votes").mkdir()
    for name, vote in votes.items():
        text = vote if isinstance(vote, str) else "```json\n" + json.dumps(
            {"verdict": vote[0], "confidence": vote[1], "critique": ""}) + "\n```"
        (folder / "votes" / (name + ".txt")).write_text(text)


def _gate(folder, capsys):
    import main  # noqa: F401  (fix_engine needs main imported first)
    passed = local_review.gate(str(folder))
    return passed, json.loads(capsys.readouterr().out)


def test_gate_uses_abs_panel_verdict(tmp_path, capsys):
    """A 2-of-3 majority approves a panel, as in AB; both panels must approve."""
    _replies(tmp_path, ["a", "b", "c"],
             **{"panel1-a": ("Approve", 0.95), "panel1-b": ("Approve", 0.92),
                "panel1-c": ("Reject", 0.4), "panel2-a": ("Approve", 0.9),
                "panel2-b": ("Reject", 0.6), "panel2-c": ("Reject", 0.5)})
    passed, out = _gate(tmp_path, capsys)
    assert passed is False
    assert out["panel1"]["verdict"] == "Approve" and out["panel2"]["verdict"] == "Reject"


def test_a_missing_reviewer_needs_the_rest_unanimous(tmp_path, capsys):
    """A seat with no reply is a failed reviewer, not an absent one: the
    survivors' 0.90 average would approve a full panel, but AB retries a
    partial panel that is not unanimous."""
    _replies(tmp_path, ["a", "b", "c"],
             **{"panel1-a": ("Approve", 0.95), "panel1-b": ("Reject", 0.85),
                "panel2-a": ("Approve", 0.95), "panel2-b": ("Approve", 0.95),
                "panel2-c": ("Approve", 0.95)})
    passed, out = _gate(tmp_path, capsys)
    assert passed is False
    assert out["panel1"] == {"votes": 2, "failed": ["c"], "confidence": 0.9, "verdict": "Retry"}
    assert out["panel2"]["verdict"] == "Approve"


def test_a_reply_without_a_verdict_is_a_failed_reviewer(tmp_path, capsys):
    """{"confidence": 0.99} expresses no verdict; it must not approve a panel."""
    _replies(tmp_path, ["a"], **{"panel1-a": '{"confidence": 0.99}',
                                 "panel2-a": ("Approve", 0.99)})
    passed, out = _gate(tmp_path, capsys)
    assert passed is False
    assert out["panel1"] == {"failed": ["a"], "verdict": "no replies"}


def test_gate_fails_closed_when_a_panel_has_no_replies(tmp_path, capsys):
    """No replies for a panel is not an approval."""
    _replies(tmp_path, ["a"], **{"panel1-a": ("Approve", 0.99)})
    passed, out = _gate(tmp_path, capsys)
    assert passed is False and out["panel2"]["verdict"] == "no replies"


def test_no_agents_loaded_is_reported_not_treated_as_no_match(monkeypatch):
    """Skills loading while the agents listing fails must surface as an error:
    otherwise panel 1 silently runs without AB's specialist checklists."""
    import sys
    import types
    import skills_loader
    monkeypatch.setitem(sys.modules, "github", types.SimpleNamespace(Github=lambda token: None))
    monkeypatch.setattr(local_review.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="tok\n"))
    monkeypatch.setattr(skills_loader, "load_skills", lambda gh, cfg: {"s": {"name": "s"}})
    assert local_review._agents_context("x") == ("", "no specialist agents loaded from lm")
    monkeypatch.setattr(skills_loader, "load_skills",
                        lambda gh, cfg: {"a": {"name": "a", "kind": "agent"}})
    monkeypatch.setattr(skills_loader, "agents_context", lambda text: "")
    assert local_review._agents_context("x") == ("", None)


def test_a_new_round_drops_the_last_rounds_votes(tmp_path):
    """Old replies must not be gated as votes on the new change."""
    old = tmp_path / "votes" / "panel2-x.txt"
    old.parent.mkdir()
    old.write_text('{"verdict": "Approve", "confidence": 0.99}')
    local_review._fresh_votes(str(tmp_path))
    assert (tmp_path / "votes").is_dir() and not old.exists()
