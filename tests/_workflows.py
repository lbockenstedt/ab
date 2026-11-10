"""Where the promotion workflows live, for the tests that read them.

ab's `.github/workflows/promote.yml` and `backmerge.yml` are thin callers: they
own only the triggers, the dispatch inputs and the token permissions. The
logic lives once, for the whole fleet, in lm's `promote-reusable.yml` and
`backmerge-reusable.yml` (see lm/docs/ci-checks.md).

CI checks lm out as `_lm` (with `.github`); a side-by-side dev checkout has it
at `../lm`. Without either, the logic tests skip locally but FAIL in CI, so CI
can never pass by testing nothing.
"""
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def caller(name):
    """Path of ab's own thin caller, e.g. caller("promote")."""
    return os.path.join(ROOT, ".github", "workflows", f"{name}.yml")


def logic(name):
    """Path of lm's shared reusable workflow holding `name`'s logic."""
    for base in (os.path.join(ROOT, "_lm"), os.path.join(os.path.dirname(ROOT), "lm")):
        path = os.path.join(base, ".github", "workflows", f"{name}-reusable.yml")
        if os.path.exists(path):
            return path
    msg = f"lm's {name}-reusable.yml not found (expected _lm/ or ../lm/)"
    if os.environ.get("CI"):
        raise AssertionError(msg)
    pytest.skip(msg, allow_module_level=True)


def load(path):
    """Parsed YAML of a workflow file."""
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def triggers(path):
    """The `on:` block (yaml parses a bare `on` key as True)."""
    doc = load(path)
    return doc.get("on", doc.get(True))
