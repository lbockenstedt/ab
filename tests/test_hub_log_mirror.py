"""Hub-log mirror: bounded reads, stale-file pruning, and production-fix
verification that actually finds evidence in spoke-named mirror files.

log_scan.py can't be imported directly (circular import via main.py), so the
functions are extracted via ast, as in test_escalation_titles.py.
"""
import ast
import logging
import os
import re
import time
from datetime import datetime

FUNCS = {"_tail_lines", "_count_lines", "_prune_stale_hub_logs", "_mirror_contains",
         "_module_log_path", "_persist_hub_logs", "verify_production_fixes"}


def _ns(tmp_path, **extra):
    tree = ast.parse(open("log_scan.py").read())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in FUNCS]
    ns = {"os": os, "re": re, "time": time, "datetime": datetime,
          "logger": logging.getLogger("test"), "HUB_LOG_DIR": str(tmp_path),
          "HUB_LOG_MAX_LINES": 20000, "HUB_LOG_DEDUP_TAIL": 500, "HUB_LOG_STALE_DAYS": 7}
    ns.update(extra)
    exec(compile(ast.Module(body=keep, type_ignores=[]), "log_scan.py", "exec"), ns)
    return ns


def test_tail_lines_reads_only_the_newest_lines(tmp_path):
    p = tmp_path / "m.log"
    p.write_text("".join(f"line {i:06d} {'x' * 200}\n" for i in range(2000)))
    out = _ns(tmp_path)["_tail_lines"](str(p), 500)
    assert len(out) == 500
    assert out[0].startswith("line 001500") and out[-1].startswith("line 001999")


def test_persist_dedups_and_caps(tmp_path):
    ns = _ns(tmp_path, HUB_LOG_MAX_LINES=5, HUB_LOG_DEDUP_TAIL=3)
    ns["_persist_hub_logs"]([{"module": "m", "log": f"l{i}"} for i in range(4)])
    ns["_persist_hub_logs"]([{"module": "m", "log": "l3"}, {"module": "m", "log": "l4"},
                             {"module": "m", "log": "l5"}])
    assert (tmp_path / "m.log").read_text().split() == ["l1", "l2", "l3", "l4", "l5"]


def test_stale_mirror_files_are_pruned(tmp_path):
    old, fresh = tmp_path / "gone-spoke.log", tmp_path / "live.log"
    old.write_text("x\n")
    fresh.write_text("y\n")
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))
    _ns(tmp_path)["_prune_stale_hub_logs"]()
    assert not old.exists() and fresh.exists()


class _Issue:
    def __init__(self, body):
        self.body = body


class _Repo:
    def __init__(self, body):
        self._body = body

    def get_issue(self, n):
        return _Issue(self._body)


class _Gh:
    def __init__(self, body):
        self.body = body

    def get_repo(self, name):
        return _Repo(self.body)


def _verify(tmp_path, mirror_text):
    (tmp_path / "mipbe-lmagent-dhcp__1a2b3c4d_.log").write_text(mirror_text)
    ns = _ns(tmp_path, load_config=lambda: {}, save_processed=lambda p: None,
             state={}, _apply_closed_label=lambda *a: None)
    processed = {"lbockenstedt/dhcp:7": {"status": "awaiting_prod_verification"}}
    body = "### Log Evidence:\n```\nKea lease4-add Timed out\n```\n"
    ns["verify_production_fixes"](_Gh(body), processed)
    return processed["lbockenstedt/dhcp:7"]


def test_evidence_still_in_a_spoke_named_file_is_not_clean(tmp_path):
    # Mirror files are named after the spoke, not the repo; this used to never
    # match, so every fix was declared clean without looking at the logs.
    info = _verify(tmp_path, "2026-10-26 10:00:00 ERROR kea lease4-add timed out\n")
    assert not info.get("clean_since")


def test_evidence_gone_starts_cooling_period(tmp_path):
    info = _verify(tmp_path, "2026-10-26 10:00:00 INFO all good\n")
    assert info.get("clean_since")


def test_nothing_awaiting_skips_all_work(tmp_path):
    def _boom(*a):
        raise AssertionError("should not be called")
    ns = _ns(tmp_path, load_config=_boom)
    ns["verify_production_fixes"](None, {"r/x:1": {"status": "resolved"}})
