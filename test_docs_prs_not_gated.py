"""Documentation PRs are reviewed, but not accuracy-gated.

Operator policy: a docs change cannot alter runtime behaviour, so holding it
because a skeptical panel doubts a sentence's accuracy buys no safety and costs
a human round-trip. In practice it also held docs PRs forever — the panel
penalised "intent fidelity" on promote PRs whose generated body says "carries
code only" while the diff is a README rewrite, a critique the PR can never
satisfy.

Two halves, both tested here:
  * pr_review._automerge_decision  — a proven docs-only diff skips the two
    panels' verdict/confidence gates (and ONLY those).
  * pr_remediate.maybe_auto_remediate — a docs-only PR is not rewritten to
    satisfy a critique nothing is waiting on.

The safety half of this file matters more than the feature half: every
containment gate must still stop a docs PR. A false positive here merges
something unattended.
"""
import ast

import pytest

import branch_policy
import feature_allowlist as real_feature_allowlist
import feature_boundary as real_feature_boundary
import pr_remediate


# ---------------------------------------------------------------------------
# _automerge_decision harness (AST-extracted; pr_review isn't importable)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def decide():
    src = open("pr_review.py", encoding="utf-8").read()
    seg = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_automerge_decision":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "_automerge_decision not found"
    ns = {"feature_boundary": real_feature_boundary,
          "feature_allowlist": real_feature_allowlist,
          "is_release_locked": branch_policy.is_release_locked}
    exec(seg, ns)
    return ns["_automerge_decision"]


def _cfg(**over):
    cfg = {
        "feature_drive_enabled": True,
        "feature_automerge_enabled": True,
        "feature_automerge_repos": ["owner/repo"],
        "feature_automerge_target_branches": ["dev"],
        "feature_automerge_min_confidence": 0.90,
        "feature_automerge_require_clean": True,
        "feature_automerge_require_allowlist": True,
        "feature_boundaries": [],
    }
    cfg.update(over)
    return cfg


def _rec(**over):
    rec = {"panel_status": "", "panel_verdict": "Approve", "panel_confidence": 0.95,
           "panel2_status": "", "panel2_verdict": "Approve", "panel2_confidence": 0.95,
           "errors": 0, "warnings": 0, "merged": False, "auto_merged": False}
    rec.update(over)
    return rec


def _meta(**over):
    m = {"repo": "owner/repo", "base_ref": "dev", "is_feature_drive": False,
         "draft": False, "state": "open", "mergeable": True}
    m.update(over)
    return m


def _files(*paths):
    return [{"path": p, "status": "modified", "additions": 3, "deletions": 1,
             "patch": "@@ -1 +1 @@\n-old\n+new\n"} for p in paths]


DOCS = _files("README.md", "docs/architecture.md")
MIXED = _files("README.md", "src/app.py")
CODE = _files("src/app.py")

# The exact shape that used to deadlock a docs PR.
REJECTED = {"panel_verdict": "Reject", "panel_confidence": 0.41,
            "panel2_verdict": "Reject", "panel2_confidence": 0.38}


# ---------------------------------------------------------------------------
# The feature: docs are not accuracy-gated
# ---------------------------------------------------------------------------

def test_docs_pr_merges_despite_panel_rejection(decide):
    should, reason = decide(_rec(**REJECTED), ["README.md"], _cfg(), _meta(),
                            changed_files=DOCS)
    assert should is True
    assert "documentation-only" in reason


def test_docs_pr_merges_despite_low_confidence(decide):
    should, _ = decide(_rec(panel_confidence=0.10, panel2_confidence=0.10),
                       ["README.md"], _cfg(), _meta(), changed_files=DOCS)
    assert should is True


def test_docs_pr_merges_when_panel_could_not_run(decide):
    """If the panel's opinion isn't a gate for docs, its absence isn't either."""
    should, _ = decide(_rec(panel_status="unavailable", panel2_status="unavailable"),
                       ["README.md"], _cfg(), _meta(), changed_files=DOCS)
    assert should is True


@pytest.mark.parametrize("paths", [
    ("README.md",), ("docs/a.md", "docs/b.md"), ("notes.rst",), ("CHANGELOG.txt",),
])
def test_recognised_doc_shapes_bypass(decide, paths):
    should, _ = decide(_rec(**REJECTED), list(paths), _cfg(), _meta(),
                       changed_files=_files(*paths))
    assert should is True


def test_reason_explains_why_it_cleared(decide):
    _, reason = decide(_rec(**REJECTED), ["README.md"], _cfg(), _meta(), changed_files=DOCS)
    assert "not accuracy-gated" in reason


# ---------------------------------------------------------------------------
# Fails closed: anything not PROVABLY docs-only keeps the full panel gate
# ---------------------------------------------------------------------------

def test_mixed_docs_and_code_is_still_gated(decide):
    """One .py alongside the .md means behaviour may change — full gate."""
    should, reason = decide(_rec(**REJECTED), ["README.md", "src/app.py"], _cfg(), _meta(),
                            changed_files=MIXED)
    assert should is False
    assert "panel 1" in reason


def test_code_only_pr_is_still_gated(decide):
    should, reason = decide(_rec(**REJECTED), ["src/app.py"], _cfg(), _meta(),
                            changed_files=CODE)
    assert should is False
    assert "panel 1" in reason


def test_absent_changed_files_does_not_bypass(decide):
    """A caller with only path strings cannot prove docs-only — fail closed."""
    should, reason = decide(_rec(**REJECTED), ["README.md"], _cfg(), _meta(),
                            changed_files=None)
    assert should is False
    assert "panel 1" in reason


def test_empty_changed_files_does_not_bypass(decide):
    should, _ = decide(_rec(**REJECTED), [], _cfg(), _meta(), changed_files=[])
    assert should is False


def test_bypass_can_be_turned_off(decide):
    should, reason = decide(_rec(**REJECTED), ["README.md"],
                            _cfg(feature_automerge_docs_bypass_panel=False), _meta(),
                            changed_files=DOCS)
    assert should is False
    assert "panel 1" in reason


def test_operator_removing_docs_from_allowlist_restores_the_gate(decide):
    """feature_automerge_allowlist is the operator's narrowing control; dropping
    docs-only from it must re-gate docs PRs rather than silently bypassing."""
    should, _ = decide(_rec(**REJECTED), ["README.md"],
                       _cfg(feature_automerge_allowlist=["log-only"]), _meta(),
                       changed_files=DOCS)
    assert should is False


# ---------------------------------------------------------------------------
# Containment gates are UNCHANGED for docs PRs — the important half
# ---------------------------------------------------------------------------

def test_docs_pr_into_main_is_still_refused(decide):
    """Merges into the release branch are owner-only, at any confidence."""
    should, reason = decide(_rec(), ["README.md"],
                            _cfg(feature_automerge_target_branches=["dev", "main"]),
                            _meta(base_ref="main"), changed_files=DOCS)
    assert should is False
    assert "release branch" in reason


def test_docs_pr_with_a_secret_is_still_blocked(decide):
    """Tier-1 findings catch a credential committed into a .md — docs bypass
    relaxes the PANEL's opinion, never the secret scan."""
    should, reason = decide(_rec(errors=1, **REJECTED), ["README.md"], _cfg(), _meta(),
                            changed_files=DOCS)
    assert should is False
    assert "Tier-1" in reason


def test_docs_pr_with_tier1_warning_is_still_blocked(decide):
    should, _ = decide(_rec(warnings=1), ["README.md"], _cfg(), _meta(), changed_files=DOCS)
    assert should is False


def test_docs_pr_under_release_lock_is_still_held(decide):
    should, reason = decide(_rec(), ["README.md"], _cfg(release_locked_branches=["dev"]),
                            _meta(), changed_files=DOCS)
    assert should is False
    assert "release lock" in reason.lower()


def test_docs_pr_touching_a_boundary_is_still_blocked(decide):
    cfg = _cfg(feature_boundaries=[{"id": "secrets-docs", "paths": ["docs/secrets/*"]}])
    files = _files("docs/secrets/creds.md")
    should, reason = decide(_rec(), ["docs/secrets/creds.md"], cfg, _meta(),
                            changed_files=files)
    assert should is False
    assert "boundary" in reason


@pytest.mark.parametrize("meta_over,expected", [
    ({"draft": True}, "draft"),
    ({"state": "closed"}, "not open"),
    ({"mergeable": None}, "mergeable"),
    ({"mergeable": False}, "mergeable"),
    ({"repo": "other/repo"}, "feature_automerge_repos"),
    ({"base_ref": "staging"}, "feature_automerge_target_branches"),
])
def test_structural_gates_still_apply_to_docs(decide, meta_over, expected):
    should, reason = decide(_rec(), ["README.md"], _cfg(), _meta(**meta_over),
                            changed_files=DOCS)
    assert should is False
    assert expected in reason


@pytest.mark.parametrize("flags", [{"paused": True}, {"blackout": True}])
def test_paused_and_blackout_still_apply_to_docs(decide, flags):
    should, _ = decide(_rec(), ["README.md"], _cfg(), _meta(), state_flags=flags,
                       changed_files=DOCS)
    assert should is False


@pytest.mark.parametrize("kill", ["feature_drive_enabled", "feature_automerge_enabled"])
def test_kill_switches_still_apply_to_docs(decide, kill):
    should, _ = decide(_rec(), ["README.md"], _cfg(**{kill: False}), _meta(),
                       changed_files=DOCS)
    assert should is False


def test_already_merged_docs_pr_is_idempotent(decide):
    should, reason = decide(_rec(auto_merged=True), ["README.md"], _cfg(), _meta(),
                            changed_files=DOCS)
    assert should is False
    assert "already merged" in reason


# ---------------------------------------------------------------------------
# PR #261 review findings (state-logic panel + broad panel)
# ---------------------------------------------------------------------------

def test_docs_bypass_refuses_when_record_is_for_a_different_head(decide):
    """rec["errors"]/["warnings"] being 0 is indistinguishable from "Tier-1
    never ran" unless the record is proven fresh for the CURRENT head — a
    stale/mismatched record must not clear the docs bypass."""
    should, reason = decide(_rec(head="old-sha"), ["README.md"],
                            _cfg(), _meta(head_sha="new-sha"), changed_files=DOCS)
    assert should is False
    assert "no pre-review record" in reason


def test_docs_bypass_refuses_when_no_record_exists_yet(decide):
    """The empty-dict rec a caller falls back to (no record at all) must not
    be treated as "Tier-1 ran clean"."""
    should, _ = decide({}, ["README.md"], _cfg(), _meta(head_sha="new-sha"),
                       changed_files=DOCS)
    assert should is False


def test_docs_bypass_still_clears_when_head_matches(decide):
    should, _ = decide(_rec(head="abc123"), ["README.md"], _cfg(),
                       _meta(head_sha="abc123"), changed_files=DOCS)
    assert should is True


def test_docs_bypass_freshness_check_is_a_noop_without_head_sha(decide):
    """Callers that don't pass pr_meta["head_sha"] (e.g. older/other call
    sites, and every existing test in this file) keep prior behaviour."""
    should, _ = decide(_rec(**REJECTED), ["README.md"], _cfg(), _meta(),
                       changed_files=DOCS)
    assert should is True


def test_docs_bypass_reason_is_honest_when_panel_could_not_run(decide):
    """Don't claim "reviewed" in the merge reason when the panel(s) never
    actually ran for this diff — the earlier wording said "reviewed but not
    accuracy-gated" even when panel_status showed a failure."""
    should, reason = decide(_rec(panel_status="unavailable", panel2_status="unavailable"),
                            ["README.md"], _cfg(), _meta(), changed_files=DOCS)
    assert should is True
    assert "could not run" in reason
    assert "reviewed but not accuracy-gated" not in reason


def test_docs_bypass_reason_still_says_reviewed_when_panel_ran(decide):
    should, reason = decide(_rec(**REJECTED), ["README.md"], _cfg(), _meta(),
                            changed_files=DOCS)
    assert should is True
    assert "reviewed but not accuracy-gated" in reason


# ---------------------------------------------------------------------------
# maybe_auto_remediate — don't rewrite docs to satisfy a non-gate
# ---------------------------------------------------------------------------

class _PR:
    def __init__(self, paths, boom=False):
        self.number, self.draft, self.merged, self.state = 5, False, False, "open"
        self._paths, self._boom = paths, boom

    def get_files(self):
        if self._boom:
            raise RuntimeError("GitHub API down")
        return [type("F", (), {"filename": p, "status": "modified", "additions": 1,
                               "deletions": 0, "patch": "@@ -1 +1 @@\n-a\n+b\n"})()
                for p in self._paths]


class _Repo:
    full_name = "owner/repo"


@pytest.fixture
def remediate_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(pr_remediate, "auto_remediate_pr",
                        lambda gh, repo, pr, config: (calls.append(pr) or (True, "remediated")))
    monkeypatch.setattr(pr_remediate, "state", {"pr_reviews": {}})
    return calls


def test_docs_only_pr_is_not_remediated(remediate_calls):
    ok, reason = pr_remediate.maybe_auto_remediate(
        None, _Repo(), _PR(["README.md", "docs/x.md"]), {})
    assert ok is False
    assert "documentation-only" in reason
    assert remediate_calls == []


def test_code_pr_is_still_remediated(remediate_calls):
    ok, _ = pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(["src/app.py"]), {})
    assert ok is True
    assert len(remediate_calls) == 1


def test_mixed_pr_is_still_remediated(remediate_calls):
    ok, _ = pr_remediate.maybe_auto_remediate(
        None, _Repo(), _PR(["README.md", "src/app.py"]), {})
    assert ok is True
    assert len(remediate_calls) == 1


def test_docs_skip_can_be_turned_off(remediate_calls):
    ok, _ = pr_remediate.maybe_auto_remediate(
        None, _Repo(), _PR(["README.md"]), {"pr_auto_remediate_skip_docs_only": False})
    assert ok is True
    assert len(remediate_calls) == 1


def test_unreadable_files_fall_through_to_remediation(remediate_calls):
    """The docs check is an optimisation, not a gate — if it can't run, behave
    exactly as before rather than silently skipping remediation."""
    ok, _ = pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(["README.md"], boom=True), {})
    assert ok is True
    assert len(remediate_calls) == 1


def test_existing_remediation_guards_still_win(remediate_calls, monkeypatch):
    monkeypatch.setattr(pr_remediate, "state", {
        "pr_reviews": {"owner/repo#5": {"auto_remediate_blocked": True}}})
    ok, reason = pr_remediate.maybe_auto_remediate(None, _Repo(), _PR(["src/app.py"]), {})
    assert ok is False
    assert "guardrail" in reason
    assert remediate_calls == []
