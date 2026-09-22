#!/usr/bin/env python3
"""Self-test: AppBuilder records and shows WHY a PR was not auto-merged.

Run:  python3 test_automerge_refusal_reason.py   (also collected by pytest)

_automerge_decision always knew the precise refusal reason (panel 2 below the
threshold, Tier-1 warnings, ...) but _maybe_auto_merge only logged it at DEBUG, so
an operator looking at a 90%+ headline (panel 1) saw a gate that looked broken.
Now the reason is persisted (auto_merge_blocked_reason), logged once at INFO, and
written into the pre-review comment as a single `<!-- ab-automerge -->` line.

pr_review / app_state import the live app (see test_feature_automerge_gate.py), so
the functions are extracted by source via ast and exec'd against fakes.
"""
import ast
import re
import threading
from datetime import datetime

HEAD = "9f0d19f88196c460595c4434ec1b737a1d16b166"
BODY = """<!-- ab-pr-review -->
<!-- head: %s -->
## 🤖 AppBuilder PR pre-review

### 📝 What changed

Adds a retry to the poller.

### Tier-1 checks

✅ **Passed** — nothing found.

### 🧠 Skeptical review (panel)

🟢 **Recommendation: APPROVE** · panel confidence **94%%**""" % HEAD

_PR_WANT_ASSIGNS = {"PR_REVIEW_MARKER", "_SUMMARY_HEADER", "_FEATURE_DRIVE_MARKER_RE",
                    "_AUTOMERGE_NOTE_MARKER", "_IDEMPOTENT_MERGED_REASON"}
_PR_WANT_FUNCS = {"_automerge_note", "_update_automerge_comment", "_find_marker_comment",
                  "_maybe_auto_merge", "_extract_summary"}


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


class _Logger:
    def __init__(self):
        self.calls = []

    def _rec(self, level):
        return lambda msg, *a: self.calls.append((level, msg % a if a else msg))

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error"):
            return self._rec(name)
        if name == "exception":
            return self._rec("error")
        raise AttributeError(name)

    def at(self, level):
        return [m for lv, m in self.calls if lv == level]


class _Comment:
    def __init__(self, body, fail=False):
        self.body, self.fail, self.edits = body, fail, []

    def edit(self, body):
        if self.fail:
            raise RuntimeError("github 500")
        self.edits.append(body)
        self.body = body


class _PR:
    number = 7
    body = ""
    state = "open"
    draft = False
    mergeable = True

    class base:
        ref = "dev"

    def __init__(self, comment):
        self.comment = comment

    def get_files(self):
        return []

    def get_issue_comments(self):
        return [self.comment] if self.comment is not None else []


class _Repo:
    full_name = "owner/repo"


class _Env:
    """One exec'd pr_review namespace with a controllable decision."""

    def __init__(self, body=BODY, fail_edit=False, has_comment=True):
        self.logger = _Logger()
        self.state = {"pr_reviews": {"owner/repo#7": {"panel_confidence": 0.94,
                                                      "panel2_confidence": 0.7}}}
        self.updates = []
        self.decision = (False, "panel 2 confidence 0.70 below threshold 0.90")
        self.merge = (200, {"status": "success"})
        self.comment = _Comment(body, fail=fail_edit) if has_comment else None
        self.pr = _PR(self.comment)

        def update_pr_review(repo, number, **fields):
            self.updates.append(fields)
            self.state["pr_reviews"]["%s#%s" % (repo, number)].update(fields)
            return True

        class _FA:
            @staticmethod
            def files_from_pr_files(files):
                return []

        ns = {"re": re, "logger": self.logger, "state": self.state,
              "update_pr_review": update_pr_review, "feature_allowlist": _FA,
              "_automerge_decision": lambda *a, **k: self.decision,
              "approve_pr": lambda *a, **k: None,
              "mark_pr_approved": lambda *a, **k: None,
              "merge_pr": lambda *a, **k: self.merge}
        exec(_extract("pr_review.py", _PR_WANT_ASSIGNS, _PR_WANT_FUNCS), ns)
        self.ns = ns

    def run(self):
        self.ns["_maybe_auto_merge"](None, _Repo, self.pr, {})

    @property
    def rec(self):
        return self.state["pr_reviews"]["owner/repo#7"]


def _note_lines(body):
    return [ln for ln in body.splitlines() if "<!-- ab-automerge -->" in ln]


def test_refusal_persists_and_logs_once_then_is_quiet():
    e = _Env()
    e.run()
    reason = e.decision[1]
    assert e.rec["auto_merge_blocked_reason"] == reason
    assert e.logger.at("info") == ["pr_review: auto-merge held for owner/repo#7: " + reason]
    assert len(e.updates) == 1 and len(e.comment.edits) == 1
    e.run()   # next poll, same reason: no write, no log, no comment edit
    assert len(e.updates) == 1
    assert len(e.logger.at("info")) == 1
    assert len(e.comment.edits) == 1


def test_changed_reason_replaces_the_single_note_line():
    e = _Env()
    e.run()
    e.decision = (False, "Tier-1 warnings present (2)")
    e.run()
    assert e.rec["auto_merge_blocked_reason"] == "Tier-1 warnings present (2)"
    assert len(e.comment.edits) == 2
    lines = _note_lines(e.comment.body)
    assert len(lines) == 1 and "Tier-1 warnings present (2)" in lines[0]
    assert "panel 2" not in e.comment.body


def test_cleared_pr_clears_blocked_reason_and_writes_cleared_note():
    e = _Env()
    e.run()
    e.decision = (True, "cleared: both panels Approve, min confidence 0.94")
    e.run()
    assert e.rec["auto_merge_blocked_reason"] is None
    assert e.rec["auto_merge_reason"].startswith("cleared")
    assert e.rec["auto_merged"] is True
    lines = _note_lines(e.comment.body)
    assert len(lines) == 1 and "✅ **Auto-merge:** cleared" in lines[0]


def test_comment_body_otherwise_preserved_and_summary_and_cache_check_intact():
    e = _Env()
    before_summary = e.ns["_extract_summary"](BODY)
    assert before_summary == "Adds a retry to the poller."
    e.run()
    edited = e.comment.body
    assert edited.startswith(BODY)   # append at the END, nothing else touched
    assert ("<!-- head: %s -->" % HEAD) in edited   # the already-current check
    assert e.ns["_extract_summary"](edited) == before_summary
    e.decision = (False, "another reason")
    e.run()
    assert e.comment.body.replace(_note_lines(e.comment.body)[0], "").rstrip() == BODY


def test_automerge_note_format():
    note = _Env().ns["_automerge_note"]
    assert note("panel 2 low", False) == "<!-- ab-automerge --> \U0001F512 **Auto-merge held:** panel 2 low"
    assert note("cleared: ok", True) == "<!-- ab-automerge --> ✅ **Auto-merge:** cleared: ok"
    assert "\n" not in note("a\nb", False)


def test_failing_comment_edit_is_swallowed_and_field_still_persisted():
    e = _Env(fail_edit=True)
    e.run()   # must not raise
    assert e.rec["auto_merge_blocked_reason"] == e.decision[1]
    assert any("could not update auto-merge note" in m for m in e.logger.at("warning"))
    assert e.logger.at("error") == []


def test_no_marker_comment_still_records_the_field():
    e = _Env(has_comment=False)
    e.run()
    assert e.rec["auto_merge_blocked_reason"] == e.decision[1]


def test_idempotent_already_merged_is_not_recorded_as_a_refusal():
    e = _Env()
    e.decision = (False, "already merged (idempotent no-op)")
    e.run()
    assert e.updates == [] and e.comment.edits == [] and e.logger.at("info") == []


def test_record_pr_review_preserves_blocked_reason_for_same_head():
    src = _extract("app_state.py", set(), {"record_pr_review"})
    state = {"pr_reviews": {}}
    ns = {"state": state, "_task_state_lock": threading.Lock(), "datetime": datetime,
          "save_pr_reviews": lambda x: None, "_PR_REVIEWS_MAX": 100, "logger": _Logger()}
    exec(src, ns)
    rec = ns["record_pr_review"]
    rec("owner/repo", 7, "t", "u", [], HEAD)
    state["pr_reviews"]["owner/repo#7"]["auto_merge_blocked_reason"] = "panel 2 low"
    rec("owner/repo", 7, "t", "u", [], HEAD)
    assert state["pr_reviews"]["owner/repo#7"]["auto_merge_blocked_reason"] == "panel 2 low"
    # A moved head re-renders the comment (dropping the note), so it must be rewritten.
    rec("owner/repo", 7, "t", "u", [], "0" * 40)
    assert state["pr_reviews"]["owner/repo#7"]["auto_merge_blocked_reason"] is None


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  [PASS] " + name)
            except Exception as ex:  # noqa: BLE001
                failed += 1
                print("  [FAIL] %s: %r" % (name, ex))
    sys.exit(1 if failed else 0)
