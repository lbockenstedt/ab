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

Every test file with a top-level ``if __name__ == "__main__":`` block is run,
including files that also hold pytest tests: their ``__main__`` block can check
more than the collected tests do. The first rule here (defines ``main()``, no
``def test_``) skipped those files and an ``async def main()`` script, so
test_feature_drive_classify.py's main went on failing unseen (its ast harness
lacked the ``_meta`` helper) while its pytest tests passed.
"""
import os
import pathlib
import re
import subprocess
import sys

import pytest

# The self-tests sit beside this file in tests/; they run from the repo root
# (as by hand), with the root on PYTHONPATH for the app modules they import.
_TESTS = pathlib.Path(__file__).resolve().parent
_HERE = _TESTS.parent

# The block that running a file by hand executes; only a top-level one counts.
_MAIN_BLOCK = re.compile(r"""^if __name__ == ["']__main__["']\s*:""", re.M)


def _is_script_selftest(path):
    """A file that does work when run as a script: it has a top-level
    ``__main__`` block, whatever its entry point is called and whether or not
    pytest also collects tests from it."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_MAIN_BLOCK.search(src))


def _discover():
    """Names of the test files to run as scripts. Never this module: given a
    ``__main__`` block, it would run itself, which would run itself again."""
    me = pathlib.Path(__file__).name
    return sorted(p.name for p in _TESTS.glob("test_*.py")
                  if p.name != me and _is_script_selftest(p))


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


@pytest.mark.parametrize("src, runs", [
    # pytest tests plus a __main__ block (test_feature_drive_classify.py's shape)
    ('import sys\n\n\ndef test_a():\n    pass\n\n\nif __name__ == "__main__":\n'
     '    sys.exit(0)\n', True),
    # an async entry point
    ("async def main():\n    return 0\n\n\nif __name__ == '__main__':\n"
     "    import asyncio\n    asyncio.run(main())\n", True),
    # a main() that nothing calls: running the file does nothing
    ("import sys\n\n\ndef main():\n    return 0\n", False),
    # the words without a top-level block
    ('def f():\n    if __name__ == "__main__":\n        pass\n\n\n'
     "NOTE = \"if __name__ == '__main__': main()\"\n", False),
])
def test_any_file_with_a_main_block_is_run_as_a_script(tmp_path, src, runs):
    """Files are picked by their top-level ``__main__`` block, whatever the
    entry point is called and whether or not pytest collects tests there."""
    path = tmp_path / "test_example.py"
    path.write_text(src)
    assert _is_script_selftest(path) is runs


@pytest.mark.parametrize("script", SCRIPT_SELFTESTS)
def test_script_selftest(script):
    proc = subprocess.run(
        [sys.executable, str(_TESTS / script)],
        cwd=str(_HERE),
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(_HERE)},
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
