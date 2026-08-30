#!/usr/bin/env python3
"""Self-test for pr_review._automerge_decision — the fence around the ONE
deliberate exception to "AppBuilder never auto-approves/auto-merges".

Run:  python3 ab/test_feature_automerge_gate.py

pr_review.py imports `main`/`app_state` (real app-context modules whose
import fully boots the live app as a side effect in this checkout — see
test_skills_loader.py's docstring), so this extracts _automerge_decision by
source via ast and execs it with feature_boundary imported for real (pure,
standalone — more faithful than reimplementing its matching logic here).

UPDATED 2026-08-29: the containment used to be PR authorship (only PRs
carrying the feature-drive marker were eligible — human PRs were
structurally blocked by pr_meta["is_feature_drive"]). Now that more than one
person works on this system, containment is by TARGET BRANCH instead:
pr_meta["base_ref"] must be in feature_automerge_target_branches (opt-in,
defaults to empty). ANY PR — human or bot — targeting an allowlisted branch
(e.g. "dev") is now eligible if both panels approve at threshold; a PR
targeting main, or any unlisted branch, never is, regardless of author.

This is the single most safety-critical test in the whole feature auto-drive
project: EVERY case must resolve to (False, reason) except the one fully-
cleared, all-conditions-met case at the end. A False-negative here (a case
that should block but doesn't) means an unattended, unreviewed PR merges
into a real repo — for `lm`, an unattended fleet-wide hub restart."""
import ast
import sys

import feature_boundary as real_feature_boundary
import feature_allowlist as real_feature_allowlist


def _load_ns():
    src = open("pr_review.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_automerge_decision":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "_automerge_decision not found in pr_review.py"
    import branch_policy
    ns = {"feature_boundary": real_feature_boundary,
          "feature_allowlist": real_feature_allowlist,
          "is_release_locked": branch_policy.is_release_locked}
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
        "feature_automerge_target_branches": ["dev"],
        "feature_automerge_min_confidence": 0.90,
        "feature_automerge_require_clean": True,
        # Existing cases below isolate ONE non-allowlist gate each, so keep the
        # DEFAULT-DENY allowlist gate off in this baseline; it has its own
        # dedicated section at the end of main(). (In production it defaults ON.)
        "feature_automerge_require_allowlist": False,
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
    # is_feature_drive defaults to False here deliberately — the clean/
    # baseline case is now a HUMAN-authored PR to prove that's genuinely
    # eligible, which is the whole point of this change.
    meta = {"repo": "owner/repo", "base_ref": "dev", "is_feature_drive": False,
            "draft": False, "state": "open", "mergeable": True}
    meta.update(overrides)
    return meta


def main():
    ok = True
    ns = _load_ns()
    decide = ns["_automerge_decision"]

    # ── the all-clear case: a HUMAN PR (is_feature_drive=False) to dev, everything
    # else clean. This is the actual point of the 2026-08-29 change — prove it
    # explicitly rather than just asserting it as a side effect. ─────────────
    should, reason = decide(_clean_rec(), ["some/file.py"], _clean_config(), _clean_pr_meta())
    ok &= _check("human PR, all conditions cleared -> auto-merge approved", should is True)

    # ── release lock: a fully-cleared PR is HELD (not merged) while its target
    # branch is locked, and flows again once the lock is lifted. Review still
    # runs; only the unattended merge waits. ─────────────────────────────────
    should, reason = decide(_clean_rec(), ["some/file.py"],
                            _clean_config(release_locked_branches=["dev"]), _clean_pr_meta())
    ok &= _check("target branch under release lock -> held (not merged)", should is False)
    ok &= _check("release-lock reason names the lock",
                 should is False and "release lock" in (reason or "").lower())
    should, reason = decide(_clean_rec(), ["some/file.py"],
                            _clean_config(release_locked_branches=["qa"]), _clean_pr_meta())
    ok &= _check("a DIFFERENT branch locked -> this dev PR still flows", should is True)

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

    # ── THE containment (as of 2026-08-29): target branch, not authorship ───
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(base_ref="main"))
    ok &= _check("PR targeting main -> blocked EVEN AT PERFECT CONFIDENCE, "
                "even from a feature-drive PR (this is what makes main structurally "
                "ineligible now that authorship no longer gates)",
                should is False and not decide(_clean_rec(), [], _clean_config(),
                    _clean_pr_meta(base_ref="main", is_feature_drive=True))[0])
    should, reason = decide(_clean_rec(), [], _clean_config(feature_automerge_target_branches=[]),
                            _clean_pr_meta())
    ok &= _check("target branch allowlist empty (default) -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(feature_automerge_target_branches=["staging"]),
                            _clean_pr_meta())
    ok &= _check("target branch not the specific one allowlisted -> blocked", should is False)

    # ── release branches are refused STRUCTURALLY, not just by omission ─────
    # The cases above only prove main is blocked when it is absent from the
    # allowlist. That is containment by configuration: the allowlist is a
    # free-text Settings field, so a single typo used to make an unattended
    # merge onto the release branch eligible. These pin that main/master/the
    # configured default_branch are refused EVEN WHEN EXPLICITLY LISTED —
    # merges into the release branch are owner-only, and that now lives in code.
    for _br in ("main", "master"):
        should, reason = decide(_clean_rec(), [],
                                _clean_config(feature_automerge_target_branches=["dev", "qa", _br]),
                                _clean_pr_meta(base_ref=_br))
        ok &= _check(f"{_br} EXPLICITLY listed in feature_automerge_target_branches -> "
                     "still blocked (config cannot override the release-branch refusal)",
                     should is False and "release branch" in reason)
    should, reason = decide(_clean_rec(), [],
                            _clean_config(feature_automerge_target_branches=["release"],
                                          default_branch="release"),
                            _clean_pr_meta(base_ref="release"))
    ok &= _check("a non-'main' default_branch, explicitly listed -> still blocked "
                 "(the refusal follows default_branch, not the literal name 'main')",
                 should is False and "release branch" in reason)

    # ── the qa/dev chain stays eligible: the refusal must not over-reach ────
    for _br in ("dev", "qa"):
        should, reason = decide(_clean_rec(), [],
                                _clean_config(feature_automerge_target_branches=["dev", "qa"]),
                                _clean_pr_meta(base_ref=_br))
        ok &= _check(f"{_br} listed -> ALLOWED (release-branch refusal does not "
                     "over-block the non-release chain)", should is True)
    should, reason = decide(_clean_rec(), [],
                            _clean_config(feature_automerge_target_branches=["dev", "qa"],
                                          default_branch="release"),
                            _clean_pr_meta(base_ref="qa"))
    ok &= _check("qa still allowed when default_branch is some other branch entirely",
                 should is True)
    # ── explicit proof of the NEW capability: human PR to an allowlisted branch ─
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(is_feature_drive=False, base_ref="dev"))
    ok &= _check("human-authored PR (no feature-drive marker) targeting dev -> "
                "NOW ELIGIBLE if everything else clears (the whole point of this change)",
                should is True)

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
    ok &= _check("AppBuilder paused -> blocked", should is False)
    should, reason = decide(_clean_rec(), [], _clean_config(), _clean_pr_meta(), state_flags={"blackout": True})
    ok &= _check("AppBuilder in blackout -> blocked", should is False)

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
    should, reason = decide(_clean_rec(), ["ab/hub_agent.py"],
                            _clean_config(feature_boundaries=boundaries), _clean_pr_meta())
    ok &= _check("diff touches a configured boundary path -> blocked", should is False)
    ok &= _check("blocking reason names the boundary id", "secrets" in reason)
    should, reason = decide(_clean_rec(), ["ab/routes.py"],
                            _clean_config(feature_boundaries=boundaries), _clean_pr_meta())
    ok &= _check("diff does NOT touch the boundary path -> not blocked by this gate", should is True)
    should, reason = decide(_clean_rec(), ["ab/hub_agent.py"],
                            _clean_config(feature_boundaries=[{**boundaries[0], "enabled": False}]),
                            _clean_pr_meta())
    ok &= _check("a DISABLED boundary rule never blocks even with a matching path", should is True)

    # ── DEFAULT-DENY allowlist gate (feature_allowlist) ─────────────────────
    # When require_allowlist is ON, a fully-cleared PR still only auto-merges
    # if its diff is a provably-additive shape. This is the positive gate that
    # makes "behaviour-changing -> human" mechanical, not trust-based.
    docs_files = [{"path": "ab/docs/foo.md", "status": "modified",
                   "additions": 2, "deletions": 1, "patch": "@@ -1 +1,2 @@\n-old\n+new\n+more\n"}]
    code_files = [{"path": "ab/routes.py", "status": "modified", "additions": 5,
                   "deletions": 0, "patch": "@@ -1 +1,5 @@\n+def handler():\n+    return do_thing()\n"}]
    log_files = [{"path": "ab/routes.py", "status": "modified", "additions": 1,
                  "deletions": 0, "patch": "@@ -1 +1,2 @@\n+    logger.info('started thing')\n"}]

    should, reason = decide(_clean_rec(), ["ab/routes.py"],
                            _clean_config(feature_automerge_require_allowlist=True),
                            _clean_pr_meta())
    ok &= _check("allowlist ON but NO changed_files supplied -> blocked (fail closed)", should is False)
    should, reason = decide(_clean_rec(), ["ab/routes.py"],
                            _clean_config(feature_automerge_require_allowlist=True),
                            _clean_pr_meta(), None, code_files)
    ok &= _check("allowlist ON + behaviour-changing code diff -> blocked (routes to human)", should is False)
    should, reason = decide(_clean_rec(), ["ab/docs/foo.md"],
                            _clean_config(feature_automerge_require_allowlist=True),
                            _clean_pr_meta(), None, docs_files)
    ok &= _check("allowlist ON + docs-only diff -> auto-merge approved", should is True)
    should, reason = decide(_clean_rec(), ["ab/routes.py"],
                            _clean_config(feature_automerge_require_allowlist=True),
                            _clean_pr_meta(), None, log_files)
    ok &= _check("allowlist ON + pure log-only diff -> auto-merge approved", should is True)
    should, reason = decide(_clean_rec(), ["ab/docs/foo.md"],
                            _clean_config(feature_automerge_require_allowlist=True,
                                          feature_automerge_allowlist=["log-only"]),
                            _clean_pr_meta(), None, docs_files)
    ok &= _check("allowlist ON but docs-only NOT in the enabled subset -> blocked", should is False)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature-automerge-gate self-test...")
    sys.exit(main())
