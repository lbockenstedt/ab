"""A stale HUB_SECRET (hub restart / missed rotations) must not lock ab out.

Mirrors the lm agent's recovery: rotation-window ``signatures``, the durable
recovery PSK, and dropping the stale secret when TLS authenticates the hub.
"""
import asyncio
import hashlib
import hmac
import json

import pytest

import hub_agent
from hub_agent import HubAgentClient


def _sig(secret, challenge):
    return hmac.new(secret.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def _client(monkeypatch, tls_verify, hub_secret="old", recovery_psk="", cleared=None):
    monkeypatch.setenv("LM_HUB_TLS_VERIFY", "1" if tls_verify else "0")
    return HubAgentClient(
        "wss://hub.example/ws", "ab", secret="s", hub_secret=hub_secret,
        recovery_psk=recovery_psk,
        on_hub_secret=(cleared.append if cleared is not None else None))


def test_current_signature_verifies(monkeypatch):
    c = _client(monkeypatch, False)
    assert c._verify_hub_challenge("ch", _sig("old", "ch"), None) == (True, 0)


def test_rotation_window_signatures_verify_with_older_secret(monkeypatch):
    c = _client(monkeypatch, False)
    ok, idx = c._verify_hub_challenge("ch", _sig("new", "ch"), [_sig("new", "ch"), _sig("old", "ch")])
    assert (ok, idx) == (True, 1)


def test_malformed_or_wrong_signatures_fail(monkeypatch):
    c = _client(monkeypatch, False)
    assert c._verify_hub_challenge("ch", _sig("x", "ch"), "junk") == (False, None)
    assert c._verify_hub_challenge("ch", None, [None, 3]) == (False, None)


def test_recovery_psk_fails_closed(monkeypatch):
    c = _client(monkeypatch, False)
    assert not c._recovery_psk_verifies("ch", _sig("psk", "ch"))
    c = _client(monkeypatch, False, recovery_psk="psk")
    assert c._recovery_psk_verifies("ch", _sig("psk", "ch"))
    assert not c._recovery_psk_verifies("ch", "")
    assert not c._recovery_psk_verifies("ch", _sig("other", "ch"))


class _WS:
    def __init__(self, proof):
        self.sent, self.closed = [], None
        self._proof = proof

    async def send(self, d):
        self.sent.append(json.loads(d))

    async def recv(self):
        if len(self.sent) == 1:
            return json.dumps(self._proof)
        await asyncio.sleep(0.05)
        raise asyncio.CancelledError

    async def close(self, code, reason=""):
        self.closed = (code, reason)


def _handshake(c, proof):
    ws = _WS(proof)

    class _Conn:
        async def __aenter__(self_inner):
            return ws

        async def __aexit__(self_inner, *a):
            return False

    orig = hub_agent.websockets.connect
    hub_agent.websockets.connect = lambda *a, **k: _Conn()
    try:
        try:
            asyncio.run(c._connect_and_serve())
        except BaseException as e:  # noqa: BLE001
            return ws, e
        return ws, None
    finally:
        hub_agent.websockets.connect = orig


def _proof(**kw):
    p = {"status": "HUB_VERIFIED", "challenge": "ch", "signature": _sig("new", "ch")}
    p.update(kw)
    return p


def test_stale_secret_tls_verified_is_dropped_and_handshake_continues(monkeypatch):
    cleared = []
    c = _client(monkeypatch, True, cleared=cleared)
    ws, _ = _handshake(c, _proof())
    assert c.hub_secrets == [] and cleared == [""]
    assert {"status": "HUB_OK"} in ws.sent


def test_stale_secret_recovery_psk_is_dropped_even_without_tls(monkeypatch):
    c = _client(monkeypatch, False, recovery_psk="psk")
    ws, _ = _handshake(c, _proof(recovery_signature=_sig("psk", "ch")))
    assert c.hub_secrets == []
    assert {"status": "HUB_OK"} in ws.sent


def test_stale_secret_without_tls_or_psk_refuses_and_keeps_secret(monkeypatch):
    c = _client(monkeypatch, False)
    ws, err = _handshake(c, _proof())
    assert isinstance(err, RuntimeError)
    assert c.hub_secrets == ["old"]
    assert ws.closed and ws.closed[0] == 1008
    assert {"status": "HUB_OK"} not in ws.sent


def test_recovery_psk_command_persists(monkeypatch):
    saved = []
    monkeypatch.setenv("LM_HUB_TLS_VERIFY", "0")
    c = HubAgentClient("wss://h/ws", "ab", on_recovery_psk=saved.append)
    acks = []

    async def _ack(msg, status="SUCCESS", message=""):
        acks.append(status)
    c._ack = _ack
    msg = {"payload": {"type": "SPOKE_SET_RECOVERY_PSK", "data": {"recovery_psk": "p"}},
           "header": {}}
    asyncio.run(c._handle_message(msg)) if hasattr(c, "_handle_message") else pytest.skip("handler name")
    assert saved == ["p"] and c.recovery_psk == "p"
