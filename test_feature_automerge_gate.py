#!/usr/bin/env python3
"""Self-test for pr_review._automerge_decision — the fence around the ONE
deliberate exception to "BugFixer never auto-approves/auto-merges".

Run:  python3 bugfixer/test_feature_automerge_gate.py

pr_review.py imports `main`/`app_state` (real app-context modules whose
import fully boots the live app as a side effect in this checkout — see
test_skills_loader.py's docstring), so this extracts _automerge_decision by
source via ast and execs it with feature_boundary imported for real (pure,
standalone — more faithful than reimplementing its matching logic here).

This is the single most safety-critical test in the whole feature auto-drive
project: EVERY case must resolve to (False, reason) except the one fully-
cleared, all-conditions-met case at the end. A False-negative here (a case
that should block but doesn't) means an unattended, unreviewed PR merges
into a real repo — for `lm`, an unattended fleet-wide hub restart."""
import ast
import sys

import feature_boundary as real_feature_boundary


def _load_ns():
    src = open("pr_review.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_automerge_decision":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "_automerge_decision not found in pr_review.py"
    ns = {"feature_boundary": real_feature_boundary}
    exec(seg, ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


# A fully-cleared baseline — every test case starts here and breaks exactly
# ONE thing, so a failure pinpoints which gate stopped enforcing.
def _clean_config(**overrides):
    cfg = {
        "feature_drive_enabled": True,
        "feature_automerge_enabled": True,
        "feature_automerge_repos": ["owner/repo"],
        "feature_automerge_min_confidence": 0.90,
        "feature_automerge_require_clean": True,
        "feature_boundaries": [],
    }
    cfg.update(overrides)
    return cfg


def _clean_rec(**overrides):
    rec = {
        "panel_status": "", "panel_verdict": "Approve", "panel_confidence": 0.95,
        "panel2_status": "", "panel2_verdict": "Approve", "panel2_confidence": 0.95,
        "errors": 0, "warnings": 0, "merged": False, "auto_merged": False,
    }
    rec.update(overrides)
    return rec


def _clean_pr_meta(**overrides):
    meta = {"repo": "owner/repo", "is_feature_drive": True, "draft": False,
            "state": "open", "mergeable": True}
    meta.update(overrides)
    return meta


def main():
    ok = True
    ns = _load_ns()
    decide = ns["_automerge_decision"]

    # ── the all-clear case: everything below is a controlled regression from this ─
    should, reason = decide(_clean_rec(), ["some/file.py"], _clean_config(), _clean_pr_meta())
    ok &= _check("all conditions cleared -> auto-merge approved", should is True)

    # ── two independent kill switches ────────────────────────────────────────
    should, reason = decide(_clean_rec(), [], _clean_config(feature_drive_enabled=False), _clean_pr_meta())
    ok &= _check("feature_drive_enabled off -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(feature_automerge_enabled=False), _clean_pr_meta())
    ok &= _check("feature_automerge_enabled off -> blocked", should is False)

    # ── per-repo opt-in (default-empty allowlist) ───────────────────────────
    should, reason = decide(_clean_rec(), [], _clean_config(feature_automerge_repos=[]), _clean_pr_meta())
    ok &= _check("repo not in feature_automerge_repos (default empty) -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(feature_automerge_repos=["other/repo"]), _clean_pr_meta())
    ok &= _check("repo present but a DIFFERENT one in the allowlist -> blocked", should is False)

    # ── THE containment: missing marker means human PRs can NEVER auto-merge ─
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(is_feature_drive=False))
    ok &= _check("PR without the feature-drive marker -> blocked EVEN AT PERFECT CONFIDENCE "
                "(this is what makes human PRs structurally ineligible)", should is False)

    # ── idempotency ──────────────────────────────────────────────────────────
    should, reason = decide(_clean_rec(merged=True), [], _clean_config(), _clean_pr_meta())
    ok &= _check("already merged -> blocked (idempotent no-op)", should is False)
    should, reason = decide(_clean_rec(auto_merged=True), [], _clean_config(), _clean_pr_meta())
    ok &= _check("already auto_merged -> blocked (idempotent no-op)", should is False)

    # ── PR-state gates ───────────────────────────────────────────────────────
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(draft=True))
    ok &= _check("draft PR -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(state="closed"))
    ok &= _check("closed PR -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(mergeable=False))
    ok &= _check("not cleanly mergeable (conflicts) -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(mergeable=None))
    ok &= _check("mergeable unknown (None, not True) -> blocked (fail closed on ambiguity)", should is False)

    # ── operational gates ────────────────────────────────────────────────────
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(), state_flags={"paused": True})
    ok &= _check("BugFixer paused -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(), state_flags={"blackout": True})
    ok &= _check("BugFixer in blackout -> blocked", should is False)

    # ── panel 1 must ACTUALLY have run and Approved ─────────────────────────
    should, reason = decide(_clean_rec(panel_status="queue_for_retry"), [], _clean_config(), _clean_pr_meta())
    ok &= _check("panel 1 could not run (queue_for_retry) -> blocked", should is False)
    should, reason = decide(_clean_rec(panel_verdict="Reject"), [], _clean_config(), _clean_pr_meta())
    ok &= _check("panel 1 verdict is Reject -> blocked", should is False)

    # ── panel 2 (state-logic) must ALSO have run and Approved ───────────────
    should, reason = decide(_clean_rec(panel2_status="queue_for_retry"), [], _clean_config(), _clean_pr_meta())
    ok &= _check("panel 2 could not run -> blocked (BOTH panels required, not just panel 1)", should is False)
    should, reason = decide(_clean_rec(panel2_verdict="Reject"), [], _clean_config(), _clean_pr_meta())
    ok &= _check("panel 2 verdict is Reject -> blocked", should is False)

    # ── confidence threshold, both panels independently ─────────────────────
    should, reason = decide(_clean_rec(panel_confidence=0.5), [], _clean_config(), _clean_pr_meta())
    ok &= _check("panel 1 confidence below threshold -> blocked", should is False)
    should, reason = decide(_clean_rec(panel2_confidence=0.5), [], _clean_config(), _clean_pr_meta())
    ok &= _check("panel 2 confidence below threshold -> blocked (checked independently)", should is False)
    should, reason = decide(_clean_rec(panel_confidence=None), [], _clean_config(), _clean_pr_meta())
    ok &= _check("panel 1 confidence missing (None) -> blocked, not treated as 0 or bypassed", should is False)
    # threshold defaults to 1.0 when unset/unparseable — effectively off
    should, reason = decide(_clean_rec(panel_confidence=0.99, panel2_confidence=0.99),
                            [], _clean_config(feature_automerge_min_confidence=None), _clean_pr_meta())
    ok &= _check("unset threshold defaults to 1.0 (0.99 confidence still blocked)", should is False)
    should, reason = decide(_clean_rec(panel_confidence=1.0, panel2_confidence=1.0),
                            [], _clean_config(feature_automerge_min_confidence="not-a-number"), _clean_pr_meta())
    ok &= _check("unparseable threshold falls back to 1.0, not crashes / not 0", should is True)

    # ── Tier-1 cleanliness ────────────────────────────────────────────────────
    should, reason = decide(_clean_rec(errors=1), [], _clean_config(), _clean_pr_meta())
    ok &= _check("Tier-1 errors present -> blocked when require_clean", should is False)
    should, reason = decide(_clean_rec(warnings=1), [], _clean_config(), _clean_pr_meta())
    ok &= _check("Tier-1 warnings present -> blocked when require_clean", should is False)
    should, reason = decide(_clean_rec(errors=1), [], _clean_config(feature_automerge_require_clean=False), _clean_pr_meta())
    ok &= _check("Tier-1 errors present but require_clean is off -> not blocked by this gate",
                should is True)

    # ── boundary check against the ACTUAL diff ───────────────────────────────
    boundaries = [{"id": "secrets", "paths": ["**/hub_agent.py"], "hard": True, "enabled": True}]
    should, reason = decide(_clean_rec(), ["bugfixer/hub_agent.py"],
                            _clean_config(feature_boundaries=boundaries), _clean_pr_meta())
    ok &= _check("diff touches a configured boundary path -> blocked", should is False)
    ok &= _check("blocking reason names the boundary id", "secrets" in reason)
    should, reason = decide(_clean_rec(), ["bugfixer/routes.py"],
                            _clean_config(feature_boundaries=boundaries), _clean_pr_meta())
    ok &= _check("diff does NOT touch the boundary path -> not blocked by this gate", should is True)
    should, reason = decide(_clean_rec(), ["bugfixer/hub_agent.py"],
                            _clean_config(feature_boundaries=[{**boundaries[0], "enabled": False}]),
                            _clean_pr_meta())
    ok &= _check("a DISABLED boundary rule never blocks even with a matching path", should is True)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature-automerge-gate self-test...")
    sys.exit(main())
