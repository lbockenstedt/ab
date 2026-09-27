#!/usr/bin/env python3
"""Copilot Responses API path: the only way to reach models GitHub has moved off
/chat/completions.

The live catalogue now lists gpt-5.5 and all three gpt-5.6 variants with
supported_endpoints == ['/responses', 'ws:/responses'] -- no /chat/completions at
all. Calling the old endpoint on one of them returns 400 unsupported_api_for_model,
which at the call site is indistinguishable from "this model is broken", so all
four looked dead. They are not; they just speak a different shape.

Three shape differences are load-bearing and each one silently breaks the call if
got wrong:
  * system prompts leave the message list and become a top-level `instructions`
  * message text is wrapped in a typed content part whose type depends on role --
    "output_text" for assistant turns, "input_text" for everything else
  * `output` is a LIST OF ITEMS, not a message: reasoning models emit
    {"type": "reasoning"} items first, and the `output_text` convenience field is
    routinely null, so a naive reader sees an empty completion and treats a good
    answer as a refusal

Routing is catalogue-driven rather than name-matched on purpose: which models sit
on which endpoint is a server-side decision that keeps moving, and a hardcoded
list is exactly how gpt-5.6-sol broke silently in the first place.

llm_client.py imports `main` (app-init side effects), so this extracts the pure
functions via ast and execs them, following the pattern in the other llm_client
tests.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_client.py")
_WANT = ("_to_responses_input", "_responses_output_text", "_usage_from_responses_json",
         "_copilot_wants_responses_api")


def _load():
    src = open(_SRC).read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT:
            segs.append(ast.get_source_segment(src, node))
    assert len(segs) == len(_WANT), "expected %d functions, found %d" % (len(_WANT), len(segs))
    ns = {}
    exec(compile("\n\n".join(segs), "llm_client.py", "exec"), ns)
    return ns


NS = _load()
_to_responses_input = NS["_to_responses_input"]
_responses_output_text = NS["_responses_output_text"]
_usage_from_responses_json = NS["_usage_from_responses_json"]


# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------

def test_system_prompt_becomes_instructions_not_a_message():
    instructions, items = _to_responses_input([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ])
    assert instructions == "be terse"
    assert [i["role"] for i in items] == ["user"], "system must not remain in the input list"


def test_multiple_system_prompts_are_joined():
    instructions, _ = _to_responses_input([
        {"role": "system", "content": "a"},
        {"role": "system", "content": "b"},
        {"role": "user", "content": "hi"},
    ])
    assert instructions == "a\n\nb"


def test_no_system_prompt_yields_none_not_empty_string():
    """`instructions: ""` is a different request from omitting the field."""
    instructions, _ = _to_responses_input([{"role": "user", "content": "hi"}])
    assert instructions is None


def test_assistant_turns_use_output_text_and_user_turns_input_text():
    """The API rejects the wrong part type; this is the easiest thing to get
    backwards and the failure is a 400 with no useful message."""
    _, items = _to_responses_input([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "q2"},
    ])
    assert [i["content"][0]["type"] for i in items] == ["input_text", "output_text", "input_text"]


def test_none_content_becomes_empty_string():
    _, items = _to_responses_input([{"role": "user", "content": None}])
    assert items[0]["content"][0]["text"] == ""


def test_tool_messages_are_dropped():
    _, items = _to_responses_input([
        {"role": "user", "content": "q"},
        {"role": "tool", "content": "result", "tool_call_id": "x"},
    ])
    assert [i["role"] for i in items] == ["user"]


def test_non_dict_messages_are_ignored_rather_than_raising():
    _, items = _to_responses_input(["junk", None, {"role": "user", "content": "q"}])
    assert len(items) == 1


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def test_reasoning_items_are_skipped_and_message_text_returned():
    """The exact live shape: a reasoning item precedes the message, and the
    output_text shortcut is null."""
    data = {"status": "completed", "output_text": None, "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "APPROVE"}]},
    ]}
    assert _responses_output_text(data) == "APPROVE"


def test_output_text_shortcut_is_preferred_when_present():
    assert _responses_output_text({"output_text": "quick", "output": []}) == "quick"


def test_multiple_text_parts_are_concatenated():
    data = {"output": [{"type": "message",
                        "content": [{"type": "output_text", "text": "a"},
                                    {"type": "output_text", "text": "b"}]}]}
    assert _responses_output_text(data) == "ab"


def test_malformed_bodies_return_empty_string_and_never_raise():
    for junk in ({}, {"output": None}, {"output": ["x", 3, None]},
                 {"output": [{"type": "message", "content": "notalist"}]},
                 None, "string", []):
        assert _responses_output_text(junk) == ""


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------

def test_usage_uses_responses_field_names():
    """Responses says input_tokens/output_tokens where chat/completions says
    prompt_tokens/completion_tokens; reading the wrong pair records zero cost."""
    out = {}
    _usage_from_responses_json({"usage": {"input_tokens": 28, "output_tokens": 7}}, out)
    assert out == {"input_tokens": 28, "output_tokens": 7, "source": "api"}


def test_usage_is_never_fatal():
    out = {}
    _usage_from_responses_json({"usage": "broken"}, out)
    _usage_from_responses_json({}, out)
    assert out == {}
    _usage_from_responses_json({"usage": {"input_tokens": 1}}, None)  # must not raise


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _wants(endpoints):
    ns = dict(NS)
    ns["_copilot_model_endpoints"] = lambda bearer, model: endpoints
    src = ast.get_source_segment(open(_SRC).read(), next(
        n for n in ast.parse(open(_SRC).read()).body
        if isinstance(n, ast.FunctionDef) and n.name == "_copilot_wants_responses_api"))
    exec(compile(src, "llm_client.py", "exec"), ns)
    return ns["_copilot_wants_responses_api"]("bearer", "some-model")


def test_routes_to_responses_only_when_chat_completions_is_absent():
    assert _wants(["/responses", "ws:/responses"]) is True


def test_stays_on_chat_completions_when_the_model_supports_it():
    assert _wants(["/responses", "/chat/completions", "ws:/responses"]) is False
    assert _wants(["/v1/messages", "/chat/completions"]) is False


def test_unknown_catalogue_falls_back_to_chat_completions():
    """A catalogue hiccup must not become a total outage -- an unknown model
    stays on the endpoint that works for almost everything."""
    assert _wants(None) is False
    assert _wants([]) is False
