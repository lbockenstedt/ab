"""verify_fix's default pytest must not recurse into the provisioned _lm
checkout (cs#184: lm core's suite errored at import, failing a spoke fix for a
reason outside the diff)."""
import ast
import os
import tempfile


def _load():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_engine.py")).read()
    ns = {"os": os, "SPOKE_CORE_DIRNAME": "_lm"}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_exclude_spoke_core_from_pytest":
            exec(ast.get_source_segment(src, node), ns)
    return ns["_exclude_spoke_core_from_pytest"]


def test_ignores_provisioned_lm_for_default_pytest():
    fn = _load()
    with tempfile.TemporaryDirectory() as d:
        assert fn("pytest", d) == "pytest"
        os.mkdir(os.path.join(d, "_lm"))
        assert fn("pytest", d) == "pytest --ignore=_lm"
        assert fn("python3 -m pytest", d) == "python3 -m pytest --ignore=_lm"
        assert fn("npm test", d) == "npm test"
        assert fn("pytest -q lm-spoke/tests", d) == "pytest -q lm-spoke/tests"
