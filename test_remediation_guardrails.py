"""Docstring here."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pr_remediate
from pr_remediate import check_pr_guardrails


class _F:
    def __init__(self, filename, patch=""):
        self.filename = filename
        self.patch = patch

class _PR:
    title = "t"
    body = "b"

class _Head:
    ref = "fix/thing"
    sha = "aaaaaaaaaaaa"

class _Repo:
    full_name = "owner/repo"


def test_ordinary_security_dir_change_is_not_blocked():
    files = [_F("core/src/security/threat_monitor.py", "+    ip = ipaddress.ip_address(raw)\n+    return None")]
    paths = ["core/src/security/threat_monitor.py"]
    passed, reason = check_pr_guardrails(_PR(), files, paths, {})
    assert passed is True and reason is None


def test_security_dir_change_with_psk_keyword_is_blocked():
    files = [_F("core/src/security/handshake.py", '+    psk = "literal-value-here"')]
    paths = ["core/src/security/handshake.py"]
    passed, reason = check_pr_guardrails(_PR(), files, paths, {})
    assert passed is False and "psk-hardcode" in reason


def test_security_dir_change_mentioning_shared_secret_is_blocked():
    files = [_F("core/src/security/handshake.py", "+    # rotate the shared secret on reconnect")]
    paths = ["core/src/security/handshake.py"]
    passed, reason = check_pr_guardrails(_PR(), files, paths, {})
    assert passed is False


def test_boundary_path_with_unreadable_patch_fails_closed():
    files = [_F("core/src/security/x.py", "")]     # no patch text at all
    paths = ["core/src/security/x.py"]
    passed, reason = check_pr_guardrails(_PR(), files, paths, {})
    assert passed is False        # fail closed: cannot disprove, so keep blocking


def test_guardrail_block_holds_on_the_same_head():
    record = {"auto_remediate_blocked": True, "auto_remediate_blocked_head": "sha-one",
              "panel_verdict": "Deny", "panel_confidence": 0.5}
    saved_state = pr_remediate.state
    saved_fn = pr_remediate.auto_remediate_pr
    calls = []
    pr_remediate.state = {"pr_reviews": {"owner/repo#7": record}}
    pr_remediate.auto_remediate_pr = lambda *a, **k: (calls.append(1) or (True, "ran"))
    try:
        class P:
            number = 7
            draft = False
            merged = False
            state = "open"
            head = type("H", (), {"sha": "sha-one", "ref": "fix/thing"})()
            def get_files(self):
                return [_F("src/app.py", "@@\n+x = 1\n")]
        ok, reason = pr_remediate.maybe_auto_remediate(None, _Repo(), P(), {})
        assert ok is False and "blocked by guardrail" in reason and calls == []
    finally:
        pr_remediate.state = saved_state
        pr_remediate.auto_remediate_pr = saved_fn


def test_guardrail_block_clears_when_the_head_moves():
    record = {"auto_remediate_blocked": True, "auto_remediate_blocked_head": "sha-one",
              "panel_verdict": "Deny", "panel_confidence": 0.5}
    saved_state = pr_remediate.state
    saved_fn = pr_remediate.auto_remediate_pr
    calls = []
    pr_remediate.state = {"pr_reviews": {"owner/repo#7": record}}
    pr_remediate.auto_remediate_pr = lambda *a, **k: (calls.append(1) or (True, "ran"))
    try:
        class P:
            number = 7
            draft = False
            merged = False
            state = "open"
            head = type("H", (), {"sha": "sha-two", "ref": "fix/thing"})()
            def get_files(self):
                return [_F("src/app.py", "@@\n+x = 1\n")]
        ok, reason = pr_remediate.maybe_auto_remediate(None, _Repo(), P(), {})
        assert calls == [1]      # it re-evaluated instead of refusing forever
    finally:
        pr_remediate.state = saved_state
        pr_remediate.auto_remediate_pr = saved_fn


def test_denied_panel_triggers_remediation():
    record = {"panel_verdict": "Deny", "panel_confidence": 0.85,
              "panel2_verdict": "Deny", "panel2_confidence": 0.67}
    saved_state = pr_remediate.state
    saved_fn = pr_remediate.auto_remediate_pr
    calls = []
    pr_remediate.state = {"pr_reviews": {"owner/repo#7": record}}
    pr_remediate.auto_remediate_pr = lambda *a, **k: (calls.append(1) or (True, "ran"))
    try:
        class P:
            number = 7
            draft = False
            merged = False
            state = "open"
            head = type("H", (), {"sha": "sha-three", "ref": "fix/thing"})()
            def get_files(self):
                return [_F("src/app.py", "@@\n+x = 1\n")]
        ok, reason = pr_remediate.maybe_auto_remediate(None, _Repo(), P(), {})
        assert calls == [1]
    finally:
        pr_remediate.state = saved_state
        pr_remediate.auto_remediate_pr = saved_fn
