"""Tests for the local-review attestation: scripts/local_review.py writes
attestation.md when both panels Approve on a clean tree, and pr_review._local_review
takes that attestation in place of AB's paid panels for the exact same head."""
import importlib.util
import json
import os
from types import SimpleNamespace

import types

import pytest

import main  # noqa: F401  (pr_review imports main; import it first to break the cycle)
import pr_review

_SPEC = importlib.util.spec_from_file_location(
    "local_review", os.path.join(os.path.dirname(__file__), "..", "scripts", "local_review.py"))
local_review = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(local_review)

HEAD = "a" * 40
MERGE_BASE = "b" * 40
TITLE = "Fix the thing"
BODY = "## Intent & Problem Statement\nLooks good."
REPO = "lbockenstedt/ab"


def _panel(verdict="Approve", confidence=0.95, seats=("gpt-5.6-sol",)):
    """One panel entry as the attestation marker stores it."""
    return {"verdict": verdict, "confidence": confidence, "seats": list(seats)}


def _marker(head=HEAD, p1=None, p2=None, desc=None, base="dev"):
    """A PR body: the reviewed BODY with one attestation marker appended."""
    att = {"head": head, "base": base, "merge_base": MERGE_BASE, "desc": desc or pr_review.local_review_desc(TITLE, BODY),
           "panel1": p1 or _panel(), "panel2": p2 or _panel(seats=("gpt-5.5",))}
    return f"{BODY}\n\n<!-- ab-local-review: {json.dumps(att, separators=(',', ':'))} -->\n"


class _Repo:
    """A fake repo answering the PR author's permission (or raising)."""
    full_name = REPO

    def __init__(self, perm="write", default_branch="main", protected=False):
        """``perm`` is returned for every user and ``protected`` for every
        branch; an Exception instance is raised instead."""
        self.perm, self.default_branch, self.protected = perm, default_branch, protected

    def compare(self, base, head):
        """A comparison whose merge base is ``self.merge_base`` (or raises it)."""
        mb = getattr(self, "merge_base", MERGE_BASE)
        if isinstance(mb, Exception):
            raise mb
        return types.SimpleNamespace(merge_base_commit=types.SimpleNamespace(sha=mb))

    def get_branch(self, name):
        """A branch whose ``protected`` flag is the configured one."""
        if isinstance(self.protected, Exception):
            raise self.protected
        return types.SimpleNamespace(protected=self.protected)

    def get_collaborator_permission(self, login):
        """The configured permission for ``login``."""
        if isinstance(self.perm, Exception):
            raise self.perm
        return self.perm


def _pr(body, base="dev", title=TITLE, head_ref="fix/thing", head_repo=REPO, login="dev-person"):
    """A fake PR with the fields the attestation is checked against."""
    return SimpleNamespace(number=7, title=title, body=body, base=SimpleNamespace(ref=base),
                           head=SimpleNamespace(ref=head_ref, repo=SimpleNamespace(full_name=head_repo)),
                           user=SimpleNamespace(login=login))


def _use(pr, config=None, repo=None, force=False):
    """pr_review._local_review on ``pr`` at HEAD with a writer's repo by default."""
    return pr_review._local_review(pr, HEAD, config or {}, repo or _Repo(), force=force)


def test_valid_attestation_replaces_both_panels(caplog):
    """A valid marker for the current head yields both panels' results, so
    the paid panels are not run."""
    review, state = _use(_pr(_marker()))
    assert review["verdict"] == "Approve" and review["confidence"] == 0.95
    assert state["verdict"] == "Approve" and state["confidence"] == 0.95
    assert "gpt-5.6-sol" in review["critique"] and "attested, not re-run" in review["critique"]


def test_head_mismatch_falls_back_to_the_panels():
    """A rebase or new push changes the head: the attestation is void."""
    assert _use(_pr(_marker(head="b" * 40))) is None


@pytest.mark.parametrize("base", ["qa", "main"])
def test_release_branches_are_never_attested(base):
    """Only dev PRs take an attestation; release branches always run the panels."""
    assert _use(_pr(_marker(), base=base)) is None


def test_knob_off_ignores_the_marker():
    """pr_review_trust_local_review off means every marker is ignored."""
    assert _use(_pr(_marker()), {"pr_review_trust_local_review": False}) is None


def test_malformed_json_falls_back():
    """A marker that is not JSON is rejected, not raised."""
    body = f'<!-- ab-local-review: {{"head": "{HEAD}", oops}} -->'
    assert _use(_pr(body)) is None


def test_one_panel_reject_falls_back():
    """Both panels must Approve; one Reject means the panels run."""
    body = _marker(p2=_panel(verdict="Reject", confidence=0.4))
    assert _use(_pr(body)) is None


@pytest.mark.parametrize("confidence", [1.5, -0.1, True, "0.9", None])
def test_confidence_must_be_a_number_in_range(confidence):
    """Out-of-range numbers, bools (a number in Python) and strings are rejected."""
    body = _marker(p1=_panel(confidence=confidence))
    assert _use(_pr(body)) is None


def test_last_marker_wins():
    """Only the last marker in the body counts, so an earlier valid one cannot
    override a later invalid one."""
    stale, good = _marker(head="b" * 40), _marker()
    tail = lambda body: body[len(BODY):]  # noqa: E731 -- just the marker part
    assert _use(_pr(BODY + tail(stale) + tail(good))) is not None
    assert _use(_pr(BODY + tail(good) + tail(stale))) is None


def test_edited_title_or_body_voids_the_attestation():
    """The panels judged the title and body too: any later edit, including text
    after the marker, means the reviewed description is not the PR's."""
    assert _use(_pr(_marker(), title="Something else")) is None
    assert _use(_pr(_marker() + "\nAlso drops the tenant check.")) is None
    assert _use(_pr(_marker().replace("Looks good.", "Looks fine."))) is None


def test_crlf_from_a_web_edit_keeps_the_digest():
    """GitHub web edits store CRLF; the digest normalises line endings."""
    assert _use(_pr(_marker().replace("\n", "\r\n"))) is not None


@pytest.mark.parametrize("perm", ["read", "triage", "none", RuntimeError("API down")])
def test_author_without_write_access_falls_back(perm):
    """Only an author who could push to dev directly may skip the panels; an
    unreadable permission is not proof."""
    assert _use(_pr(_marker()), repo=_Repo(perm)) is None


@pytest.mark.parametrize("perm", ["write", "maintain", "admin"])
def test_writers_may_attest(perm):
    """write, maintain and admin all can push to dev."""
    assert _use(_pr(_marker()), repo=_Repo(perm)) is not None


def test_fork_pr_falls_back():
    """A PR from another repository never takes an attestation."""
    assert _use(_pr(_marker(), head_repo="someone/ab")) is None


@pytest.mark.parametrize("head_ref", ["bug/fix-x", "ai-feature/add-y"])
def test_appbuilder_branches_fall_back(head_ref):
    """AB's own PR bodies hold LLM text, so a marker there is never trusted."""
    assert _use(_pr(_marker(), head_ref=head_ref)) is None


def test_reprocess_runs_the_panels():
    """force=True (Reprocess) promises fresh panels, so the marker is ignored."""
    assert _use(_pr(_marker()), force=True) is None


def test_repo_default_branch_never_takes_an_attestation():
    """A repo whose default branch is not a built-in release name still never
    takes one there, even if listed in pr_review_local_review_branches."""
    config = {"pr_review_local_review_branches": "dev,trunk"}
    assert _use(_pr(_marker(base="trunk"), base="trunk"), config, _Repo(default_branch="trunk")) is None
    assert _use(_pr(_marker(base="trunk"), base="trunk"), config, _Repo(default_branch="main")) is not None


def test_dev_as_repo_default_or_operator_protected_never_takes_one():
    """Dropping dev from the built-in release names must not drop it when it is
    the repo's own default branch or a branch the operator protected."""
    assert _use(_pr(_marker()), repo=_Repo(default_branch="dev")) is None
    assert _use(_pr(_marker()), {"protected_branches": "dev"}) is None


def test_dev_branch_named_like_a_release_branch_never_takes_one():
    """A dev_branch of "qa" must not strip qa from the release names."""
    cfg = {"dev_branch": "qa", "pr_review_local_review_branches": "qa"}
    assert _use(_pr(_marker(), base="qa"), cfg) is None


@pytest.mark.parametrize("protected", [True, None, RuntimeError("boom")])
def test_a_base_github_protects_or_cannot_confirm_never_takes_one(protected):
    """The marker stands only while GitHub shows the base unprotected."""
    assert _use(_pr(_marker()), repo=_Repo(protected=protected)) is None


def test_a_retargeted_pr_or_moved_merge_base_voids_the_attestation():
    """The attestation covers the diff against the reviewed base only."""
    cfg = {"pr_review_local_review_branches": "dev feature"}
    assert _use(_pr(_marker(), base="feature"), cfg) is None
    assert _use(_pr(_marker(base="")), cfg) is None
    for mb in ("c" * 40, RuntimeError("boom")):
        repo = _Repo()
        repo.merge_base = mb
        assert _use(_pr(_marker()), repo=repo) is None


def test_cached_attested_result_is_rechecked_before_reuse():
    """A cached head reuses attested panels only while the attestation holds;
    real panel results are reused without the extra checks."""
    attested = {"panel_attested": True}
    hold = pr_review._attestation_still_holds
    assert hold(attested, _pr(_marker()), HEAD, {}, _Repo())
    assert not hold(attested, _pr(_marker(), base="feature"), HEAD,
                    {"pr_review_local_review_branches": "dev feature"}, _Repo())
    assert not hold(attested, _pr(_marker()), HEAD, {}, _Repo(protected=True))
    real = {"panel_critique": "a real panel quoting (attested, not re-run)"}
    assert hold(real, _pr(_marker()), HEAD, {}, _Repo(protected=True))


def test_attested_flag_is_persisted_only_for_attested_results(monkeypatch):
    """record_pr_review stores panel_attested from the review dict, not its text."""
    import app_state
    monkeypatch.setattr(app_state, "state", {"pr_reviews": {}})
    monkeypatch.setattr(app_state, "save_pr_reviews", lambda x: None)
    for review, want in (({"verdict": "Approve", "confidence": 0.95, "attested": True}, True),
                         ({"verdict": "Approve", "confidence": 0.95,
                           "critique": "quoting (attested, not re-run)"}, False)):
        app_state.record_pr_review(REPO, 7, TITLE, "u", [], HEAD, review=review, review2=review)
        assert app_state.state["pr_reviews"][f"{REPO}#7"]["panel_attested"] is want


def test_desc_digest_ignores_markers_and_outer_whitespace():
    """The digest a local review writes matches the PR body it is pasted into."""
    assert pr_review.local_review_desc(TITLE, BODY) == pr_review.local_review_desc(TITLE + " ", _marker())


def _gate_folder(tmp_path, panel1_verdict, dirty=False):
    """A --out folder with reviewers.json, meta.json and one reply per panel."""
    (tmp_path / "reviewers.json").write_text(json.dumps(
        {"panel1": ["openai/gpt-5.6-sol"], "panel2": ["openai/gpt-5.5"]}))
    (tmp_path / "meta.json").write_text(json.dumps({
        "head": HEAD, "base": "dev", "merge_base": MERGE_BASE, "dirty": dirty, "title": TITLE, "body": BODY,
        "desc": pr_review.local_review_desc(TITLE, BODY)}))
    (tmp_path / "votes").mkdir()
    replies = {"panel1-openai%2Fgpt-5.6-sol": (panel1_verdict, 0.95 if panel1_verdict == "Approve" else 0.4),
               "panel2-openai%2Fgpt-5.5": ("Approve", 0.96)}
    for name, (verdict, conf) in replies.items():
        text = "```json\n" + json.dumps({"verdict": verdict, "confidence": conf, "critique": ""}) + "\n```"
        (tmp_path / "votes" / (name + ".txt")).write_text(text)
    return tmp_path


def test_gate_writes_attestation_when_both_approve_on_a_clean_tree(tmp_path, capsys):
    """Both panels Approve on a clean tree: the marker and the PR title/body to
    open are written, and that PR passes _local_review."""
    folder = _gate_folder(tmp_path, "Approve")
    assert local_review.gate(str(folder)) is True
    marker = (folder / "attestation.md").read_text().strip()
    assert marker.startswith("<!-- ab-local-review: ") and marker.endswith(" -->")
    att = json.loads(marker[len("<!-- ab-local-review: "):-len(" -->")])
    assert att["head"] == HEAD and att["desc"] == pr_review.local_review_desc(TITLE, BODY)
    assert att["panel1"] == {"verdict": "Approve", "confidence": 0.95, "seats": ["gpt-5.6-sol"]}
    assert att["panel2"]["seats"] == ["gpt-5.5"]
    assert json.loads(capsys.readouterr().out)["passed"] is True
    pr = _pr((folder / "pr-body.md").read_text(), title=(folder / "pr-title.txt").read_text().strip())
    assert _use(pr) is not None


def test_gate_removes_a_stale_attestation_on_a_dirty_tree(tmp_path, capsys):
    """Uncommitted edits were reviewed too, so nothing is attested; old files go."""
    folder = _gate_folder(tmp_path, "Approve", dirty=True)
    for name in ("attestation.md", "pr-body.md", "pr-title.txt"):
        (folder / name).write_text("stale")
    assert local_review.gate(str(folder)) is True
    assert not any((folder / n).exists() for n in ("attestation.md", "pr-body.md", "pr-title.txt"))


def test_gate_removes_a_stale_attestation_when_a_panel_rejects(tmp_path, capsys):
    """A panel that does not Approve gets no attestation, and the gate still fails."""
    folder = _gate_folder(tmp_path, "Reject")
    (folder / "attestation.md").write_text("stale")
    assert local_review.gate(str(folder)) is False
    assert not (folder / "attestation.md").exists()


def _attest_folder(tmp_path, replies):
    """A --out folder with meta.json and the frontier replies {panel: (verdict, confidence)}."""
    (tmp_path / "meta.json").write_text(json.dumps({
        "head": HEAD, "base": "dev", "merge_base": MERGE_BASE, "dirty": False, "title": TITLE, "body": BODY,
        "desc": pr_review.local_review_desc(TITLE, BODY)}))
    for panel, (verdict, conf) in replies.items():
        reply = json.dumps({"verdict": verdict, "confidence": conf, "critique": ""})
        (tmp_path / f"{panel}-gpt-5.6-sol.out").write_text("noise\n" + reply)
    return tmp_path


def test_a_new_round_drops_old_frontier_replies(tmp_path):
    """Reusing the folder for a new head must not keep the last head's Approves."""
    for name in ("panel1-m.out", "panel2-m.out", "panel1.txt"):
        (tmp_path / name).write_text("x")
    local_review._fresh_votes(str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["panel1.txt", "votes"]


def test_attest_writes_when_both_frontier_replies_approve_at_the_bar(tmp_path, capsys):
    """The ladder's frontier reviewer Approves both panels at 0.90+: attested,
    and the PR it describes passes _local_review."""
    folder = _attest_folder(tmp_path, {"panel1": ("Approve", 0.95), "panel2": ("Approve", 0.9)})
    assert local_review.attest(str(folder), "gpt-5.6-sol") is True
    pr = _pr((folder / "pr-body.md").read_text(), title=(folder / "pr-title.txt").read_text().strip())
    review, state = _use(pr)
    assert review["confidence"] == 0.95 and state["confidence"] == 0.9
    assert "gpt-5.6-sol" in review["critique"]


@pytest.mark.parametrize("replies", [
    {"panel1": ("Approve", 0.95), "panel2": ("Approve", 0.89)},
    {"panel1": ("Approve", 0.95), "panel2": ("Reject", 0.95)},
    {"panel1": ("Approve", 0.95)},
])
def test_attest_refuses_below_the_bar_a_reject_or_a_missing_panel(tmp_path, capsys, replies):
    """Anything short of two frontier Approves at 0.90+ writes nothing and
    removes an old attestation."""
    folder = _attest_folder(tmp_path, replies)
    (folder / "attestation.md").write_text("stale")
    assert local_review.attest(str(folder), "gpt-5.6-sol") is False
    assert not (folder / "attestation.md").exists()
