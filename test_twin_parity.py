"""check_parity / _resolve_cross_repo_twins — dns & dhcp retirement + twin resolver.

The standalone dns / dhcp repos are retired: lm/dns and lm/dhcp are the only
live copies, so lm PRs get no twin advisory and code changes in the retired
repos are flagged. The resolver tests below still use dns-shaped findings.

HISTORY
The dns and dhcp modules used to exist in two shapes: a standalone repo (`dns`, root =
the module) and a copy nested in the lm repo (`lm/dns/`). The advisory used to
map EVERY changed path 1:1 onto the other shape, but the two shapes do not have
the same file set -- of the 20 files in the dns repo only 4 have any counterpart
under lm/dns, and one of those (VERSION) must never be synced at all.

The concrete failure: rolling shared CI plumbing into the dns repo produced
three "twin NOT updated" WARNINGs naming `lm/dns/.github/scripts/promote.sh`,
`lm/dns/.github/workflows/backmerge.yml` and `.../branch-flow.yml`. None of
those can exist -- `lm/dns` is a subdirectory of the lm repo and cannot have its
own `.github/` -- so no PR could ever satisfy them. An advisory that cannot be
satisfied is worse than none, because it teaches people to ignore the ones that
can.

pr_review.py cannot be imported (circular import via github_ops -> main), so the
functions under test are extracted with ast and exec'd against a stub namespace,
the same approach test_promote_routes.py uses.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(ROOT, "pr_review.py")).read()


def _extract(name, src=SRC):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in pr_review.py")


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _parity():
    ns = {}
    exec(_extract("_basename"), ns)
    exec(_extract("check_parity"), ns)
    return ns["check_parity"]


check_parity = _parity()


def _twins(findings):
    """Just the cross-repo twin advisories, as (twin_repo, twin_path)."""
    return [(f["twin"]["repo"], f["twin"]["path"]) for f in findings if f.get("twin")]


# --------------------------------------------------------------------------
# dns / dhcp standalone repos are retired -- lm/dns and lm/dhcp are the only copies
# --------------------------------------------------------------------------
def _retired(findings):
    return [f for f in findings if "is retired" in f.get("title", "")]


@pytest.mark.parametrize("path", [
    "dns/src/dns_spoke.py", "dns/requirements.txt", "dhcp/src/kea_manager.py",
    "dns/VERSION", "dhcp/.github/workflows/ci.yml",
])
def test_lm_side_gets_no_twin_advisory(path):
    out = check_parity("lbockenstedt/lm", [path])
    assert _twins(out) == [] and _retired(out) == []


@pytest.mark.parametrize("repo,path", [
    ("lbockenstedt/dns", "src/dns_spoke.py"),
    ("lbockenstedt/dns", "requirements.txt"),
    ("lbockenstedt/dhcp", "src/kea_manager.py"),
])
def test_code_change_in_retired_repo_is_flagged(repo, path):
    out = check_parity(repo, [path])
    assert _twins(out) == []
    assert len(_retired(out)) == 1 and _retired(out)[0]["level"] == "warning"
    assert path in _retired(out)[0]["detail"]


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", ".github/scripts/promote.sh", "VERSION",
    "docs/dns.md", "README.md", "tests/test_dns_spoke_offload.py",
])
def test_plumbing_in_retired_repo_is_not_flagged(path):
    assert check_parity("lbockenstedt/dns", [path]) == []
    assert check_parity("lbockenstedt/dhcp", [path]) == []


def test_unrelated_repos_get_no_dns_findings():
    assert check_parity("lbockenstedt/ab", ["src/dns_spoke.py", "VERSION"]) == []


# --------------------------------------------------------------------------
# _resolve_cross_repo_twins — a path with no counterpart must not become a
# WARNING no PR can satisfy
# --------------------------------------------------------------------------
class _Contents:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.asked = []

    def get_repo(self, name):
        self.repo = name
        return self

    def get_contents(self, path):
        self.asked.append(path)
        if path in self.missing:
            err = Exception("Not Found")
            err.status = 404
            raise err
        return object()


def _resolver(missing=(), verdict=False):
    ns = {"logger": _NoLog(), "_twin_open_pr_touches": lambda *a, **k: verdict}
    exec(_extract("_twin_path_exists"), ns)
    exec(_extract("_resolve_cross_repo_twins"), ns)
    gh = _Contents(missing)
    return ns["_resolve_cross_repo_twins"], gh


def _finding(path="dns/.github/scripts/promote.sh"):
    return {"level": "advisory", "title": "dns module twin lives in the lm repo",
            "detail": "orig", "twin": {"repo": "lbockenstedt/lm", "path": path}}


def test_missing_twin_path_stays_advisory_and_is_not_escalated():
    resolve, gh = _resolver(missing={"dns/.github/scripts/promote.sh"})
    out = resolve(gh, [_finding()])
    assert len(out) == 1
    assert out[0]["level"] == "advisory", "a path that cannot exist became a WARNING"
    assert "twin NOT updated" not in out[0]["title"]
    assert "does not exist" in out[0]["detail"]


def test_existing_twin_with_no_matching_pr_is_still_a_warning():
    """The fix must not disarm the real check."""
    resolve, gh = _resolver(verdict=False)
    out = resolve(gh, [_finding("dns/src/dns_spoke.py")])
    assert out[0]["level"] == "warning"
    assert "twin NOT updated" in out[0]["title"]


def test_existing_twin_updated_by_a_matching_pr_is_dropped():
    resolve, gh = _resolver(verdict=True)
    assert resolve(gh, [_finding("dns/src/dns_spoke.py")]) == []


def test_findings_without_a_twin_pass_through_untouched():
    resolve, gh = _resolver()
    f = {"level": "advisory", "title": "something else", "detail": "d"}
    assert resolve(gh, [f]) == [f]


def test_the_twin_key_is_always_stripped_before_rendering():
    resolve, gh = _resolver(missing={"dns/.github/scripts/promote.sh"})
    assert all("twin" not in f for f in resolve(gh, [_finding()]))


def test_existence_is_cached_so_one_path_is_not_fetched_repeatedly():
    """A module-wide refactor can emit many advisories; each extra lookup is a
    GitHub API call against the twin repo."""
    resolve, gh = _resolver(missing={"dns/.github/scripts/promote.sh"})
    resolve(gh, [_finding(), _finding(), _finding()])
    assert gh.asked == ["dns/.github/scripts/promote.sh"]


# --------------------------------------------------------------------------
# External (cross-repo) Tier-1 findings must not be treated as in-repo
# remediable work.
#
# lm#1058 is the case this encodes. Five "twin NOT updated" WARNINGs said the
# dhcp twins were out of lockstep -- factually true, and genuinely blocking.
# But the fix engine only rewrites files in the PR's own checkout, so it could
# not clear a single one: it burned all three attempts producing diffs the
# panel correctly rejected as "not addressing the actual blocking findings",
# then stamped "Automated Remediation Limit Reached -- manual operator
# intervention required", pointing the operator at the wrong PR. The action was
# always to open a PR in the twin repo.
# --------------------------------------------------------------------------

def test_escalated_twin_finding_is_flagged_external():
    resolve, gh = _resolver(verdict=False)
    out = resolve(gh, [_finding("dns/src/a.py")])
    assert out[0]["level"] == "warning"
    assert out[0]["external"] is True, "escalated twin finding must be marked external"


def test_dropped_and_unreachable_twins_carry_no_external_flag():
    # verdict True -> the twin IS being updated, finding is dropped entirely.
    resolve, gh = _resolver(verdict=True)
    assert resolve(gh, [_finding("dns/src/a.py")]) == []
    # verdict None -> twin repo unreachable; stays an advisory, so it is not
    # Tier-1 at all and must not claim to be an external blocker.
    resolve, gh = _resolver(verdict=None)
    out = resolve(gh, [_finding("dns/src/a.py")])
    assert out[0]["level"] == "advisory"
    assert "external" not in out[0]


def test_non_twin_findings_are_untouched():
    resolve, gh = _resolver(verdict=False)
    plain = {"level": "warning", "title": "something real", "detail": "d"}
    out = resolve(gh, [plain])
    assert out == [plain], "a finding without a twin key must pass through unchanged"


# --- app_state counts them separately -------------------------------------

def _count_external(findings):
    """Replicate record_pr_review's counting loop from source."""
    src = open(os.path.join(ROOT, "app_state.py")).read()
    assert '"tier1_external": tier1_external,' in src, \
        "record_pr_review must persist tier1_external"
    levels = {"error": 0, "warning": 0, "advisory": 0}
    tier1_external = 0
    for f in (findings or []):
        f = f or {}
        lvl = f.get("level")
        if lvl in levels:
            levels[lvl] += 1
        if lvl in ("error", "warning") and f.get("external"):
            tier1_external += 1
    return levels, tier1_external


def test_only_tier1_external_findings_are_counted():
    levels, ext = _count_external([
        {"level": "warning", "external": True},
        {"level": "error", "external": True},
        {"level": "warning"},
        # An advisory is not Tier-1, so it must never inflate the count --
        # otherwise external could exceed warnings and skip a real repair.
        {"level": "advisory", "external": True},
    ])
    assert (levels["warning"], levels["error"]) == (2, 1)
    assert ext == 2


def test_tier1_external_is_recomputed_not_carried_forward():
    # record_pr_review REBUILDS the record every scan. Counters that must
    # survive are read from `prev`; this one is derived from `findings`, so
    # reading it from `prev` would pin a stale count after the twin PR landed.
    src = open(os.path.join(ROOT, "app_state.py")).read()
    line = [ln for ln in src.splitlines() if '"tier1_external"' in ln][0]
    assert "prev" not in line, "tier1_external must be recomputed, never carried from prev"


# --- should_remediate skips them ------------------------------------------

def _should_remediate():
    src = open(os.path.join(ROOT, "pr_remediate.py")).read()
    # _clears_merge_bar is only reached once findings are clear; stub it so the
    # findings branches under test can be exercised in isolation.
    ns = {"DEFAULT_TARGET_SCORE": 0.95, "logger": _NoLog(),
          "_clears_merge_bar": lambda *a, **k: (True, 0.90)}
    exec(_extract("should_remediate", src), ns)
    return ns["should_remediate"]


def _rec(**kw):
    base = {"panel_verdict": "Approve", "panel_confidence": 0.95,
            "panel2_verdict": "Approve", "panel2_confidence": 0.95}
    base.update(kw)
    return base


def test_all_external_warnings_skip_remediation():
    should, reason, deficit = _should_remediate()(
        _rec(errors=0, warnings=5, tier1_external=5), {})
    assert should is False, "spent the attempt budget on an impossible obligation"
    assert deficit == 0.0
    assert "twin" in reason.lower()


def test_mixed_findings_still_remediate():
    # Four external + one in-repo warning: there IS real work to do, so the
    # skip must not fire. This is the regression that would silently strand
    # every genuinely repairable finding that happened to share a record with
    # a twin advisory.
    should, reason, _ = _should_remediate()(
        _rec(errors=0, warnings=5, tier1_external=4), {})
    assert should is True
    assert "Tier-1 findings present" in reason


def test_errors_always_remediate_even_if_external_matches_warnings():
    should, _, _ = _should_remediate()(
        _rec(errors=1, warnings=2, tier1_external=2), {})
    assert should is True, "a Tier-1 ERROR is in-repo work regardless of twin warnings"


def test_absent_or_zero_external_count_is_unchanged_behaviour():
    for rec in (_rec(errors=0, warnings=3),
                _rec(errors=0, warnings=3, tier1_external=0)):
        should, reason, _ = _should_remediate()(rec, {})
        assert should is True
        assert "Tier-1 findings present" in reason


def test_no_findings_is_not_treated_as_all_external():
    # 0 <= 0 must not match: a clean record has to fall through to the
    # merge-bar logic, not be reported as blocked on a twin.
    should, reason, _ = _should_remediate()(
        _rec(errors=0, warnings=0, tier1_external=0), {})
    assert "twin" not in reason.lower()
