"""A hub ACK, not issue creation alone, completes bug/feature report ingestion."""
import ast
import json
import logging
import re
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def ingestion(caplog):
    """Run the real scanner with an offline hub and the existing issue-dedup contract."""
    reports, replies, acknowledgements, issues, filings, state = {}, {}, [], {}, [], {}

    def request(command, data, timeout):
        """Serve the durable index and reports, and inject per-report ACK outcomes."""
        if command == "GET_BUG_REPORTS":
            return {"reports": list(reports.values())}
        rid = data["id"]
        if command == "GET_BUG_REPORT":
            return reports[rid]
        assert command in {"MARK_BUG_FILED", "MARK_BUG_DUPLICATE"}
        acknowledgements.append((command, data))
        result = replies[rid].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def file_issue(_gh, _monitored, _repo, data, **kwargs):
        """A retry finds the same issue through the normal global-dedup path."""
        rid = next(rid for rid in reports if f"bug-report-id: {rid}" in data["body"])
        filings.append(rid)
        if rid not in issues:
            issues[rid] = SimpleNamespace(
                html_url="https://example.test/issue/1", number=1,
                _bf_was_duplicate=reports[rid]["duplicate"])
        else:
            issues[rid]._bf_was_duplicate = True
        return issues[rid]

    namespace = {
        "state": state, "time": time, "re": re, "json": json, "textwrap": textwrap,
        "logger": logging.getLogger(__name__), "MODULE_TYPE_REPO": {},
        "_get_hub_agent_client": lambda: SimpleNamespace(request_sync=request),
        "get_monitored_repos": lambda config: ["owner/repo"],
        "resolve_self_diagnosis_repo": lambda config: "owner/repo",
        "create_automated_issue": file_issue,
    }
    source = (Path(__file__).resolve().parents[1] / "log_scan.py").read_text()
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef)
             and node.name in {"scan_bugs", "_safe_json_field"}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "log_scan.py", "exec"), namespace)
    scan = namespace["scan_bugs"]
    caplog.set_level(logging.INFO, logger=__name__)

    def run():
        """Nonempty unrelated logs allow durable-index discovery without a marker."""
        scan(SimpleNamespace(get_repo=lambda name: SimpleNamespace(full_name=name)),
             {"bug_report_repo": "owner/repo"}, [{"log": "heartbeat"}])

    def add(rid, kind, duplicate, responses):
        """Queue one unfiled report and its hub acknowledgements."""
        reports[rid] = {"id": rid, "type": kind, "summary": "Example failure",
                        "duplicate": duplicate}
        replies[rid] = list(responses)

    return SimpleNamespace(
        run=run, add=add, scan=scan, state=state, acknowledgements=acknowledgements,
        issues=issues, filings=filings)


@pytest.mark.parametrize("kind", ["bug", "feature"])
@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("failure", [
    None, {"status": "not_found"}, {"status": "failed"}, TimeoutError("ACK timeout")],
    ids=["empty", "not-found", "failed", "exception"])
def test_failed_ack_retries_existing_issue_then_skips_confirmed_report(
        ingestion, caplog, kind, duplicate, failure):
    """Every failed ACK stays retryable without falsely counting or permanently caching it."""
    env = ingestion
    env.add("a1", kind, duplicate, [failure, {"status": "ok"}])
    env.run()
    assert "a1" not in env.scan._filed
    assert env.state["bug_ingest"]["filed_this_cycle"] == 0
    assert env.state["feature_ingest"]["filed_this_cycle"] == 0
    assert env.state["bug_ingest"]["error"]
    assert bool(env.state["feature_ingest"]["error"]) is (kind == "feature")
    kind_label = "feature request" if kind == "feature" else "bug report"
    assert not any(f"{kind_label} a1 ->" in record.message for record in caplog.records)
    first_command = "MARK_BUG_DUPLICATE" if duplicate else "MARK_BUG_FILED"
    assert env.acknowledgements[0][0] == first_command

    env.run()
    assert env.scan._filed == {"a1"}
    assert env.state["bug_ingest"]["filed_this_cycle"] == 1
    assert env.state["feature_ingest"]["filed_this_cycle"] == int(kind == "feature")
    assert env.acknowledgements[1][0] == "MARK_BUG_DUPLICATE"
    assert env.filings == ["a1", "a1"] and len(env.issues) == 1
    assert not env.state["bug_ingest"]["error"]

    env.run()
    assert env.state["bug_ingest"]["filed_this_cycle"] == 0
    assert len(env.acknowledgements) == 2 and env.filings == ["a1", "a1"]


@pytest.mark.parametrize("kind", ["bug", "feature"])
def test_failed_ack_does_not_block_later_report(ingestion, kind):
    """A failed report does not prevent another report's successful ACK in the same scan."""
    env = ingestion
    env.add("a1", kind, False, [{"status": "failed"}])
    env.add("b2", kind, False, [{"status": "ok"}])
    env.run()
    assert env.scan._filed == {"b2"}
    assert env.state["bug_ingest"]["filed_this_cycle"] == 1
    assert env.state["feature_ingest"]["filed_this_cycle"] == int(kind == "feature")
    assert [data["id"] for _, data in env.acknowledgements] == ["a1", "b2"]


@pytest.mark.parametrize("kind", ["bug", "feature"])
@pytest.mark.parametrize("duplicate", [False, True])
def test_confirmed_ack_keeps_existing_success_path(ingestion, kind, duplicate):
    """An immediate successful ACK still caches, counts and links the report once."""
    env = ingestion
    env.add("a1", kind, duplicate, [{"status": "ok"}])
    env.run()
    assert env.scan._filed == {"a1"}
    assert env.state["bug_ingest"]["filed_this_cycle"] == 1
    assert env.state["feature_ingest"]["filed_this_cycle"] == int(kind == "feature")
    command, data = env.acknowledgements[0]
    assert command == ("MARK_BUG_DUPLICATE" if duplicate else "MARK_BUG_FILED")
    assert data == {"id": "a1", "issue_url": env.issues["a1"].html_url}
