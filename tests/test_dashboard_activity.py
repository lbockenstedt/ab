"""Dashboard "All activity" in the Mission Control style: filter chips carry the
counts the old sidebar System Health block showed, one line per issue or PR
with one status chip and a ⋯ menu, bulk actions in a menu, and Working on now
start times the browser can read in any timezone."""
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

import auth
import main
import mission_control
import routes
from app_state import state

HOLD = "CI is failing on head abc — needs human review"
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


@pytest.fixture
def page(monkeypatch):
    """The dashboard rendered with Mission Control OFF, so everything asserted
    here comes from All activity, not from the Mission Control block."""
    monkeypatch.setattr(auth, "any_users", lambda: True)
    monkeypatch.setattr(auth, "verify_session", lambda token: "admin")
    monkeypatch.setattr(routes, "load_config", lambda: {"dashboard_mission_control": False})
    now = datetime.now()
    monkeypatch.setitem(state, "processed", {
        "o/lm:1": {"status": "failed", "timestamp": now.isoformat(), "title": "Sync drops tags", "error": "no edits"},
        "o/lm:2": {"status": "failed", "timestamp": now.isoformat(), "title": "Topology crash"},
        "o/lm:3": {"status": "pending_verification", "timestamp": now.isoformat(), "title": "Heartbeat"},
        # Finished 30 days ago: out of the 7-day feed, so neither a row nor a count.
        "o/lm:4": {"status": "fixed", "timestamp": (now - timedelta(days=30)).isoformat(), "title": "Old fix"},
    })
    monkeypatch.setitem(state, "pr_reviews", {
        "o/lm#5": {"repo": "o/lm", "number": 5, "title": "PR 5", "url": "u", "items": [],
                   "reviewed_at": now.strftime("%Y-%m-%d %H:%M:%S"), "panel_verdict": "Approve",
                   "panel_confidence": 0.91, "auto_merge_blocked_reason": HOLD},
        "o/lm#6": {"repo": "o/lm", "number": 6, "title": "PR 6", "url": "u", "items": [], "denied": True,
                   "reviewed_at": now.strftime("%Y-%m-%d %H:%M:%S"), "auto_merge_blocked_reason": "stale hold"},
    })
    start = now - timedelta(minutes=11)
    monkeypatch.setitem(state, "active_tasks", {"o/lm:9": {"name": "Fix o/lm:9", "start_time": start}})
    client = TestClient(main.app)
    return client, client.get("/").text, start


def _rows(html):
    """Each All activity row's markup, up to the next row."""
    return html.split('class="act-row')[1:]


def test_sidebar_counters_moved_onto_the_filter_chips(page):
    _, html, _ = page
    assert ">System Health<" not in html and "goFilter(" not in html
    def chip(key, label, count):
        return re.search(rf'data-filter="{key}"[^>]*>{label} <span class="act-count">{count}</span>', html)
    assert chip("failed", "Failed", 2) and chip("pending_verification", "Pending verification", 1)
    assert chip("all", "Active", 3) and chip("pr_review", "Pending", 1)
    # A chip with nothing behind it is dimmed.
    assert re.search(r'data-filter="closed"[^>]*opacity-50', html)
    assert not re.search(r'data-filter="failed"[^>]*opacity-50', html)


def test_an_issue_row_is_one_line_with_one_chip_a_primary_and_a_menu(page):
    _, html, _ = page
    row = next(r for r in _rows(html) if 'data-id="o/lm:1"' in r)
    assert row.count("act-status") == 1
    primary, menu = row.split('<details class="act-menu', 1)
    assert 'data-mc="retry" data-id="o/lm:1"' in primary
    for action in ("reopen", "resolve", "dismiss"):
        assert f'data-mc="{action}" data-id="o/lm:1"' in menu.split("</details>")[0]
    # The old always-open detail row is a collapsed expander now.
    assert '<details class="act-details' in row and "no edits" in row


def test_pr_row_shows_one_status_chip_panel_chips_and_the_hold_reason(page):
    _, html, _ = page
    row = next(r for r in _rows(html) if 'data-pr-state="reviewed"' in r)
    assert row.count("act-status") == 1 and ">Held<" in row
    assert "Panel 1 · Approve 0.91" in row
    assert f">{HOLD}<" in row
    assert "ADVISORY" not in row and "AUTO-MERGE HELD" not in row


def test_bulk_actions_menu_and_no_inline_handlers(page):
    _, html, _ = page
    menu = html[html.index('id="act-bulk-menu"'):]
    menu = menu[:menu.index("</details>")]
    assert 'data-mc="clear-history"' in menu and 'data-mc="dismiss-all"' in menu
    assert 'data-mc="retry-all"' in html and 'data-mc="retry-all"' not in menu
    with open("templates/index.html", encoding="utf-8") as f:
        src = f.read()
    activity = src[src.index("<!-- All activity:"):src.index("{% if view == 'logs' %}")]
    assert "onclick=" not in activity
    dismiss_all = src[src.index("async function deleteAllIssues"):]
    dismiss_all = dismiss_all[:dismiss_all.index("fetch(")]
    assert "prompt(" in dismiss_all and "'DISMISS'" in dismiss_all


def test_working_on_now_start_time_carries_its_utc_offset(page):
    """A naive start_time read by a browser in another timezone than the server
    showed hours of extra elapsed time; the offset makes it one instant."""
    client, html, start = page
    data_start = re.search(r'data-tid="o/lm:9" data-start="([^"]+)"', html).group(1)
    api_start = client.get("/api/task-details").json()["active_tasks"]["o/lm:9"]["start_time"]
    for value in (data_start, api_start):
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed == start.astimezone()


def test_active_count_is_the_rows_the_active_filter_shows(page):
    """The Active chip counts the rendered rows that are not closed/resolved;
    an issue that aged out of the feed has neither a row nor a count."""
    _, html, _ = page
    rows = [r for r in _rows(html) if 'data-pr-state' not in r.split(">", 1)[0]]
    shown = [r for r in rows if re.match(r'[^"]*" data-status="(?!closed"|resolved")', r)]
    assert 'data-id="o/lm:4"' not in html
    assert re.search(rf'data-filter="all"[^>]*>Active <span class="act-count">{len(shown)}</span>', html)
    assert re.search(r'data-filter="fixed"[^>]*>Fixed <span class="act-count">0</span>', html)


def test_why_line_only_on_open_prs(page):
    _, html, _ = page
    denied = next(r for r in _rows(html) if 'data-pr-state="denied"' in r)
    assert "Why:" not in denied and "stale hold" not in denied


def test_pr_chip_follows_mission_control_state():
    """The row's filter key, chip and Why come from _pr_state, so an approved
    PR that still needs a person is never a green Approved row."""
    def chip(**pr):
        return mission_control.pr_chip(pr)
    # Approved, but GitHub refused the merge or a hold waits on a person: Pending filter.
    assert chip(approved=True, merge_blocked_reason="branch protected")[:3] == ("reviewed", "Merge refused", "red")
    assert chip(approved=True, auto_merge_blocked_reason=HOLD) == ("reviewed", "Held", "amber", HOLD)
    # Approved and AppBuilder is merging it: working, under Pending until it lands.
    assert chip(approved=True, auto_merge_reason="clean")[:3] == ("reviewed", "Auto-merging", "blue")
    # Plainly approved, waiting for Merge: the only open PR under Approved.
    assert chip(approved=True)[:3] == ("approved", "Approved", "green")
    # Other needs-you states keep their own label instead of a generic Pending.
    assert chip(auto_remediate_status="exhausted_human_review")[:2] == ("reviewed", "Fixes stopped")
    assert chip()[:3] == ("reviewed", "Awaiting review", "indigo")
    # Self-clearing holds are in flight in Mission Control, so not Held here.
    for hold in ("waiting on GitHub Actions — outage", "under a release lock", "in a blackout window"):
        assert mission_control._pr_state({"auto_merge_blocked_reason": hold})[0] == "working"
        assert chip(auto_merge_blocked_reason=hold)[:3] == ("reviewed", "Improving", "blue")
    # Finished PRs are unchanged; auto-merged wins over merged.
    assert chip(approved=True, merged=True, auto_merged=True)[:2] == ("auto_merged", "Auto-merged")
    assert chip(approved=True, merged=True)[:2] == ("merged", "Merged")
    assert chip(denied=True, auto_merge_blocked_reason=HOLD) == ("denied", "Denied", "red", "")


def test_approved_prs_that_need_a_person_are_pending_rows_and_counts(page, monkeypatch):
    client, _, _ = page
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    def pr(n, **kw):
        return {"repo": "o/lm", "number": n, "title": "PR %d" % n, "url": "u", "items": [], "reviewed_at": now, **kw}
    monkeypatch.setitem(state, "pr_reviews", {
        "o/lm#10": pr(10, approved=True, auto_merge_blocked_reason=HOLD),
        "o/lm#11": pr(11, approved=True, merge_blocked_reason="branch protected"),
        "o/lm#12": pr(12, approved=True),
        "o/lm#13": pr(13, auto_merge_blocked_reason="feature_automerge_enabled is off"),
    })
    html = client.get("/").text
    rows = {n: next(r for r in _rows(html) if "PR %d<" % n in r or "#%d<" % n in r) for n in (10, 11, 12, 13)}
    def state_and_chip(r):
        return (re.search(r'data-pr-state="(\w+)"', r).group(1), re.search(r'act-status[^>]*>([^<]+)<', r).group(1))
    assert state_and_chip(rows[10]) == ("reviewed", "Held")
    assert state_and_chip(rows[11]) == ("reviewed", "Merge refused")
    assert state_and_chip(rows[12]) == ("approved", "Approved")
    assert state_and_chip(rows[13]) == ("reviewed", "Awaiting review")
    assert re.search(r'data-filter="pr_review"[^>]*>Pending <span class="act-count">3</span>', html)
    assert re.search(r'data-filter="pr_approved"[^>]*>Approved <span class="act-count">1</span>', html)
    # The Why line is _pr_state's why, not the raw hold string.
    assert "GitHub refused the merge: branch protected" in rows[11]
    assert "auto-merge does not apply (feature_automerge_enabled is off)" in rows[13]


class _Ancestors(HTMLParser):
    """Records, for each ⋯ menu inside #activity, the classes of its ancestors
    from the #activity card down."""

    def __init__(self):
        super().__init__()
        self.stack, self.menus = [], []

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        cls = a.get("class") or ""
        ids = [i for _, i, _ in self.stack]
        if tag == "details" and "act-menu" in cls and "activity" in ids:
            # From the card down: the page's own scroll container (main) is fine,
            # the menu scrolls with it.
            self.menus.append([c for _, _, c in self.stack[ids.index("activity"):]])
        self.stack.append((tag, a.get("id"), cls))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


def test_no_ancestor_clips_the_activity_menus(page):
    """overflow-hidden on the card clipped the ⋯ menu of the last row, often
    the only row under the default filter, so Deny/Fix/Reprocess were unreachable."""
    _, html, _ = page
    parser = _Ancestors()
    parser.feed(html)
    assert len(parser.menus) >= 3  # the bulk menu and the row menus
    for ancestors in parser.menus:
        assert not any(re.search(r"\boverflow-(hidden|auto|scroll|clip|[xy]-)", c) for c in ancestors), ancestors
