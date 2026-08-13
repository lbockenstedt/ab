#!/usr/bin/env python3
"""Self-test for feature_drive.py — the deterministic-then-LLM classifier and
the flag/clarify actions (Phase 1 of the feature auto-drive plan).

Run:  python3 bugfixer/test_feature_drive_classify.py

feature_drive.py imports `main` (real feature-drive worker deployment
context), and this checkout's main.py fully boots the live app as a side
effect of being imported (not just raising ImportError) — see
test_skills_loader.py's docstring for the same discovery. So this extracts
feature_drive.py's functions by source via ast and execs them into a stub
namespace, exactly like test_issue_counter_recompute.py /
test_dismiss_background_retry.py do for routes.py.

feature_boundary.py is genuinely standalone (no app imports) so it's used
for real here rather than stubbed — more faithful than reimplementing its
matching logic a second time in this test.
"""
import ast
import sys

import feature_boundary


def _load_ns():
    fd_src = open("feature_drive.py").read()
    fe_src = open("fix_engine.py").read()
    fd_tree = ast.parse(fd_src)
    fe_tree = ast.parse(fe_src)

    segs = []
    want_funcs = {
        "classify", "_flag_issue", "_clarify_issue", "_clarify_marker",
        "_already_commented", "_is_feature_request", "scan_feature_requests",
    }
    want_assigns = {
        "_TERMINAL_STATUSES", "_FEATURE_MARKER_RE", "_BOUNDARY_FLAG_MARKER",
        "_CLASSIFY_SYSTEM", "_CLASSIFY_PROMPT_TMPL", "_CLASSIFY_JSON_SCHEMA",
    }
    for node in fd_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(fd_src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in want_assigns:
                    segs.append(ast.get_source_segment(fd_src, node))

    # Pull _robust_json_loads/_norm_confidence (+ their _JSON_BAD_ESCAPE_RE
    # constant) out of fix_engine.py by source too — real logic, no reimplementation,
    # no import of fix_engine itself (same app-boot trap as main.py).
    for node in fe_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ("_robust_json_loads", "_norm_confidence"):
            segs.append(ast.get_source_segment(fe_src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_JSON_BAD_ESCAPE_RE":
                    segs.append(ast.get_source_segment(fe_src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    class _FakeSkillsLoader:
        def __init__(self):
            self._loaded = {}
        def get_loaded(self):
            return dict(self._loaded)

    class _FakeIssue:
        """Minimal PyGithub Issue stand-in."""
        def __init__(self, number, title="", body="", labels=None, comments=None):
            self.number = number
            self.title = title
            self.body = body
            self.pull_request = None
            self.labels = [type("L", (), {"name": n}) for n in (labels or [])]
            self._comments = list(comments or [])
            self.added_labels = []
            self.comments_posted = []
        def get_comments(self):
            return list(self._comments)
        def add_to_labels(self, name):
            self.added_labels.append(name)
        def create_comment(self, body):
            c = type("C", (), {"body": body})
            self._comments.append(c)
            self.comments_posted.append(body)

    class _FakeRepo:
        def __init__(self):
            self.ensured_labels = []

    _store = {}  # in-memory stand-in for processed.json

    def load_processed():
        return dict(_store)

    def save_processed(d):
        _store.clear()
        _store.update(d)

    _config_holder = {"config": {}}

    def load_config():
        return dict(_config_holder["config"])

    def get_monitored_repos(config):
        return config.get("monitored_repos", ["owner/repo"])

    def recompute_issue_counters(processed):
        pass

    def _schedule_check(config):
        return {"allowed": True, "reason": ""}

    def _ensure_label(gh_repo, name):
        gh_repo.ensured_labels.append(name)
        return True

    _llm_holder = {"fn": lambda *a, **k: '{"verdict": "flag", "reason": "default stub", "confidence": 0.5}'}

    def call_llm(*a, **k):
        return _llm_holder["fn"](*a, **k)

    state = {"paused": False, "blackout": False}

    ns = {
        "logger": _NoLog(), "state": state,
        "load_config": load_config, "load_processed": load_processed,
        "save_processed": save_processed, "recompute_issue_counters": recompute_issue_counters,
        "get_monitored_repos": get_monitored_repos, "call_llm": call_llm,
        "feature_boundary": feature_boundary, "skills_loader": _FakeSkillsLoader(),
        "_ensure_label": _ensure_label, "_schedule_check": _schedule_check,
        "hashlib": __import__("hashlib"), "re": __import__("re"),
        "json": __import__("json"),  # needed by the extracted _robust_json_loads
        "datetime": __import__("datetime").datetime,
        "_fix_context_block": lambda body: "",  # stubbed — see module docstring
    }
    exec("\n\n".join(segs), ns)
    ns["_store"] = _store
    ns["_config_holder"] = _config_holder
    ns["_llm_holder"] = _llm_holder
    ns["_FakeIssue"] = _FakeIssue
    ns["_FakeRepo"] = _FakeRepo
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


PSK_BOUNDARY = {
    "id": "psk-hardcode", "label": "Hardcoded PSK", "rule": "Never hardcode a PSK.",
    "paths": ["**/hub_agent.py"], "keywords": ["psk", "pre-shared key"],
    "hard": True, "enabled": True,
}


def main():
    ok = True
    ns = _load_ns()

    # ── classify(): Stage A hard boundary short-circuits, no LLM call ──────
    ns["_config_holder"]["config"] = {"feature_boundaries": [PSK_BOUNDARY]}
    called = {"n": 0}
    ns["_llm_holder"]["fn"] = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "{}"
    result = ns["classify"]("Add PSK support", "Please hardcode a PSK for the spoke.", ns["load_config"]())
    ok &= _check("hard boundary hit classifies as 'flag' without calling the LLM",
                result["verdict"] == "flag" and called["n"] == 0)
    ok &= _check("flagged result carries the matched boundary id",
                result["boundary_ids"] == ["psk-hardcode"])

    # ── classify(): LLM returns build, with a resolvable skill ──────────────
    ns["_config_holder"]["config"] = {"feature_boundaries": []}
    ns["skills_loader"]._loaded = {"add-webui-control": {"description": "..."}}
    ns["_llm_holder"]["fn"] = lambda *a, **k: (
        '{"verdict": "build", "skill": "add-webui-control", "reason": "safe bolt-on", "confidence": 0.92}'
    )
    result = ns["classify"]("Add a clear-dongles button", "Adds a button to VM Server.", ns["load_config"]())
    ok &= _check("LLM 'build' verdict with a resolvable skill passes through",
                result["verdict"] == "build" and result["skill"] == "add-webui-control")
    ok &= _check("confidence is normalized to 0.0-1.0",
                abs(result["confidence"] - 0.92) < 1e-6)

    # ── classify(): LLM names a skill that isn't actually loaded ────────────
    ns["skills_loader"]._loaded = {}
    ns["_llm_holder"]["fn"] = lambda *a, **k: (
        '{"verdict": "build", "skill": "nonexistent-skill", "reason": "x", "confidence": 0.9}'
    )
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("an unresolvable skill name is nulled out, not trusted verbatim",
                result["verdict"] == "build" and result["skill"] is None)

    # ── classify(): LLM confidence on the 0-100 scale gets normalized ──────
    ns["_llm_holder"]["fn"] = lambda *a, **k: (
        '{"verdict": "flag", "boundary_ids": [], "reason": "x", "confidence": 95}'
    )
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("a 0-100-scale confidence (95) normalizes to 0.95",
                abs(result["confidence"] - 0.95) < 1e-6)

    # ── classify(): fail-closed on unrecognized verdict ─────────────────────
    ns["_llm_holder"]["fn"] = lambda *a, **k: '{"verdict": "maybe", "reason": "x", "confidence": 0.9}'
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("an unrecognized verdict string fails closed to 'flag'",
                result["verdict"] == "flag")

    # ── classify(): fail-closed on unparseable response ─────────────────────
    ns["_llm_holder"]["fn"] = lambda *a, **k: "not json at all, just prose"
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("an unparseable LLM response fails closed to 'flag'",
                result["verdict"] == "flag")

    # ── classify(): fail-closed when call_llm itself raises ────────────────
    def _raiser(*a, **k):
        raise RuntimeError("all providers offline")
    ns["_llm_holder"]["fn"] = _raiser
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("call_llm raising fails closed to 'flag'", result["verdict"] == "flag")

    # ── classify(): clarify verdict with no questions is untrustworthy ─────
    ns["_llm_holder"]["fn"] = lambda *a, **k: '{"verdict": "clarify", "questions": [], "reason": "x", "confidence": 0.5}'
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("a 'clarify' verdict with zero questions fails closed to 'flag' (would loop forever otherwise)",
                result["verdict"] == "flag")

    # ── classify(): well-formed clarify verdict passes through ─────────────
    ns["_llm_holder"]["fn"] = lambda *a, **k: (
        '{"verdict": "clarify", "questions": ["Which page?", "What should the button do?"], '
        '"reason": "under-specified", "confidence": 0.6}'
    )
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("a well-formed 'clarify' verdict passes through with its questions",
                result["verdict"] == "clarify" and len(result["questions"]) == 2)

    # ── _flag_issue: labels, comments, idempotent ───────────────────────────
    ns["_config_holder"]["config"] = {"feature_boundaries": [PSK_BOUNDARY]}
    issue = ns["_FakeIssue"](1, title="x", body="y")
    repo = ns["_FakeRepo"]()
    flag_result = {"verdict": "flag", "boundary_ids": ["psk-hardcode"], "reason": "x"}
    ns["_flag_issue"](repo, issue, flag_result)
    ok &= _check("_flag_issue applies the needs-human label", "bugfixer-needs-human" in issue.added_labels)
    ok &= _check("_flag_issue posts exactly one comment", len(issue.comments_posted) == 1)
    ok &= _check("_flag_issue's comment quotes the crossed rule", "Never hardcode a PSK" in issue.comments_posted[0])
    ns["_flag_issue"](repo, issue, flag_result)  # second call, same issue
    ok &= _check("_flag_issue is idempotent — a repeat call does not re-comment", len(issue.comments_posted) == 1)

    # ── _clarify_issue: labels, comments, idempotent, re-comments on change ─
    issue2 = ns["_FakeIssue"](2, title="x", body="y")
    clarify_result = {"verdict": "clarify", "questions": ["Which page?"], "reason": "x"}
    ns["_clarify_issue"](repo, issue2, clarify_result)
    ok &= _check("_clarify_issue applies the needs-info label", "bugfixer-needs-info" in issue2.added_labels)
    ok &= _check("_clarify_issue posts exactly one comment", len(issue2.comments_posted) == 1)
    ok &= _check("_clarify_issue's comment lists the question", "Which page?" in issue2.comments_posted[0])
    ns["_clarify_issue"](repo, issue2, clarify_result)  # same questions again
    ok &= _check("_clarify_issue is idempotent for the SAME questions", len(issue2.comments_posted) == 1)
    different = {"verdict": "clarify", "questions": ["A totally different question?"], "reason": "x"}
    ns["_clarify_issue"](repo, issue2, different)
    ok &= _check("_clarify_issue re-comments when the questions actually changed",
                len(issue2.comments_posted) == 2)

    # ── _is_feature_request ──────────────────────────────────────────────
    ok &= _check("marker present is detected",
                ns["_is_feature_request"]("blah\n<!-- report-type: feature -->\nblah"))
    ok &= _check("marker absent is not detected", not ns["_is_feature_request"]("just a normal bug body"))
    ok &= _check("empty/None body does not crash", ns["_is_feature_request"](None) is False)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature_drive classify/flag/clarify self-test...")
    sys.exit(main())
