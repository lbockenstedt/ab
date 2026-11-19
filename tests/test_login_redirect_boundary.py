"""Both login paths must keep redirect destinations on this host."""
import ast
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from starlette.responses import RedirectResponse


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("destination,expected", [
    ("", "/"),
    ("/settings?tab=models", "/settings?tab=models"),
    (" /diagnostics ", "/diagnostics"),
    ("https://example.invalid/landing", "/"),
    ("//example.invalid/landing", "/"),
    ("/\\example.invalid/landing", "/"),
    ("/\t/example.invalid/landing", "/"),
    ("relative-path", "/"),
])
def test_login_redirect_stays_local(monkeypatch, method, destination, expected):
    """Reject browser-normalized external URLs without breaking local navigation."""
    source = (Path(__file__).resolve().parents[1] / "routes.py").read_text()
    wanted = {"_login_redirect_path", "login_page", "login_submit"}
    nodes = [
        node for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    for node in nodes:
        node.decorator_list = []
    namespace = {
        "Request": object, "RedirectResponse": RedirectResponse,
        "logger": SimpleNamespace(info=lambda *args: None),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "routes.py", "exec"), namespace)
    monkeypatch.setitem(sys.modules, "auth", SimpleNamespace(
        any_users=lambda: True, verify_session=lambda token: "fixture-user",
        verify_credentials=lambda *args: True, issue_session=lambda user: "fixture-session",
        SESSION_COOKIE="ab_session", SESSION_TTL_S=60,
    ))

    async def form():
        """Supply a successful offline form submission."""
        return {"username": "fixture-user", "password": "fixture", "next": destination}

    request = SimpleNamespace(
        cookies={"ab_session": "fixture-session"}, form=form, client=None,
        url=SimpleNamespace(scheme="https"),
    )
    if method == "get":
        response = asyncio.run(namespace["login_page"](request, next=destination))
    else:
        response = asyncio.run(namespace["login_submit"](request))
    assert response.status_code == 303
    assert response.headers["location"] == expected
