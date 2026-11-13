"""Dashboard "All activity" in the Mission Control style: filter chips carry the
counts the old sidebar System Health block showed, one line per issue or PR
with one status chip and a ⋯ menu, bulk actions in a menu, and Working on now
start times the browser can read in any timezone."""
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import auth
import main
import routes
from app_state import state

HOLD = "CI is failing on head abc — needs human review"


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
    })
    monkeypatch.setitem(state, "pr_reviews", {
        "o/lm#5": {"repo": "o/lm", "number": 5, "title": "PR 5", "url": "u", "items": [],
                   "reviewed_at": now.strftime("%Y-%m-%d %H:%M:%S"), "panel_verdict": "Approve",
                   "panel_confidence": 0.91, "auto_merge_blocked_reason": HOLD},
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
