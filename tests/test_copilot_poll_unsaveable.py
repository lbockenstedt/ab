"""The Copilot device-flow poll thread reports an unsaveable credential instead of sitting on "pending"."""
import pytest

import main  # noqa: F401 - loads routes' dependencies in import order
import routes
from app_state import state


class _Resp:
    """GitHub's token reply for an authorized device code."""
    headers = {"content-type": "application/json"}

    def json(self):
        """The token payload."""
        return {"access_token": "gho_test"}


def test_unsaveable_credential_ends_the_flow_with_an_error(monkeypatch):
    """save_config raises ConfigUnreadableError while config.json is unreadable. The poll
    thread has no caller, so an uncaught raise left copilot_auth on 'pending' forever."""
    import requests

    def unreadable(_cfg):
        """Stand in for save_config refusing to overwrite an unreadable config.json."""
        raise RuntimeError("config.json: [Errno 13] Permission denied")

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(routes, "load_config", lambda: {})
    monkeypatch.setattr(routes, "save_config", unreadable)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    routes._copilot_poll_loop("device-code", 5, "copilot")

    assert state["copilot_auth"]["status"] == "error"
    assert "could not be saved" in state["copilot_auth"]["message"]


def test_saved_credential_reports_authorized(monkeypatch):
    """The normal path still ends 'authorized' and stores the token under the provider."""
    import requests
    saved = {}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(routes, "load_config", lambda: {})
    monkeypatch.setattr(routes, "save_config", saved.update)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    routes._copilot_poll_loop("device-code", 5, "copilot")

    assert state["copilot_auth"]["status"] == "authorized"
    assert saved["llm_credentials"]["copilot"]["api_key"] == "gho_test"
