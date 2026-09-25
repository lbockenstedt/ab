"""The undefined-name class of reviewer doubt must be settled MECHANICALLY.

lm#1047 and cs#143 both sat unmerged at attempts=3/3 with ZERO findings. Their
only blockers were of the form "I could not verify NAME is defined/imported":

  * lm#1047 — ``_console_creds_for_tenant`` (defined at console.py:686) and
    ``ipaddress`` (imported at threat_monitor.py:58)
  * cs#143 — a supposedly missing ``import client_api`` (present at line 17)

All three were present. The reviewer had simply run out of tool budget before
fetching the file. No code edit can clear that objection, so remediation burned
every attempt and gave up — while AB was ALREADY running ruff F821 over the
same files and getting a clean result it never told anyone about.

The safety property under test is asymmetric and matters more than the feature:
a file may be called verified ONLY if ruff actually ran on it. Claiming ruff
cleared a file it never opened would launder a guess into "ground truth".
"""
import json
import subprocess

import pytest

import lint_python


class _F:
    def __init__(self, filename, status="modified"):
        self.filename = filename
        self.status = status


@pytest.fixture
def clean_ruff(monkeypatch):
    """ruff present, runs, finds nothing — stdout is '[]', never empty."""
    def _run(cmd, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
    monkeypatch.setattr(lint_python.subprocess, "run", _run)
    monkeypatch.setattr(lint_python, "_fetch_full_content", lambda r, p, s: "x = 1\n")


def test_clean_file_is_reported_verified(clean_ruff):
    findings, verified = lint_python.check_undefined_names_verified(
        None, [_F("a.py")], "sha")
    assert findings == []
    assert verified == ["a.py"]


def test_missing_ruff_binary_is_not_verified(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("ruff")
    monkeypatch.setattr(lint_python.subprocess, "run", _boom)
    monkeypatch.setattr(lint_python, "_fetch_full_content", lambda r, p, s: "x = 1\n")
    findings, verified = lint_python.check_undefined_names_verified(
        None, [_F("a.py")], "sha")
    assert findings == []
    # The whole point: no ruff means no claim.
    assert verified == []


def test_timeout_is_not_verified(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired("ruff", 5)
    monkeypatch.setattr(lint_python.subprocess, "run", _boom)
    monkeypatch.setattr(lint_python, "_fetch_full_content", lambda r, p, s: "x = 1\n")
    assert lint_python.check_undefined_names_verified(None, [_F("a.py")], "sha")[1] == []


def test_empty_stdout_is_not_verified(monkeypatch):
    """A clean ruff run prints '[]', so silence means something broke.

    Treating empty stdout as clean is exactly how a tooling failure would get
    laundered into "ruff says this file is fine".
    """
    def _run(cmd, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(lint_python.subprocess, "run", _run)
    monkeypatch.setattr(lint_python, "_fetch_full_content", lambda r, p, s: "x = 1\n")
    assert lint_python.check_undefined_names_verified(None, [_F("a.py")], "sha")[1] == []


def test_abnormal_exit_code_is_not_verified(monkeypatch):
    """ruff exits 0 (clean) or 1 (violations). Anything else is ruff failing."""
    def _run(cmd, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, 2, stdout="[]", stderr="boom")
    monkeypatch.setattr(lint_python.subprocess, "run", _run)
    monkeypatch.setattr(lint_python, "_fetch_full_content", lambda r, p, s: "x = 1\n")
    assert lint_python.check_undefined_names_verified(None, [_F("a.py")], "sha")[1] == []


def test_unfetchable_file_is_not_verified(monkeypatch):
    monkeypatch.setattr(lint_python, "_fetch_full_content", lambda r, p, s: None)
    assert lint_python.check_undefined_names_verified(None, [_F("a.py")], "sha")[1] == []


def test_violation_still_reported_and_file_still_verified(monkeypatch):
    diag = [{"code": "F821", "message": "Undefined name `nope`",
             "location": {"row": 3}}]
    def _run(cmd, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(diag), stderr="")
    monkeypatch.setattr(lint_python.subprocess, "run", _run)
    monkeypatch.setattr(lint_python, "_fetch_full_content", lambda r, p, s: "x = 1\n")
    findings, verified = lint_python.check_undefined_names_verified(
        None, [_F("a.py")], "sha")
    assert len(findings) == 1 and "F821" in findings[0]["detail"]
    # ruff DID run here, so the file is legitimately verified.
    assert verified == ["a.py"]


def test_legacy_wrapper_still_returns_only_findings(clean_ruff):
    """check_undefined_names keeps its old single-value contract."""
    assert lint_python.check_undefined_names(None, [_F("a.py")], "sha") == []


def test_deleted_files_are_skipped(clean_ruff):
    assert lint_python.check_undefined_names_verified(
        None, [_F("gone.py", status="removed")], "sha")[1] == []


def test_non_python_files_are_skipped(clean_ruff):
    assert lint_python.check_undefined_names_verified(
        None, [_F("a.js")], "sha")[1] == []


# ---------------------------------------------------------------- context text

def test_context_is_empty_when_nothing_verified():
    """No verified files means NO claim in the prompt at all."""
    assert lint_python.format_undefined_name_context([]) == ""
    assert lint_python.format_undefined_name_context(None) == ""


def test_context_names_the_verified_files_and_forbids_the_doubt():
    txt = lint_python.format_undefined_name_context(
        ["core/src/routes/console.py", "core/src/security/threat_monitor.py"])
    assert "core/src/routes/console.py" in txt
    assert "core/src/security/threat_monitor.py" in txt
    # It must actually instruct the reviewer not to block on this class.
    assert "Do NOT withhold approval" in txt
    assert "settled" in txt


def test_context_does_not_vouch_for_unverified_files():
    txt = lint_python.format_undefined_name_context(["a.py"])
    assert "a.py" in txt
    assert "b.py" not in txt
    # and it tells the reviewer what to do about files it did not vouch for
    assert "not listed above" in txt


# ------------------------------------------------------------------ wiring
# pr_review imports fix_engine/app_state at module scope, which boots workers
# and writes /etc/ab, so the suite asserts wiring against the source text --
# the same approach as test_pr_remediate.test_skeptical_review_prompt_*.

def _pr_review_src():
    import os
    with open(os.path.join(os.path.dirname(__file__), "pr_review.py"),
              "r", encoding="utf-8") as f:
        return f.read()


def test_pr_review_collects_verified_paths():
    src = _pr_review_src()
    assert "check_undefined_names_verified(repo, files, head_sha)" in src, \
        "the review path must capture WHICH files ruff cleared"


def test_pr_review_passes_evidence_to_the_panel():
    src = _pr_review_src()
    assert "undefined_verified=_undef_verified" in src, \
        "the skeptical panel must receive the ruff result"
    assert "format_undefined_name_context(undefined_verified)" in src, \
        "the evidence must be rendered into the reviewer prompt"
