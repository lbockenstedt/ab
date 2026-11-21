"""Server and endpoint text rendered into the WebUI cannot inject markup or script.

Process names (any local user can pick one), cert subjects, Ollama model names
(from whatever endpoint the admin typed), SSO usernames and error strings reach
innerHTML. Each test runs the real render function from index.html in Node
against a fake DOM and a hostile payload.
"""

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EVIL = "\"'><img src=x onerror=alert(1)>"

FUNCTIONS = ("_bfEsc", "_diagPill", "_fmtUptime", "refreshSystemStats", "refreshDiagnostics",
             "_mmEsc", "_mmBase", "mmRefresh", "loadAuthUsers", "loadPromoteRepos")

RUNNER = r"""
const vm = require('node:vm');
const input = JSON.parse(process.argv[2]);
const els = {};
const clicks = [];
// Minimal element: records innerHTML and exposes data-* buttons parsed from it.
function el(id) {
    if (els[id]) return els[id];
    const e = {id, textContent: '', innerHTML: '', className: '', title: '', dataset: {}, value: '',
               classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
               querySelectorAll(sel) {
                   const attr = sel.slice(1, -1);
                   const key = attr.replace(/^data-/, '').replace(/-(\w)/g, (_, c) => c.toUpperCase());
                   const dec = v => v.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
                                     .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
                   return [...e.innerHTML.matchAll(new RegExp(attr + '="([^"]*)"', 'g'))].map(m => ({
                       dataset: {[key]: dec(m[1])},
                       addEventListener: (type, fn) => clicks.push(fn),
                   }));
               }};
    if (id === 'mm-baseurl') e.value = 'http://ollama.test:11434';
    return (els[id] = e);
}
const calls = [];
const context = {
    window: {}, console, setTimeout, Date, JSON, encodeURIComponent,
    document: {getElementById: el},
    fetch: async url => {
        const path = url.split('?')[0];
        if (input.fail) throw new Error(input.fail);
        if (!(path in input.replies)) throw new Error('unexpected RPC ' + url);
        return {ok: true, status: 200, json: async () => input.replies[path]};
    },
    _bfIndexPerf() {}, bfAnnotateModelSelects() {}, _tpsFor: () => input.tps,
    mmDelete: name => calls.push(['mmDelete', name]),
    authDeleteUser: name => calls.push(['authDeleteUser', name]),
};
vm.createContext(context);
vm.runInContext(process.argv[1], context);
(async () => {
    await context[input.call]();
    await new Promise(r => setTimeout(r, 0));
    clicks.forEach(fn => fn());
    const html = {};
    for (const [id, e] of Object.entries(els)) if (e.innerHTML) html[id] = e.innerHTML;
    console.log(JSON.stringify({html, calls}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def _source():
    """The named top-level functions from index.html, without the rest of its script."""
    html = (ROOT / "templates/index.html").read_text()
    parts = []
    for name in FUNCTIONS:
        one_line = re.search(rf"\n {{8}}function {name}\([^)\n]*\) \{{[^\n]*\}}\n", html)
        block = re.search(
            rf"\n {{8}}(?:async )?function {name}\([^)]*\) \{{.*?\n {{8}}\}}\n", html, re.DOTALL)
        arrow = re.search(rf"\n {{8}}const {name} = .*?;\n", html, re.DOTALL)
        match = one_line or block or arrow
        assert match is not None, name
        parts.append(match.group())
    return "\n".join(parts)


def _render(call, replies=None, fail=None, tps=None):
    """Run one render function with canned API replies; returns its innerHTML and handler calls."""
    proc = subprocess.run(
        ["node", "-e", RUNNER, _source(),
         json.dumps({"call": call, "replies": replies or {}, "fail": fail, "tps": tps})],
        capture_output=True, text=True, timeout=20, check=True)
    return json.loads(proc.stdout)


def _assert_inert(html):
    """No hostile tag or quote break-out survived into any rendered fragment."""
    assert html
    for fragment in html.values():
        assert "<img" not in fragment
        assert "\"'>" not in fragment


def test_diagnostics_escapes_every_server_field():
    """Providers, certs, chain, EKU, SANs, restart log and the process table are text, not markup."""
    cert = {"exists": True, "self_signed": False, "subject": EVIL, "issuer": EVIL, "chain_len": EVIL,
            "chain": [{"subject": EVIL, "issuer": EVIL, "self_signed": True}],
            "eku": [EVIL], "client_auth": True, "sans": [EVIL], "not_after": EVIL}
    replies = {
        "/api/diagnostics": {
            "versions": {}, "process": {}, "heartbeat": {}, "bug_ingest": {}, "feature_ingest": {},
            "providers": [{"provider": EVIL, "model": EVIL, "tier": EVIL, "speed": EVIL, "n": EVIL,
                           "exhausted": True, "exhausted_reason": EVIL, "key": "k"}],
            "hub_connection": {"available": True, "client_cert": cert,
                               "webui_cert": {"exists": True, "path": EVIL, "parse_error": EVIL}},
            "update": {"restart_log": [{"at": EVIL, "kind": EVIL, "message": EVIL}]},
        },
        "/api/system-stats": {"processes": [{"pid": EVIL, "name": EVIL, "rss_mb": EVIL,
                                              "cpu_percent": EVIL}]},
    }
    html = _render("refreshDiagnostics", replies, tps=EVIL)["html"]
    assert {"diag-providers-body", "diag-hc-client-cert", "diag-hc-webui-cert",
            "diag-restart-log", "diag-sys-procs"} <= set(html)
    _assert_inert(html)
    assert "&lt;img src=x onerror=alert(1)&gt;" in html["diag-sys-procs"]


def test_diagnostics_error_and_missing_cert_path_are_escaped():
    """The catch-all error line and a missing cert's path are rendered as text."""
    _assert_inert(_render("refreshDiagnostics", fail=EVIL)["html"])
    replies = {"/api/diagnostics": {"hub_connection": {"available": True,
                                                       "client_cert": {"exists": False, "path": EVIL}}},
               "/api/system-stats": {}}
    _assert_inert(_render("refreshDiagnostics", replies)["html"])


def test_ollama_model_delete_gets_the_exact_name_without_an_inline_handler():
    """A model name with quotes cannot break out of an onclick; the click passes it verbatim."""
    result = _render("mmRefresh", {"/api/local-llm/models": {"models": [{"name": EVIL, "size_gb": EVIL}]}})
    _assert_inert(result["html"])
    assert "onclick" not in result["html"]["mm-list"]
    assert result["calls"] == [["mmDelete", EVIL]]


def test_user_remove_gets_the_exact_username_without_an_inline_handler():
    """An SSO username with quotes cannot break out of an onclick; the click passes it verbatim."""
    result = _render("loadAuthUsers", {"/api/auth/users": {"users": [{"username": EVIL, "last_login": EVIL}]}})
    _assert_inert(result["html"])
    assert "onclick" not in result["html"]["auth-users-list"]
    assert result["calls"] == [["authDeleteUser", EVIL]]


def test_promote_repo_options_escape_repo_names():
    """Repo names from config land in option values and labels as text."""
    result = _render("loadPromoteRepos", {"/api/promote/repos": {
        "repos": [EVIL], "all_value": EVIL, "default": EVIL}})
    _assert_inert(result["html"])
