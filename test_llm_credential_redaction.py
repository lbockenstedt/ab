#!/usr/bin/env python3
"""Self-test for the LLM credential/entry api_key disclosure fix.

Run:  python3 bugfixer/test_llm_credential_redaction.py

routes.py cannot be imported directly (main.py's app-init side effects — see
test_dismiss_background_retry.py's docstring), so this extracts the specific
pieces via ast: the two redaction statements from inside settings_page's body
(a statement-level extraction, not a whole-function one — settings_page has
far more dependencies than this fix touches), plus the full save_llm_credential
and get_llm_config functions (small, self-contained).

Regression guard: llm_credentials/llm_entries used to flow into the Settings
page (via the wholesale **config Jinja merge) and into GET /api/llm/config
with plaintext api_key values intact — readable via view-source or a raw API
call, no auth bypass needed beyond what already loads the page. This pins
that every path a browser can reach returns has_key/configured flags only,
and that omitting api_key from a save payload preserves (never wipes) the
existing stored key.
"""
import ast
import asyncio
import sys


def _extract_redaction_source(src):
    """The two redaction Assign statements live INSIDE settings_page's body
    (a much larger function than this fix touches) — pull just their source
    text out, unexecuted, so the caller can exec() it fresh with its own
    `config` already bound instead of needing the whole route's dependencies
    (GitHub repo fetch, DEFAULT_ENV, os.getenv, state, templates, ...)."""
    tree = ast.parse(src)
    lines = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "settings_page":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign):
                    for t in inner.targets:
                        if getattr(t, "id", "") in ("_safe_llm_credentials", "_safe_llm_entries"):
                            lines.append(ast.get_source_segment(src, inner))
    assert len(lines) == 2, f"expected 2 redaction statements in settings_page, found {len(lines)}"
    return "\n".join(lines)


def _load_ns():
    src = open("routes.py").read()
    tree = ast.parse(src)

    segs = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
                "save_llm_credential", "get_llm_config", "update_llm_entry"):
            segs.append(ast.get_source_segment(src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    class _JSONResponse:
        def __init__(self, status_code=200, content=None):
            self.status_code = status_code
            self.content = content

    class _FakeRequest:
        def __init__(self, payload):
            self._payload = payload
        async def json(self):
            return self._payload

    _config_holder = {"config": {}}

    def load_config():
        return dict(_config_holder["config"])

    def save_config(cfg):
        _config_holder["config"] = dict(cfg)

    ns = {
        "logger": _NoLog(), "load_config": load_config, "save_config": save_config,
        "JSONResponse": _JSONResponse, "Request": _FakeRequest,
    }
    exec("\n\n".join(segs), ns)
    ns["_FakeRequest"] = _FakeRequest
    ns["_config_holder"] = _config_holder
    ns["_redaction_source"] = _extract_redaction_source(src)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


SECRET = "sk-super-secret-plaintext-key-12345"


def main():
    ok = True
    ns = _load_ns()

    # ── settings_page's redaction (the Jinja-render path) — real extracted
    # source, exec'd fresh with a `config` we control. ─────────────────────
    config = {
        "llm_credentials": {"openai": {"api_key": SECRET, "base_url": "https://api.openai.com"}},
        "llm_entries": [
            {"id": "e1", "label": "x", "provider": "ollama", "model": "qwen", "rpm": 0,
             "base_url": "", "api_key": SECRET, "escalation_models": ""},
            {"id": "e2", "label": "y", "provider": "openai", "model": "gpt-4", "rpm": 0,
             "base_url": "", "api_key": "", "escalation_models": ""},
        ],
    }
    exec_ns = {"config": config}
    exec(ns["_redaction_source"], exec_ns)

    ok &= _check("settings_page redaction: credential api_key is never the real secret",
                SECRET not in str(exec_ns["_safe_llm_credentials"]))
    ok &= _check("settings_page redaction: credential has_key=True when a key is stored",
                exec_ns["_safe_llm_credentials"]["openai"]["has_key"] is True)
    ok &= _check("settings_page redaction: base_url is preserved (not sensitive)",
                exec_ns["_safe_llm_credentials"]["openai"]["base_url"] == "https://api.openai.com")
    ok &= _check("settings_page redaction: entries never carry the real secret",
                SECRET not in str(exec_ns["_safe_llm_entries"]))
    ok &= _check("settings_page redaction: entry has_key=True when a key is stored",
                exec_ns["_safe_llm_entries"][0]["has_key"] is True)
    ok &= _check("settings_page redaction: entry has_key=False when no key is stored",
                exec_ns["_safe_llm_entries"][1]["has_key"] is False)
    ok &= _check("settings_page redaction: entry's other fields (model/provider/label) survive intact",
                exec_ns["_safe_llm_entries"][0]["model"] == "qwen"
                and exec_ns["_safe_llm_entries"][0]["provider"] == "ollama"
                and exec_ns["_safe_llm_entries"][0]["label"] == "x")

    # ── empty credentials/entries don't crash the redaction ─────────────────
    exec_ns2 = {"config": {}}
    exec(ns["_redaction_source"], exec_ns2)
    ok &= _check("settings_page redaction: empty config -> empty redacted structures, no crash",
                exec_ns2["_safe_llm_credentials"] == {} and exec_ns2["_safe_llm_entries"] == [])

    # ── GET /api/llm/config ─────────────────────────────────────────────────
    ns["_config_holder"]["config"] = config
    result = _run(ns["get_llm_config"]())
    ok &= _check("get_llm_config: never returns the real secret anywhere in the response",
                SECRET not in str(result))
    ok &= _check("get_llm_config: credentials use 'configured', entries use 'has_key' (both True)",
                result["credentials"]["openai"]["configured"] is True
                and result["entries"][0]["has_key"] is True)
    ok &= _check("get_llm_config: entry without a key reports has_key=False",
                result["entries"][1]["has_key"] is False)

    # ── save_llm_credential: omitting api_key preserves the existing one ────
    ns["_config_holder"]["config"] = {"llm_credentials": {"openai": {"api_key": SECRET, "base_url": "old-url"}}}
    _run(ns["save_llm_credential"](ns["_FakeRequest"]({"provider": "openai", "base_url": "new-url"})))
    saved = ns["_config_holder"]["config"]["llm_credentials"]["openai"]
    ok &= _check("save_llm_credential: api_key omitted from payload -> existing key PRESERVED",
                saved["api_key"] == SECRET)
    ok &= _check("save_llm_credential: base_url is still updated even when api_key is omitted",
                saved["base_url"] == "new-url")

    # ── save_llm_credential: an explicit new key replaces the old one ───────
    ns["_config_holder"]["config"] = {"llm_credentials": {"openai": {"api_key": SECRET, "base_url": "u"}}}
    _run(ns["save_llm_credential"](ns["_FakeRequest"]({"provider": "openai", "api_key": "new-key", "base_url": "u"})))
    ok &= _check("save_llm_credential: an explicit new api_key in the payload DOES replace the old one",
                ns["_config_holder"]["config"]["llm_credentials"]["openai"]["api_key"] == "new-key")

    # ── save_llm_credential: an explicit empty string clears the key ────────
    ns["_config_holder"]["config"] = {"llm_credentials": {"openai": {"api_key": SECRET, "base_url": "u"}}}
    _run(ns["save_llm_credential"](ns["_FakeRequest"]({"provider": "openai", "api_key": "", "base_url": "u"})))
    ok &= _check("save_llm_credential: an explicit empty api_key in the payload CLEARS the stored key",
                ns["_config_holder"]["config"]["llm_credentials"]["openai"]["api_key"] == "")

    # ── save_llm_credential: brand-new provider (no existing entry) works ───
    ns["_config_holder"]["config"] = {}
    _run(ns["save_llm_credential"](ns["_FakeRequest"]({"provider": "anthropic", "api_key": "brand-new", "base_url": ""})))
    ok &= _check("save_llm_credential: first-time save for a provider with no prior entry works",
                ns["_config_holder"]["config"]["llm_credentials"]["anthropic"]["api_key"] == "brand-new")

    # ── update_llm_entry: pre-existing "only overwrite if key present" logic
    # — unmodified by this fix, but now load-bearing (the JS only sends
    # api_key at all when the operator actually typed into the field). ─────
    ns["_config_holder"]["config"] = {"llm_entries": [
        {"id": "e1", "label": "x", "provider": "ollama", "model": "qwen", "rpm": 0,
         "base_url": "", "api_key": SECRET, "escalation_models": ""}]}
    _run(ns["update_llm_entry"]("e1", ns["_FakeRequest"]({"label": "renamed"})))
    ok &= _check("update_llm_entry: api_key omitted from payload -> existing key PRESERVED",
                ns["_config_holder"]["config"]["llm_entries"][0]["api_key"] == SECRET)
    ok &= _check("update_llm_entry: fields present in the payload (label) still update",
                ns["_config_holder"]["config"]["llm_entries"][0]["label"] == "renamed")

    ns["_config_holder"]["config"] = {"llm_entries": [
        {"id": "e1", "label": "x", "provider": "ollama", "model": "qwen", "rpm": 0,
         "base_url": "", "api_key": SECRET, "escalation_models": ""}]}
    _run(ns["update_llm_entry"]("e1", ns["_FakeRequest"]({"api_key": "new-key"})))
    ok &= _check("update_llm_entry: an explicit new api_key in the payload DOES replace the old one",
                ns["_config_holder"]["config"]["llm_entries"][0]["api_key"] == "new-key")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running LLM credential/entry redaction self-test...")
    sys.exit(main())
