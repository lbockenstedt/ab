"""Tests for scripts/local_review.py, the local run of AB's PR review.

The point of the script is that a review before `git push` sees the same change
AB will see after it, so it must not miss work that is not committed yet.
"""
import importlib.util
import json
import os
import subprocess

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
    assert local_review._reviewer_models("owner/repo") == ["model-a", "model-b"]
    assert "sort=updated" in calls[0][1]


@pytest.mark.parametrize("reply", [None, [], [{"body": "no review here"}]])
def test_no_reviewer_models_is_an_error_not_an_empty_panel(monkeypatch, reply):
    """gh failing and AB never having reviewed are both errors: an empty list
    would launch no reviewers and let the gate pass with nothing reviewed."""
    import main  # noqa: F401
    monkeypatch.setattr(local_review, "_gh_json", lambda *a: reply)
    with pytest.raises(RuntimeError):
        local_review._reviewer_models("owner/repo")


def _replies(folder, models, **votes):
    """Lay out a --out folder as the skill does: reviewers.json plus
    votes/<panel>-<model>.txt for each reply (a str is saved verbatim)."""
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
