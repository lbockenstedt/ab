"""The Copilot device-flow poll thread reports an unsaveable credential instead of sitting on "pending"."""
import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from test_config_store_atomic_write import _load_ns


class _Resp:
    """GitHub's token reply for an authorized device code."""
    headers = {"content-type": "application/json"}

    def json(self):
        """The token payload."""
        return {"access_token": "gho_test"}


@pytest.fixture
def poll(tmp_path, monkeypatch):
    """Run the actual poll loop and config transaction without starting main."""
    ns = _load_ns()
    path = tmp_path / "config.json"
    ns.update(CONFIG_FILE=str(path), state={})
    tree = ast.parse((Path(__file__).resolve().parents[1] / "routes.py").read_text())
    node = next(n for n in tree.body if getattr(n, "name", "") == "_copilot_poll_loop")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "routes.py", "exec"), ns)
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(
        COPILOT_CLIENT_ID="test-id", GITHUB_OAUTH_TOKEN_URL="https://example.invalid"
    ))
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return ns, path


def test_unsaveable_credential_ends_the_flow_with_an_error(poll):
    """An unreadable config must not leave the UI 'pending' or show 'authorized'."""
    ns, path = poll
    path.write_text("{broken")
    ns["_copilot_poll_loop"]("device-code", 5, "copilot")
    assert ns["state"]["copilot_auth"]["status"] == "error"
    assert "could not be saved" in ns["state"]["copilot_auth"]["message"]
    assert path.read_text() == "{broken"


def test_saved_credential_reports_authorized(poll):
    """The normal path still ends 'authorized' and stores the token under the provider."""
    ns, _ = poll
    ns["save_config"]({"copilot_device": {"test": True}, "other": "keep"})
    ns["_copilot_poll_loop"]("device-code", 5, "copilot")
    saved = ns["load_config"]()
    assert ns["state"]["copilot_auth"]["status"] == "authorized"
    assert saved["llm_credentials"]["copilot"]["api_key"] == "gho_test"
    assert "copilot_device" not in saved
    assert saved["other"] == "keep"
