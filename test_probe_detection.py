"""AppBuilder edge HTTPS-port scanner detection.

AppBuilder serves a FastAPI SPA on :443. A request for a path it never serves
(PHP/dotfiles/DB-admin panels/...) is a scanner fingerprinting the box, not a
real client. The FastAPI middleware answers a bare 404 and reports the probe up
the authenticated hub tunnel (``HubAgentClient.report_probe`` →
``HTTP_PROBE_REPORT``) so the hub blocks the source centrally on the NSG.

These tests pin the vendored classifier (byte-twin of the hub's) and the
fire-and-forget reporter's frame shape + safe no-op when not connected.

Run:  python3 -m pytest test_probe_detection.py
"""
import asyncio
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import probe_signatures  # noqa: E402
import hub_agent  # noqa: E402


# ── vendored classifier (must match the hub's signatures) ───────────────────
def test_scanner_paths_flagged():
    for p in ["/wp-login.php", "/.env", "/.git/config", "/phpmyadmin/index.php",
              "/xmlrpc.php", "/actuator/health", "/cgi-bin/x.cgi", "/config.bak"]:
        assert probe_signatures.looks_like_probe(p), p


def test_app_paths_not_flagged():
    for p in ["/", "/index.html", "/assets/app.js?v=1", "/api/tasks",
              "/login", "/setup-admin", "/chat"]:
        assert not probe_signatures.looks_like_probe(p), p


# ── reporter frame shape ────────────────────────────────────────────────────
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


def test_report_probe_sends_http_probe_report():
    captured = {}

    class _WS:
        async def send(self, wire):
            captured["wire"] = wire

    c, loop = _client_with_loop(ws=_WS())
    try:
        c.report_probe("203.0.113.7", "/wp-login.php", "POST")
        time.sleep(0.1)  # let the scheduled coroutine run on the loop thread
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert "wire" in captured
    _sig, _, body = captured["wire"].partition(".")
    msg = json.loads(body)
    assert msg["payload"]["type"] == "HTTP_PROBE_REPORT"
    assert msg["payload"]["data"] == {"source_ip": "203.0.113.7",
                                      "path": "/wp-login.php", "method": "POST",
                                      "node": "ab-1"}
    assert msg["header"]["destination_id"] == "hub"


def test_report_probe_noop_when_not_approved():
    class _WS:
        def __init__(self):
            self.sent = False

        async def send(self, wire):
            self.sent = True

    ws = _WS()
    c, loop = _client_with_loop(approved=False, ws=ws)
    try:
        c.report_probe("203.0.113.7", "/.env", "GET")
        time.sleep(0.05)
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert ws.sent is False


def test_report_probe_noop_without_connection():
    c = hub_agent.HubAgentClient.__new__(hub_agent.HubAgentClient)
    c.spoke_id = "ab-1"
    c.signer = hub_agent.MessageSigner("s")
    c._approved = True
    c._ws = None
    c.loop = None
    # No loop / no socket → must return cleanly, never raise.
    c.report_probe("203.0.113.7", "/.env", "GET")
