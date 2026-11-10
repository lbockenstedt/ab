#!/usr/bin/env python3
"""The Copilot chat path must send `max_completion_tokens`, never `max_tokens`.

OpenAI deprecated `max_tokens` for chat completions, and the newer Copilot-hosted
GPT models now reject it. `gpt-5.4` answers a payload carrying `max_tokens` with a
bare HTTP 400 -- empty body, no error code, no message -- while the byte-identical
payload using `max_completion_tokens` returns 200.

That silent 400 was expensive. It read as "this model is broken" rather than "this
field is wrong", so every OpenAI-family model looked unusable. Those models are the
only non-Anthropic members of the review-panel allowlist, so the cross-vendor
reviewer rule had nothing to select and relaxed to a same-vendor panel on every PR
-- the exact failure mode that rule exists to prevent, arriving silently.

Verified against the live Copilot catalogue before the change: every chat-capable
model served there (claude-*, gemini-*, gpt-3.5 through gpt-5.4, gpt-5-mini)
accepts `max_completion_tokens`, so this is strictly wider compatibility.

llm_client.py imports `main` (app-init side effects), so these assertions read the
function sources via ast rather than importing the module -- the established
pattern in the other llm_client tests.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_client.py")


def _func_source(name):
    src = open(_SRC_PATH).read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError("%s not found in llm_client.py" % name)


def _payload_keys(name):
    """Every string literal assigned into `payload[...]` or present in a payload
    dict literal, so the assertions below test the request actually built rather
    than merely the presence of a word in a comment."""
    src = _func_source(name)
    keys = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # payload["x"] = ...
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == "payload" and isinstance(t.slice, ast.Constant)):
                    keys.add(t.slice.value)
        # payload = {"x": ...}
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "payload":
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant):
                            keys.add(k.value)
    return keys


def test_copilot_sends_max_completion_tokens():
    keys = _payload_keys("_request_copilot")
    assert "max_completion_tokens" in keys, (
        "the Copilot payload must cap output with max_completion_tokens; found %s" % sorted(keys))


def test_copilot_never_sends_max_tokens():
    """The regression itself: gpt-5.4 400s on this field with an empty body."""
    keys = _payload_keys("_request_copilot")
    assert "max_tokens" not in keys, (
        "max_tokens is rejected by newer Copilot GPT models with a bare 400 — "
        "use max_completion_tokens")


def test_generic_openai_path_still_sends_max_tokens():
    """_request_openai also serves ollama and LM Studio, which only understand
    the old spelling, so the fix must NOT be applied there."""
    keys = _payload_keys("_request_openai")
    assert "max_tokens" in keys
    assert "max_completion_tokens" not in keys


def test_anthropic_path_still_sends_max_tokens():
    """The native Messages API requires max_tokens and has no such field."""
    keys = _payload_keys("_request_anthropic")
    assert "max_tokens" in keys
    assert "max_completion_tokens" not in keys


if __name__ == "__main__":
    test_copilot_sends_max_completion_tokens()
    test_copilot_never_sends_max_tokens()
    test_generic_openai_path_still_sends_max_tokens()
    test_anthropic_path_still_sends_max_tokens()
    print("all passed")
