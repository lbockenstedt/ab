"""A reviewer that spends its whole tool budget must still be asked for a verdict.

_run_reviewer_turn drives a bounded tool-calling loop (fetch_repo_file). The loop
returned `last_text` when it ran out of iterations — but `last_text` is the
narration that came ALONGSIDE the model's final tool call, not an answer. The
model was still mid-investigation; it had never been asked to conclude.

Live on LM-AB this produced exactly two shapes, both logged as "returned no
parseable verdict after retry":

    Reviewer (copilot/claude-opus-5.5) ... raw response: "Key uncertainties: whether `_console...
    Reviewer (copilot/claude-opus-5)   ... raw response: ''

Those reviewers were then dropped from the panel, which pushed the panel's
verdict/confidence around and kept auto-remediation cycling on PRs whose diffs
were big enough to need several file fetches (cs#143, lm#1047, qa#16). A direct
probe of the same models with a small prompt returned clean JSON every time, so
the models were never the problem — the loop just never let them answer.

The loop now makes one final tools-free turn telling the reviewer its budget is
gone and it must answer from what it has seen.

Also pins the two budgets against drifting back apart: _REVIEW_TOOL_MAX_ITER was
3 while _REVIEW_TOOL_MAX_FILES was 5, so a reviewer fetching one file per turn
ran out of TURNS after 2 files and could never reach the file budget it was told
it had.

fix_engine imports main (which boots workers), so this uses the codebase's
AST-extraction pattern rather than importing the module.
"""
import ast
import json
import re

import pytest


SRC_PATH = "fix_engine.py"


def _extract(names):
    src = open(SRC_PATH, encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            segs.append(ast.get_source_segment(src, node))
    return "\n\n".join(segs)


def _constant(name):
    """Read an int module constant without importing fix_engine."""
    src = open(SRC_PATH, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


class _Logger:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return lambda msg, *a: self.calls.append(msg % a if a else msg)
        raise AttributeError(name)


def _build(dispatch_results):
    """Exec _run_reviewer_turn in a synthetic namespace.

    dispatch_results: list consumed one per _dispatch call. Each entry is either
    an Exception (raised) or the value returned.
    """
    ns = {
        "json": json,
        "re": re,
        "logger": _Logger(),
        "load_config": lambda: {},
        "_REVIEW_TOOLS": [{"type": "function", "function": {"name": "fetch_repo_file"}}],
        "_REVIEW_TOOL_MAX_ITER": _constant("_REVIEW_TOOL_MAX_ITER"),
        "_REVIEW_TOOL_MAX_FILES": _constant("_REVIEW_TOOL_MAX_FILES"),
        "_REVIEW_FILE_MAX_CHARS": 20000,
        "_REVIEWER_JSON_SCHEMA": {},
        "_DIFF_FILE_HEADER_RE": re.compile(r"\+\+\+ b/(\S+)"),
        "_parse_review_text_tool_calls": lambda t: (t, []),
        "_fetch_repo_file_for_review": lambda *a, **k: {"content": "x = 1"},
        "call_llm": lambda *a, **k: "",
    }

    calls = []

    class _FakeLlmClient:
        @staticmethod
        def _try_candidate(candidate, messages, tools, _flag, task_id, config, **kwargs):
            calls.append({"messages": [dict(m) for m in messages], "tools": tools})
            nxt = dispatch_results[len(calls) - 1]
            if isinstance(nxt, Exception):
                return None, nxt
            return nxt, None

    ns["llm_client"] = _FakeLlmClient
    exec(_extract({"_run_reviewer_turn", "_extract_reviewer_verdict", "_parse_reviewer_json",
                   "_has_verdict_key", "_canon_verdict_keys", "_balanced_brace_span"}),
         ns)  # noqa: S102 — repo-standard test pattern
    return ns["_run_reviewer_turn"], calls


def _tool_turn(text):
    """A turn that narrates and then asks for a file — never a verdict."""
    return {"text": text, "tool_calls": [
        {"id": "c1", "function": {"name": "fetch_repo_file",
                                  "arguments": json.dumps({"path": "a.py"})}}]}


VERDICT = '{"confidence": 0.82, "verdict": "Reject", "critique": "unverified path"}'


def _run(dispatch_results):
    fn, calls = _build(dispatch_results)
    out = fn("diff", "sys", {"provider": "copilot", "model": "claude-opus-5.5"},
             "task", object(), "abc123")
    return out, calls


def test_budget_exhausted_still_returns_a_verdict():
    """The exact live failure: every turn wants another file, budget runs out."""
    iters = _constant("_REVIEW_TOOL_MAX_ITER")
    results = [_tool_turn("Key uncertainties: whether `_console_probe` exists")] * iters
    results.append({"text": VERDICT, "tool_calls": []})
    out, calls = _run(results)
    assert json.loads(out)["verdict"] == "Reject"
    assert len(calls) == iters + 1, "expected one extra tools-free wrap-up turn"


def test_wrapup_turn_is_sent_without_tools():
    """It must be tools-free, or the model can just ask for another file again."""
    iters = _constant("_REVIEW_TOOL_MAX_ITER")
    results = [_tool_turn("narration")] * iters + [{"text": VERDICT, "tool_calls": []}]
    _, calls = _run(results)
    assert calls[-1]["tools"] is None
    assert all(c["tools"] is not None for c in calls[:-1])


def test_wrapup_prompt_demands_json_and_forbids_more_files():
    iters = _constant("_REVIEW_TOOL_MAX_ITER")
    results = [_tool_turn("narration")] * iters + [{"text": VERDICT, "tool_calls": []}]
    _, calls = _run(results)
    last = calls[-1]["messages"][-1]
    assert last["role"] == "user"
    body = last["content"].lower()
    assert "verdict" in body and "confidence" in body
    assert "do not ask for more files" in body


def test_empty_wrapup_falls_back_to_last_text():
    """claude-opus-5 returned '' live — never return None/blow up."""
    iters = _constant("_REVIEW_TOOL_MAX_ITER")
    results = [_tool_turn("narration")] * iters + [{"text": "", "tool_calls": []}]
    out, _ = _run(results)
    assert out == "narration"


def test_wrapup_missing_text_key_does_not_raise():
    """A dict without 'text' must not hit None.strip() — the defect in the
    first generated version of this fix."""
    iters = _constant("_REVIEW_TOOL_MAX_ITER")
    results = [_tool_turn("narration")] * iters + [{"tool_calls": []}]
    out, _ = _run(results)
    assert out == "narration"


def test_wrapup_exception_falls_back_to_last_text():
    iters = _constant("_REVIEW_TOOL_MAX_ITER")
    results = [_tool_turn("narration")] * iters + [RuntimeError("provider down")]
    out, _ = _run(results)
    assert out == "narration"


def test_early_verdict_makes_no_extra_call():
    """The common case must not pay for the wrap-up turn."""
    out, calls = _run([{"text": VERDICT, "tool_calls": []}])
    assert json.loads(out)["verdict"] == "Reject"
    assert len(calls) == 1


def test_verdict_after_one_fetch_makes_no_extra_call():
    out, calls = _run([_tool_turn("let me check"), {"text": VERDICT, "tool_calls": []}])
    assert json.loads(out)["verdict"] == "Reject"
    assert len(calls) == 2


def test_iteration_budget_can_reach_the_file_budget():
    """A reviewer told it may fetch N files must get enough turns to fetch them
    AND still answer. At ITER=3 / FILES=5 it ran out of turns after 2 files."""
    iters = _constant("_REVIEW_TOOL_MAX_ITER")
    files = _constant("_REVIEW_TOOL_MAX_FILES")
    assert iters >= files + 1, (
        f"_REVIEW_TOOL_MAX_ITER ({iters}) must allow _REVIEW_TOOL_MAX_FILES "
        f"({files}) one-file-per-turn fetches plus a final answer turn")


def test_non_dict_dispatch_result_short_circuits():
    """Providers that return a bare string (no tools support) are unaffected."""
    out, calls = _run([VERDICT])
    assert json.loads(out)["verdict"] == "Reject"
    assert len(calls) == 1


# --- narration-only stop -------------------------------------------------
#
# The second half of the live defect. These turns make NO tool call, so the
# loop's `if not tool_calls: return text` returned the prose immediately and
# never reached the wrap-up. Observed on LM-AB after the first fix shipped:
#
#   raw response: 'Need to verify the endpoints and port in api_server.py.'
#   raw response: 'Key uncertainties: `_console_creds_for_tenant` exists? ...'

NARRATION = "Need to verify the endpoints and port in api_server.py."


def test_narration_without_tool_call_still_returns_a_verdict():
    out, calls = _run([{"text": NARRATION, "tool_calls": []},
                       {"text": VERDICT, "tool_calls": []}])
    assert json.loads(out)["verdict"] == "Reject"
    assert len(calls) == 2
    assert calls[-1]["tools"] is None, "wrap-up must be tools-free"


def test_narration_stop_asks_for_the_verdict():
    _, calls = _run([{"text": NARRATION, "tool_calls": []},
                     {"text": VERDICT, "tool_calls": []}])
    body = calls[-1]["messages"][-1]["content"].lower()
    assert "do not ask for more files" in body
    assert "verdict" in body


def test_narration_is_kept_in_context_for_the_wrapup():
    """The model should see its own half-finished reasoning when concluding."""
    _, calls = _run([{"text": NARRATION, "tool_calls": []},
                     {"text": VERDICT, "tool_calls": []}])
    roles = [(m["role"], m.get("content")) for m in calls[-1]["messages"]]
    assert ("assistant", NARRATION) in roles


def test_empty_stop_with_no_tool_call_reaches_wrapup():
    """The '' case must not be mistaken for a finished answer."""
    out, calls = _run([{"text": "", "tool_calls": []},
                       {"text": VERDICT, "tool_calls": []}])
    assert json.loads(out)["verdict"] == "Reject"
    assert len(calls) == 2


def test_narration_wrapup_failure_falls_back_to_narration():
    out, _ = _run([{"text": NARRATION, "tool_calls": []}, RuntimeError("down")])
    assert out == NARRATION


def test_verdict_with_surrounding_prose_is_accepted_without_a_wrapup():
    """Don't burn an extra call when the verdict is merely wrapped in commentary."""
    wrapped = f"Here is my assessment:\n```json\n{VERDICT}\n```\nHappy to expand."
    out, calls = _run([{"text": wrapped, "tool_calls": []}])
    assert len(calls) == 1, "a parseable verdict must return immediately"
    assert out == wrapped
