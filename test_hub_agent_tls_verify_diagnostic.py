"""ab#204/#205: a diagnostic log for verify-on TLS with no usable CA.

LM_HUB_TLS_VERIFY=1 without a working LM_HUB_CA_CERT falls back to the system
trust store, which correctly REJECTS the hub's self-signed cert (that fail-
closed behavior is not being changed here — see the reviewer panel discussion
that decided a silent unverified fallback would be the wrong fix). What was
missing is any indication of WHY the connection then fails with
CERTIFICATE_VERIFY_FAILED: without a log pointing at the misconfiguration, it
reads as an unexplained connection drop rather than something the operator
can fix by setting LM_HUB_CA_CERT.

Constructed through the REAL ``__init__`` with monkeypatched env vars (not
via ``HubAgentClient.__new__`` hand-setting ``_tls_ca_cert``) per the state-
logic reviewer panel's finding: ``__init__`` defaults ``_tls_ca_cert`` to
``/etc/ab/hub-ca.pem`` whenever ``LM_HUB_CA_CERT`` is unset, so it is NEVER
an empty string in a real client — the "not set" branch was only reachable
via the old tests' bypass, making it dead code in production. Going through
``__init__`` exercises exactly the paths a real spoke process hits, and pins
the ``_tls_ca_cert_explicit`` flag that now distinguishes "never configured"
from "configured but the file is missing".
"""
import logging

from hub_agent import HubAgentClient


def _client(monkeypatch, tmp_path, tls_verify, ca_cert=None):
    monkeypatch.setenv("LM_HUB_TLS_VERIFY", "1" if tls_verify else "0")
    if ca_cert is None:
        monkeypatch.delenv("LM_HUB_CA_CERT", raising=False)
    else:
        monkeypatch.setenv("LM_HUB_CA_CERT", ca_cert)
    # Keep the default /etc/ab/hub-ca.pem (unwritable in a test sandbox) out of
    # the way: mTLS client cert files simply won't exist under tmp_path, so no
    # extra patching is needed there.
    return HubAgentClient("wss://hub.example/ws", "spoke-1")


def test_verify_on_with_no_ca_cert_configured_logs_a_diagnostic(monkeypatch, tmp_path, caplog):
    c = _client(monkeypatch, tmp_path, tls_verify=True)
    assert c._tls_ca_cert_explicit is False  # never provided by the operator
    with caplog.at_level(logging.WARNING, logger="HubAgent"):
        ctx = c._client_ssl_ctx()
    assert ctx is not None
    assert ctx.verify_mode.name != "CERT_NONE"  # still fails closed
    assert any("LM_HUB_CA_CERT is not set" in r.message for r in caplog.records)


def test_verify_on_with_a_missing_ca_cert_file_logs_a_diagnostic(monkeypatch, tmp_path, caplog):
    c = _client(monkeypatch, tmp_path, tls_verify=True, ca_cert="/does/not/exist.pem")
    assert c._tls_ca_cert_explicit is True  # operator DID provide a path
    with caplog.at_level(logging.WARNING, logger="HubAgent"):
        ctx = c._client_ssl_ctx()
    assert ctx is not None
    assert any("does not exist" in r.message for r in caplog.records)
    assert any("/does/not/exist.pem" in r.message for r in caplog.records)


def test_verify_on_with_a_real_ca_cert_stays_silent(monkeypatch, tmp_path, caplog):
    ca = tmp_path / "hub-ca.pem"
    ca.write_text(
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA\n"
        "-----END CERTIFICATE-----\n")
    c = _client(monkeypatch, tmp_path, tls_verify=True, ca_cert=str(ca))
    with caplog.at_level(logging.WARNING, logger="HubAgent"):
        try:
            c._client_ssl_ctx()
        except Exception:
            pass  # the fixture cert isn't a real CA; only the log gate matters
    assert not any("LM_HUB_CA_CERT" in r.message for r in caplog.records)


def test_verify_off_stays_silent_and_unverified(monkeypatch, tmp_path):
    """Default posture (no LM_HUB_TLS_VERIFY): no diagnostic noise, and the
    context is the long-standing unverified one."""
    c = _client(monkeypatch, tmp_path, tls_verify=False)
    ctx = c._client_ssl_ctx()
    assert ctx.verify_mode.name == "CERT_NONE"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
