"""Real disk history/publication helpers for isolated AST and imported-module tests."""
import ast
import copy
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from collections.abc import MutableMapping
from datetime import datetime


class DiskHistory(MutableMapping):
    """Dictionary-shaped test fixture backed by the actual JSON transaction."""

    def __init__(self, ns):
        self.ns = ns

    def __getitem__(self, key):
        return self.ns["load_processed"]()[key]

    def __setitem__(self, key, value):
        self.ns["update_processed"](lambda current: current.__setitem__(key, value))

    def __delitem__(self, key):
        self.ns["update_processed"](lambda current: current.__delitem__(key))

    def __iter__(self):
        return iter(self.ns["load_processed"]())

    def __len__(self):
        return len(self.ns["load_processed"]())

    def __deepcopy__(self, memo):
        return copy.deepcopy(self.ns["load_processed"](), memo)


def bind_history(ns=None):
    """Install actual storage and ordered publication functions into a test namespace."""
    ns = ns if ns is not None else {}
    root = Path(__file__).resolve().parents[1]
    names = {
        "config_store.py": {
            "ProcessedStoreError", "ProcessedUnreadableError", "_atomic_write_json",
            "_read_processed_file", "_validate_processed", "_read_processed_authority",
            "load_processed", "_write_processed", "_update_processed", "save_processed",
        },
        "app_state.py": {"update_processed", "recompute_issue_counters", "_publish_issue_counters"},
    }
    tmp = tempfile.TemporaryDirectory(prefix="ab-history-test-")
    ns.update(os=os, json=json, copy=copy, threading=threading, datetime=datetime,
              STATE_FILE=str(Path(tmp.name) / "processed_issues.json"),
              _processed_lock=threading.RLock(), _history_tmp=tmp,
              _RESOLVED_STATUSES=("fixed", "verified", "awaiting_prod_verification", "resolved"))
    ns.setdefault("state", {})
    ns.setdefault("logger", logging.getLogger("history-test"))
    for filename, wanted in names.items():
        src = (root / filename).read_text()
        nodes = [node for node in ast.parse(src).body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), ns)
    # Ensure cwd fallback can never see a developer's real history during these tests.
    Path(ns["STATE_FILE"]).write_text("{}")
    ns["_store"] = DiskHistory(ns)
    return ns


def patch_history(monkeypatch, module, initial):
    """Bind production callers to a real isolated store and its actual view publisher."""
    ns = bind_history()
    ns["save_processed"](initial)
    for name in ("load_processed", "update_processed"):
        monkeypatch.setattr(module, name, ns[name], raising=False)
    if hasattr(module, "ProcessedStoreError"):
        monkeypatch.setattr(module, "ProcessedStoreError", ns["ProcessedStoreError"])
    return ns
