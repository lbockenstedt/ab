#!/usr/bin/env python3
"""Self-test for feature_drive.py — the deterministic-then-LLM classifier and
the flag/clarify actions (Phase 1 of the feature auto-drive plan).

Run:  python3 ab/test_feature_drive_classify.py

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
        "_model_label", "_is_classifier_error_record", "_flag_classifier_exhausted",
    }
    want_assigns = {
        "_TERMINAL_STATUSES", "_FEATURE_MARKER_RE", "_BOUNDARY_FLAG_MARKER",
        "_CLASSIFY_SYSTEM", "_CLASSIFY_PROMPT_TMPL", "_CLASSIFY_JSON_SCHEMA",
        "_CLASSIFY_RETRY_STATUS", "_CLASSIFIER_ERROR_PREFIX", "_CLASSIFIER_ERROR_MARKER",
        "_RECLASSIFY_STATUSES",
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
        if isinstance(node, ast.FunctionDef) and node.name in ("_robust_json_loads", "_loads_llm_json_object", "_norm_confidence"):
            segs.append(ast.get_source_segment(fe_src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in ("_JSON_BAD_ESCAPE_RE", "_JSON_FENCE_RE"):
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
        def remove_from_labels(self, name):
            self.labels = [lbl for lbl in self.labels if lbl.name != name]
            self.added_labels.append("-" + name)
        def create_comment(self, body):
            c = type("C", (), {"body": body})
            self._comments.append(c)
            self.comments_posted.append(body)

    class _FakeRepo:
        def __init__(self, issues=None):
            self.ensured_labels = []
            self.issues = list(issues or [])
        def get_issues(self, labels=None, state=None):
            return list(self.issues)

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

    # ── classify(): LLM Selection Redesign Phase 5 site #13 — requirements= wiring ──
    captured = {}
    ns["_config_holder"]["config"] = {"feature_boundaries": []}

    def _capturing_call_llm(prompt, **kwargs):
        captured["kwargs"] = kwargs
        return '{"verdict": "build", "skill": "add-webui-control", "reason": "ok", "confidence": 0.8}'

    ns["_llm_holder"]["fn"] = _capturing_call_llm
    ns["classify"]("x", "y", ns["load_config"]())
    kw = captured.get("kwargs", {})
    ok &= _check("classify(): call_llm invoked with requirements= (not task_kind=)",
                "requirements" in kw and "task_kind" not in kw)
    reqs = kw.get("requirements")
    ok &= _check("classify(): requirements.complexity == 'small'",
                reqs is not None and reqs.complexity == "small")
    ok &= _check("classify(): requirements.needs_structured_output is True",
                reqs is not None and reqs.needs_structured_output is True)
    ok &= _check("classify(): json_schema= is still passed through unchanged",
                kw.get("json_schema") is not None)


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
    ok &= _check("_flag_issue applies the needs-human label", "ab-needs-human" in issue.added_labels)
    ok &= _check("_flag_issue posts exactly one comment", len(issue.comments_posted) == 1)
    ok &= _check("_flag_issue's comment quotes the crossed rule", "Never hardcode a PSK" in issue.comments_posted[0])
    ns["_flag_issue"](repo, issue, flag_result)  # second call, same issue
    ok &= _check("_flag_issue is idempotent — a repeat call does not re-comment", len(issue.comments_posted) == 1)

    # ── _clarify_issue: labels, comments, idempotent, re-comments on change ─
    issue2 = ns["_FakeIssue"](2, title="x", body="y")
    clarify_result = {"verdict": "clarify", "questions": ["Which page?"], "reason": "x"}
    ns["_clarify_issue"](repo, issue2, clarify_result)
    ok &= _check("_clarify_issue applies the needs-info label", "ab-needs-info" in issue2.added_labels)
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

    # ── classify(): fenced JSON (Copilot claude-haiku-4.5 shape, cs#184) ───
    ns["_config_holder"]["config"] = {"feature_boundaries": []}
    ns["skills_loader"]._loaded = {}
    ns["_llm_holder"]["fn"] = lambda *a, **k: (
        '```json\n{\n  "verdict": "build",\n  "boundary_ids": [],\n  "skill": null,\n'
        '  "reason": "UI-only change to sim-views.js", "confidence": 0.85\n}\n```'
    )
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("a ```json-fenced response parses instead of failing closed",
                result["verdict"] == "build" and not result.get("classifier_error"))

    def _with_model(*a, **k):
        k["used_model_out"].update({"provider": "copilot", "model": "claude-haiku-4.5"})
        return ""
    ns["_llm_holder"]["fn"] = _with_model
    result = ns["classify"]("x", "y", ns["load_config"]())
    ok &= _check("an empty response is a classifier_error (still verdict 'flag')",
                result["verdict"] == "flag" and "empty" in result.get("classifier_error", ""))
    ok &= _check("classifier errors record which model answered",
                result.get("model") == "copilot/claude-haiku-4.5")

    # ── scan: classifier errors retry, then park as classifier_error ───────
    def _scan_env(issue, store):
        repo = ns["_FakeRepo"]([issue])
        repo.full_name = "owner/repo"
        gh = type("G", (), {"get_repo": lambda self, n: repo})()
        ns["_store"].clear()
        ns["_store"].update(store)
        cfg = {"feature_drive_enabled": True, "feature_drive_require_marker": False,
               "feature_drive_repos": ["owner/repo"], "feature_boundaries": []}
        ns["_config_holder"]["config"] = cfg
        return gh, cfg

    ns["_llm_holder"]["fn"] = lambda *a, **k: "not json"
    issue = ns["_FakeIssue"](184, title="x", body="y", labels=["enhancement"])
    gh, cfg = _scan_env(issue, {})
    ns["scan_feature_requests"](gh, cfg)
    rec = ns["_store"]["owner/repo:184"]
    ok &= _check("first classifier error -> feature_classify_retry (not terminal)",
                rec["status"] == "feature_classify_retry" and rec["attempts"] == 1)
    ok &= _check("classifier error records the raw response excerpt", rec["raw_excerpt"] == "not json")
    ok &= _check("classifier error posts NO boundary comment",
                not issue.comments_posted and "ab-needs-human" not in issue.added_labels)
    ns["scan_feature_requests"](gh, cfg)
    ns["scan_feature_requests"](gh, cfg)
    rec = ns["_store"]["owner/repo:184"]
    ok &= _check("third failure parks it: feature_flagged, flag_kind=classifier_error",
                rec["status"] == "feature_flagged" and rec["flag_kind"] == "classifier_error"
                and rec["attempts"] == 3)
    ok &= _check("exhausted comment says it is NOT a boundary decision",
                len(issue.comments_posted) == 1 and "not** a boundary" in issue.comments_posted[0])
    ns["_llm_holder"]["fn"] = lambda *a, **k: '{"verdict": "clarify", "questions": ["Q?"], "reason": "r", "confidence": 0.5}'
    ns["scan_feature_requests"](gh, cfg)
    ok &= _check("an exhausted classifier_error record is terminal (not re-classified)",
                ns["_store"]["owner/repo:184"]["status"] == "feature_flagged")

    # ── scan: legacy stuck record (pre-retry) is un-stuck ──────────────────
    legacy = {"owner/repo:184": {
        "status": "feature_flagged", "boundary_ids": [],
        "reason": "Classifier error (failed closed): Expecting value: line 1 column 1 (char 0)"}}
    issue = ns["_FakeIssue"](184, title="x", body="y", labels=["enhancement", "ab-needs-human"])
    gh, cfg = _scan_env(issue, legacy)
    ns["scan_feature_requests"](gh, cfg)
    ok &= _check("a legacy classifier-error flag is re-classified on the next cycle",
                ns["_store"]["owner/repo:184"]["status"] == "feature_needs_info")
    ok &= _check("re-classifying a legacy flag clears its stale ab-needs-human label",
                "-ab-needs-human" in issue.added_labels)

    # ── scan: a real boundary flag stays terminal ──────────────────────────
    real = {"owner/repo:9": {"status": "feature_flagged", "flag_kind": "boundary",
                             "boundary_ids": ["psk-hardcode"], "reason": "x"}}
    issue = ns["_FakeIssue"](9, title="x", body="y", labels=["enhancement"])
    gh, cfg = _scan_env(issue, real)
    called["n"] = 0
    ns["_llm_holder"]["fn"] = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "{}"
    ns["scan_feature_requests"](gh, cfg)
    ok &= _check("a real boundary flag is still terminal (no re-classify)", called["n"] == 0)

    # ── scan: an issue handed off to the fix pipeline is owned by it ───────
    for st in ("processing", "fixed", "failed", "non-actionable"):
        owned = {"owner/repo:7": {"status": st, "pipeline": "fix_engine"}}
        issue = ns["_FakeIssue"](7, title="x", body="y", labels=["enhancement"])
        gh, cfg = _scan_env(issue, owned)
        called["n"] = 0
        ns["scan_feature_requests"](gh, cfg)
        ok &= _check(f"fix-pipeline-owned status {st!r} is not re-classified",
                    called["n"] == 0 and ns["_store"]["owner/repo:7"]["status"] == st)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature_drive classify/flag/clarify self-test...")
    sys.exit(main())
