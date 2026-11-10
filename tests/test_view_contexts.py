"""index.html's layout reads `state`, so every view context literal must supply it."""
import ast


def test_every_view_context_passes_state():
    tree = ast.parse(open("routes.py").read())
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "view" in keys and "state" not in keys:
                missing.append(node.lineno)
    assert not missing, f"view contexts without 'state' at routes.py lines {missing}"
