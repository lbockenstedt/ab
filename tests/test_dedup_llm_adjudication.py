#!/usr/bin/env python3
"""Self-test for the LLM-based dedup fallback (Gap 2).

Run:  python3 ab/test_dedup_llm_adjudication.py

github_ops.py cannot be imported directly (it pulls in the app's circular
import chain), so this extracts the SOURCE of the pure functions via ast and
execs them with a stubbed call_llm.

Covers three things:
1. _llm_confirms_same_issue's LLM Selection Redesign wiring: call_llm is
   invoked with requirements=LlmRequirements(complexity="trivial",
   needs_structured_output=True, min_context_tokens=<estimate>) instead of the
   old task_kind="log_review" pin (Phase 5, call site #16).
2. _llm_confirms_same_issue itself: correct JSON-verdict parsing, and —
   critically — FAILS CLOSED (same_issue=False) on every kind of failure
   (malformed response, non-dict JSON, an exception from call_llm itself).
   An LLM outage must only ever make the system file MORE issues / reopen or
   suppress FEWER, never silently swallow a genuinely new bug.
3. The reopen gate inside find_global_duplicate_issue: when the fast body
   heuristic is too strict to confirm a recurring CLOSED issue, the LLM gets
   a second opinion — confirms it -> issue is matched (reopened), declines or
   errors -> issue is correctly skipped, same as the pre-LLM behavior.
"""
import ast
import json
import re
from datetime import datetime, timedelta, timezone


def _load_ns(call_llm_stub):
    src = open("github_ops.py").read()
    tree = ast.parse(src)
    want = {"_llm_confirms_same_issue", "find_global_duplicate_issue"}
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            segs.append(ast.get_source_segment(src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    def _is_duplicate_match(nt, nb, et, eb):
        return True  # isolate the gate under test, not the title/body heuristic

    def _body_signal_match(nb, eb):
        return False  # force every candidate through the LLM fallback

    ns = {
        "logger": _NoLog(), "json": json, "re": re,
        "datetime": datetime, "timezone": timezone,
        "call_llm": call_llm_stub,
        "load_config": lambda: {},
        "_is_duplicate_match": _is_duplicate_match,
        "_body_signal_match": _body_signal_match,
        "_normalize_for_dedup": lambda t: (t or "").lower(),
        "_jaccard": lambda a, b: 1.0 if a and b else 0.0,
        "DEDUP_CLOSED_WINDOW_DAYS": 60, "GLOBAL_FALLBACK_JACCARD": 0.8,
    }
    exec("\n\n".join(segs), ns)
    return ns


class _Label:
    def __init__(self, name):
        self.name = name


class _Issue:
    def __init__(self, number, state, title="t", body="x y z w q", labels=()):
        self.number = number
        self.state = state
        self.title = title
        self.body = body
        self.labels = [_Label(n) for n in labels]
        self.closed_at = datetime.now(timezone.utc) - timedelta(days=10)
        self.updated_at = datetime.now(timezone.utc) - timedelta(days=1)


class _Repo:
    def __init__(self, issues):
        self._issues = issues

    def get_issues(self, state='all', sort='updated', direction='desc'):
        return list(self._issues)


class _GhCurrent:
    def __init__(self, repo):
        self._repo = repo

    def get_repo(self, name):
        return self._repo


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True

    # ---- 0. LLM Selection Redesign Phase 5, site #16: requirements= wiring ----
    captured = {}

    def _capturing_call_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return '{"same_issue": true, "reason": "captured ok"}'

    ns_cap = _load_ns(_capturing_call_llm)
    ns_cap["_llm_confirms_same_issue"]("a", "b", "c", "d", 7)
    kw = captured.get("kwargs", {})
    ok &= _check("call_llm is invoked with requirements= (not task_kind=)",
                 "requirements" in kw and "task_kind" not in kw)
    reqs = kw.get("requirements")
    ok &= _check("requirements.complexity == 'trivial'",
                 reqs is not None and getattr(reqs, "complexity", None) == "trivial")
    ok &= _check("requirements.needs_structured_output is True",
                 reqs is not None and getattr(reqs, "needs_structured_output", None) is True)
    ok &= _check("requirements.min_context_tokens is a positive estimate from the prompt length",
                 reqs is not None and isinstance(reqs.min_context_tokens, int)
                 and reqs.min_context_tokens == len(captured["prompt"]) // 4 > 0)

    # ---- 1. _llm_confirms_same_issue in isolation ----
    ns_ok = _load_ns(lambda *a, **k: '{"same_issue": true, "reason": "same root cause"}')
    same, reason = ns_ok["_llm_confirms_same_issue"]("new t", "new b", "ex t", "ex b", 42)
    ok &= _check("well-formed same_issue=true response parses correctly",
                 same is True and reason == "same root cause")

    ns_false = _load_ns(lambda *a, **k: '{"same_issue": false, "reason": "different cause"}')
    same2, reason2 = ns_false["_llm_confirms_same_issue"]("a", "b", "c", "d")
    ok &= _check("well-formed same_issue=false response parses correctly",
                 same2 is False and reason2 == "different cause")

    ns_garbage = _load_ns(lambda *a, **k: "not json at all, sorry")
    same3, _ = ns_garbage["_llm_confirms_same_issue"]("a", "b", "c", "d")
    ok &= _check("unparseable LLM response fails CLOSED (same_issue=False)", same3 is False)

    def _raiser(*a, **k):
        raise RuntimeError("all providers cooling down")
    ns_err = _load_ns(_raiser)
    same4, reason4 = ns_err["_llm_confirms_same_issue"]("a", "b", "c", "d")
    ok &= _check("call_llm raising fails CLOSED (same_issue=False), reason captured",
                 same4 is False and "all providers cooling down" in reason4)

    # ---- 2. Wired into the reopen gate ----
    closed_issue = _Issue(201, "closed")

    ns_confirm = _load_ns(lambda *a, **k: '{"same_issue": true, "reason": "same recurring bug"}')
    found, _, was_closed = ns_confirm["find_global_duplicate_issue"](
        _GhCurrent(_Repo([closed_issue])), ["r/x"],
        {"title": "t", "body": "x y z w q", "repo": "r/x"})
    ok &= _check("reopen gate: LLM CONFIRMS -> closed issue is matched (reopened)",
                 found is not None and found.number == 201 and was_closed is True)

    ns_decline = _load_ns(lambda *a, **k: '{"same_issue": false, "reason": "unrelated"}')
    found2, _, _ = ns_decline["find_global_duplicate_issue"](
        _GhCurrent(_Repo([_Issue(202, "closed")])), ["r/x"],
        {"title": "t", "body": "x y z w q", "repo": "r/x"})
    ok &= _check("reopen gate: LLM DECLINES -> closed issue is NOT matched (skipped)",
                 found2 is None)

    ns_outage = _load_ns(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("outage")))
    found3, _, _ = ns_outage["find_global_duplicate_issue"](
        _GhCurrent(_Repo([_Issue(203, "closed")])), ["r/x"],
        {"title": "t", "body": "x y z w q", "repo": "r/x"})
    ok &= _check("reopen gate: LLM ERRORS -> fails closed, issue NOT matched (safe default)",
                 found3 is None)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running LLM dedup adjudication self-test...")
    import sys
    sys.exit(main())
