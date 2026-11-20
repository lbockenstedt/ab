"""A refused or unconfirmed local action must not become a pending Hub badge."""

import json
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("reply", [
    {"ok": True, "body": {"status": "restarted", "message": "setup requested"}},
    {"ok": False, "body": {"status": "error", "message": "already in progress"}},
    {"ok": False, "body": {"status": "error", "message": "setup failed"}},
    {"ok": True, "body": {"status": "unexpected"}},
    {"ok": True, "parse_error": "invalid JSON"},
    {"fetch_error": "network unavailable"},
])
def test_registration_badge_requires_an_actual_acknowledgement(reply):
    """Execute the actual browser handler with only its RPC, confirm and UI effects stubbed."""
    # The full Settings script also starts unrelated network and DOM work.
    html = (ROOT / "templates/index.html").read_text()
    function = re.search(
        r"async function hubAgentReregister\(\) \{.*?\n        \}", html, re.DOTALL)
    assert function is not None
    script = """
const vm = require('node:vm');
const reply = JSON.parse(process.argv[2]);
const result = {badges: [], toasts: []};
const context = {
    confirm: () => true,
    fetch: async () => {
        if (reply.fetch_error) throw new Error(reply.fetch_error);
        return {ok: reply.ok, json: async () => {
            if (reply.parse_error) throw new Error(reply.parse_error);
            return reply.body;
        }};
    },
    _renderHubAgentBadge: (...args) => result.badges.push(args),
    showToast: (...args) => result.toasts.push(args),
};
vm.createContext(context);
vm.runInContext(process.argv[1], context);
context.hubAgentReregister().then(() => console.log(JSON.stringify(result)))
    .catch(error => { console.error(error); process.exitCode = 1; });
"""
    proc = subprocess.run(
        ["node", "-e", script, function.group(), json.dumps(reply)],
        capture_output=True, text=True, timeout=10, check=True)
    result = json.loads(proc.stdout)
    # A refused RPC must not relabel the independently observed Hub connection.
    if reply.get("body", {}).get("status") == "restarted" and reply.get("ok"):
        assert result == {"badges": [["pending", "setup requested"]], "toasts": []}
    else:
        assert result["badges"] == []
        assert len(result["toasts"]) == 1 and result["toasts"][0][1] == "error"
        detail = reply.get("body", {}).get("message") or reply.get("parse_error") or reply.get("fetch_error")
        if detail:
            assert detail in result["toasts"][0][0]
