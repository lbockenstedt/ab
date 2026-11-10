"""The description-rewrite budget must survive record_pr_review's rebuild.

`record_pr_review` REBUILDS state["pr_reviews"][key] from a fixed field list on
every scan. `fix_pr_description` persists its per-head budget through
`update_pr_review`, so any field the rebuild forgets to carry forward is wiped
once per poll. That happened in production: AppBuilder logged

    lbockenstedt/kvm #27 description rewritten to match the diff (1/2 for this head)

five times in seven minutes, always "1/2", because `done` read back as 0 every
time and `_MAX_DESC_FIXES` could never bind. These tests pin the carry-forward.
"""
import ast
import threading
from datetime import datetime

import pytest


def _extract(path, funcs):
    src = open(path, encoding="utf-8").read()
    segs = [ast.get_source_segment(src, n) for n in ast.parse(src).body
            if isinstance(n, ast.FunctionDef) and n.name in funcs]
    return "\n\n".join(segs)


class _DummyLogger:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def exception(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass


@pytest.fixture
def pr_env():
    src = _extract("app_state.py", {"record_pr_review", "_panel_dissent_stats",
                                    "update_pr_review"})
    state = {"pr_reviews": {}}
    ns = {
        "state": state,
        "_task_state_lock": threading.Lock(),
        "datetime": datetime,
        "save_pr_reviews": lambda x: None,
        "_PR_REVIEWS_MAX": 100,
        "logger": _DummyLogger(),
    }
    exec(src, ns)
    return ns, state



def test_desc_fixes_survives_a_re_review(pr_env):
    ns, state = pr_env
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    ns["update_pr_review"]("test/repo", 1, desc_fixes={"h1": 1})
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    assert state["pr_reviews"]["test/repo#1"]["desc_fixes"] == {"h1": 1}


def test_budget_binds_after_the_cap_is_reached(pr_env):
    """The actual livelock: the count must reach the ceiling and stay there."""
    ns, state = pr_env
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    for expected in (1, 2):
        rec = state["pr_reviews"]["test/repo#1"]
        done = int((rec.get("desc_fixes") or {}).get("h1", 0) or 0)
        assert done == expected - 1, "budget reset — this is the livelock"
        ns["update_pr_review"]("test/repo", 1, desc_fixes={"h1": done + 1})
        ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    rec = state["pr_reviews"]["test/repo#1"]
    assert int((rec.get("desc_fixes") or {}).get("h1", 0) or 0) == 2


def test_a_new_head_gets_a_fresh_budget(pr_env):
    ns, state = pr_env
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    ns["update_pr_review"]("test/repo", 1, desc_fixes={"h1": 2})
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h2")
    assert state["pr_reviews"]["test/repo#1"]["desc_fixes"].get("h2", 0) == 0


def test_stale_head_entries_are_pruned(pr_env):
    """The persisted map must not grow one entry per commit."""
    ns, state = pr_env
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    ns["update_pr_review"]("test/repo", 1, desc_fixes={"h1": 1})
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h2")
    assert "h1" not in state["pr_reviews"]["test/repo#1"]["desc_fixes"]


def test_desc_fixes_is_always_a_dict(pr_env):
    ns, state = pr_env
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    assert isinstance(state["pr_reviews"]["test/repo#1"]["desc_fixes"], dict)


def test_corrupt_desc_fixes_does_not_raise(pr_env):
    ns, state = pr_env
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    state["pr_reviews"]["test/repo#1"]["desc_fixes"] = None
    ns["record_pr_review"]("test/repo", 1, "t", "u", [], "h1")
    assert state["pr_reviews"]["test/repo#1"]["desc_fixes"] == {}
