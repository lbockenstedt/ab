"""Self-test: fixes that keep failing before a push park the PR for a CLI session.

The stall rule only scores pushed heads, so a fix rejected before the push never
advances it. `pr_remediate_fail_limit` consecutive failed attempts therefore mark
the PR `exhausted_human_review` (a CLI session looks at it) and stop the spend.
"""
import pytest

import pr_remediate
from pr_remediate import auto_remediate_pr


class _PR:
    number = 99
    title = "Remediation PR"
    body = "Body"
    state = "open"
    merged = False
    draft = False

    def __init__(self):
        self.comments = []

    def get_files(self):
        class _F:
            filename = "src/clean.py"
            patch = "+ def clean(): pass"
        return [_F()]

    def create_issue_comment(self, body):
        self.comments.append(body)


class _Repo:
    full_name = "lbockenstedt/nw"


def _run(rec, ok=False, msg="Fix rejected by the pre-push review", config=None):
    """Drive auto_remediate_pr once with a fix_fn that fails (or succeeds)."""
    pr = _PR()
    pr_remediate.state["pr_reviews"] = {"lbockenstedt/nw#99": dict(rec)}

    def fix(*a, **k):
        if k.get("used_model_out") is not None:
            k["used_model_out"]["model"] = "gemini-3.8-flash"
        return ok, msg

    cfg = {"pr_auto_remediate_max_attempts": 5}
    cfg.update(config or {})
    result = auto_remediate_pr(None, _Repo(), pr, cfg, fix_fn=fix)
    return result, pr, pr_remediate.state["pr_reviews"]["lbockenstedt/nw#99"]


REC = {"panel_critique": "Issue persists", "remediation_attempts": 0}


def test_second_failure_in_a_row_parks_the_pr_for_a_cli_session():
    (_, _), pr, out = _run({**REC, "remediation_fail_streak": 1})
    assert out["auto_remediate_status"] == "exhausted_human_review"
    assert out["remediation_fail_streak"] == 2
    assert "CLI session" in out["auto_remediate_reason"]
    assert len(pr.comments) == 1 and "CLI Session" in pr.comments[0]


def test_first_failure_only_counts():
    (_, _), pr, out = _run(dict(REC))
    assert out["auto_remediate_status"] == "failed"
    assert out["remediation_fail_streak"] == 1
    assert pr.comments == []


def test_limit_zero_turns_the_guard_off():
    (_, _), _, out = _run({**REC, "remediation_fail_streak": 7}, config={"pr_remediate_fail_limit": 0})
    assert out["auto_remediate_status"] == "failed"


def test_a_landed_fix_resets_the_streak():
    (ok, _), _, out = _run({**REC, "remediation_fail_streak": 1}, ok=True, msg="fixed")
    assert ok and out["remediation_fail_streak"] == 0


def test_parked_pr_is_not_remediated_again():
    _, _, out = _run({**REC, "remediation_fail_streak": 1})
    should, reason = pr_remediate.remediation_pending(out, {})
    assert should is False and "exhausted" in reason


@pytest.mark.parametrize("raw, expected", [(None, 2), ("x", 2), (-3, 0), (99, 5), ("3", 3)])
def test_fail_limit_is_clamped(raw, expected):
    from review_progress import fail_limit
    cfg = {} if raw is None else {"pr_remediate_fail_limit": raw}
    assert fail_limit(cfg) == expected
