"""Loopback-only, token-gated admin ops API (admin_ops.py)."""
import logging
import os
import stat
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import main  # first: main wires in admin_ops (importing admin_ops alone is circular)
import admin_ops
from app_state import state

TOKEN = "test-token-value-123"
HDR = {"X-AB-Admin-Token": TOKEN}


@pytest.fixture
def loop(monkeypatch):
    """A loopback client, with the token set through the env override."""
    monkeypatch.setenv("AB_ADMIN_OPS_TOKEN", TOKEN)
    return TestClient(main.app, client=("127.0.0.1", 50000))


def test_non_loopback_peer_gets_404_even_with_token(monkeypatch):
    monkeypatch.setenv("AB_ADMIN_OPS_TOKEN", TOKEN)
    remote = TestClient(main.app, client=("10.1.2.3", 50000))
    # A forged forwarding header must not turn a remote peer into loopback.
    r = remote.get("/admin/ops/ping", headers={**HDR, "X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 404


def test_loopback_without_or_with_wrong_token_gets_403(loop):
    assert loop.get("/admin/ops/ping").status_code == 403
    assert loop.get("/admin/ops/ping", headers={"X-AB-Admin-Token": "nope"}).status_code == 403


def test_correct_token_pings_without_a_session(loop):
    r = loop.get("/admin/ops/ping", headers=HDR)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_state_counts_and_needs_you(loop, monkeypatch):
    import config_store
    ts = datetime.now().isoformat()
    monkeypatch.setattr(config_store, "load_processed", lambda: {
        "o/r:1": {"status": "failed", "title": "Boom", "error": "x" * 300 + " host 10.0.0.5", "timestamp": ts},
        "o/r:2": {"status": "resolved", "title": "Fine", "timestamp": ts},
        "o/r:3": {"status": "ops-alert", "title": "Alert", "timestamp": ts},
    })
    body = loop.get("/admin/ops/state", headers=HDR).json()
    assert body["counts"] == {"failed": 1, "resolved": 1, "ops-alert": 1}
    ids = {n["id"]: n for n in body["needs_you"]}
    assert set(ids) == {"o/r:1", "o/r:3"}
    assert len(ids["o/r:1"]["reason"]) <= 200


def _log_to(monkeypatch, tmp_path, text):
    """Point the log route at a temp file holding ``text``."""
    path = tmp_path / "ab.log"
    path.write_text(text)
    monkeypatch.setattr(main, "get_log_path", lambda: str(path))


def test_log_grep_is_literal_and_lines_are_capped(loop, monkeypatch, tmp_path):
    _log_to(monkeypatch, tmp_path, "2026-01-01 10:00:00 - a - INFO - plain\n"
                                   "2026-01-01 10:00:01 - a - INFO - has a.b+c(d text\n"
                                   + "".join("2026-01-01 10:00:02 - a - INFO - n%d\n" % i for i in range(30)))
    # A regex metacharacter is matched literally, not compiled.
    r = loop.get("/admin/ops/log", params={"grep": "a.b+c("}, headers=HDR)
    assert r.status_code == 200 and len(r.json()["lines"]) == 1
    assert loop.get("/admin/ops/log", params={"grep": ".*"}, headers=HDR).json()["lines"] == []
    # Case-insensitive, and the lines cap applies (and is itself capped at 2000).
    assert len(loop.get("/admin/ops/log", params={"grep": "INFO", "lines": 5}, headers=HDR).json()["lines"]) == 5
    assert len(loop.get("/admin/ops/log", params={"lines": 99999}, headers=HDR).json()["lines"]) == 32


def test_log_since_filters_by_timestamp(loop, monkeypatch, tmp_path):
    _log_to(monkeypatch, tmp_path, "2026-01-01 10:00:00 - a - INFO - old\n2026-01-02 10:00:00 - a - INFO - new\n")
    lines = loop.get("/admin/ops/log", params={"since": "2026-01-02T00:00:00"}, headers=HDR).json()["lines"]
    assert len(lines) == 1 and "new" in lines[0]


def test_log_output_masks_secrets(loop, monkeypatch, tmp_path):
    _log_to(monkeypatch, tmp_path, "2026-01-01 10:00:00 - a - INFO - password=hunter2 Authorization: Bearer abc.def "
                                   "ghp_abcdefghijklmnopqrstuvwx from 10.9.8.7\n")
    out = loop.get("/admin/ops/log", headers=HDR).json()["lines"][0]
    assert "hunter2" not in out and "abc.def" not in out and "ghp_abcdef" not in out and "10.9.8.7" not in out


def test_llm_reports_health_state(loop, monkeypatch):
    monkeypatch.setitem(state, "any_llm_online", True)
    monkeypatch.setitem(state, "llm_endpoints_online", [{"name": "x", "online": True}])
    body = loop.get("/admin/ops/llm", headers=HDR).json()
    assert body["any_llm_online"] is True and body["llm_endpoints_online"][0]["online"] is True
    assert "provider_1_online" in body


def test_token_file_minted_0600_and_never_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("AB_ADMIN_OPS_TOKEN", raising=False)
    monkeypatch.setattr(admin_ops, "_token_dirs", lambda: [str(tmp_path)])
    monkeypatch.setattr(admin_ops, "_token_cache", None)
    with caplog.at_level(logging.DEBUG):
        tok = admin_ops.load_or_mint_token()
    path = tmp_path / "admin_ops_token"
    assert path.read_text() == tok
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert tok not in caplog.text
    # A second call reuses the same token.
    assert admin_ops.load_or_mint_token() == tok


def test_underscored_secret_names_are_masked():
    """OPENAI_API_KEY=short style values are masked even though word boundaries miss them."""
    import admin_ops
    out = admin_ops._redact("start OPENAI_API_KEY=shortsecret GH_TOKEN: abc end")
    assert "shortsecret" not in out and "abc" not in out
    assert "two words" not in admin_ops._redact('X_SECRET="two words" tail')


def test_spaced_quoted_secret_is_fully_masked():
    """A secret with spaces after "is" is masked to the end of the line."""
    import admin_ops
    out = admin_ops._redact('ok password is "correct horse battery staple"')
    assert "horse" not in out and "staple" not in out


def test_cloud_access_key_is_masked():
    """AWS_ACCESS_KEY_ID=... style values are masked."""
    import admin_ops
    assert "AKIAIOSFODNN7EXAMPLE" not in admin_ops._redact("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
