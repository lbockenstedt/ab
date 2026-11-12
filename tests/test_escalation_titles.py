#!/usr/bin/env python3
"""Escalated hub-log issues must be titled after the actual problem, not the
analysis boilerplate "1. **Is there a problem?** Yes." that every analysis
opens with (which folded unrelated problems into lm#1228).

log_scan.py can't be imported directly (circular import via main.py), so the
helper is extracted via ast, as in test_log_scan_requirements.py.
"""
import ast
import re


def _summary_fn():
    tree = ast.parse(open("log_scan.py").read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_escalation_summary_line")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_ESCALATION_HEADER"
                                                  for t in n.targets))]
    ns = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "log_scan.py", "exec"), ns)
    return ns["_escalation_summary_line"]


ANALYSIS = """1. **Is there a problem?** Yes.

2. **What is going wrong:**
Two distinct issues require attention:

* **CPPM agent timeouts:** Multiple `LIST_ENDPOINTS` requests timed out after 20 seconds.
"""


def test_skips_boilerplate_headers():
    assert _summary_fn()(ANALYSIS) == "Two distinct issues require attention:"


def test_strips_markdown_from_problem_line():
    out = _summary_fn()("1. **Is there a problem?** Yes.\n\n**Certificate push to `ab` fails**")
    assert out == "Certificate push to ab fails"


def test_empty_analysis():
    assert _summary_fn()("") == ""


def test_rule_lines_are_skipped():
    assert _summary_fn()("---\n****\nKea control socket refuses connections") == \
        "Kea control socket refuses connections"


def _recent_fn():
    import logging
    from datetime import datetime
    tree = ast.parse(open("log_scan.py").read())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "recent_deploy_changes"]
    ns = {"datetime": datetime, "logger": logging.getLogger("t")}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "log_scan.py", "exec"), ns)
    return ns["recent_deploy_changes"]


def test_post_deploy_watch_lists_recent_commits_on_the_deployed_branch():
    from types import SimpleNamespace as N
    seen = {}

    class Repo:
        default_branch = "main"

        def get_commits(self, sha, since):
            seen.update(sha=sha, since=since)
            return [N(sha="abcdef1234", commit=N(message="nw: dedupe rows\n\nbody"))]

    out = _recent_fn()(Repo(), {"post_deploy_window_h": 2})
    assert out == ["- `abcdef12` nw: dedupe rows"] and seen["sha"] == "main"
    assert _recent_fn()(Repo(), {"post_deploy_window_h": 0}) == []


def _fns(*names):
    tree = ast.parse(open("log_scan.py").read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("_OPERATIONAL_FAULT", "OPS_ALERT_LABEL")
                                                  for t in n.targets))]
    ns = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "log_scan.py", "exec"), ns)
    return ns


SLICE_A = """2026-11-10 23:11:39 - Hub - ERROR - Request Timeout: [SPOKE_GET_QUEUE_STATS] req=38da01db from ab after 8.0s
2026-11-10 23:12:27 - Hub - ERROR - Request Timeout: [NETBOX_GET_DEVICES] req=41059bf6 from mipbe-lmagent-netbox (48f15d31) after 8.0s
2026-11-10 23:12:28 - Hub - INFO - cycle done
2026-11-10 23:12:29 - Hub - ERROR - Request Timeout: [NETBOX_GET_DEVICES] req=b6db167b from mipbe-lmagent-netbox (48f15d31) after 8.0s"""

SLICE_B = """2026-11-11 05:11:39 - Hub - ERROR - Request Timeout: [NETBOX_GET_DEVICES] req=0c11aa92 from mipbe-lmagent-netbox (48f15d31) after 30.0s
2026-11-11 05:11:41 - Hub - ERROR - Request Timeout: [NETBOX_GET_DEVICES] req=77de0f13 from mipbe-lmagent-netbox (48f15d31) after 30.0s"""


def test_same_fault_in_two_slices_gets_the_same_signature():
    """The sentinel's prose differs on every run (lm#1800, #1802, #1809, ...);
    the masked log line does not, so dedup can fold the repeats."""
    sig = _fns("_mask_ids", "_slice_signature")["_slice_signature"]
    assert sig(SLICE_A) == sig(SLICE_B) == \
        "Request Timeout: [NETBOX_GET_DEVICES] req=# from mipbe-lmagent-netbox (#) after #s"


def test_slice_without_errors_has_no_signature():
    sig = _fns("_mask_ids", "_slice_signature")["_slice_signature"]
    assert sig("2026-11-10 - Hub - INFO - all good") == ""


def test_operational_faults_are_recognised():
    ops = _fns("is_operational_fault")["is_operational_fault"]
    for title in ("Request Timeout: [NETBOX_GET_DEVICES] req=# from x after #s",
                  "Health check: NW_GET_MAC_TABLE keeps timing out on agent-dxp",
                  "Health check: slow command NW_GET_ARP on agent-dxp",
                  "Curl request failed for https://#: curl: (#) Connection timed out after # ms",
                  "HTTP error # for GET https://#/api/endpoint: 403 Forbidden",
                  "All OAuth# attempts failed",
                  "Let's Encrypt certificate renewal failed"):
        assert ops(title), title


def test_code_faults_still_go_to_the_fixer():
    ops = _fns("is_operational_fault")["is_operational_fault"]
    for title in ("Health check: excessive Key Vault reads",
                  "Health check: NETBOX_GET_DEVICES is called very often on netbox",
                  "UNCAUGHT EXCEPTION: 'feed_counts' is undefined",
                  "KeyError: 'device_id' in nw_discovery_sync"):
        assert not ops(title), title


def test_operational_faults_found_beyond_the_masked_title():
    """The 90-char masked title hides "403" and cuts "timed out"; the raw first
    ERROR line and the fixed health-check text still name the fault."""
    op = _fns("is_operational_fault", "escalation_is_operational")["escalation_is_operational"]
    curl = ("Health check: repeated error: - OpnsenseEngine - ERROR - Curl request failed for "
            "https://#.#.#.#:#/a")
    assert op(curl, "", curl + "\n\ncurl: (28) Connection timed out after 10002 milliseconds",
              "health-check")
    cppm = "Health check: repeated error: - CPPMClient - ERROR - HTTP error # for GET https://#.#.#.#/api/endpoi"
    assert not _fns("is_operational_fault")["is_operational_fault"](cppm)
    assert op(cppm, "", "HTTP error 403 for GET https://172.16.1.94/api/endpoint ... Forbidden",
              "health-check")
    assert op("x", "2026-11-12 - CPPMClient - ERROR - HTTP error 403 for GET https://h/api", "", "escalate")
    assert op("renew failed for *.example.com: Saving debug log to /var/log/letsencrypt/letsencrypt.log",
              "", "", "escalate")
    assert op("nw [cx_switch/ssh] device_info # -> error: cli #: SSH connection failed", "", "", "escalate")
    # Sentinel prose mentions timeouts loosely: not a reason on its own.
    assert not op("KeyError: 'device_id'", "", "might be a timeout upstream", "escalate")


def test_code_faults_stay_with_the_fixer_on_all_inputs():
    op = _fns("is_operational_fault", "escalation_is_operational")["escalation_is_operational"]
    for title in ("failed to list NetBox devices: 400 Bad Request from /api/dcim/devices/",
                  "Health check: slow page POST /api/dns/cluster/discover"):
        assert not op(title, "", title, "health-check"), title
    tb = "2026-11-12 - Hub - ERROR - TypeError: unsupported operand; KeyError: 'device_id'"
    assert not op("TypeError: unsupported operand", tb, "", "escalate")


def test_operational_match_on_an_existing_issue_moves_it_to_ops_alert():
    """lm#1739/lm#1826 kept automated-fix after an operational repeat, so the
    fixer kept retrying them."""
    import logging
    tree = ast.parse(open("log_scan.py").read())
    names = ("file_escalated_issue", "_mark_ops_alert", "is_operational_fault",
             "escalation_is_operational", "_escalation_summary_line", "_slice_signature", "_mask_ids")
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in
                                                  ("_OPERATIONAL_FAULT", "OPS_ALERT_LABEL", "_ESCALATION_HEADER")
                                                  for t in n.targets))]

    class Label:
        def __init__(self, name):
            self.name = name

    class Issue:
        number = 1739
        _bf_was_duplicate = True

        def __init__(self):
            self.labels = [Label("automated-fix"), Label("log-detected")]

        def add_to_labels(self, name):
            self.labels.append(Label(name))

        def remove_from_labels(self, name):
            self.labels = [lbl for lbl in self.labels if lbl.name != name]

    issue = Issue()

    class Gh:
        def __init__(self, token):
            pass

        def get_repo(self, name):
            return object()

    import os
    ns = {"re": re, "os": os, "logger": logging.getLogger("t"), "Github": Gh,
          "load_config": dict, "get_monitored_repos": lambda c: [],
          "MODULE_TYPE_REPO": {"hub": "o/lm"}, "resolve_module_repo": lambda *a: None,
          "recent_deploy_changes": lambda *a: [],
          "create_automated_issue": lambda *a, **k: issue}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "log_scan.py", "exec"), ns)
    ok, _ = ns["file_escalated_issue"](
        "hub", "2026-11-12 - CPPMClient - ERROR - HTTP error 403 for GET https://h/api",
        "**CPPM rejects us**", config={"GITHUB_TOKEN": "t"})
    assert ok
    assert [lbl.name for lbl in issue.labels] == ["log-detected", "ops-alert"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
