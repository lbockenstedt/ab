"""WebUI shell and log viewer in the Mission Control style: the run controls
(Scan Now, updates, restart, blackout, stop) sit in the topbar instead of a
footer action bar, and the Logs pages show one compact line per log entry."""
import pytest
from fastapi.testclient import TestClient

import auth
import main
import routes

BANNER = [
    "2026-11-13 08:53:42 - AppBuilder - WARNING - ",
    "=" * 20,
    "!!  LLM CONFIGURATION WARNING  !!",
    "Neither LLM provider is fully configured.",
    "2026-11-13 08:53:43 - httpx - INFO - HTTP Request: GET /logs",
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "any_users", lambda: True)
    monkeypatch.setattr(auth, "verify_session", lambda token: "admin")
    log = tmp_path / "ab.log"
    log.write_text("\n".join(BANNER) + "\n")
    monkeypatch.setattr(routes, "get_log_path", lambda: str(log))
    return TestClient(main.app)


def test_continuation_lines_join_the_entry_above_them():
    entries = routes.group_log_lines(BANNER)
    assert [e["module"] for e in entries] == ["httpx", "AppBuilder"]
    assert entries[1]["level"] == "warning" and entries[1]["ts"] == "2026-11-13 08:53:42"
    assert entries[1]["msg"] == "=" * 20 + "\n!!  LLM CONFIGURATION WARNING  !!\nNeither LLM provider is fully configured."
    assert entries[0]["msg"] == "HTTP Request: GET /logs"


def test_a_line_in_another_shape_keeps_its_text():
    assert routes.log_entry("Traceback (most recent call last):", "hub") == {
        "module": "hub", "ts": "", "level": "", "msg": "Traceback (most recent call last):"}


def test_every_level_spelling_keeps_its_level_and_its_own_row():
    entries = routes.group_log_lines([
        "2026-11-13 08:00:00 - hub - WARN - disk 91%",
        "2026-11-13 08:00:01 - hub - FATAL - gave up",
        "2026-11-13 08:00:02 - hub - NOTICE - rotated",
        "2026-11-13 08:00:03 - hub - TRACE - tick",
        "2026-11-13 08:00:04 plain timestamped line",
    ])
    assert [(e["level"], e["msg"]) for e in entries][::-1] == [
        ("warning", "disk 91%"), ("critical", "gave up"), ("info", "rotated"),
        ("debug", "tick"), ("", "plain timestamped line")]
    assert entries[0]["ts"] == "2026-11-13 08:00:04"


def test_logs_page_shows_one_row_per_entry_without_repeating_the_timestamp(client):
    html = client.get("/logs").text
    assert html.count('class="log-row') == 2
    assert 'data-level="warning"' in html and ">Log Message</th>" not in html
    row = html.split('class="log-row')[2].split('id="log-empty"')[0]
    assert "2026-11-13 08:53:42 - AppBuilder" not in row


def test_hub_logs_page_uses_the_same_viewer(client, monkeypatch):
    monkeypatch.setattr(routes, "get_hub_logs", lambda *a: [
        {"module": "LM-AGENT", "log": "2026-11-13 13:28:54 - netbox.autorack - ERROR - conflicts=2"}])
    monkeypatch.setattr(routes, "hub_log_line_count", lambda: 1)
    html = client.get("/hub-logs").text
    assert html.count('class="log-row') == 1
    assert 'data-level="error"' in html and ">conflicts=2<" in html and ">LM-AGENT<" in html


@pytest.mark.parametrize("path", ["/logs", "/hub-logs", "/chat"])
def test_run_controls_are_in_the_topbar_not_a_footer(client, monkeypatch, path):
    monkeypatch.setattr(routes, "get_hub_logs", lambda *a: [])
    monkeypatch.setattr(routes, "hub_log_line_count", lambda: 0)
    html = client.get(path).text
    assert "<footer" not in html
    header = html[html.index("<header"):html.index("</header>")]
    for needle in ('action="/scan_now"', 'action="/update_now"', 'action="/restart"',
                   'action="/trigger_hub_update"', 'id="blackout-btn"', 'id="pause-btn"',
                   'id="refresh-pause-btn"', 'id="hdr-blackout-chip"', 'id="hdr-paused-chip"'):
        assert header.count(needle) == 1 and html.count(needle) == 1, needle
    # Fleet rule 7.6: the version and Update stay visible, outside the menu.
    outside_menu = header[:header.index('id="hdr-actions"')]
    assert 'id="hdr-version"' in outside_menu and 'action="/update_now"' in outside_menu


def test_hub_log_tracebacks_stay_with_their_entry(monkeypatch, tmp_path):
    import log_scan
    (tmp_path / "hub.log").write_text(
        "2026-11-13 08:00:00 - core - ERROR - sync failed\n"
        "Traceback (most recent call last):\n"
        "ValueError: bad\n")
    (tmp_path / "nw.log").write_text("2026-11-13 09:00:00 - nw - INFO - walk done\n")
    monkeypatch.setattr(log_scan, "HUB_LOG_DIR", str(tmp_path))
    rows = log_scan.get_hub_logs(500, fold=True)
    assert [r["module"] for r in rows] == ["nw", "hub"]
    entry = routes.log_entry(rows[1]["log"], rows[1]["module"])
    assert entry["level"] == "error"
    assert entry["msg"] == "sync failed\nTraceback (most recent call last):\nValueError: bad"
    assert len(log_scan.get_hub_logs(500)) == 4


def test_bracketed_levels_and_untimestamped_sources():
    assert routes.log_entry("2026-11-13 08:00:00 [ERROR] boom")["level"] == "error"
    syslog = ["Nov 13 08:00:00 host a", "Nov 13 08:00:01 host b"]
    assert len(routes.group_log_lines(syslog)) == 2


def test_syslog_style_hub_file_is_not_folded(monkeypatch, tmp_path):
    import log_scan
    (tmp_path / "sys.log").write_text("Nov 13 08:00:00 a\nNov 13 08:00:01 b\n")
    monkeypatch.setattr(log_scan, "HUB_LOG_DIR", str(tmp_path))
    assert len(log_scan.get_hub_logs(500, fold=True)) == 2


def test_failed_log_read_shows_as_an_error(client, monkeypatch, tmp_path):
    monkeypatch.setattr(routes, "get_log_path", lambda: str(tmp_path / "missing.log"))
    html = client.get("/logs").text
    assert 'data-level="error"' in html and "Error reading logs" in html


def test_a_traceback_across_the_100_line_cut_keeps_its_entry(client, monkeypatch, tmp_path):
    log = tmp_path / "long.log"
    log.write_text("2026-11-13 08:00:00 - ab - ERROR - crashed\n" + "  frame\n" * 100)
    monkeypatch.setattr(routes, "get_log_path", lambda: str(log))
    html = client.get("/logs").text
    assert "crashed" in html and html.count('class="log-row') == 1
