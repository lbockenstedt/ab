"""A recurring error updates ONE AppBuilder comment instead of adding one per
repeat (nw#211 reached 100 comments), and no UUID, IP or spoke-id fragment
reaches the public issue."""
from datetime import datetime, timezone
from types import SimpleNamespace as N

import main  # noqa: F401 - loads github_ops in import order
import github_ops


_IP = ".".join(("198", "51", "100", "9"))  # documentation range, built not literal


class Comment:
    def __init__(self, body):
        self.body = body

    def edit(self, body):
        self.body = body


class Issue:
    def __init__(self, number=211, title="t", body="b", comments=()):
        self.number, self.title, self.body = number, title, body
        self.comments = [Comment(c) for c in comments]
        self.labels = []
        self.state = "open"

    def get_comments(self):
        return list(self.comments)

    def create_comment(self, body):
        self.comments.append(Comment(body))

    def edit(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_redact_identity_masks_ids_but_keeps_log_numbers():
    uuid = "-".join(("ab12cd34", "1a2b", "4c3d", "8e9f", "0a1b2c3d4e5f"))  # built, not a literal
    ip = ".".join(("198", "51", "100", "7"))
    text = ("Health check: slow command NW_GET_ARP on agent-dxp-lm-agent-network (c73fbb13)\n"
            f"2026-11-09 11:56:33 - NwSpoke - INFO - {{'device_id': '{uuid}', "
            f"'address': '{ip}'}} 26 of 72 calls >= 5.0s from x (#) oid 1.3.6.1.2.1.1")
    out = github_ops.redact_identity(text)
    assert "c73fbb13" not in out and uuid not in out and ip not in out
    assert "oid 1.3.6.1.2.1.1" in out
    assert "on agent-dxp-lm-agent-network\n" in out
    assert "'<id>'" in out and "'<ip>'" in out
    assert "2026-11-09 11:56:33" in out and "26 of 72 calls >= 5.0s from x (#)" in out


def test_repeats_edit_one_comment_and_count_the_old_ones():
    issue = Issue(comments=["🤖 **AppBuilder Update**\n\nAdditional instance of this error detected in x (c73fbb13)",
                            "a human comment",
                            "🤖 **AppBuilder Update**\n\nAdditional instance of this error detected in x"])
    t1 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    assert github_ops.upsert_recurrence_comment(issue, "o/nw", "first", now=t1) == 3
    t2 = datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)
    assert github_ops.upsert_recurrence_comment(issue, "o/nw", "second", now=t2) == 4
    assert len(issue.comments) == 4
    mine = issue.comments[-1].body
    assert "**4** time(s)" in mine and "2026-11-16 10:00 UTC" in mine and "2026-11-16 11:00 UTC" in mine
    assert "````\nsecond\n````" in mine and "````\nfirst" not in mine
    assert "c73fbb13" not in issue.comments[0].body


def test_open_duplicate_updates_the_comment_redacts_and_stamps_the_record(monkeypatch):
    issue = Issue(title="Hub log alert (nw): slow command NW_GET_ARP on agent-network (c73fbb13)",
                  body="evidence from " + _IP)
    store = {"o/nw:211": {"status": "failed", "timestamp": "2026-11-13T10:00:00"}}
    monkeypatch.setattr(github_ops, "_in_update_cooldown", lambda: (False, 0))
    monkeypatch.setattr(github_ops, "find_global_duplicate_issue", lambda *a: (issue, "o/nw", False))
    monkeypatch.setattr(github_ops, "load_processed", lambda: store)
    monkeypatch.setattr(github_ops, "save_processed", lambda p: None)
    for _ in range(3):
        got = github_ops.create_automated_issue(
            None, ["o/nw"], N(full_name="o/nw"),
            {"title": "slow command on agent-network (c73fbb13)", "body": "seen on 10.9.8.7", "repo": "o/nw"})
        assert got is issue
    assert len(issue.comments) == 1
    assert "**3** time(s)" in issue.comments[0].body and "10.9.8.7" not in issue.comments[0].body
    assert "c73fbb13" not in issue.title and _IP not in issue.body
    assert store["o/nw:211"]["last_recurrence_at"]
