"""Regression tests for locating the ruff binary.

ruff is installed into AppBuilder's virtualenv (``<venv>/bin/ruff``) but the
systemd unit runs with a minimal PATH that does not include the venv's bin
directory:

    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin

Invoking a bare "ruff" therefore raised FileNotFoundError in production and
silently degraded every PR review to "ruff not installed — skipping
undefined-name pass", so the F821/F822/F823 undefined-name pass never actually
ran. Verified live on LM-AB: under that exact PATH ``shutil.which("ruff")``
returns None while ``<dir(sys.executable)>/ruff`` exists.
"""
import os
import stat
import sys

import lint_python


def _make_fake_ruff(dirpath):
    path = os.path.join(dirpath, "ruff")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_prefers_ruff_next_to_the_running_interpreter(tmp_path, monkeypatch):
    """The venv's own ruff must win even when it is not on PATH at all."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    expected = _make_fake_ruff(str(venv_bin))

    monkeypatch.setattr(sys, "executable", str(venv_bin / "python3"))
    # Reproduce the production failure mode: nothing named ruff on PATH.
    monkeypatch.setattr(lint_python.shutil, "which", lambda _n: None)

    assert lint_python._ruff_executable() == expected


def test_falls_back_to_path_when_not_in_venv(tmp_path, monkeypatch):
    empty_bin = tmp_path / "empty" / "bin"
    empty_bin.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(empty_bin / "python3"))
    monkeypatch.setattr(lint_python.shutil, "which", lambda _n: "/usr/bin/ruff")

    assert lint_python._ruff_executable() == "/usr/bin/ruff"


def test_falls_back_to_bare_name_when_ruff_is_absent(tmp_path, monkeypatch):
    """Must still degrade gracefully (caller catches FileNotFoundError)."""
    empty_bin = tmp_path / "empty2" / "bin"
    empty_bin.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(empty_bin / "python3"))
    monkeypatch.setattr(lint_python.shutil, "which", lambda _n: None)

    assert lint_python._ruff_executable() == "ruff"


def test_non_executable_candidate_is_ignored(tmp_path, monkeypatch):
    venv_bin = tmp_path / "venv2" / "bin"
    venv_bin.mkdir(parents=True)
    path = os.path.join(str(venv_bin), "ruff")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not executable")
    os.chmod(path, 0o644)

    monkeypatch.setattr(sys, "executable", str(venv_bin / "python3"))
    monkeypatch.setattr(lint_python.shutil, "which", lambda _n: "/usr/bin/ruff")

    assert lint_python._ruff_executable() == "/usr/bin/ruff"


def test_missing_ruff_still_degrades_to_no_findings(tmp_path, monkeypatch):
    """A missing binary must yield [] rather than raising into the review."""
    empty_bin = tmp_path / "empty3" / "bin"
    empty_bin.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(empty_bin / "python3"))
    monkeypatch.setattr(lint_python.shutil, "which", lambda _n: None)
    monkeypatch.setattr(
        lint_python, "_ruff_executable",
        lambda: os.path.join(str(tmp_path), "definitely-not-a-real-ruff"))

    assert lint_python._run_ruff("x = 1\n", "x.py") == []
