"""AppBuilder WebUI login-failure reporting.

A failed ``/login`` attempt against AppBuilder's own WebUI (``auth.verify_credentials``
already rejected it) is relayed up the authenticated hub tunnel
(``HubAgentClient.report_login_failure`` -> ``APP_LOGIN_FAILURE``) so the hub's
threat monitor counts repeat attempts against this box toward the SAME
brute-force threshold/NSG block as its own ``/login`` — one deny protects every
edge, not just the hub's.

These tests pin the fire-and-forget reporter's frame shape and its safe no-op
when not connected/approved (mirrors test_probe_detection.py).

Run:  python3 -m pytest test_login_failure_report.py
"""
import asyncio
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import hub_agent  # noqa: E402


def _client_with_loop(approved=True, ws=None):
    c = hub_agent.HubAgentClient.__new__(hub_agent.HubAgentClient)
    c.spoke_id = "ab-1"
    c.signer = hub_agent.MessageSigner("s3cr3t")
    c._approved = approved
    c._ws = ws
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    c.loop = loop
    return c, loop


def test_report_login_failure_sends_app_login_failure():
    captured = {}

    class _WS:
        async def send(self, wire):
            captured["wire"] = wire

    c, loop = _client_with_loop(ws=_WS())
    try:
        c.report_login_failure("203.0.113.7", "admin")
        time.sleep(0.1)  # let the scheduled coroutine run on the loop thread
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert "wire" in captured
    _sig, _, body = captured["wire"].partition(".")
    msg = json.loads(body)
    assert msg["payload"]["type"] == "APP_LOGIN_FAILURE"
    assert msg["payload"]["data"] == {"source_ip": "203.0.113.7",
                                      "username": "admin", "node": "ab-1"}
    assert msg["header"]["destination_id"] == "hub"


def test_report_login_failure_truncates_long_username():
    captured = {}

    class _WS:
        async def send(self, wire):
            captured["wire"] = wire

    c, loop = _client_with_loop(ws=_WS())
    try:
        c.report_login_failure("203.0.113.7", "x" * 500)
        time.sleep(0.1)
    finally:
        loop.call_soon_threadsafe(loop.stop)
    msg = json.loads(captured["wire"].partition(".")[2])
    assert len(msg["payload"]["data"]["username"]) == 128


def test_report_login_failure_noop_when_not_approved():
    class _WS:
        def __init__(self):
            self.sent = False

        async def send(self, wire):
            self.sent = True

    ws = _WS()
    c, loop = _client_with_loop(approved=False, ws=ws)
    try:
        c.report_login_failure("203.0.113.7", "admin")
        time.sleep(0.05)
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert ws.sent is False


def test_report_login_failure_noop_without_connection():
    c = hub_agent.HubAgentClient.__new__(hub_agent.HubAgentClient)
    c.spoke_id = "ab-1"
    c.signer = hub_agent.MessageSigner("s")
    c._approved = True
    c._ws = None
    c.loop = None
    # No loop / no socket → must return cleanly, never raise.
    c.report_login_failure("203.0.113.7", "admin")
