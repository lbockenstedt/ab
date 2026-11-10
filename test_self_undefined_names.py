"""AppBuilder runs its own undefined-name pass over its own source.

AB already flags F821 on everyone else's PRs (`lint_python`, wired into
`pr_review.check_undefined_names`). It was not applying that check to itself:
`llm_client.py` called `uuid.uuid4()` in three places without ever importing
`uuid`, which AB duly reported as a Tier-1 ERROR on its own promotion PR
(ab#267) — and, because a PR fix only ever runs when a human clicks the Fix
button, the report sat there while the NameError stayed in dev, qa and main.

Deliberately dogfoods `lint_python._run_ruff`, the exact function used on PRs,
so this test and the PR review can never disagree about what counts.

`_run_ruff` is written to degrade to "no findings" when the ruff binary is
missing, which is right for a PR review (a broken linter must not block a
merge) but would make THIS test pass vacuously. So ruff's presence is asserted
first — it is a hard dependency in requirements.txt.
"""
import os
import shutil

import pytest

import lint_python


_HERE = os.path.dirname(os.path.abspath(__file__))

#: Scratch/sandbox trees and vendored copies — not AppBuilder's own source.
_SKIP_DIRS = {"_lm", "venv", ".venv", "node_modules", "__pycache__",
              ".test_agentic_repo_tools", ".git"}


def _own_modules():
    for root, dirs, files in os.walk(_HERE):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(root, f)


def test_ruff_is_actually_available():
    """Guard the guard: _run_ruff swallows a missing binary, so without this
    every assertion below would pass by finding nothing."""
    exe = lint_python._ruff_executable()
    assert shutil.which(exe) or os.path.exists(exe), (
        "ruff not found (%r) — it is in requirements.txt and the undefined-name "
        "pass silently reports zero findings without it" % exe)


@pytest.mark.parametrize("path", list(_own_modules()),
                         ids=lambda p: os.path.relpath(p, _HERE))
def test_no_undefined_names_in_own_source(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        source = fh.read()
    findings = lint_python._run_ruff(source, os.path.basename(path))
    bad = ["%s:%s %s" % (os.path.relpath(path, _HERE),
                         f.get("location", {}).get("row"), f.get("message"))
           for f in findings or []]
    assert not bad, "undefined name(s) — a NameError waiting to happen:\n  " + "\n  ".join(bad)
