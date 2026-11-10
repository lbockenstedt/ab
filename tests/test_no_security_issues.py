"""AB never files hub security-monitor output to GitHub.

The fleet repos are public: an issue quoting a decoy hit, tripwire or threat
block tells attackers the security monitor exists and what it watches.
"""
import main  # noqa: F401  (loads github_ops through main; a direct import is circular)
import github_ops


class _Repo:
    """Fake repo that records every issue AB tries to create."""
    full_name = "o/lm"

    def __init__(self):
        self.created = []

    def create_issue(self, **kw):
        """Record the issue instead of filing it."""
        self.created.append(kw)
        return type("I", (), {"number": 1})()


def _file(monkeypatch, body):
    """Run create_automated_issue with dedup and cooldown stubbed; return the fake repo."""
    monkeypatch.setattr(github_ops, "_in_update_cooldown", lambda: (False, 0))
    monkeypatch.setattr(github_ops, "find_global_duplicate_issue", lambda *a: (None, None, False))
    monkeypatch.setattr(github_ops, "_ensure_label", lambda *a: None)
    repo = _Repo()
    github_ops.create_automated_issue(None, [], repo, {"title": "Hub log alert", "body": body})
    return repo


def test_security_monitor_lines_are_never_filed(monkeypatch):
    for body in ("2026-11-10 17:53:36 - Security - ERROR - SECURITY ANOMALY x [critical] — GET /y",
                 "the honeypot flagged an event",
                 "2026-11-10 17:53:36 - Security - WARNING - THREAT BLOCK 1.2.3.4 (auto)"):
        assert _file(monkeypatch, body).created == [], body


def test_ordinary_errors_are_still_filed(monkeypatch):
    repo = _file(monkeypatch, "2026-11-10 17:54:17 - Hub - ERROR - Request Timeout: [NETBOX_GET_DEVICES]")
    assert len(repo.created) == 1
