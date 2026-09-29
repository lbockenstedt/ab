#!/usr/bin/env python3
"""Self-test for the promotion confidence bypass in pr_review._automerge_decision.

Run:  python3 ab/test_promotion_confidence_bypass.py

THE DEADLOCK THIS PINS (lm#1070, 2026-09-29)
--------------------------------------------
A promotion PR reached this exact state and could never leave it:

    panel_verdict   Approve   panel_confidence   0.92    -> clears 0.90
    panel2_verdict  Approve   panel2_confidence  0.8833  -> BELOW 0.90
    findings 0   errors 0   warnings 0   dissents 0/0   composite Approve

The merge gate held it for the panel-2 deficit. The remediation guard
(pr_remediate._promotion_evidence_is_actionable) refused to edit it, correctly:
a promotion branch carrying no stated defect is exactly what the 206-push-a-day
livelock was made of. Refusing to edit AND refusing to merge is the same refusal
applied twice, so the PR polled silently forever (the cached review path logs
nothing, and the merge hold logs only when its reason CHANGES).

The bypass skips THE CONFIDENCE THRESHOLD AND NOTHING ELSE, only for a
promotion/back-merge head that both panels Approve with no filed
finding. The threshold itself is never lowered -- every case below proves some
way the full 0.90 bar is still enforced.

Like test_feature_automerge_gate.py, this extracts _automerge_decision by source
via ast: pr_review.py imports main/app_state, whose import boots the live app.
That is also WHY the branch prefixes are inlined in the function rather than
referencing pr_remediate._PROMOTION_HEAD_RE -- the last case here pins the two
in agreement so they cannot drift apart silently.
"""
import ast
import os
import re
import sys

import feature_boundary as real_feature_boundary
import feature_allowlist as real_feature_allowlist

_HERE = os.path.dirname(os.path.abspath(__file__))


def _src(name):
    """Read a sibling module's source. Absolute, not cwd-relative: this file is
    also collected by pytest (see test_promotion_confidence_bypass at the
    bottom), which does not guarantee the repo root is the working directory."""
    with open(os.path.join(_HERE, name)) as fh:
        return fh.read()


def _load_ns():
    src = _src("pr_review.py")
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
    return bool(cond)


def _config(**overrides):
    cfg = {
        "feature_drive_enabled": True,
        "feature_automerge_enabled": True,
        "feature_automerge_repos": ["lbockenstedt/lm"],
        "feature_automerge_target_branches": ["dev", "qa", "main"],
        "feature_automerge_min_confidence": 0.90,
        "feature_automerge_require_clean": True,
        "feature_automerge_require_allowlist": False,
        "feature_boundaries": [],
    }
    cfg.update(overrides)
    return cfg


def _rec(**overrides):
    """The lm#1070 record, verbatim in shape: panel 2 a hair under the bar,
    nothing else wrong with it."""
    rec = {
        "panel_status": "", "panel_verdict": "Approve", "panel_confidence": 0.92,
        "panel2_status": "", "panel2_verdict": "Approve",
        "panel2_confidence": 0.8833333333333333,
        "panel_dissents": 0, "panel2_dissents": 0,
        "findings": 0, "errors": 0, "warnings": 0,
        "merged": False, "auto_merged": False,
        "head_ref": "promote/dev-to-qa", "base_ref": "qa",
    }
    rec.update(overrides)
    return rec


def _meta(**overrides):
    meta = {"repo": "lbockenstedt/lm", "base_ref": "qa", "is_feature_drive": False,
            "draft": False, "state": "open", "mergeable": True}
    meta.update(overrides)
    return meta


def main():
    ok = True
    decide = _load_ns()["_automerge_decision"]
    paths = ["agent/src/agent_spoke.py"]

    print("\n-- the deadlock itself --")
    should, reason = decide(_rec(), paths, _config(), _meta())
    ok &= _check("lm#1070 shape (panel 2 at 0.8833, nothing else wrong) -> merges", should is True)
    ok &= _check("reason names it a promotion PR",
                 should is True and "promotion PR" in (reason or ""))
    ok &= _check("reason is honest that the deficit is unresolvable",
                 should is True and "unresolvable" in (reason or ""))

    print("\n-- the bypass is opt-out-able and narrow --")
    should, reason = decide(
        _rec(), paths,
        _config(feature_automerge_promotion_confidence_bypass=False), _meta())
    ok &= _check("bypass switched OFF -> held at the threshold again", should is False)
    ok &= _check("...and the reason is the confidence deficit",
                 should is False and "below the threshold" in (reason or ""))

    print("\n-- the 0.90 threshold is NOT lowered for anything else --")
    for head in ("feature/add-a-thing", "bug/fix-a-thing", "ai-feature/x", "", None):
        should, _r = decide(_rec(head_ref=head), paths, _config(), _meta())
        ok &= _check("non-promotion head %r at 0.8833 -> still blocked" % (head,),
                     should is False)
    # A promotion head must not let a LOW score through either -- the bypass is
    # justified by "no stated defect", and it applies whatever the number is,
    # so prove the other gates (not the number) are what contain it.
    should, _r = decide(_rec(panel2_verdict="Reject"), paths, _config(), _meta())
    ok &= _check("promotion head but panel 2 did NOT Approve -> blocked", should is False)
    should, _r = decide(_rec(panel_verdict="Request changes"), paths, _config(), _meta())
    ok &= _check("promotion head but panel 1 did NOT Approve -> blocked", should is False)
    should, _r = decide(_rec(panel_status="queue_for_retry"), paths, _config(), _meta())
    ok &= _check("promotion head but panel 1 could not RUN -> blocked", should is False)
    should, _r = decide(_rec(panel2_status="queue_for_retry"), paths, _config(), _meta())
    ok &= _check("promotion head but panel 2 could not RUN -> blocked", should is False)

    print("\n-- any stated defect at all revokes the bypass --")
    for field in ("findings", "errors", "warnings"):
        should, _r = decide(_rec(**{field: 1}), paths, _config(), _meta())
        ok &= _check("%s=1 -> bypass revoked, held at the threshold" % field, should is False)

    print("\n-- a dissent is NOT a stated defect (lm#1070, second deadlock) --")
    # This is the complement invariant, and it is the whole reason this file
    # exists. pr_remediate._promotion_evidence_is_actionable reads verdicts and
    # filed findings ONLY -- it does not read dissents -- so a promotion PR that
    # comes back Approve/Approve with zero findings and a dissent is one that
    # remediation is FORBIDDEN to edit. If the merge gate also refuses it, the
    # PR has no path to any terminal state and polls silently forever, which is
    # exactly what lm#1070 did at 0.8633 with one dissent per panel. Any
    # condition this gate applies that the guardrail does not also treat as
    # actionable re-opens that deadlock.
    for kw in ({"panel_dissents": 1}, {"panel2_dissents": 1},
               {"panel_dissents": 2, "panel2_dissents": 3}):
        should, reason = decide(_rec(**kw), paths, _config(), _meta())
        ok &= _check("%r -> still merges (remediation may not act on a dissent)" % (kw,),
                     should is True and "promotion PR" in (reason or ""))
    # The live lm#1070 record, verbatim from /etc/ab/pr_reviews.json.
    should, reason = decide(
        _rec(panel_confidence=0.8633333333333333, panel2_confidence=0.88,
             panel_dissents=1, panel2_dissents=1, panel_rated=3, panel2_rated=3,
             findings=0, errors=0, warnings=0, tier1_external=0),
        paths, _config(), _meta())
    ok &= _check("the exact lm#1070 deadlock record merges", should is True)

    print("\n-- the bypass and the remediation guardrail must be COMPLEMENTS --")
    # Enumerate the shapes a promotion PR can hold below the bar and assert that
    # for every one of them exactly one side is willing to move it: either
    # remediation may edit it, or the merge gate clears it. Never neither.
    import pr_remediate
    for verdict1 in ("Approve", "Reject"):
        for verdict2 in ("Approve", "Reject"):
            for dis in (0, 1):
                for err in (0, 1):
                    rec = _rec(panel_verdict=verdict1, panel2_verdict=verdict2,
                               panel_dissents=dis, panel2_dissents=dis,
                               errors=err, warnings=err, findings=err)
                    actionable = pr_remediate._promotion_evidence_is_actionable(rec)
                    merges, _r = decide(rec, paths, _config(), _meta())
                    ok &= _check(
                        "v=%s/%s dissent=%d defect=%d -> remediable=%s merges=%s (not both stuck)"
                        % (verdict1, verdict2, dis, err, actionable, merges),
                        bool(actionable) or bool(merges))

    print("\n-- both promotion head shapes, and a clean one still merges normally --")
    should, _r = decide(_rec(head_ref="backmerge/qa-to-dev"), paths, _config(), _meta())
    ok &= _check("backmerge/ head -> bypass applies", should is True)
    should, _r = decide(_rec(head_ref="promote/qa-to-main", base_ref="main"), paths,
                        _config(feature_automerge_allow_release_branch=True),
                        _meta(base_ref="main"))
    ok &= _check("promote/qa-to-main with the release opt-out on -> merges", should is True)
    should, _r = decide(_rec(panel_confidence=0.99, panel2_confidence=0.99), paths,
                        _config(), _meta())
    ok &= _check("a promotion PR already ABOVE the bar still merges", should is True)

    print("\n-- the bypass must not raise on a missing confidence --")
    # min(None, 0.88) is a TypeError; this gate must never raise out.
    for c1, c2 in ((None, 0.88), (0.92, None), (None, None)):
        try:
            should, reason = decide(_rec(panel_confidence=c1, panel2_confidence=c2),
                                    paths, _config(), _meta())
            raised = False
        except Exception as e:  # noqa: BLE001
            should, reason, raised = None, str(e), True
        ok &= _check("confidences (%r, %r) -> no exception, merges" % (c1, c2),
                     raised is False and should is True)
    should, reason = decide(_rec(panel_confidence=None, panel2_confidence=None),
                            paths, _config(), _meta())
    ok &= _check("...and the reason degrades to 'n/a' rather than a crash",
                 "n/a" in (reason or ""))

    print("\n-- EVERY containment gate still applies to a bypassed PR --")
    cases = [
        ("feature_drive_enabled off", _config(feature_drive_enabled=False), _meta(), _rec()),
        ("feature_automerge_enabled off", _config(feature_automerge_enabled=False), _meta(), _rec()),
        ("repo not opted in", _config(feature_automerge_repos=[]), _meta(), _rec()),
        ("target branch not opted in", _config(feature_automerge_target_branches=["dev"]),
         _meta(), _rec()),
        ("release branch without the opt-out", _config(), _meta(base_ref="main"),
         _rec(head_ref="promote/qa-to-main")),
        ("release lock on the target", _config(release_locked_branches=["qa"]), _meta(), _rec()),
        ("PR is a draft", _config(), _meta(draft=True), _rec()),
        ("PR is not open", _config(), _meta(state="closed"), _rec()),
        ("PR is not cleanly mergeable", _config(), _meta(mergeable=False), _rec()),
        ("PR mergeability unknown", _config(), _meta(mergeable=None), _rec()),
        ("already merged", _config(), _meta(), _rec(merged=True)),
        ("record is for a different head", _config(),
         _meta(head_sha="aaaa"), _rec(head="bbbb")),
    ]
    for label, cfg, meta, rec in cases:
        should, _r = decide(rec, paths, cfg, meta)
        ok &= _check("%s -> still blocked" % label, should is False)

    should, _r = decide(_rec(), paths, _config(), _meta(), {"paused": True})
    ok &= _check("AppBuilder paused -> still blocked", should is False)
    should, _r = decide(_rec(), paths, _config(), _meta(), {"blackout": True})
    ok &= _check("AppBuilder in a blackout window -> still blocked", should is False)
    should, _r = decide(_rec(), ["agent/src/agent_spoke.py"],
                        _config(feature_boundaries=[{"id": "agent", "paths": ["agent/**"]}]),
                        _meta())
    ok &= _check("diff touches a configured boundary -> still blocked", should is False)
    should, _r = decide(_rec(), paths, _config(feature_automerge_require_allowlist=True),
                        _meta(), None, [])
    ok &= _check("default-deny allowlist ON -> still blocked", should is False)
    # Tier-1 is the secrets gate; it is already covered by errors/warnings above,
    # but pin it against require_clean explicitly so turning that key off cannot
    # quietly widen the bypass into "merge a promotion PR carrying a secret".
    should, _r = decide(_rec(errors=1), paths,
                        _config(feature_automerge_require_clean=True), _meta())
    ok &= _check("Tier-1 error -> still blocked with require_clean on", should is False)

    print("\n-- the inlined prefixes agree with pr_remediate's regex --")
    # The function is exec'd standalone, so it CANNOT import pr_remediate; the
    # prefixes are literals. If either side is ever edited alone, fail here
    # rather than silently disagreeing about what a promotion branch is.
    rsrc = _src("pr_remediate.py")
    m = re.search(r'^_PROMOTION_HEAD_RE\s*=\s*re\.compile\(\s*r?["\'](.+?)["\']', rsrc, re.M)
    ok &= _check("_PROMOTION_HEAD_RE found in pr_remediate.py", m is not None)
    if m:
        rx = re.compile(m.group(1))
        for ref in ("promote/dev-to-qa", "backmerge/qa-to-dev"):
            ok &= _check("pr_remediate agrees %r is a promotion head" % ref,
                         bool(rx.match(ref)))
            should, _r = decide(_rec(head_ref=ref), paths, _config(), _meta())
            ok &= _check("...and the inlined check bypasses it too", should is True)
        for ref in ("feature/x", "bug/x"):
            ok &= _check("pr_remediate agrees %r is NOT a promotion head" % ref,
                         not rx.match(ref))
            should, _r = decide(_rec(head_ref=ref), paths, _config(), _meta())
            ok &= _check("...and the inlined check does NOT bypass it", should is False)

    psrc = _src("pr_review.py")
    seg = psrc[psrc.index("def _automerge_decision"):]
    seg = seg[:seg.index("\n_AUTOMERGE_NOTE_MARKER")]
    ok &= _check("the bypass only ever relaxes the confidence comparison",
                 "if not docs_bypass and not promotion_bypass:" in seg
                 and seg.count("promotion_bypass") >= 3)
    ok &= _check("the panel verdict block is NOT relaxed by the bypass",
                 'if not docs_bypass:\n        if rec.get("panel_status"):' in seg)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


def test_promotion_confidence_bypass():
    """pytest entry point. CI runs `pytest -q .` (see .github/workflows/ci.yml),
    which collects nothing from a script whose only entry point is main() under
    __main__ -- test_feature_automerge_gate.py has that shape and is therefore
    never actually executed by CI. Expose the run so this one genuinely gates
    the promotion flow it is about."""
    assert main() == 0


if __name__ == "__main__":
    print("Running promotion-confidence-bypass self-test...")
    sys.exit(main())
