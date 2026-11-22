"""Never remediate a PR whose only changes are files lm syncs into the repo.

The case that prompted this is lbockenstedt/truenas#104, a promote/dev-to-qa PR
whose diff was only lm-synced files (.github/ai-budget.json, fleet-rules.md,
hooks, AGENTS.md, VERSION). The panel rejected it and AB then generated fixes
to those synced copies in every repo, burning spend. Fleet rule 2.7 says these
files are edited only in lm, and the next sync overwrites any copy, so a fix
made here can never stick. The guard lives in pr_remediate._remediate.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import pr_remediate

TRUENAS_104 = [".github/ai-budget.json", ".github/fleet-rules.md",
               ".github/hooks/ai_budget.py", ".github/hooks/fleet-hooks.json",
               "AGENTS.md", "VERSION"]


def test_truenas_104_synced_files_are_fleet_owned():
    """The real truenas#104 file list is lm-owned, VERSION aside."""
    assert pr_remediate._only_fleet_owned("lbockenstedt/truenas", TRUENAS_104) is True


def test_any_repo_file_makes_the_pr_not_fleet_owned():
    """One repo-local file alongside the synced ones means the PR is remediable."""
    assert pr_remediate._only_fleet_owned("lbockenstedt/truenas",
                                          TRUENAS_104 + ["src/truenas_spoke.py"]) is False


def test_lm_itself_is_never_fleet_owned():
    """lm is the source of these files, so its own PRs are not skipped."""
    assert pr_remediate._only_fleet_owned("lbockenstedt/lm", [".github/hooks/ai_budget.py"]) is False


def test_version_only_or_empty_is_not_fleet_owned():
    """A VERSION-only or empty diff is not an lm-synced change."""
    assert pr_remediate._only_fleet_owned("lbockenstedt/truenas", ["VERSION"]) is False
    assert pr_remediate._only_fleet_owned("lbockenstedt/truenas", []) is False


def test_repo_own_agent_and_skill_entries_are_not_fleet_owned():
    """A repo's own <repo>-* agent or skill is that repo's file, not lm's."""
    assert pr_remediate._only_fleet_owned(
        "lbockenstedt/truenas", [".github/agents/truenas-storage.agent.md"]) is False
    assert pr_remediate._only_fleet_owned(
        "lbockenstedt/truenas", [".claude/skills/truenas-x/SKILL.md"]) is False


def test_shared_agent_is_fleet_owned():
    """A shared lm agent with no repo prefix is synced, so it is fleet owned."""
    assert pr_remediate._only_fleet_owned(
        "lbockenstedt/truenas", [".github/agents/tenant-scope-auditor.agent.md"]) is True


class _Head:
    def __init__(self, ref, sha):
        self.ref = ref
        self.sha = sha


FLEET_RULES_TEXT = "# Fleet rules\n\n<!-- Edit ONLY lm/.github/fleet-rules.md. -->\n"


class _Content:
    """A GitHub contents response carrying decoded_content."""

    def __init__(self, text):
        self.decoded_content = text.encode("utf-8")


class _BaseRepo:
    """The base repo of a PR; get_contents serves .github/fleet-rules.md.

    `responses` is a list consumed one per call: an Exception is raised, a str
    is returned as the file text. The last entry repeats once the list is spent."""

    def __init__(self, full_name, responses):
        self.full_name = full_name
        self._responses = list(responses)
        self.calls = 0

    def get_contents(self, path, ref=None):
        """Return the next scripted response for the fleet-rules.md read."""
        self.calls += 1
        item = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Content(item)


class _Base:
    def __init__(self, repo, ref):
        self.repo = repo
        self.ref = ref


class _PR:
    def __init__(self, number, head, filenames, base_repo=None):
        self.number = number
        self.head = head
        self.base = _Base(base_repo or _BaseRepo("lbockenstedt/truenas", [FLEET_RULES_TEXT]), "dev")
        self.draft = False
        self._files = [types.SimpleNamespace(filename=f) for f in filenames]
        self.comments = []

    def get_files(self):
        return self._files

    def create_issue_comment(self, body):
        self.comments.append(body)


@pytest.fixture(autouse=True)
def clear_fleet_repo_cache(monkeypatch):
    """Each test starts with an empty fleet-repo cache."""
    monkeypatch.setattr(pr_remediate, "_FLEET_REPO_CACHE", {})


@pytest.fixture
def isolated_state(monkeypatch):
    """Give pr_remediate a fresh state dict and a recorder for update_pr_review."""
    fake_state = {"pr_reviews": {}}
    monkeypatch.setattr(pr_remediate, "state", fake_state)

    def _update(repo, number, **fields):
        """Merge fields into the in-test PR review record."""
        fake_state["pr_reviews"].setdefault(f"{repo}#{number}", {}).update(fields)
        return True

    monkeypatch.setattr(pr_remediate, "update_pr_review", _update)
    return fake_state


def test_auto_remediate_skips_truenas_104_even_when_promotion_remediation_enabled(isolated_state):
    """The integration path: truenas#104 is never sent to the fix engine, and the skip is recorded."""
    def fail_fn(*_a, **_k):
        """The fix engine must not be reached for a fleet-owned PR."""
        raise AssertionError("fix_fn called for a fleet-owned PR")

    repo = types.SimpleNamespace(full_name="lbockenstedt/truenas")
    pr = _PR(104, _Head("promote/dev-to-qa", "abc123"), TRUENAS_104)
    ok, reason = pr_remediate.auto_remediate_pr(
        None, repo, pr, config={"pr_auto_remediate_skip_promotion": False}, fix_fn=fail_fn)

    assert ok is False
    assert "rule 2.7" in reason
    rec = isolated_state["pr_reviews"]["lbockenstedt/truenas#104"]
    assert rec["auto_remediate_blocked"] is True
    assert rec["auto_remediate_blocked_head"] == "abc123"


def test_rename_from_a_repo_file_into_a_synced_dir_is_not_skipped(isolated_state):
    """Moving src/local.py into .github/hooks/ changes repo code, so it is not fleet-only."""
    repo = types.SimpleNamespace(full_name="lbockenstedt/truenas")
    pr = _PR(105, _Head("promote/dev-to-qa", "def456"), [])
    pr._files = [types.SimpleNamespace(filename=".github/hooks/local.py",
                                       previous_filename="src/local.py")]
    ok, reason = pr_remediate.auto_remediate_pr(None, repo, pr, config={})

    assert ok is False
    assert "rule 2.7" not in reason
    assert "promotion PR" in reason


def test_a_latched_reason_survives_later_scans(isolated_state):
    """On the same head the skip keeps the latch's own reason, prefixed once only."""
    key = "lbockenstedt/truenas#104"
    isolated_state["pr_reviews"][key] = {
        "auto_remediate_blocked": True, "auto_remediate_blocked_head": "abc123",
        "auto_remediate_reason": "PR changes only fleet files synced from lm; fix them in lm "
                                 "(fleet rule 2.7) and the next sync carries the fix here"}
    repo = types.SimpleNamespace(full_name="lbockenstedt/truenas")
    pr = _PR(104, _Head("promote/dev-to-qa", "abc123"), TRUENAS_104)
    pr.state, pr.merged = "open", False

    for _ in range(2):
        ok, msg = pr_remediate.maybe_auto_remediate(None, repo, pr, {})
        assert ok is False
        assert msg.startswith("PR remediation is blocked by guardrail: PR changes only fleet")
        assert msg.count("blocked by guardrail") == 1


def test_agents_md_counts_only_alongside_fleet_rules():
    """AGENTS.md holds repo text too; only a sync (which also changes fleet-rules.md) is lm's."""
    assert pr_remediate._only_fleet_owned("lbockenstedt/truenas", ["AGENTS.md", "VERSION"]) is False
    assert pr_remediate._only_fleet_owned(
        "lbockenstedt/truenas", ["AGENTS.md", ".github/fleet-rules.md", "VERSION"]) is True


def test_the_skip_is_not_recorded_as_a_remediation_failure(isolated_state):
    """Nothing ran, so an earlier head's auto_remediate_failure is cleared, not kept."""
    isolated_state["pr_reviews"]["lbockenstedt/truenas#104"] = {
        "auto_remediate_failure": "Reviewer panel rejected the generated fix"}
    repo = types.SimpleNamespace(full_name="lbockenstedt/truenas")
    pr = _PR(104, _Head("promote/dev-to-qa", "abc123"), TRUENAS_104)
    pr_remediate.auto_remediate_pr(None, repo, pr, config={})
    rec = isolated_state["pr_reviews"]["lbockenstedt/truenas#104"]
    assert "rule 2.7" in rec["auto_remediate_reason"]
    assert rec["auto_remediate_failure"] is None


@pytest.mark.parametrize("prior", [{"auto_remediate_status": "exhausted_human_review",
                                    "auto_remediate_failure": "old failure"}, {}])
def test_maybe_auto_remediate_latches_before_the_attempt_limit_and_docs_exits(isolated_state, prior, monkeypatch):
    """An exhausted or docs-only fleet-sync head still records the fleet-owned reason."""
    monkeypatch.setattr(pr_remediate, "skips_docs_only", lambda *a, **k: True)
    key = "lbockenstedt/truenas#104"
    isolated_state["pr_reviews"][key] = dict(prior)
    repo = types.SimpleNamespace(full_name="lbockenstedt/truenas")
    pr = _PR(104, _Head("promote/dev-to-qa", "abc123"), TRUENAS_104)
    pr.state, pr.merged = "open", False
    ok, msg = pr_remediate.maybe_auto_remediate(None, repo, pr, {})
    rec = isolated_state["pr_reviews"][key]
    assert ok is False and "rule 2.7" in msg
    assert rec["auto_remediate_blocked_head"] == "abc123"
    assert rec["auto_remediate_failure"] is None


def test_non_fleet_repo_with_synced_looking_paths_is_not_latched(isolated_state):
    """A repo outside the fleet (no fleet-rules.md on its base) is never latched."""
    base_repo = _BaseRepo("someone/other", [Exception("404 Not Found")])
    pr = _PR(7, _Head("feature/x", "aaa111"), [".github/hooks/ai_budget.py"], base_repo=base_repo)
    assert pr_remediate._latch_fleet_owned("someone/other", pr, pr.get_files(), "aaa111") == ""
    assert "someone/other#7" not in isolated_state["pr_reviews"]


def test_fleet_rules_without_the_marker_is_not_a_fleet_repo(isolated_state):
    """A base branch whose fleet-rules.md lacks lm's marker is not a fleet repo."""
    base_repo = _BaseRepo("someone/other", ["# Their own rules\n"])
    pr = _PR(8, _Head("feature/x", "aaa222"), [".github/hooks/ai_budget.py"], base_repo=base_repo)
    assert pr_remediate._latch_fleet_owned("someone/other", pr, pr.get_files(), "aaa222") == ""


def test_transient_fleet_repo_error_is_not_cached(isolated_state):
    """A failed fleet-rules read is retried on the next scan, not remembered as non-fleet."""
    base_repo = _BaseRepo("lbockenstedt/truenas",
                          [Exception("503 Service Unavailable"), FLEET_RULES_TEXT])
    pr = _PR(9, _Head("promote/dev-to-qa", "bbb333"), TRUENAS_104, base_repo=base_repo)
    assert pr_remediate._latch_fleet_owned("lbockenstedt/truenas", pr, pr.get_files(), "bbb333") == ""
    reason = pr_remediate._latch_fleet_owned("lbockenstedt/truenas", pr, pr.get_files(), "bbb333")
    assert "fleet files synced from lm" in reason
    assert base_repo.calls == 2


def test_same_head_promotion_latch_is_rechecked_as_fleet_owned(isolated_state):
    """A promotion latch on this head that predates the fleet check gets the fleet reason."""
    key = "lbockenstedt/truenas#104"
    isolated_state["pr_reviews"][key] = {
        "auto_remediate_blocked": True, "auto_remediate_blocked_head": "abc123",
        "auto_remediate_reason": "promotion PR ... remediation is off",
        "auto_remediate_failure": "old failure"}
    repo = types.SimpleNamespace(full_name="lbockenstedt/truenas")
    pr = _PR(104, _Head("promote/dev-to-qa", "abc123"), TRUENAS_104)
    pr.state, pr.merged = "open", False
    ok, msg = pr_remediate.maybe_auto_remediate(None, repo, pr, {})
    rec = isolated_state["pr_reviews"][key]
    assert ok is False and "fleet files synced from lm" in msg
    assert "fleet files synced from lm" in rec["auto_remediate_reason"]
    assert rec["auto_remediate_failure"] is None


def test_same_head_fleet_latch_does_not_reread_the_file_list(isolated_state):
    """Once the head is latched as fleet-owned, later scans make no get_files call."""
    key = "lbockenstedt/truenas#104"
    isolated_state["pr_reviews"][key] = {
        "auto_remediate_blocked": True, "auto_remediate_blocked_head": "abc123",
        "auto_remediate_reason": "PR changes only fleet files synced from lm; fix them in lm "
                                 "(fleet rule 2.7) and the next sync carries the fix here"}
    repo = types.SimpleNamespace(full_name="lbockenstedt/truenas")
    pr = _PR(104, _Head("promote/dev-to-qa", "abc123"), TRUENAS_104)
    pr.state, pr.merged = "open", False

    calls = []

    def no_reads():
        """Record any file-list fetch; the test asserts none happened."""
        calls.append(1)
        return []

    pr.get_files = no_reads
    ok, msg = pr_remediate.maybe_auto_remediate(None, repo, pr, {})
    assert ok is False and "fleet files synced from lm" in msg
    assert calls == []
