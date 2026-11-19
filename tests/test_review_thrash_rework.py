"""Review thrash: when fixes stop making progress, AppBuilder reworks the
change ONCE from every round's panel data before a human takes over.

The owner's case: one change went 10 review rounds, panel scores 0.70, 0.745,
0.865, 0.71, 0.85, 0.79, 0.82, 0.87. Each round's point fixes uncovered a new
nearby objection, and a +0.005 gain reset the stall counter, so the loop never
stopped and never converged. What worked by hand was taking all the panel
results and reworking the change as one job instead of point fixes.

Covers the shared progress rule (review_progress), the rework at the stall
skip and at the attempt cap (pr_remediate), and the rework fix prompt
(pr_review.fix_one_pr, via the AST harness of test_pr_fix_retry_feedback).
"""
import threading
import time
import types

import pytest

import fix_failures
import pr_remediate
import review_progress
from test_pr_fix_retry_feedback import APPROVE, Harness, _extract

KEY = "o/r#7"
HISTORY = [
    {"head": "aaa", "score": [0, 0.7], "panel_critique": "round one: the retry is unbounded",
     "panel2_critique": "round one: state X is never cleared"},
    {"head": "bbb", "score": [0, 0.71], "panel_critique": "round two: the timeout is ignored",
     "panel2_critique": ""},
    {"head": "ccc", "score": [0, 0.72], "panel_critique": "round three: the lock is held across I/O",
     "panel2_critique": "round three: X and Y are conflated"},
]
CAP = {"pr_auto_remediate_max_attempts": 3}


# -- the shared progress rule ---------------------------------------------

def test_a_small_gain_is_not_progress_but_a_real_one_is():
    """+0.005 is noise; 0.03 (float-rounded) is progress; one more approving
    panel is always progress, one fewer never is."""
    made = review_progress.made_progress
    assert made([0, 0.5], None)
    assert not made([0, 0.805], [0, 0.8])
    assert made([0, 0.83], [0, 0.8])
    assert made([1, 0.1], [0, 0.99]) and not made([0, 0.99], [1, 0.1])
    assert not made([0, 0.8], [0, 0.8])
    # A margin of 0 is the old strict "beat the best" rule.
    assert made([0, 0.801], [0, 0.8], 0) and not made([0, 0.8], [0, 0.8], 0)


def test_the_knobs_default_and_clamp():
    """Defaults live in one place; a bad value falls back, a wild one is clamped."""
    rp = review_progress
    assert (rp.stall_limit(), rp.min_gain(), rp.rework_limit()) == (2, 0.03, 1)
    assert rp.min_gain({"pr_remediate_min_gain": 0.9}) == 0.5
    assert rp.min_gain({"pr_remediate_min_gain": -1}) == 0.0
    assert rp.min_gain({"pr_remediate_min_gain": "x"}) == 0.03
    assert rp.rework_limit({"pr_remediate_rework_limit": "3"}) == 3
    assert rp.rework_limit({"pr_remediate_rework_limit": 9}) == 3
    assert rp.rework_limit({"pr_remediate_rework_limit": -2}) == 0
    assert rp.rework_limit({"pr_remediate_rework_limit": None}) == 1
    assert rp.stall_limit({"pr_remediate_stall_limit": 0}) == 0
    assert rp.rework_defer_hours() == 24
    assert rp.rework_defer_hours({"pr_remediate_rework_defer_hours": 999}) == 168
    assert rp.rework_defer_hours({"pr_remediate_rework_defer_hours": ""}) == 24


def test_upsert_replaces_by_key_and_keeps_the_newest():
    rounds = review_progress.upsert([{"k": 1, "v": "a"}], "k", {"k": 1, "v": "b"})
    assert rounds == [{"k": 1, "v": "b"}]
    for k in range(2, 6):
        rounds = review_progress.upsert(rounds, "k", {"k": k}, keep=3)
    assert [r["k"] for r in rounds] == [3, 4, 5]


def test_a_rework_always_carries_the_head_being_fixed():
    """History holds only scored heads, each critique capped. The head being
    fixed takes its live critiques in full, wherever its round is (a branch
    reset back to an earlier head); an empty live critique keeps the stored
    one; a head reviewed but never scored is added last, not dropped."""
    full = "the lock is held across I/O " * 200
    rec = {"review_history": HISTORY, "panel_critique": full, "panel2_critique": ""}
    rounds = review_progress.rework_rounds(rec, "bbb")
    assert [r["head"] for r in rounds] == ["aaa", "bbb", "ccc"]
    assert rounds[1]["panel_critique"] == full.strip() and rounds[1]["score"] == [0, 0.71]
    assert rounds[0] is HISTORY[0] and HISTORY[1]["panel_critique"] != full.strip()
    rec = dict(rec, panel_critique="", panel2_critique="")
    assert review_progress.rework_rounds(rec, "ccc") == HISTORY
    rec = dict(rec, panel2_critique="the newest objection")
    rounds = review_progress.rework_rounds(rec, "ddd0123456789")
    assert rounds[:3] == HISTORY and rounds[3] == {
        "head": "ddd012345678", "score": "not scored", "panel_critique": "",
        "panel2_critique": "the newest objection"}
    assert review_progress.rework_rounds({"panel_critique": ""}, "h1") == []


# -- the rework in pr_remediate -------------------------------------------

class _PR:
    """An open PR at head ``sha`` that changes one code file."""

    def __init__(self, sha="h1", ref="feature/x"):
        self.number, self.title, self.body = 7, "t", "b"
        self.draft, self.merged, self.state = False, False, "open"
        self.head = types.SimpleNamespace(sha=sha, ref=ref)
        self.comments = []

    def get_files(self):
        return [types.SimpleNamespace(filename="src/app.py", status="modified", additions=1,
                                      deletions=0, patch="+ x = 1")]

    def create_issue_comment(self, body):
        self.comments.append(body)


class _Repo:
    full_name = "o/r"


class _Fix:
    """A fix_fn that records each call's keyword arguments, and reports
    ``model`` as the model that wrote the fix."""

    def __init__(self, result=(True, "fixed"), model=None):
        self.calls, self.result, self.model = [], result, model

    def __call__(self, repo, number, **kw):
        self.calls.append(kw)
        if self.model:
            kw["used_model_out"]["model"] = self.model
        return self.result


def _cand(model, key=None, **fields):
    """A configured model as llm_client._enumerate_candidates lists it, with
    every field model_selection's hard filters read: available, on the top
    tier, with a large context window."""
    return {"key": key or "c/" + model, "model": model, "provider": "c", "base_url": "",
            "api_key": "", "rpm": 0, "available": True, "unavailable_reason": None,
            "caps": {"cost_tier": "frontier", "max_complexity": "large",
                     "context_window": 200000}, **fields}


def _configure(monkeypatch, *cands):
    """Make ``cands`` the configured models."""
    import llm_client
    monkeypatch.setattr(llm_client, "_enumerate_candidates", lambda config: list(cands))


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    """The configured models: an Opus-class one, an ordinary one and a
    premium one (DEFAULT_PREMIUM_FIX_ALLOWLIST)."""
    import main  # noqa: F401  (fix_engine needs main imported first)
    _configure(monkeypatch, *(_cand(m) for m in ("claude-opus-5.5", "gpt-5.6-sol", "claude-fable-5")))


@pytest.fixture
def pr_state(monkeypatch):
    """Install one denied PR record with three scored rounds; returns it
    (update_pr_review updates it in place)."""
    def install(**fields):
        rec = {"panel_verdict": "Deny", "panel_confidence": 0.72, "panel2_verdict": "Deny",
               "panel2_confidence": 0.72, "review_history": HISTORY, **fields}
        monkeypatch.setitem(pr_remediate.state, "pr_reviews", {KEY: rec})
        return rec
    return install


def test_the_attempt_cap_runs_one_rework_instead_of_handing_off(pr_state):
    """Budget spent but no rework yet: one consolidated rework on the top
    tier, not charged as an attempt, counted and announced before it runs."""
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    fix, pr = _Fix(), _PR()
    ok, _ = pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix)
    assert ok is True and fix.calls[0]["rework"] is True
    assert fix.calls[0]["requirements"].complexity == "large"
    assert rec["remediation_attempts"] == 3
    assert (rec["remediation_reworks"], rec["remediation_rework_head"]) == (1, "h1")
    assert rec["auto_remediate_status"] == "success"
    assert len(pr.comments) == 1 and "Consolidated Rework" in pr.comments[0]
    assert "[0, 0.7] → [0, 0.71] → [0, 0.72]" in pr.comments[0]
    assert "3 round(s) of panel findings (rework 1 of 1)" in pr.comments[0]


def test_after_the_rework_the_cap_hands_off_saying_so(pr_state):
    rec = pr_state(remediation_attempts=3, premium_attempts=1, remediation_reworks=1,
                   remediation_rework_head="h0")
    fix, pr = _Fix(), _PR()
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix)
    assert (ok, msg) == (False, "Remediation attempt limit reached after 1 consolidated rework(s)")
    assert fix.calls == [] and rec["auto_remediate_status"] == "exhausted_human_review"
    assert "after 1 consolidated rework(s). Leaving for human review" in pr.comments[0]


def test_rework_limit_zero_hands_off_at_the_cap_as_before(pr_state):
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    fix = _Fix()
    ok, msg = pr_remediate.auto_remediate_pr(
        None, _Repo(), _PR(), dict(CAP, pr_remediate_rework_limit=0), fix_fn=fix)
    assert (ok, msg) == (False, "Remediation attempt limit reached")
    assert fix.calls == [] and "remediation_reworks" not in rec


def test_only_a_rework_gets_the_top_tier(pr_state):
    """The floor comes from the rework, not from the critique or the deficit."""
    pr_state(remediation_attempts=0)
    fix = _Fix()
    pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), CAP, fix_fn=fix)
    pr_remediate.auto_remediate_pr(None, _Repo(), _PR("h2"), CAP, fix_fn=fix, rework=True)
    assert [c["requirements"].complexity == "large" for c in fix.calls] == [False, True]
    assert "rework" not in fix.calls[0]


def test_a_rework_runs_on_the_opus_pool_whatever_the_attempt_count(pr_state):
    """From the first attempt on, a rework's pool is the Opus-class models,
    even when Opus wrote the last point fix; premium models stay out (their
    own budget), and the picker may not fall back to its safety floor. An
    ordinary early attempt keeps every non-premium model."""
    pr_state(remediation_attempts=0)
    fix = _Fix(model="c/claude-opus-5.5")
    pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), CAP, fix_fn=fix)
    pr_remediate.auto_remediate_pr(None, _Repo(), _PR("h2"), CAP, fix_fn=fix, rework=True)
    ordinary, rework = (set(c["requirements"].exclude_models) for c in fix.calls)
    assert ordinary == {"c/claude-fable-5"}
    assert rework == {"c/gpt-5.6-sol", "c/claude-fable-5"}
    assert [c["requirements"].must_escalate_to_human for c in fix.calls] == [False, True]


@pytest.mark.parametrize("models, failed", [
    (("claude-fable-5",), []),
    (("claude-opus-5.5", "claude-fable-5"), ["claude-opus-5.5"]),
])
def test_a_rework_never_falls_back_to_a_premium_model(pr_state, monkeypatch, models, failed):
    """With only premium models left (none other configured, or the others
    already failed), a rework does not run and the premium budget is not
    touched: a stalled PR waits, a PR out of attempts goes to a human."""
    _configure(monkeypatch, *(_cand(m) for m in models))
    why = " (no model outside the premium tier can run a consolidated rework)"
    fix = _Fix(model="c/claude-fable-5")
    rec = pr_state(remediation_attempts=0, excluded_models=failed)
    ok, msg = pr_remediate.auto_remediate_pr(
        None, _Repo(), _PR(), dict(CAP, pr_remediate_premium_attempts=0), fix_fn=fix, rework=True)
    assert (ok, msg) == (False, "remediation stalled" + why)
    assert rec["auto_remediate_reason"] == msg and "remediation_reworks" not in rec
    rec = pr_state(remediation_attempts=3, premium_attempts=1, excluded_models=failed)
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), CAP, fix_fn=fix)
    assert (ok, msg) == (False, "Remediation attempt limit reached" + why)
    assert rec["auto_remediate_status"] == "exhausted_human_review"
    assert "remediation_reworks" not in rec and rec["premium_attempts"] == 1
    assert fix.calls == []


def _real_keys(monkeypatch, *models):
    """Configure ``models`` with ModelKey tuples, as llm_client lists them
    (the autouse fixture's string keys hid a name/key mismatch)."""
    cands = [_cand(m, key=("c", "", m)) for m in models]
    _configure(monkeypatch, *cands)
    return cands


def _pick(reqs, cands, first):
    """The model the real picker chooses under ``reqs``. ``first`` is in a
    cheaper tier, so it wins unless ``reqs`` excludes it."""
    import model_selection
    full = [dict(c, caps=dict(c["caps"], cost_tier="cheap" if c["model"] == first else "frontier"))
            for c in cands]
    return model_selection.select_model(reqs, full).model


def test_a_rework_never_reruns_a_model_that_already_failed(pr_state, monkeypatch):
    """Opus failed to produce a point fix, so pr_remediate recorded its name
    in excluded_models. The rework must run on another model, even though
    the picker would rank Opus first."""
    cands = _real_keys(monkeypatch, "claude-opus-5.5", "gpt-5.6-sol", "claude-fable-5")
    pr_state(remediation_attempts=1, excluded_models=["claude-opus-5.5"])
    fix = _Fix()
    pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), CAP, fix_fn=fix, rework=True)
    assert fix.calls[0]["rework"] is True
    assert _pick(fix.calls[0]["requirements"], cands, first="claude-opus-5.5") == "gpt-5.6-sol"


def test_a_rework_is_not_spent_on_a_model_the_fix_floor_bars(pr_state, monkeypatch):
    """Opus failed; of the rest, the fix allowlist (pr_fix_model_allowlist)
    bars one from writing code and the other is premium. fix_one_pr would
    drop the first and the rework must not use the second, so the rework
    does not run and is not counted."""
    _real_keys(monkeypatch, "claude-opus-5.5", "gemini-3.8-flash", "claude-fable-5")
    rec = pr_state(remediation_attempts=1, excluded_models=["claude-opus-5.5"])
    fix = _Fix()
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), CAP, fix_fn=fix, rework=True)
    assert (ok, msg) == (False, "remediation stalled (no model outside the premium tier "
                                "can run a consolidated rework)")
    assert fix.calls == [] and "remediation_reworks" not in rec


@pytest.mark.parametrize("msg", [fix_failures.CONTENDED_LOCK, fix_failures.BARREN_EMPTY])
def test_a_rework_that_did_no_work_is_given_back(pr_state, msg):
    """Lock contention and a barren reply are not a rework; the next scan
    retries it, without a second announcement."""
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    pr = _PR()
    pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=_Fix((False, msg)))
    assert (rec["remediation_reworks"], rec["remediation_rework_head"]) == (0, None)
    fix = _Fix()
    pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix)
    assert fix.calls[0]["rework"] is True and rec["remediation_reworks"] == 1
    assert len(pr.comments) == 1


def test_a_given_back_rework_is_announced_once_across_a_rescan(pr_state, monkeypatch):
    """Every scan rebuilds the record (record_pr_review). The notice stamp
    must survive that, or a rework given back is announced again on the
    same head."""
    import app_state
    # Imported before main, pr_remediate keeps its own state dict (its
    # fallback); the rebuild must land in the dict it reads, whatever the order.
    monkeypatch.setattr(app_state, "state", pr_remediate.state)
    pr_state(remediation_attempts=3, premium_attempts=1)
    pr = _PR()
    pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP,
                                   fix_fn=_Fix((False, fix_failures.CONTENDED_LOCK)))
    before = pr_remediate.state["pr_reviews"][KEY]
    app_state.record_pr_review("o/r", 7, "t", "u", [], "h1",
                               review={"verdict": "Reject", "confidence": 0.72},
                               review2={"verdict": "Reject", "confidence": 0.72})
    rec = pr_remediate.state["pr_reviews"][KEY]
    assert rec is not before and rec["remediation_attempts"] == 3
    fix = _Fix()
    pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix)
    assert fix.calls[0]["rework"] is True and len(pr.comments) == 1


def test_a_stall_at_the_cap_never_hands_off_an_unused_premium_turn(pr_state, monkeypatch):
    """Out of ordinary attempts with the premium turn unused, a stalled PR
    whose rework has no model (Opus failed, Fable is premium) waits for a new
    head, unlatched, instead of going to a human. The next head that is not
    stalled takes the premium turn."""
    _configure(monkeypatch, _cand("claude-opus-5.5"), _cand("claude-fable-5"))
    rec = pr_state(remediation_attempts=3, premium_attempts=0, remediation_stalls=2,
                   excluded_models=["claude-opus-5.5"])
    fix = _Fix(model="c/claude-fable-5")
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), CAP, fix_fn=fix, rework=True)
    assert (ok, msg) == (False, "remediation stalled (no model outside the premium tier "
                                "can run a consolidated rework)")
    assert rec.get("auto_remediate_status") is None and rec["premium_attempts"] == 0
    assert fix.calls == []
    ok, _ = pr_remediate.auto_remediate_pr(None, _Repo(), _PR("h2"), CAP, fix_fn=fix)
    assert ok is True and len(fix.calls) == 1 and rec["premium_attempts"] == 1


def test_a_rework_whose_models_cannot_be_listed_is_deferred_not_handed_off(pr_state, monkeypatch):
    """Models that cannot be listed say nothing about the rework pool
    ("couldn't ask" is not "nothing there"). On the stall path and at the
    attempt cap alike the rework is deferred, uncounted and unannounced, and
    the PR is not handed to a human; once the models list again it runs."""
    import llm_client

    def _down(config):
        raise RuntimeError("model registry unavailable")
    monkeypatch.setattr(llm_client, "_enumerate_candidates", _down)
    deferred = (False, "consolidated rework deferred: the configured models could not be listed")
    fix, pr = _Fix(), _PR()
    stalled = pr_state(remediation_attempts=0, remediation_stalls=2)
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix, rework=True) == deferred
    assert stalled["auto_remediate_reason"] == deferred[1] and "remediation_reworks" not in stalled
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix) == deferred
    assert rec.get("auto_remediate_status") is None and rec["auto_remediate_reason"] == deferred[1]
    assert "remediation_reworks" not in rec and rec["premium_attempts"] == 1
    assert fix.calls == [] and pr.comments == []
    _configure(monkeypatch, _cand("claude-opus-5.5"), _cand("gpt-5.6-sol"))
    ok, _ = pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix)
    assert ok is True and fix.calls[0]["rework"] is True and rec["remediation_reworks"] == 1


_TOP = {"cost_tier": "frontier", "max_complexity": "large", "context_window": 200000}


@pytest.mark.parametrize("fields, config", [
    ({"caps": dict(_TOP, max_complexity="medium")}, {}),
    ({"caps": dict(_TOP, capability_rank=50)}, {"min_capability_rank": 60}),
    ({"available": False, "unavailable_reason": "dead_model"}, {}),
])
def test_a_rework_no_model_can_run_is_not_counted(pr_state, monkeypatch, fields, config):
    """The pool is judged as the picker judges it at call time. Opus and gpt
    below the top tier, under the capability floor, or removed until restart
    (dead_model) cannot run a rework, and Fable is premium, so a stalled PR
    waits for a new head and a PR out of attempts goes to a human, the rework
    unspent. It used to be counted, then lost to the picker's escalation."""
    _configure(monkeypatch, _cand("claude-opus-5.5", **fields), _cand("gpt-5.6-sol", **fields),
               _cand("claude-fable-5"))
    why = " (no model outside the premium tier can run a consolidated rework)"
    cfg, fix = dict(CAP, **config), _Fix()
    rec = pr_state(remediation_attempts=0, remediation_stalls=2)
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), cfg, fix_fn=fix, rework=True)
    assert (ok, msg) == (False, "remediation stalled" + why) and "remediation_reworks" not in rec
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), cfg, fix_fn=fix)
    assert (ok, msg) == (False, "Remediation attempt limit reached" + why)
    assert rec["auto_remediate_status"] == "exhausted_human_review"
    assert "remediation_reworks" not in rec and fix.calls == []


@pytest.mark.parametrize("reason", ["credit_cooldown", "rate_limited", "hard_failing"])
@pytest.mark.parametrize("models, who", [
    (("claude-opus-5.5", "gpt-5.6-sol"), "the Opus-class models it prefers are"),
    (("gpt-5.6-sol",), "every model that can run it is"),
])
def test_a_rework_whose_models_are_cooling_down_is_deferred(pr_state, monkeypatch, models,
                                                            who, reason):
    """A model in a timed cooldown will be back, so a rework only it can run
    is deferred, uncounted and unannounced, on the stall path and at the
    attempt cap alike, and the PR is not handed to a human."""
    down = {"available": False, "unavailable_reason": reason}
    _configure(monkeypatch, *(_cand(m, **down) for m in models), _cand("claude-fable-5"))
    deferred = (False, "consolidated rework deferred: %s in a timed cooldown" % who)
    fix, pr = _Fix(), _PR()
    stalled = pr_state(remediation_attempts=0, remediation_stalls=2)
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix, rework=True) == deferred
    assert stalled["auto_remediate_reason"] == deferred[1] and "remediation_reworks" not in stalled
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix) == deferred
    assert rec.get("auto_remediate_status") is None and "remediation_reworks" not in rec
    assert fix.calls == [] and pr.comments == []


def test_a_deferred_rework_waits_a_bounded_time(pr_state, monkeypatch):
    """A cooldown can re-arm on every retry, so a deferred rework waits at
    most pr_remediate_rework_defer_hours from its first deferral at a head.
    The wait is stamped once per head; once it is over the rework has no
    model: a stalled PR waits for a new head, which starts a new wait, and a
    PR out of attempts goes to a human. A wait of 0 never waits. It used to
    defer for good, so a PR out of attempts was never handed on."""
    _configure(monkeypatch, _cand("gpt-5.6-sol", available=False,
                                  unavailable_reason="credit_cooldown"), _cand("claude-fable-5"))
    deferred = (False, "consolidated rework deferred: every model that can run it is in a "
                       "timed cooldown")
    why = (" (no model could run a consolidated rework within 24 hour(s): every model that "
           "can run it is in a timed cooldown)")
    fix, pr, day_ago = _Fix(), _PR(), time.time() - 25 * 3600
    stalled = pr_state(remediation_attempts=0, remediation_stalls=2)
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix, rework=True) == deferred
    stamp = dict(stalled["remediation_rework_deferred"])
    assert stamp["head"] == "h1" and time.time() - stamp["at"] < 60
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix, rework=True) == deferred
    assert stalled["remediation_rework_deferred"] == stamp
    stalled["remediation_rework_deferred"] = {"head": "h1", "at": day_ago}
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix, rework=True) == (
        False, "remediation stalled" + why)
    assert stalled.get("auto_remediate_status") is None
    assert pr_remediate.auto_remediate_pr(None, _Repo(), _PR("h2"), CAP, fix_fn=fix,
                                          rework=True) == deferred
    assert stalled["remediation_rework_deferred"]["head"] == "h2"
    capped = pr_state(remediation_attempts=3, premium_attempts=1,
                      remediation_rework_deferred={"head": "h1", "at": day_ago})
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix) == (
        False, "Remediation attempt limit reached" + why)
    assert capped["auto_remediate_status"] == "exhausted_human_review"
    assert len(pr.comments) == 1 and why.strip() in pr.comments[0]
    pr_state(remediation_attempts=0, remediation_stalls=2)
    no_wait = dict(CAP, pr_remediate_rework_defer_hours=0)
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, no_wait, fix_fn=fix, rework=True) == (
        False, "remediation stalled" + why.replace(" 24 ", " 0 "))
    assert fix.calls == []


def test_a_rework_waits_for_a_cooling_opus_model(pr_state, monkeypatch):
    """A rework prefers the Opus-class models, as the last ordinary turn
    does: while Opus is only cooling down it waits rather than taking gpt,
    then runs on Opus."""
    _configure(monkeypatch, _cand("claude-opus-5.5", available=False,
                                  unavailable_reason="rate_limited"),
               _cand("gpt-5.6-sol"), _cand("claude-fable-5"))
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    fix, pr = _Fix(), _PR()
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix) == (
        False, "consolidated rework deferred: the Opus-class models it prefers are in a "
               "timed cooldown")
    assert fix.calls == [] and "remediation_reworks" not in rec
    _configure(monkeypatch, _cand("claude-opus-5.5"), _cand("gpt-5.6-sol"), _cand("claude-fable-5"))
    ok, _ = pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix)
    assert ok is True and fix.calls[0]["rework"] is True
    assert set(fix.calls[0]["requirements"].exclude_models) == {"c/gpt-5.6-sol", "c/claude-fable-5"}


def test_a_rework_does_not_wait_for_a_model_removed_until_restart(pr_state, monkeypatch):
    """Opus removed until restart (dead_model) will not come back by
    waiting: the rework runs now on the rest of the pool."""
    _configure(monkeypatch, _cand("claude-opus-5.5", available=False,
                                  unavailable_reason="dead_model"),
               _cand("gpt-5.6-sol"), _cand("claude-fable-5"))
    pr_state(remediation_attempts=3, premium_attempts=1)
    fix = _Fix()
    ok, _ = pr_remediate.auto_remediate_pr(None, _Repo(), _PR(), CAP, fix_fn=fix)
    assert ok is True and fix.calls[0]["rework"] is True
    assert set(fix.calls[0]["requirements"].exclude_models) == {"c/claude-fable-5"}


def test_a_rework_with_no_model_at_call_time_is_given_back_within_the_barren_allowance(pr_state):
    """The pool can empty between judging it and the call
    (fix_failures.NO_MODEL). Nothing ran, so the rework is given back, like a
    barren reply and within the same allowance; past it the rework is spent,
    so a pool that stays empty cannot hold the PR forever."""
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    cfg = dict(CAP, pr_remediate_max_barren_retries=1)
    fix, pr = _Fix((False, fix_failures.NO_MODEL + " No candidate satisfies requirements")), _PR()
    assert pr_remediate.auto_remediate_pr(None, _Repo(), pr, cfg, fix_fn=fix)[0] is False
    assert (rec["auto_remediate_status"], rec["remediation_barren"]) == ("no_model", 1)
    assert (rec["remediation_reworks"], rec["remediation_rework_head"]) == (0, None)
    pr_remediate.auto_remediate_pr(None, _Repo(), pr, cfg, fix_fn=fix)
    assert (rec["remediation_reworks"], rec["auto_remediate_status"]) == (1, "failed")
    assert rec["remediation_attempts"] == 3 and len(fix.calls) == 2 and len(pr.comments) == 1


def test_a_rework_judged_on_a_stale_record_is_not_taken_twice(pr_state):
    """maybe_auto_remediate judges the rework before auto_remediate_pr holds
    the PR's claim; if another worker took it in between, nothing runs."""
    rec = pr_state(remediation_stalls=2, remediation_reworks=1, remediation_rework_head="h1")
    fix, pr = _Fix(), _PR()
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix, rework=True)
    assert (ok, msg) == (False, "consolidated rework already taken")
    assert fix.calls == [] and pr.comments == [] and rec["remediation_reworks"] == 1


def test_a_second_worker_cannot_take_or_give_back_a_running_rework(pr_state):
    """Two workers judge the rework before either reserves it. The second one
    used to reserve it too, hit the per-PR fix lock, and restore the count it
    read first, erasing the first one's reservation, so the rework could run
    again. Now the first holds the PR's claim, so the second returns as
    contended and touches nothing."""
    rec = pr_state(remediation_stalls=2)
    second = {}
    paused, resume = threading.Event(), threading.Event()

    class _SecondPR(_PR):
        def create_issue_comment(self, body):
            # The old code got here: hold it until the first worker reserved.
            paused.set()
            resume.wait(5)
            super().create_issue_comment(body)

    def run_second():
        second["result"] = pr_remediate.auto_remediate_pr(
            None, _Repo(), _SecondPR(), CAP, rework=True,
            fix_fn=_Fix((False, fix_failures.CONTENDED_LOCK)))
        paused.set()

    worker = threading.Thread(target=run_second)

    class _FirstPR(_PR):
        def create_issue_comment(self, body):
            # The first worker has read the record but not yet reserved.
            worker.start()
            paused.wait(5)
            super().create_issue_comment(body)

    def first_fix(repo, number, **kw):
        resume.set()
        worker.join(5)
        return True, "fixed"

    ok, _ = pr_remediate.auto_remediate_pr(None, _Repo(), _FirstPR(), CAP, rework=True,
                                           fix_fn=first_fix)
    assert ok is True and second["result"] == (False, fix_failures.CONTENDED_LOCK)
    assert (rec["remediation_reworks"], rec["remediation_rework_head"]) == (1, "h1")


def test_a_rejected_rework_is_spent_so_it_never_loops(pr_state):
    """Rule 10.3: a rework the panel rejected changed nothing; it is not run
    again on the same code, the PR goes to a human."""
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    pr = _PR()
    pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=_Fix((False, "rejected")))
    assert (rec["remediation_reworks"], rec["remediation_attempts"]) == (1, 3)
    fix = _Fix()
    ok, msg = pr_remediate.auto_remediate_pr(None, _Repo(), pr, CAP, fix_fn=fix)
    assert ok is False and fix.calls == [] and "after 1 consolidated rework(s)" in msg


def test_a_promotion_pr_is_never_reworked(pr_state):
    rec = pr_state(remediation_attempts=3, premium_attempts=1)
    fix = _Fix()
    ok, _ = pr_remediate.auto_remediate_pr(None, _Repo(), _PR(ref="promote/dev-to-qa"), CAP,
                                           fix_fn=fix, rework=True)
    assert ok is False and fix.calls == [] and "remediation_reworks" not in rec


@pytest.fixture
def remediate_calls(monkeypatch):
    """maybe_auto_remediate's calls to auto_remediate_pr, by keyword arguments."""
    calls = []
    monkeypatch.setattr(pr_remediate, "auto_remediate_pr",
                        lambda gh, repo, pr, config, **kw: (calls.append(kw), (True, "ok"))[1])
    return calls


def test_a_stall_runs_a_rework_once_per_head(pr_state, remediate_calls):
    """Thrash with budget left is a rework, not a skip; a re-scan of the head
    it started at never repeats it, a new head may (budget allowing)."""
    rec = pr_state(remediation_stalls=2)
    two = {"pr_remediate_rework_limit": 2}
    assert pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(), two)[0] is True
    assert remediate_calls[-1]["rework"] is True
    rec.update(remediation_reworks=1, remediation_rework_head="h1")
    ok, reason = pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(), two)
    assert ok is False and reason.startswith("remediation stalled after 1 consolidated rework(s)")
    assert len(remediate_calls) == 1
    assert pr_remediate.maybe_auto_remediate(None, _Repo(), _PR("h2"), two)[0] is True
    assert remediate_calls[-1]["rework"] is True


def test_progress_resumes_ordinary_remediation(pr_state, remediate_calls):
    pr_state(remediation_stalls=0, remediation_reworks=1, remediation_rework_head="h0")
    assert pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(), {})[0] is True
    assert remediate_calls[-1]["rework"] is False


def test_a_stall_after_the_rework_hands_off_saying_so(pr_state, remediate_calls):
    pr_state(remediation_stalls=3, remediation_reworks=1, remediation_rework_head="h0")
    ok, reason = pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(), {})
    assert ok is False and remediate_calls == []
    assert reason == ("remediation stalled after 1 consolidated rework(s): 3 reviewed heads in a "
                      "row made no progress over the best review (pr_remediate_stall_limit)")


def test_rework_limit_zero_skips_a_stall_as_before(pr_state, remediate_calls):
    pr_state(remediation_stalls=2)
    ok, reason = pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(),
                                                   {"pr_remediate_rework_limit": 0})
    assert ok is False and reason.startswith("remediation stalled: 2 reviewed heads")
    assert remediate_calls == []


@pytest.mark.parametrize("fields, config", [
    ({"auto_remediate_blocked": True, "auto_remediate_blocked_head": "h1"}, {}),
    ({}, {"pr_auto_remediate_enabled": False}),
    ({"panel_verdict": "Approve", "panel_confidence": 0.99, "panel2_verdict": "Approve",
      "panel2_confidence": 0.99}, {}),
    ({"auto_remediate_status": "exhausted_human_review"}, {}),
])
def test_a_rework_still_respects_every_containment_gate(pr_state, remediate_calls, fields, config):
    """Guardrail latch, kill switch, target met, exhausted latch: thrash never
    opens a gate the ordinary path keeps shut."""
    pr_state(remediation_stalls=2, **fields)
    assert pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(), config)[0] is False
    assert remediate_calls == []


def test_a_rework_still_to_run_holds_the_merge(pr_state, monkeypatch):
    """remediation_pending holds auto-merge while a consolidated rework is
    still to run (rework_pending): at a stall, at the attempt cap, and while
    it is deferred, for at most pr_remediate_rework_defer_hours from the
    first deferral at this head. It stops holding once the rework ran at
    this head, when no model can run it, or once that wait is over."""
    import llm_client
    stalled = pr_state(head="h1", remediation_stalls=2)
    capped = pr_state(head="h1", remediation_attempts=3, premium_attempts=1)
    assert pr_remediate.remediation_pending(stalled, CAP)[0] is True
    assert pr_remediate.remediation_pending(capped, CAP)[0] is True
    ran = pr_state(head="h1", remediation_stalls=2, remediation_reworks=1,
                   remediation_rework_head="h1")
    assert pr_remediate.remediation_pending(ran, dict(CAP, pr_remediate_rework_limit=2))[0] is False
    _configure(monkeypatch, _cand("claude-fable-5"))
    assert pr_remediate.remediation_pending(stalled, CAP)[0] is False
    assert pr_remediate.remediation_pending(capped, CAP)[0] is False

    def _down(config):
        raise RuntimeError("model registry unavailable")
    monkeypatch.setattr(llm_client, "_enumerate_candidates", _down)
    assert pr_remediate.remediation_pending(stalled, CAP)[0] is True
    assert pr_remediate.remediation_pending(capped, CAP)[0] is True
    # A stamp for another head starts no wait; 0 hours never waits.
    day_ago = time.time() - 25 * 3600
    for rec in (stalled, capped):
        rec["remediation_rework_deferred"] = {"head": "h0", "at": day_ago}
        assert pr_remediate.remediation_pending(rec, CAP)[0] is True
        assert pr_remediate.remediation_pending(
            rec, dict(CAP, pr_remediate_rework_defer_hours=0))[0] is False
        rec["remediation_rework_deferred"] = {"head": "h1", "at": day_ago}
        assert pr_remediate.remediation_pending(rec, CAP)[0] is False
        assert pr_remediate.remediation_pending(
            rec, dict(CAP, pr_remediate_rework_defer_hours=48))[0] is True


def _files(*paths):
    """The PR's changed files as pr.get_files() lists them."""
    return [types.SimpleNamespace(filename=p, status="modified", additions=1, deletions=0,
                                  patch="@@ -1 +1 @@\n-a\n+b\n") for p in paths]


@pytest.mark.parametrize("stalls", [0, 2])
def test_a_docs_only_pr_is_never_held_for_remediation_that_skips_it(pr_state, stalls):
    """maybe_auto_remediate never fixes a docs-only PR
    (pr_auto_remediate_skip_docs_only), so the merge gate must not wait for a
    fix, or a consolidated rework, that will never run: the merge would be
    held forever. A PR that touches code is still held."""
    rec = pr_state(head="h1", remediation_stalls=stalls)
    docs, code = _files("README.md", "docs/ab.md"), _files("docs/ab.md", "pr_remediate.py")
    assert pr_remediate.remediation_pending(rec, CAP, docs) == (False, pr_remediate.DOCS_ONLY_SKIP)
    assert pr_remediate.remediation_pending(rec, CAP, code)[0] is True
    assert pr_remediate.remediation_pending(
        rec, dict(CAP, pr_auto_remediate_skip_docs_only=False), docs)[0] is True


# -- the rework fix prompt (pr_review.fix_one_pr) --------------------------

class ReworkHarness(Harness):
    """The fix_one_pr harness with a review_history on the PR record, which
    keeps every prompt handed to the model."""

    def __init__(self, **kw):
        super().__init__(parse_results=[True], reviews=[APPROVE], max_attempts=1, **kw)
        self.prompts = []

    def apply_ai_fix(self, repo_path, issue_body, error_context=None, **kw):
        self.prompts.append(issue_body)
        return super().apply_ai_fix(repo_path, issue_body, error_context, **kw)

    def _namespace(self):
        """The harness namespace, plus the rounds on the PR record and the
        _rework_brief that fix_one_pr calls on a rework."""
        ns = super()._namespace()
        ns["state"]["pr_reviews"]["o/r#7"]["review_history"] = HISTORY
        exec(_extract("pr_review.py", {"_rework_brief"}), ns)
        return ns

    def run(self, rework=True):
        """Run fix_one_pr as a rework, or as an ordinary fix."""
        return super().run(rework=rework)


def test_the_rework_prompt_carries_every_round():
    """Every round's critiques from both panels, oldest first, with the score
    trend and the rework instructions. The head being fixed (deadbeef) was
    reviewed but never scored, so it has no history entry: its live critique
    is the last round, once, rather than dropped or repeated as a last pass."""
    h = ReworkHarness(panel_critique="the last pass only")
    assert h.run()[0] is True
    prompt = h.prompts[0]
    found = [prompt.index(c) for r in HISTORY
             for c in (r["panel_critique"], r["panel2_critique"]) if c]
    found.append(prompt.index("the last pass only"))
    assert found == sorted(found) and len(found) == 6
    assert "[0, 0.7] -> [0, 0.71] -> [0, 0.72] -> not scored" in prompt
    for line in ("point fixes stopped converging", "Re-read the whole change end to end",
                 "by root cause", "one coherent design that resolves every root cause at once",
                 "keep the PR's stated intent", "Make all the edits in this single pass",
                 "An objection resolved in an earlier round must stay resolved",
                 "YOUR OBJECTIVE: make the reviewer panels APPROVE this PR"):
        assert line in prompt
    assert prompt.count("the last pass only") == 1 and "from the last panel pass" not in prompt


def test_the_rework_prompt_gives_a_scored_head_its_full_critique():
    """The head being fixed is the last scored round, whose stored critique
    was capped: the prompt carries the full live one there, keeps its score,
    and adds no extra round."""
    full = "the full critique of this head " * 150
    h = ReworkHarness(panel_critique=full)
    capped = dict(HISTORY[-1], head="deadbeef", panel_critique=full[:100])
    real_ns = h._namespace

    def _namespace():
        """The harness namespace, with the head being fixed as the last round."""
        ns = real_ns()
        ns["state"]["pr_reviews"]["o/r#7"]["review_history"] = HISTORY[:2] + [capped]
        return ns
    h._namespace = _namespace
    assert h.run()[0] is True
    prompt = h.prompts[0]
    assert full.strip() in prompt and "not scored" not in prompt
    assert "[0, 0.7] -> [0, 0.71] -> [0, 0.72]" in prompt and "Round 3 of 3" in prompt


def test_an_ordinary_fix_prompt_is_unchanged():
    h = ReworkHarness(panel_critique="the last pass only")
    h.run(rework=False)
    assert "the last pass only" in h.prompts[0]
    assert "CONSOLIDATED REWORK" not in h.prompts[0] and "round one" not in h.prompts[0]


def test_a_rework_is_of_the_code_not_the_description():
    """A description objection diverts an ordinary fix to fix_pr_description;
    the rework re-reads the whole change instead."""
    from test_pr_fix_retry_feedback import _DESC_CRITIQUE
    h = ReworkHarness(panel_critique=_DESC_CRITIQUE, desc_fix=(True, "Description updated."))
    assert h.run()[0] is True
    assert h.desc_fix_calls == [] and len(h.prompts) == 1


class _NoModelHarness(ReworkHarness):
    """A rework whose picker finds no model it may use."""

    def apply_ai_fix(self, repo_path, issue_body, error_context=None, **kw):
        """Record the prompt, then escalate as the picker does under
        must_escalate_to_human."""
        import llm_client
        self.prompts.append(issue_body)
        raise llm_client.LlmHumanEscalationNeeded("No candidate satisfies requirements")


def test_a_rework_with_no_model_stops_at_once_and_says_so():
    """fix_one_pr returns fix_failures.NO_MODEL on the first escalation,
    rather than retrying (a retry finds no model either) or reporting a
    failed fix, so pr_remediate can give the rework back."""
    h = _NoModelHarness()
    h.max_attempts = 3
    ok, msg = h.run()
    assert ok is False and fix_failures.is_no_model_failure(msg)
    assert "No candidate satisfies requirements" in msg and len(h.prompts) == 1
