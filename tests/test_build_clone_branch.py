"""AppBuilder's builders must clone the integration branch (dev).

A plain clone checks out the default branch, which is main. A fix or
feature forked from main and opened into dev carries main's VERSION files
and promotion commits with it (seen on cs#258 and lm#1903, both flagged
"VERSION is branch-owned"). process_single_issue is too large to run here,
so this checks the clone call itself.
"""
import ast
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clone_calls(filename, function):
    tree = ast.parse(open(os.path.join(_ROOT, filename)).read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == function)
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "clone_from"]


def test_fix_and_feature_builds_clone_the_integration_branch():
    for filename, function in (("fix_engine.py", "process_single_issue"),
                               ("feature_build.py", "build_feature")):
        calls = _clone_calls(filename, function)
        assert calls, (filename, function)
        for call in calls:
            branch = [k.value for k in call.keywords if k.arg == "branch"]
            assert branch and getattr(branch[0], "id", None) == "base_branch", (
                f"{filename}:{call.lineno} clones the default branch (main)")
