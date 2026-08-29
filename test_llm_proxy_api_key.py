"""The LLM router (/v1/*) must require a token, and the token must be settable
from the WebUI without ever being disclosed to the browser.

Why this file exists
--------------------
``/v1/*`` is exempt from the WebUI session middleware (``main._AUTH_EXEMPT_PREFIX``)
and does its own key check, and AppBuilder binds ``0.0.0.0``. ``_authorized`` used
to return **True when no key was configured**, so a default install exposed an LLM
router -- and, with the agentic toggles on, the real fix pipeline -- to anyone who
could route to the host, with only a log line to say so. These tests pin the
fail-closed behaviour so that default can never come back silently.

They also pin the two halves of the Settings plumbing that are easy to get wrong:

* the stored key must never be rendered into the served HTML, and
* because Settings is ONE form and a Save from ANY tab submits every field, a
  blank key input must mean "keep the stored key" -- otherwise saving an
  unrelated setting would silently turn authentication off.

These are written as real ``def test_*`` functions on purpose: this repo's older
``test_*.py`` self-tests are ``__main__`` scripts that ``pytest`` does not collect,
so they never run in CI.

``llm_proxy.py`` and ``routes.py`` cannot be imported directly (they transitively
pull in main.py's circular import chain), so -- per this repo's convention (see
test_llm_proxy_agentic.py / test_feature_settings_roundtrip.py) -- the functions
under test are extracted with ``ast`` and exec'd against stubs.
"""
import ast
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


# ── llm_proxy._authorized ───────────────────────────────────────────────────

def _load_auth_ns(configured_key: str):
    """Exec llm_proxy's key-check helpers with a stubbed config + env."""
    src = open(os.path.join(HERE, "llm_proxy.py")).read()
    tree = ast.parse(src)
    wanted = {"_configured_key", "_presented_key", "_authorized"}
    segs = [ast.get_source_segment(src, n) for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert len(segs) == 3, f"expected 3 helpers, found {len(segs)}"

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    import hmac as _hmac

    class _Env(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    ns = {
        "logger": _NoLog(),
        "hmac": _hmac,
        "os": type("os", (), {"environ": _Env()})(),
        "load_config": lambda: {"llm_proxy_api_key": configured_key},
        "Request": object,
    }
    for seg in segs:
        exec(seg, ns)
    return ns


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_no_configured_key_refuses_every_request():
    """THE regression this file exists for: keyless must mean CLOSED, not open."""
    auth = _load_auth_ns("")["_authorized"]
    assert auth(_Req()) is False, "a keyless proxy must refuse an anonymous request"
    assert auth(_Req({"x-api-key": "anything"})) is False
    assert auth(_Req({"authorization": "Bearer anything"})) is False


def test_configured_key_accepts_only_the_exact_key():
    auth = _load_auth_ns("s3cret-key")["_authorized"]
    assert auth(_Req({"x-api-key": "s3cret-key"})) is True
    assert auth(_Req({"authorization": "Bearer s3cret-key"})) is True
    assert auth(_Req({"x-api-key": "wrong"})) is False
    assert auth(_Req()) is False, "no header must not pass once a key is set"


def test_partial_key_is_rejected():
    """Guards against a prefix/truncation comparison creeping in."""
    auth = _load_auth_ns("s3cret-key")["_authorized"]
    assert auth(_Req({"x-api-key": "s3cret"})) is False
    assert auth(_Req({"x-api-key": "s3cret-key-extra"})) is False
    assert auth(_Req({"x-api-key": ""})) is False


def test_env_var_overrides_stored_config():
    ns = _load_auth_ns("from-config")
    ns["os"].environ["AB_PROXY_KEY"] = "from-env"
    auth = ns["_authorized"]
    assert auth(_Req({"x-api-key": "from-env"})) is True
    assert auth(_Req({"x-api-key": "from-config"})) is False


# ── save_settings: the key must survive a save from another tab ─────────────

def _save_settings_ns():
    from test_feature_settings_roundtrip import _load_ns
    cwd = os.getcwd()
    os.chdir(HERE)
    try:
        return _load_ns()
    finally:
        os.chdir(cwd)


def _run_save(ns, pairs):
    import asyncio
    from test_feature_settings_roundtrip import _FakeRequest
    asyncio.run(ns["save_settings"](_FakeRequest(pairs)))
    return ns["_config_holder"]["config"]


BASE = [("label_mode", "ANY")]


def test_blank_key_field_keeps_the_stored_key():
    """Settings is one form: a Save from ANY tab submits llm_proxy_api_key blank.
    Treating that as "clear" would silently disable API authentication."""
    ns = _save_settings_ns()
    _run_save(ns, BASE + [("llm_proxy_api_key", "initial-key")])
    assert ns["_config_holder"]["config"]["llm_proxy_api_key"] == "initial-key"
    cfg = _run_save(ns, BASE + [("llm_proxy_api_key", "")])
    assert cfg["llm_proxy_api_key"] == "initial-key", "blank submit wiped the key"


def test_absent_key_field_keeps_the_stored_key():
    ns = _save_settings_ns()
    _run_save(ns, BASE + [("llm_proxy_api_key", "initial-key")])
    cfg = _run_save(ns, BASE)
    assert cfg["llm_proxy_api_key"] == "initial-key"


def test_new_value_replaces_the_key():
    ns = _save_settings_ns()
    _run_save(ns, BASE + [("llm_proxy_api_key", "initial-key")])
    cfg = _run_save(ns, BASE + [("llm_proxy_api_key", "rotated-key")])
    assert cfg["llm_proxy_api_key"] == "rotated-key"


def test_sentinel_clears_the_key_explicitly():
    ns = _save_settings_ns()
    _run_save(ns, BASE + [("llm_proxy_api_key", "initial-key")])
    cfg = _run_save(ns, BASE + [("llm_proxy_api_key", "__CLEAR__")])
    assert cfg["llm_proxy_api_key"] == ""


def test_whitespace_is_stripped_not_stored():
    ns = _save_settings_ns()
    cfg = _run_save(ns, BASE + [("llm_proxy_api_key", "  padded-key  ")])
    assert cfg["llm_proxy_api_key"] == "padded-key"


# ── the WebUI control must not disclose the key ─────────────────────────────

def _key_block():
    src = open(os.path.join(HERE, "templates", "index.html")).read()
    m = re.search(r'(<!-- LLM Router API key\..*?</div>\s*\n)'
                  r'(?=\s*<div class="flex items-center gap-3)', src, re.S)
    assert m, "LLM Router API key block not found in index.html"
    return m.group(1)


@pytest.mark.parametrize("configured", [True, False])
def test_stored_key_is_never_rendered_into_the_html(configured):
    from jinja2 import Environment
    out = Environment().from_string(_key_block()).render(
        settings={"llm_proxy_api_key_configured": configured,
                  "llm_proxy_api_key": "SUPER-SECRET-VALUE"})
    assert "SUPER-SECRET-VALUE" not in out, "the proxy key leaked into the HTML"
    assert re.search(r'name="llm_proxy_api_key"[^>]*value=""', out), \
        "the key input must render empty so a blank save means 'keep'"


def test_status_line_tells_the_operator_the_api_is_unprotected():
    from jinja2 import Environment
    env = Environment()
    unset = env.from_string(_key_block()).render(
        settings={"llm_proxy_api_key_configured": False})
    assert "No key" in unset and "refusing all requests" in unset
    setted = env.from_string(_key_block()).render(
        settings={"llm_proxy_api_key_configured": True})
    assert "Key configured" in setted


def test_settings_page_redacts_the_key_into_a_presence_flag():
    """The whole config is merged into the template context via {**settings,
    **config}, so the key must be explicitly overridden after that merge."""
    src = open(os.path.join(HERE, "routes.py")).read()
    ctx = src[src.index('"llm_credentials": _safe_llm_credentials'):][:600]
    assert '"llm_proxy_api_key": ""' in ctx
    assert '"llm_proxy_api_key_configured": bool(config.get("llm_proxy_api_key"))' in ctx
