"""ab#204/#205: a diagnostic log for verify-on TLS with no usable CA.

LM_HUB_TLS_VERIFY=1 without a working LM_HUB_CA_CERT falls back to the system
trust store, which correctly REJECTS the hub's self-signed cert (that fail-
closed behavior is not being changed here — see the reviewer panel discussion
that decided a silent unverified fallback would be the wrong fix). What was
missing is any indication of WHY the connection then fails with
CERTIFICATE_VERIFY_FAILED: without a log pointing at the misconfiguration, it
reads as an unexplained connection drop rather than something the operator
can fix by setting LM_HUB_CA_CERT.
"""
import logging

from hub_agent import HubAgentClient


def _client(tls_verify, tls_ca_cert):
    c = HubAgentClient.__new__(HubAgentClient)
    c._tls_verify = tls_verify
    c._tls_ca_cert = tls_ca_cert
    c._hub_mtls = False
    c._present_cert = False
    c._client_cert_file = "/nonexistent-cert.pem"
    c._client_key_file = "/nonexistent-key.pem"
    return c


def test_verify_on_with_no_ca_cert_configured_logs_a_diagnostic(caplog):
    c = _client(tls_verify=True, tls_ca_cert="")
    with caplog.at_level(logging.WARNING, logger="HubAgent"):
        ctx = c._client_ssl_ctx()
    assert ctx is not None
    assert ctx.verify_mode.name != "CERT_NONE"  # still fails closed
    assert any("LM_HUB_CA_CERT is not set" in r.message for r in caplog.records)


def test_verify_on_with_a_missing_ca_cert_file_logs_a_diagnostic(caplog):
    c = _client(tls_verify=True, tls_ca_cert="/does/not/exist.pem")
    with caplog.at_level(logging.WARNING, logger="HubAgent"):
        ctx = c._client_ssl_ctx()
    assert ctx is not None
    assert any("does not exist" in r.message for r in caplog.records)


def test_verify_on_with_a_real_ca_cert_stays_silent(caplog, tmp_path):
    ca = tmp_path / "hub-ca.pem"
    ca.write_text(
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA\n"
        "-----END CERTIFICATE-----\n")
    c = _client(tls_verify=True, tls_ca_cert=str(ca))
    with caplog.at_level(logging.WARNING, logger="HubAgent"):
        try:
            c._client_ssl_ctx()
        except Exception:
            pass  # the fixture cert isn't a real CA; only the log gate matters
    assert not any("LM_HUB_CA_CERT" in r.message for r in caplog.records)


def test_verify_off_stays_silent_and_unverified():
    """Default posture (no LM_HUB_TLS_VERIFY): no diagnostic noise, and the
    context is the long-standing unverified one."""
    c = _client(tls_verify=False, tls_ca_cert="")
    ctx = c._client_ssl_ctx()
    assert ctx.verify_mode.name == "CERT_NONE"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
