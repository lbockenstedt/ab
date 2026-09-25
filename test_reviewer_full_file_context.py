"""Tool-capable reviewers must be handed the touched files, like tools-blind ones.

_run_reviewer_turn has two context strategies. The tools-blind path proactively
embedded the FULL text of every file the diff touches. The tool-capable path
embedded nothing and made the reviewer spend fetch calls rediscovering it.

Measured on LM-AB across 98 reviewed PRs: 9 of 35 panel-1 Rejects (26%) cited
something the reviewer could not verify rather than an actual defect, and every
one of those PRs had zero findings, errors, warnings and advisories. On lm#1047
claude-opus-5.5 said so outright:

    "I could not verify two key dependencies before the tool budget ran out,
     and that is the main reason for the Reject."

Its two blockers were whether `ipaddress` was imported in threat_monitor.py and
whether `_console_creds_for_tenant` existed in console.py. Both files were
touched by the diff, so both were answerable from context that already existed —
and claude-opus-5, reviewing the SAME diff on the SAME panel, confirmed both
were fine.

Both paths now share _full_file_context.

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


class _Logger:
    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return lambda *a, **k: None
        raise AttributeError(name)


VERDICT = '{"confidence": 0.9, "verdict": "Approve", "critique": "ok"}'

THREAT_MONITOR = "import ipaddress\nimport time\n\ndef block_manual(ip):\n    ...\n"
CONSOLE = "def _console_creds_for_tenant(t):\n    return set()\n"

DIFF = (
    "Review this diff.\n"
    "--- a/core/src/threat_monitor.py\n+++ b/core/src/threat_monitor.py\n"
    "@@\n+    ipaddress.ip_address(ip)\n"
    "--- a/core/src/console.py\n+++ b/core/src/console.py\n"
    "@@\n+    own = _console_creds_for_tenant(t)\n"
)

FILES = {
    "core/src/threat_monitor.py": THREAT_MONITOR,
    "core/src/console.py": CONSOLE,
}


def _build(dispatch_results, fetch=None, fetch_log=None):
    ns = {
        "json": json,
        "re": re,
        "logger": _Logger(),
        "load_config": lambda: {},
        "_REVIEW_TOOLS": [{"type": "function", "function": {"name": "fetch_repo_file"}}],
        "_REVIEW_TOOL_MAX_ITER": 7,
        "_REVIEW_TOOL_MAX_FILES": 5,
        "_REVIEW_FILE_MAX_CHARS": 20000,
        "_REVIEWER_JSON_SCHEMA": {},
        "_DIFF_FILE_HEADER_RE": re.compile(r"\+\+\+ b/(\S+)"),
        "_parse_review_text_tool_calls": lambda t: (t, []),
        "call_llm": lambda *a, **k: "",
    }

    def _default_fetch(repo, head_sha, path, **kw):
        if fetch_log is not None:
            fetch_log.append(path)
        if path in FILES:
            return {"content": FILES[path]}
        return {"error": "not found"}

    ns["_fetch_repo_file_for_review"] = fetch or _default_fetch

    calls = []

    class _FakeLlmClient:
        @staticmethod
        def _try_candidate(candidate, messages, tools, _f, task_id, config, **kw):
            calls.append({"messages": [dict(m) for m in messages], "tools": tools})
            nxt = dispatch_results[len(calls) - 1]
            if isinstance(nxt, Exception):
                return None, nxt
            return nxt, None

    ns["llm_client"] = _FakeLlmClient
    exec(_extract({"_run_reviewer_turn", "_full_file_context", "_extract_reviewer_verdict",
                   "_parse_reviewer_json", "_has_verdict_key", "_canon_verdict_keys",
                   "_balanced_brace_span"}), ns)  # noqa: S102 — repo-standard test pattern
    return ns, calls


def _run_tool_capable(results, **kw):
    ns, calls = _build(results, **kw)
    out = ns["_run_reviewer_turn"](DIFF, "sys", {"provider": "copilot", "model": "claude-opus-5"},
                                   "task", object(), "abc123")
    return out, calls


def _first_user_prompt(calls):
    return [m for m in calls[0]["messages"] if m["role"] == "user"][0]["content"]


def test_tool_capable_reviewer_receives_the_touched_files():
    """The regression: this path used to send the diff and nothing else."""
    _, calls = _run_tool_capable([{"text": VERDICT, "tool_calls": []}])
    body = _first_user_prompt(calls)
    assert "import ipaddress" in body, "threat_monitor.py content missing"
    assert "_console_creds_for_tenant" in body, "console.py content missing"


def test_both_touched_files_are_labelled():
    _, calls = _run_tool_capable([{"text": VERDICT, "tool_calls": []}])
    body = _first_user_prompt(calls)
    assert "--- FULL FILE: core/src/threat_monitor.py ---" in body
    assert "--- FULL FILE: core/src/console.py ---" in body


def test_tool_capable_context_says_not_to_spend_budget_refetching():
    _, calls = _run_tool_capable([{"text": VERDICT, "tool_calls": []}])
    body = _first_user_prompt(calls)
    assert "do NOT need to spend your fetch budget" in body
    assert "unverifiable" in body


def test_the_tool_is_still_offered():
    """Up-front context supplements the tool, it does not replace it."""
    _, calls = _run_tool_capable([{"text": VERDICT, "tool_calls": []}])
    assert calls[0]["tools"] is not None
    assert "fetch_repo_file" in _first_user_prompt(calls)


def test_tools_blind_path_still_gets_context_with_its_own_wording():
    ns, calls = _build([VERDICT])
    ns["_run_reviewer_turn"](DIFF, "sys", {"provider": "groq", "model": "llama"},
                             "task", None, None)
    # repo/head_sha are None -> no context is fetchable at all
    assert calls[0]["tools"] is None

    ns2, calls2 = _build([VERDICT])
    ns2["_run_reviewer_turn"](DIFF, "sys", {"provider": "claude_cli", "model": "claude"},
                              "task", object(), "abc123")
    body = [m for m in calls2[0]["messages"] if m["role"] == "user"][0]["content"]
    assert "can't fetch files on its own" in body
    assert "import ipaddress" in body


def test_context_is_capped_at_the_file_budget():
    many = "Review.\n" + "".join(
        f"--- a/f{i}.py\n+++ b/f{i}.py\n@@\n+x\n" for i in range(12))
    log = []
    ns, calls = _build([{"text": VERDICT, "tool_calls": []}], fetch_log=log)
    ns["_run_reviewer_turn"](many, "sys", {"provider": "copilot", "model": "claude-opus-5"},
                             "task", object(), "abc123")
    assert len(log) == 5, f"fetched {len(log)} files; must stop at _REVIEW_TOOL_MAX_FILES"


def test_a_failing_fetch_never_breaks_the_review():
    def _boom(repo, head_sha, path, **kw):
        raise RuntimeError("github down")

    out, calls = _run_tool_capable([{"text": VERDICT, "tool_calls": []}], fetch=_boom)
    assert json.loads(out)["verdict"] == "Approve"
    body = _first_user_prompt(calls)
    assert "FULL FILE" not in body


def test_no_fetchable_files_adds_no_header():
    def _none(repo, head_sha, path, **kw):
        return {"error": "not found"}

    _, calls = _run_tool_capable([{"text": VERDICT, "tool_calls": []}], fetch=_none)
    assert "FULL FILE" not in _first_user_prompt(calls)


def test_missing_repo_or_head_yields_no_context():
    ns, _ = _build([VERDICT])
    assert ns["_full_file_context"](None, "abc", DIFF, True) == ""
    assert ns["_full_file_context"](object(), None, DIFF, True) == ""
