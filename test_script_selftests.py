#!/usr/bin/env python3
"""Makes the repo's script-style self-tests actually run in CI.

Most test files here are standalone scripts: they do their work in ``main()``,
print ``[PASS]``/``[FAIL]`` lines, and ``sys.exit(main())``. That form is
invisible to pytest -- it imports the module, finds no ``test_*`` function, and
collects nothing. ci.yml runs ``pytest -q .``, so at the time this file was
added **51 of the 52** such files never executed a single assertion in CI. They
only ran if someone remembered to invoke them by hand.

That is the worst failure mode for a test: present, detailed, reviewed, and
silently inert. It had already let a real defect sit -- test_feature_build.py
was dying on a missing ``auto_branch_name`` stub, so every assertion past its
first few cases was asserting against a run that had already aborted.

This module discovers those files and runs each one in a subprocess, asserting
a zero exit status. A subprocess (rather than importing and calling ``main()``)
is deliberate: these scripts ast-extract functions from the app modules, mutate
module globals, monkeypatch ``git.Repo.clone_from``, and chdir -- running them
in-process would let one leak state into the next and into the real pytest
tests. Each gets a clean interpreter, exactly as when run by hand.

Discovery is dynamic on purpose: a new self-test file is picked up with no edit
here, so the next one cannot be born invisible.
"""
import os
import pathlib
import subprocess
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent


def _is_script_selftest(path):
    """A file that defines main() but exposes no pytest-collectable test."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "\ndef main()" in src and "\ndef test_" not in src


def _discover():
    return sorted(p.name for p in _HERE.glob("test_*.py") if _is_script_selftest(p))


SCRIPT_SELFTESTS = _discover()


def test_script_selftests_are_discovered():
    """Guards the discovery itself.

    If a refactor broke the predicate this module would silently pass with an
    empty parameter list -- the same invisibility it exists to fix.
    """
    assert SCRIPT_SELFTESTS, (
        "no script-style self-tests discovered — the predicate in "
        "_is_script_selftest is probably wrong; this module would otherwise "
        "pass while testing nothing"
    )


@pytest.mark.parametrize("script", SCRIPT_SELFTESTS)
def test_script_selftest(script):
    proc = subprocess.run(
        [sys.executable, script],
        cwd=str(_HERE),
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode != 0:
        failures = [l for l in proc.stdout.splitlines() if "[FAIL]" in l]
        detail = "\n".join(failures) if failures else proc.stdout[-3000:]
        pytest.fail(
            f"{script} exited {proc.returncode}\n"
            f"--- failing cases ---\n{detail}\n"
            f"--- stderr (tail) ---\n{proc.stderr[-2000:]}"
        )


def _discover_shell_selftests():
    """The same invisibility problem, in the shell scripts under .github/.

    promotion_selftest.sh proves the properties the whole dev -> qa -> main flow
    rests on -- VERSION never leaking across branches, and (now) promotion being
    cut into one original PR per promotion PR. Nothing ran it: no workflow
    invoked it and pytest cannot see a .sh file, so it only executed when
    someone remembered to. Discovery is dynamic here too, so a second shell
    self-test cannot be born invisible either.
    """
    scripts = sorted(
        str(p) for p in (_HERE / ".github" / "scripts").glob("*selftest*.sh")
    )
    return scripts


SHELL_SELFTESTS = _discover_shell_selftests()


def test_shell_selftests_are_discovered():
    assert SHELL_SELFTESTS, (
        "no shell self-tests discovered under .github/scripts — this module "
        "would otherwise pass while testing nothing"
    )


@pytest.mark.parametrize("script", SHELL_SELFTESTS)
def test_shell_selftest(script):
    proc = subprocess.run(
        ["bash", script],
        cwd=str(_HERE),
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
    if proc.returncode != 0:
        failures = [l for l in proc.stdout.splitlines() if "FAIL" in l]
        detail = "\n".join(failures) if failures else proc.stdout[-3000:]
        pytest.fail(
            f"{script} exited {proc.returncode}\n"
            f"--- failing cases ---\n{detail}\n"
            f"--- stderr (tail) ---\n{proc.stderr[-2000:]}"
        )
