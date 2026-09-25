"""``pr_auto_remediate_skip_promotion`` must be a REGISTERED default, not just a
key ``pr_remediate`` happens to read.

Background. ``pr_remediate.auto_remediate_pr`` reads the flag with
``config.get("pr_auto_remediate_skip_promotion", True)``, and its behaviour is
already pinned by test_pr_remediate.py. What was missing is that the key was
never registered in either of routes.py's two config-default blocks, so it did
not exist in a freshly-written config.json at all. That matters for the
unattended ``dev -> qa -> main`` flow: the operator has to turn the flag OFF for
a denied promotion PR to get repaired instead of stalling the chain, and an
unregistered key is an undiscoverable one — it appears in no saved config and
in no settings render, so the only way to find it is to read the source.

routes.py cannot be imported here (it pulls in the whole app), so these are
static AST assertions against the two blocks, in the same spirit as the other
routes.py settings tests. They pin BOTH blocks because they are maintained in
parallel and it is the pair drifting apart that silently breaks a settings
round-trip.
"""

import ast

KEY = "pr_auto_remediate_skip_promotion"
# Registered alongside these, which are the other headless (no UI control)
# remediation knobs — if they ever grow a settings widget, KEY should too.
SIBLING = "pr_auto_remediate_skip_docs_only"


def _tree():
    with open("routes.py", encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(name):
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("routes.py has no function %r" % name)


def _setdefault_keys(func):
    """Every literal key passed to a ``<something>.setdefault("key", ...)`` call
    inside `func`."""
    keys = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "setdefault"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, str):
            keys.append(node.args[0].value)
    return keys


def test_ensure_config_defaults_registers_the_promotion_flag():
    keys = _setdefault_keys(_func("ensure_config_defaults"))
    assert KEY in keys, (
        "ensure_config_defaults must setdefault %r — an unregistered key never "
        "reaches a written config.json, so the operator cannot discover the one "
        "switch that lets promotion PRs be remediated" % KEY)


def test_settings_page_registers_the_promotion_flag():
    keys = _setdefault_keys(_func("settings_page"))
    assert KEY in keys, (
        "settings_page's default block must setdefault %r too — the two blocks "
        "are maintained in parallel and a key present in only one renders "
        "differently from what a save persists" % KEY)


def test_default_is_closed():
    """Default ON (skip promotion PRs) in BOTH blocks. Remediating a promotion
    branch pushes code to qa/main that was never on dev; that is only safe
    because backmerge.yml carries qa back to dev, so it stays an opt-in."""
    for fname in ("ensure_config_defaults", "settings_page"):
        found = []
        for node in ast.walk(_func(fname)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setdefault"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == KEY):
                found.append(node.args[1])
        assert found, "%s does not register %r" % (fname, KEY)
        for value in found:
            assert isinstance(value, ast.Constant) and value.value is True, (
                "%s must default %r to True (skip) — a default-open value would "
                "let AppBuilder rewrite promotion branches with no opt-in"
                % (fname, KEY))


def test_registered_next_to_its_sibling_knob():
    """Keep the headless remediation knobs together: the pair is how a reader
    finds them, and separating them is how one gets dropped."""
    for fname in ("ensure_config_defaults", "settings_page"):
        keys = _setdefault_keys(_func(fname))
        assert SIBLING in keys and KEY in keys
        assert abs(keys.index(KEY) - keys.index(SIBLING)) == 1, (
            "%s should register %r adjacent to %r" % (fname, KEY, SIBLING))


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", name, exc)
    sys.exit(1 if failures else 0)
