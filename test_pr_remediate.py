"""Unit tests for closed-loop PR auto-remediation engine and intent specification.

Tests cover:
  - Guardrail checks: transport scheme tampering, hardcoded PSKs, reverse shells, TLS verify bypass, clean code.
  - Complexity assessment: small (nits/lint/tooltips), medium (control-flow/parsing), large (concurrency/reject/deny).
  - Remediation requirements escalation and model exclusion.
  - Attempt limit ceiling enforcement.
  - Skeptical review prompt intent-vs-diff fidelity inclusion.
"""
import ast
import os
import pytest

from model_selection import LlmRequirements
import pr_remediate
from pr_remediate import (
    check_pr_guardrails,
    assess_fix_complexity,
    next_remediation_requirements,
    auto_remediate_pr,
)


class MockFile:
    def __init__(self, filename, patch=""):
        self.filename = filename
        self.patch = patch


class MockPR:
    def __init__(self, title="Test PR", body="", number=42, files=None):
        self.title = title
        self.body = body
        self.number = number
        self._files = files or []
        self.comments = []
        self.state = "open"
        self.merged = False
        self.draft = False

    def get_files(self):
        return self._files

    def create_issue_comment(self, body):
        self.comments.append(body)


class MockRepo:
    def __init__(self, full_name="lbockenstedt/nw"):
        self.full_name = full_name


def test_guardrails_blocks_transport_scheme_tampering():
    pr = MockPR(title="Update ws transport", body="Modify connection scheme")
    files = [MockFile("src/transports/ws_client.py", "+ def connect(): pass")]
    passed, reason = check_pr_guardrails(pr, files, ["src/transports/ws_client.py"], {})
    assert passed is False
    assert reason is not None
    assert "transport-scheme" in reason or "boundary" in reason.lower()


def test_guardrails_blocks_hardcoded_psk():
    pr = MockPR(title="Update psk", body="hardcode preshared secret")
    files = [MockFile("src/agent.py", '+ psk = "super_secret_preshared_key_12345"')]
    passed, reason = check_pr_guardrails(pr, files, ["src/agent.py"], {})
    assert passed is False
    assert reason is not None
    assert any(k in reason.lower() for k in ("psk", "secret", "credential", "boundary"))


def test_guardrails_blocks_reverse_shell_injection():
    pr = MockPR(title="Utility fix", body="clean utility")
    files = [MockFile("src/utils.py", '+ import pty; pty.spawn("/bin/bash")')]
    passed, reason = check_pr_guardrails(pr, files, ["src/utils.py"], {})
    assert passed is False
    assert reason is not None
    assert "reverse shell" in reason.lower()


def test_guardrails_blocks_tls_verify_bypass():
    pr = MockPR(title="Bypass tls", body="skip cert check")
    files = [MockFile("src/transports/agent_client.py", '+ ssl_verify = False')]
    passed, reason = check_pr_guardrails(pr, files, ["src/transports/agent_client.py"], {})
    assert passed is False
    assert reason is not None
    assert "tls" in reason.lower()


def test_guardrails_passes_clean_code():
    pr = MockPR(title="Clean addition", body="Pure math helper")
    files = [MockFile("src/math_helper.py", "+ def add(a: int, b: int) -> int:\n+     return a + b\n")]
    passed, reason = check_pr_guardrails(pr, files, ["src/math_helper.py"], {})
    assert passed is True
    assert reason is None


def test_assess_fix_complexity_minor_nits_is_small():
    res1 = assess_fix_complexity(critique="minor nit: typo in docstring and comment formatting", dissent_feedback="")
    assert res1 == "small"

    res2 = assess_fix_complexity(
        critique="",
        dissent_feedback="",
        findings=[{"title": "Missing tooltip", "detail": "Button requires tooltip"}],
    )
    assert res2 == "small"

    res3 = assess_fix_complexity(
        critique="Looks mostly fine",
        dissent_feedback="",
        files=[MockFile("src/one.py")],
        confidence=0.92,
    )
    assert res3 == "small"


def test_assess_fix_complexity_state_logic_is_medium():
    res1 = assess_fix_complexity(critique="control-flow issue during parsing", dissent_feedback="")
    assert res1 == "medium"

    res2 = assess_fix_complexity(critique="", dissent_feedback="pagination and caching regex status exception")
    assert res2 == "medium"

    res3 = assess_fix_complexity(
        critique="Requires adjustment",
        dissent_feedback="",
        files=[MockFile("src/a.py"), MockFile("src/b.py")],
        confidence=0.75,
    )
    assert res3 == "medium"


def test_assess_fix_complexity_concurrency_or_reject_is_large():
    res1 = assess_fix_complexity(critique="concurrency race condition and potential deadlock", dissent_feedback="")
    assert res1 == "large"

    res2 = assess_fix_complexity(critique="Recommendation: REJECT — reachability flaw in refactor", dissent_feedback="")
    assert res2 == "large"

    res3 = assess_fix_complexity(critique="Recommendation: DENY", dissent_feedback="")
    assert res3 == "large"

    res4 = assess_fix_complexity(
        critique="Complex changes across modules",
        dissent_feedback="",
        files=[MockFile("a.py"), MockFile("b.py"), MockFile("c.py"), MockFile("d.py")],
    )
    assert res4 == "large"


def test_next_remediation_requirements_escalates_rank_and_excludes_model():
    req0 = LlmRequirements(complexity="small", exclude_models=())
    req1 = next_remediation_requirements(req0, failure_kind="retry", tried_key="ollama/qwen-small")
    assert req1.complexity == "medium"
    assert "ollama/qwen-small" in req1.exclude_models

    req2 = next_remediation_requirements(req1, failure_kind="retry", tried_key="ollama/qwen-medium")
    assert req2.complexity == "large"
    assert "ollama/qwen-small" in req2.exclude_models
    assert "ollama/qwen-medium" in req2.exclude_models

    req3 = next_remediation_requirements(req2, failure_kind="retry", tried_key="gemini/pro")
    assert req3.complexity == "large"
    assert "gemini/pro" in req3.exclude_models


def test_auto_remediate_respects_max_attempt_ceiling():
    repo = MockRepo("lbockenstedt/nw")
    files = [MockFile("src/clean.py", "+ def clean(): pass")]
    pr = MockPR(title="Remediation PR", body="Attempts", number=99, files=files)

    pr_remediate.state["pr_reviews"] = {
        "lbockenstedt/nw#99": {
            "remediation_attempts": 3,
            "panel_critique": "Issue persists",
        }
    }
    config = {"pr_auto_remediate_max_attempts": 3}

    called = False

    def dummy_fix(*args, **kwargs):
        nonlocal called
        called = True
        return True, "fixed"

    success, msg = auto_remediate_pr(None, repo, pr, config, fix_fn=dummy_fix)
    assert success is False
    assert "attempt limit reached" in msg.lower()
    assert not called
    assert any("Automated Remediation Limit Reached" in c for c in pr.comments)
    assert pr_remediate.state["pr_reviews"]["lbockenstedt/nw#99"]["auto_remediate_status"] == "exhausted_human_review"


def test_skeptical_review_prompt_includes_intent_vs_diff_fidelity():
    pr_review_path = os.path.join(os.path.dirname(__file__), "pr_review.py")
    with open(pr_review_path, "r", encoding="utf-8") as f:
        src = f.read()

    assert "INTENT vs DIFF FIDELITY" in src
    assert "Extract the PR's stated INTENT from the description and compare it against the actual DIFF" in src
    assert "Does the diff introduce unstated side effects, scope creep, or contradict the stated intent?" in src
