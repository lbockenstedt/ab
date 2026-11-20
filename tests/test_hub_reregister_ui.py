"""A local setup acknowledgement cannot establish an actual Hub connection state."""

import json
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_handlers(action, observed=None):
    """Execute both actual browser handlers with only RPC, confirm and UI effects stubbed."""
    # The full Settings script also starts unrelated network and DOM work.
    html = (ROOT / "templates/index.html").read_text()
    functions = []
    for name in ("hubAgentReregister", "hubAgentRefresh"):
        function = re.search(
            rf"async function {name}\(\) \{{.*?\n        \}}", html, re.DOTALL)
        assert function is not None
        functions.append(function.group())
    script = """
const vm = require('node:vm');
const input = JSON.parse(process.argv[2]);
const result = {badges: [], toasts: [], requests: [], warnings: []};
const context = {
    confirm: () => true,
    console: {warn: (...args) => result.warnings.push(args.map(String))},
    fetch: async url => {
        result.requests.push(url);
        const reply = url === '/api/hub-agent/reregister' ? input.action :
            url === '/api/hub-agent/status' ? input.observed : null;
        if (!reply) throw new Error('unexpected RPC');
        if (reply.fetch_error) throw new Error(reply.fetch_error);
        return {ok: reply.ok, status: reply.status || 200, json: async () => {
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
        ["node", "-e", script, "\n".join(functions),
         json.dumps({"action": action, "observed": observed})],
        capture_output=True, text=True, timeout=10, check=True)
    return json.loads(proc.stdout)


@pytest.mark.parametrize("status", ["pending", "error", "not_registered", "approved"])
def test_acknowledged_setup_displays_observed_status_not_assumed_pending(status):
    """A normal startup return can coexist with failure, no configuration or fast approval."""
    observed = {"status": status, "message": "independently observed " + status}
    result = _run_handlers(
        {"ok": True, "body": {"status": "restarted", "message": "setup requested"}},
        {"ok": True, "body": observed})
    assert result["badges"] == [[status, observed["message"]]]
    assert result["toasts"] == [] and result["warnings"] == []
    assert result["requests"] == ["/api/hub-agent/reregister", "/api/hub-agent/status"]


@pytest.mark.parametrize("reply", [
    {"ok": False, "body": {"status": "error", "message": "already in progress"}},
    {"ok": False, "body": {"status": "error", "message": "setup failed"}},
    {"ok": False, "body": {"status": "restarted", "message": "not accepted"}},
    {"ok": True, "body": {"status": "unexpected"}},
    {"ok": True, "parse_error": "invalid JSON"},
    {"fetch_error": "network unavailable"},
])
def test_refused_or_unconfirmed_registration_does_not_change_connection_state(reply):
    """Only accepted setup may request a status refresh; every other RPC outcome is explicit."""
    result = _run_handlers(reply)
    # A refused RPC must not relabel the independently observed Hub connection.
    assert result["badges"] == []
    assert result["requests"] == ["/api/hub-agent/reregister"]
    assert len(result["toasts"]) == 1 and result["toasts"][0][1] == "error"
    detail = reply.get("body", {}).get("message") or reply.get("parse_error") or reply.get("fetch_error")
    if detail:
        assert detail in result["toasts"][0][0]


@pytest.mark.parametrize("observed", [
    {"ok": False, "status": 503},
    {"ok": True, "parse_error": "bad status JSON"},
    {"ok": True, "body": {}},
    {"fetch_error": "status unavailable"},
])
def test_status_read_failure_preserves_acknowledgement_and_observed_state(observed):
    """A failed post-ack read is not a failed restart or evidence of any connection state."""
    result = _run_handlers({"ok": True, "body": {"status": "restarted"}}, observed)
    assert result["badges"] == []
    assert result["requests"] == ["/api/hub-agent/reregister", "/api/hub-agent/status"]
    assert result["warnings"]
    assert len(result["toasts"]) == 1 and result["toasts"][0][1] == "error"
    assert "acknowledged" in result["toasts"][0][0]
    assert "status" in result["toasts"][0][0]
