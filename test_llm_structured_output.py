"""json_schema must reach OpenAI-compatible providers as response_format.

claude_cli enforced the fix schema natively via --json-schema, so a claude_cli
fix response was valid JSON by construction. _call_provider used to forward
json_schema ONLY to claude_cli and silently drop it everywhere else, so once the
fix engine's default provider moved to Copilot the model was merely *asked* in
prose to return JSON — the regression behind the recurring "AI generated invalid
JSON format for the fix." failures.

llm_client.py cannot be imported standalone (circular main/workers imports, and
`tenacity` is not installed in every dev env), so these tests use the codebase's
established AST-extraction pattern.
"""
import ast

import pytest


_WANT_FUNCS = {"_response_format_from_schema", "_post_maybe_structured", "_call_provider",
               "_is_uninformative_4xx_body"}
_WANT_ASSIGNS = {"_UNSUPPORTED_SCHEMA_HINTS", "_OPAQUE_4XX_BODIES"}


def _extract(path, assigns, funcs):
    src = open(path, encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in assigns for t in node.targets):
            segs.append(ast.get_source_segment(src, node))
    return "\n\n".join(segs)


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code, self.text = status_code, text


class _HTTPError(Exception):
    def __init__(self, msg="", response=None):
        super().__init__(msg)
        self.response = response


class _Logger:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return lambda msg, *a: self.calls.append(msg % a if a else msg)
        raise AttributeError(name)


@pytest.fixture
def ns():
    import json as _json
    import types

    requests = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(HTTPError=_HTTPError))
    namespace = {"json": _json, "requests": requests, "logger": _Logger()}
    exec(_extract("llm_client.py", _WANT_ASSIGNS, _WANT_FUNCS), namespace)
    return namespace


# --------------------------------------------------------------------------
# _response_format_from_schema
# --------------------------------------------------------------------------

def test_schema_becomes_openai_response_format(ns):
    schema = {"type": "object", "required": ["edits"]}
    rf = ns["_response_format_from_schema"](schema)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] is schema
    assert rf["json_schema"]["name"]


def test_schema_is_not_strict(ns):
    """strict=True additionally demands additionalProperties:false on every
    object and every property in `required`; AB's schemas don't satisfy that, so
    strict mode would make the API reject the request outright."""
    rf = ns["_response_format_from_schema"]({"type": "object"})
    assert rf["json_schema"]["strict"] is False


def test_json_string_schema_is_parsed(ns):
    rf = ns["_response_format_from_schema"]('{"type": "object"}')
    assert rf["json_schema"]["schema"] == {"type": "object"}


@pytest.mark.parametrize("bad", [None, "", {}, "not json", 42, []])
def test_unusable_schema_yields_no_response_format(ns, bad):
    assert ns["_response_format_from_schema"](bad) is None


# --------------------------------------------------------------------------
# _post_maybe_structured
# --------------------------------------------------------------------------

def _wire_post(ns, behaviour):
    """behaviour(payload) -> response, or raises. Records every payload seen."""
    seen = []

    def _fake(endpoint, payload, headers, config, stream=False, provider="openai"):
        seen.append(payload)
        return behaviour(payload)

    ns["_llm_retry_post"] = _fake
    return seen


def test_response_format_is_sent_when_schema_given(ns):
    seen = _wire_post(ns, lambda p: _Resp())
    ns["_post_maybe_structured"]("u", {"model": "m", "messages": []}, {}, {},
                                 False, "copilot", {"type": "object"})
    assert len(seen) == 1
    assert seen[0]["response_format"]["type"] == "json_schema"


def test_no_schema_means_no_response_format(ns):
    seen = _wire_post(ns, lambda p: _Resp())
    ns["_post_maybe_structured"]("u", {"model": "m"}, {}, {}, False, "copilot", None)
    assert "response_format" not in seen[0]


def test_tool_calls_never_get_response_format(ns):
    """Tool-calling and forced-JSON are mutually exclusive on most backends."""
    seen = _wire_post(ns, lambda p: _Resp())
    ns["_post_maybe_structured"]("u", {"model": "m", "tools": [{"x": 1}]}, {}, {},
                                 False, "copilot", {"type": "object"})
    assert "response_format" not in seen[0]


def test_caller_payload_is_not_mutated(ns):
    _wire_post(ns, lambda p: _Resp())
    payload = {"model": "m", "messages": []}
    ns["_post_maybe_structured"]("u", payload, {}, {}, False, "copilot", {"type": "object"})
    assert "response_format" not in payload


@pytest.mark.parametrize("status,body", [
    (400, '{"error":{"message":"Unsupported parameter: response_format"}}'),
    (400, "model does not support json_schema"),
    (422, "response_format is not supported"),
    (404, "unsupported_api_for_model"),
])
def test_unsupported_response_format_degrades_to_plain_request(ns, status, body):
    """A model that rejects response_format must still get its answer — the old
    behaviour — rather than failing the whole fix attempt."""
    calls = {"n": 0}

    def behaviour(payload):
        calls["n"] += 1
        if "response_format" in payload:
            raise _HTTPError("bad request", response=_Resp(status, body))
        return _Resp(200, "ok")

    seen = _wire_post(ns, behaviour)
    out = ns["_post_maybe_structured"]("u", {"model": "m"}, {}, {}, False, "copilot",
                                       {"type": "object"})
    assert out.status_code == 200
    assert calls["n"] == 2
    assert "response_format" in seen[0] and "response_format" not in seen[1]


def test_unrelated_http_error_is_not_retried(ns):
    """A genuine 400 (e.g. context-length) must propagate, not be masked by a
    silent schema-less retry that would fail identically and cost double."""
    def behaviour(payload):
        raise _HTTPError("ctx", response=_Resp(400, "maximum context length exceeded"))

    seen = _wire_post(ns, behaviour)
    with pytest.raises(_HTTPError):
        ns["_post_maybe_structured"]("u", {"model": "m"}, {}, {}, False, "copilot",
                                     {"type": "object"})
    assert len(seen) == 1


def test_server_error_is_not_swallowed(ns):
    def behaviour(payload):
        raise _HTTPError("boom", response=_Resp(500, "internal"))

    seen = _wire_post(ns, behaviour)
    with pytest.raises(_HTTPError):
        ns["_post_maybe_structured"]("u", {"model": "m"}, {}, {}, False, "copilot",
                                     {"type": "object"})
    assert len(seen) == 1


# --------------------------------------------------------------------------
# _call_provider dispatch — the actual regression site
# --------------------------------------------------------------------------

def _wire_dispatch(ns):
    got = {}

    def _rec(name):
        def _f(*a, **kw):
            got[name] = kw
            return "ok"
        return _f

    ns["_is_copilot"] = lambda p: p == "copilot"
    ns["_is_ollama"] = lambda p: p == "ollama"
    ns["_is_lmstudio"] = lambda p: p == "lmstudio"
    ns["_normalize_lmstudio_url"] = lambda u: u or "http://127.0.0.1:1234/v1"
    ns["OPENROUTER_BASE_URL"] = "https://openrouter.ai/api/v1"
    ns["OPENROUTER_HEADERS"] = {}
    ns["OPENAI_BASE_URL"] = "https://api.openai.com/v1"
    for fn in ("_request_copilot", "_request_openai", "_request_anthropic",
               "_request_google", "_request_ollama", "_request_claude_cli"):
        ns[fn] = _rec(fn)
    return got


@pytest.mark.parametrize("provider,expected", [
    ("copilot", "_request_copilot"),
    ("openai", "_request_openai"),
    ("groq", "_request_openai"),
    ("openrouter", "_request_openai"),
    ("lmstudio", "_request_openai"),
])
def test_openai_compatible_providers_receive_json_schema(ns, provider, expected):
    """The regression: every one of these used to drop json_schema on the floor."""
    got = _wire_dispatch(ns)
    schema = {"type": "object"}
    ns["_call_provider"](provider, "m", "k", None, [], None, False, "t", {},
                         json_schema=schema)
    assert got[expected].get("json_schema") is schema


def test_claude_cli_still_receives_json_schema(ns):
    got = _wire_dispatch(ns)
    schema = {"type": "object"}
    ns["_call_provider"]("claude_cli", "m", "k", None, [], None, False, "t", {},
                         json_schema=schema)
    assert got["_request_claude_cli"].get("json_schema") is schema


@pytest.mark.parametrize("provider,expected", [
    ("anthropic", "_request_anthropic"),
    ("google", "_request_google"),
    ("ollama", "_request_ollama"),
])
def test_providers_without_a_structured_path_are_unchanged(ns, provider, expected):
    """These have no response_format equivalent wired up; passing json_schema
    would be a TypeError. They must keep ignoring it."""
    got = _wire_dispatch(ns)
    ns["_call_provider"](provider, "m", "k", None, [], None, False, "t", {},
                         json_schema={"type": "object"})
    assert "json_schema" not in got[expected]


# --------------------------------------------------------------------------
# Opaque 4xx bodies — ab#263
# --------------------------------------------------------------------------
# Copilot's /chat/completions answers some response_format rejections with the
# bare body "Bad Request\n". It names no parameter, no code and no model, so it
# matched none of _UNSUPPORTED_SCHEMA_HINTS and the schema-free retry — the
# exact request AB sent successfully before response_format existed — was never
# attempted. Every such call failed outright and the self-log scan filed each
# one as a fresh issue.

@pytest.mark.parametrize("opaque", ["Bad Request\n", "bad request", "", "   ",
                                    "400 Bad Request", "Client Error."])
def test_opaque_4xx_body_degrades_to_plain_request(ns, opaque):
    calls = {"n": 0}

    def behaviour(payload):
        calls["n"] += 1
        if "response_format" in payload:
            raise _HTTPError("bad", response=_Resp(400, opaque))
        return _Resp()

    seen = _wire_post(ns, behaviour)
    out = ns["_post_maybe_structured"]("u", {"model": "m"}, {}, {}, False, "copilot",
                                       {"type": "object"})
    assert out.status_code == 200
    assert calls["n"] == 2
    assert "response_format" in seen[0] and "response_format" not in seen[1]


@pytest.mark.parametrize("informative", [
    "maximum context length exceeded",
    "you exceeded your current quota",
    '{"error":{"code":"context_length_exceeded"}}',
    '{"error":{"message":"too many tokens"}}',
    '{"message":"repository not found"}',
])
def test_informative_4xx_body_is_still_not_retried(ns, informative):
    """The opaque-body allowance must not become a blanket retry-on-400: a body
    that states a reason would fail identically the second time."""
    def behaviour(payload):
        raise _HTTPError("nope", response=_Resp(400, informative))

    seen = _wire_post(ns, behaviour)
    with pytest.raises(_HTTPError):
        ns["_post_maybe_structured"]("u", {"model": "m"}, {}, {}, False, "copilot",
                                     {"type": "object"})
    assert len(seen) == 1


def test_opaque_body_on_a_5xx_is_not_retried(ns):
    """Only 400/404/422 carry the 'you sent something I don't accept' meaning;
    an opaque 500 is a server fault and must surface."""
    def behaviour(payload):
        raise _HTTPError("boom", response=_Resp(500, "Bad Request\n"))

    seen = _wire_post(ns, behaviour)
    with pytest.raises(_HTTPError):
        ns["_post_maybe_structured"]("u", {"model": "m"}, {}, {}, False, "copilot",
                                     {"type": "object"})
    assert len(seen) == 1


@pytest.mark.parametrize("body,want", [
    ("Bad Request\n", True), ("", True), ("400", True),
    ("maximum context length exceeded", False),
    ('{"error":{"message":"x"}}', False),
    ('{"error":{}}', True),
    ('{"error":null}', True),
])
def test_uninformative_body_classifier(ns, body, want):
    assert ns["_is_uninformative_4xx_body"](body) is want
