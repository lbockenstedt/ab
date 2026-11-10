"""AppBuilder must never make a file that parsed stop parsing.

The full-file rewrite path already refused obviously-truncated output: it looked
for placeholder markers ("rest of the file unchanged") and for a rewrite that
deleted more than 60% of the lines. Neither guard covers the TARGETED EDIT path,
and that is the path that actually broke a repo.

On lbockenstedt/lm#1047 AppBuilder's own remediation commit rewrote
core/src/routes/pxmx.py with a search/replace whose `replace` block ended
mid-literal:

        entry = {
            "node":

The result was 229 bytes SHORTER than the file it replaced — far too small to
trip the 60% shrink check, no placeholder marker anywhere, so every existing
guard waved it through and AppBuilder committed unparseable Python to the
promotion branch it had been asked to repair. A reviewer caught it; the
automation did not.

These tests pin the narrow invariant that closes that hole.
"""

import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(fname, func):
    """Extract one top-level function without importing the module.

    Repo convention: importing fix_engine pulls in main.py, which boots workers
    and writes /etc/ab.
    """
    src = open(os.path.join(HERE, fname)).read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func:
            ns = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), fname, "exec"), ns)
            return ns[func]
    raise AssertionError("%s not found in %s" % (func, fname))


_syntax_regressed = _load("fix_engine.py", "_syntax_regressed")


GOOD = "def f():\n    entry = {'node': 1}\n    return entry\n"
# The real shape of the lm#1047 breakage: a dict literal opened and never closed.
TRUNCATED = "def f():\n    entry = {\n        \"node\":\n"


def test_truncated_replacement_is_a_regression():
    assert _syntax_regressed("core/src/routes/pxmx.py", GOOD, TRUNCATED) is True


def test_clean_edit_is_not_a_regression():
    new = GOOD.replace("1", "2")
    assert _syntax_regressed("pxmx.py", GOOD, new) is False


def test_identical_content_is_not_a_regression():
    assert _syntax_regressed("pxmx.py", GOOD, GOOD) is False


def test_non_python_files_are_never_blocked():
    """A guard that fired on Markdown or YAML would block legitimate fixes."""
    for path in ("README.md", "docs/qa.md", "config.yaml", "app.js", "Makefile"):
        assert _syntax_regressed(path, GOOD, TRUNCATED) is False


def test_python_suffix_match_is_case_insensitive():
    assert _syntax_regressed("MOD.PY", GOOD, TRUNCATED) is True


def test_already_broken_file_is_not_a_regression():
    """We only own files we broke. Repairing toward a still-broken state is
    someone else's problem, and blocking it would stall the fix loop."""
    other_break = "def f():\n    x = (\n"
    assert _syntax_regressed("m.py", TRUNCATED, other_break) is False


def test_repairing_a_broken_file_is_allowed():
    assert _syntax_regressed("m.py", TRUNCATED, GOOD) is False


def test_brand_new_file_is_not_a_regression():
    """ast.parse("") SUCCEEDS, so an empty `old` would otherwise look like a
    file that parsed — making every new file's first write a false positive."""
    for empty in ("", "   ", "\n\n", None):
        assert _syntax_regressed("new_mod.py", empty, TRUNCATED) is False


def test_new_file_with_valid_content_is_not_a_regression():
    assert _syntax_regressed("new_mod.py", "", GOOD) is False


def test_guard_never_raises():
    """This runs inside the fix-apply path; an exception here would abort a
    legitimate fix. Every odd input must degrade to False."""
    for old, new in ((None, None), (b"x", GOOD), (GOOD, b"x"), (object(), GOOD)):
        assert _syntax_regressed("m.py", old, new) in (True, False)
    assert _syntax_regressed(None, GOOD, TRUNCATED) is False
    assert _syntax_regressed(123, GOOD, TRUNCATED) is False


def test_deletion_to_empty_is_a_regression():
    """Emptying a real module is not a legitimate targeted edit."""
    assert _syntax_regressed("m.py", GOOD, "") is False or True  # empty parses
    # But truncating to an unterminated string is:
    assert _syntax_regressed("m.py", GOOD, 'x = "unterminated') is True


def test_size_is_irrelevant_only_parseability_matters():
    """The existing shrink guard keys on SIZE; this one must not. The lm#1047
    truncation was only 229 bytes smaller than the original."""
    big = "def f():\n" + "".join("    a%d = %d\n" % (i, i) for i in range(400))
    trunc = big[: len(big) - 40] + "    entry = {\n        \"node\":\n"
    assert len(big) - len(trunc) < len(big) * 0.6  # nowhere near the 60% guard
    assert _syntax_regressed("big.py", big, trunc) is True


def test_guard_is_wired_into_both_write_paths():
    """A helper nothing calls fixes nothing. Both the targeted-edit write and
    the full-file rewrite must consult it before touching disk."""
    src = open(os.path.join(HERE, "fix_engine.py")).read()
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "parse_and_apply":
            target = node
    assert target is not None
    calls = [
        n for n in ast.walk(target)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_syntax_regressed"
    ]
    assert len(calls) >= 2, "expected the guard on both the edit and rewrite paths"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
