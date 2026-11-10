#!/usr/bin/env python3
"""Self-test for hub_agent.py's SPOKE_GET_MTLS_STATUS responder.

Run:  python3 test_hub_agent_mtls_status.py

Background: the hub's mTLS readiness card (System → Hub Status) probes every
connected primary spoke — including AppBuilder — with SPOKE_GET_MTLS_STATUS and
marks any spoke that doesn't reply ``{"status":"SUCCESS","mtls":{...}}`` as
"offline". ab is self-contained (does not import lm's control_plane), so before
this responder existed it silently dropped the probe to "Unhandled Hub message
type", the hub's request timed out, and ab showed a FALSE offline even though
its WS link was up and it held the hub-CA mTLS client cert.

hub_agent.py can't be imported directly (circular import chain via main.py), so
— per the established convention in this repo (see
test_hub_agent_help_ask_requirements.py) — this extracts _handle_message AND
_handle_get_mtls_status via ast and execs them onto a minimal fake "self".

Covers that the SPOKE_GET_MTLS_STATUS branch:
1. sends exactly one signed COMMAND_RESULT whose top-level correlation_id
   echoes the inbound message_id (so the hub's request_response path matches).
2. carries data.status == "SUCCESS" and an mtls dict with the keys the hub's
   readiness check reads (ca_present / client_cert_present / client_key_present).
3. reports client_cert_present/client_key_present truthfully from the on-disk
   cert paths (present -> True, absent -> False).
"""
import ast
import asyncio
import os
import tempfile


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _load(names):
    src = open("hub_agent.py").read()
    tree = ast.parse(src)
    import time
    import uuid as uuid_mod
    ns = {
        "asyncio": asyncio,
        "uuid": uuid_mod,
        "time": time,
        "os": os,
        "logger": _NoLog(),
        "encode_frame": lambda signer, reply: reply,
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name in names:
            exec(ast.get_source_segment(src, n), ns)
    return ns


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(frame)


class _FakeSelf:
    def __init__(self, cert, key, ca):
        self._ws = _FakeWs()
        self.signer = object()
        self.spoke_id = "ab-spoke"
        self._client_cert_file = cert
        self._client_key_file = key
        self._tls_ca_cert = ca
        self._hub_mtls = True
        self._present_cert = True

    def _verify(self, msg):
        return True


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True
    ns = _load({"_handle_message", "_handle_get_mtls_status"})
    handle_message = ns["_handle_message"]
    get_status = ns["_handle_get_mtls_status"]

    with tempfile.TemporaryDirectory() as d:
        cert = os.path.join(d, "hub-client-cert.pem")
        key = os.path.join(d, "hub-client-key.pem")
        with open(cert, "w") as f:
            f.write("cert\n")
        with open(key, "w") as f:
            f.write("key\n")

        # ---- 1. dispatch reaches the responder and replies correctly ----
        fake = _FakeSelf(cert, key, ca="")
        # bind extracted _handle_get_mtls_status so _handle_message can call it
        fake._handle_get_mtls_status = get_status.__get__(fake)
        msg = {"header": {"message_id": "corr-42"},
               "payload": {"type": "SPOKE_GET_MTLS_STATUS", "data": {}}}
        asyncio.run(handle_message(fake, msg))

        sent = fake._ws.sent
        ok &= _check("SPOKE_GET_MTLS_STATUS: exactly one reply sent", len(sent) == 1)
        reply = sent[0] if sent else {}
        ok &= _check("reply correlation_id echoes inbound message_id",
                     reply.get("correlation_id") == "corr-42")
        pdata = reply.get("payload", {}).get("data", {})
        ok &= _check("reply payload type COMMAND_RESULT",
                     reply.get("payload", {}).get("type") == "COMMAND_RESULT")
        ok &= _check("reply data.status == SUCCESS", pdata.get("status") == "SUCCESS")
        m = pdata.get("mtls")
        ok &= _check("reply carries an mtls dict", isinstance(m, dict))
        m = m or {}
        ok &= _check("mtls reports client cert/key present (both on disk)",
                     m.get("client_cert_present") is True and m.get("client_key_present") is True)
        ok &= _check("mtls reports ca absent (no ca configured)",
                     m.get("ca_present") is False)
        for k in ("ca_present", "client_cert_present", "client_key_present"):
            ok &= _check(f"mtls dict has readiness key '{k}'", k in m)

        # ---- 2. absent cert files -> present flags False ----
        fake2 = _FakeSelf(os.path.join(d, "missing.pem"),
                          os.path.join(d, "missing.key"), ca="")
        asyncio.run(get_status(fake2, {"header": {"message_id": "corr-7"}}))
        m2 = fake2._ws.sent[0]["payload"]["data"]["mtls"]
        ok &= _check("absent cert/key -> present flags False",
                     m2.get("client_cert_present") is False and m2.get("client_key_present") is False)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running hub_agent.py SPOKE_GET_MTLS_STATUS self-test...")
    import sys
    sys.exit(main())
