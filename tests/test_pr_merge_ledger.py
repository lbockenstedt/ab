#!/usr/bin/env python3
"""The PRs Merged / Auto-Merged badges must never go DOWN.

They used to be tallied by iterating ``state["pr_reviews"]`` in the template.
That store is a ring buffer capped at ``_PR_REVIEWS_MAX`` and trimmed
oldest-first, so once it saturated, every newly reviewed PR evicted the oldest
record — and if that record happened to be a merged one, the displayed total
dropped by one. Observed live: pr_reviews.json sitting at exactly 100 entries
while the operator watched the numbers move both directions.

The ledger fixes this by tracking the SET of PR keys that have ever merged,
outside the capped store. Sets of keys rather than a running integer, matching
the issue counters' "derive, never increment" rule — re-recording a PR that is
already in the ledger is a no-op, so the tally is idempotent by construction.

app_state.py cannot be imported (it pulls in the whole web app), so the
functions are extracted by source via ast and exec'd — same technique as the
sibling harnesses.
"""
import ast

import pytest


def _load(path, *names):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    wanted = set(names)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(ast.get_source_segment(src, node), ns)
    missing = wanted - set(ns)
    if missing:
        raise AssertionError("not found in %s: %s" % (path, sorted(missing)))
    return ns


@pytest.fixture
def env():
    """_record_merge_milestone / pr_merge_totals wired to a fake state + a
    save that records calls instead of touching /etc/ab."""
    ns = _load("app_state.py", "_record_merge_milestone", "pr_merge_totals",
               "_seed_merge_ledger")
    saved = []
    state = {"pr_merge_ledger": {}}
    ns["state"] = state
    ns["save_pr_merge_ledger"] = lambda led: saved.append(
        {k: set(v) for k, v in led.items()})
    ns["logger"] = type("L", (), {"error": staticmethod(lambda *a, **k: None)})()
    ns["_saved"] = saved
    ns["_state"] = state
    return ns


def test_merge_is_recorded(env):
    env["_record_merge_milestone"]("o/r#1", {"merged": True})
    assert env["pr_merge_totals"]() == {"merged": 1, "auto_merged": 0}


def test_auto_merge_is_its_own_bucket(env):
    """An auto-merged PR carries BOTH flags; the template renders it as
    auto_merged only, so the buckets must stay disjoint."""
    env["_record_merge_milestone"]("o/r#1", {"merged": True, "auto_merged": True})
    assert env["pr_merge_totals"]() == {"merged": 0, "auto_merged": 1}


def test_recording_the_same_pr_twice_does_not_double_count(env):
    """PR records are rewritten on every poll — the tally must be idempotent."""
    for _ in range(10):
        env["_record_merge_milestone"]("o/r#1", {"merged": True})
    assert env["pr_merge_totals"]() == {"merged": 1, "auto_merged": 0}


def test_non_merge_updates_do_not_touch_the_ledger(env):
    env["_record_merge_milestone"]("o/r#1", {"approved": True})
    env["_record_merge_milestone"]("o/r#2", {"denied": True, "merged": False})
    assert env["pr_merge_totals"]() == {"merged": 0, "auto_merged": 0}
    assert env["_saved"] == []


def test_ledger_only_persists_when_it_actually_changed(env):
    env["_record_merge_milestone"]("o/r#1", {"merged": True})
    assert len(env["_saved"]) == 1
    env["_record_merge_milestone"]("o/r#1", {"merged": True})
    assert len(env["_saved"]) == 1, "re-recording must not rewrite the file"


def test_total_survives_ring_buffer_eviction(env):
    """THE REGRESSION. Merge 150 PRs while the capped pr_reviews store only
    ever retains the most recent 100 — the lifetime total must still be 150,
    and must never decrease along the way."""
    pr_reviews = {}
    cap = 100
    last = 0
    for i in range(150):
        key = "o/r#%d" % i
        pr_reviews[key] = {"merged": True}
        # Same trimming rule as app_state.record_pr_review.
        extra = len(pr_reviews) - cap
        if extra > 0:
            for k in list(pr_reviews.keys())[:extra]:
                pr_reviews.pop(k, None)
        env["_record_merge_milestone"](key, {"merged": True})

        total = env["pr_merge_totals"]()["merged"]
        assert total >= last, "count went DOWN at PR %d (%d -> %d)" % (i, last, total)
        last = total

    assert len(pr_reviews) == cap, "precondition: the store really did evict"
    assert env["pr_merge_totals"]() == {"merged": 150, "auto_merged": 0}


def test_the_old_tally_is_what_regressed(env):
    """Pin the actual bug: tallying the capped store under-counts once it has
    evicted, which is the number the operator saw moving downward."""
    pr_reviews = {}
    cap = 100
    for i in range(150):
        pr_reviews["o/r#%d" % i] = {"merged": True}
        extra = len(pr_reviews) - cap
        if extra > 0:
            for k in list(pr_reviews.keys())[:extra]:
                pr_reviews.pop(k, None)
    old_tally = sum(1 for r in pr_reviews.values() if r.get("merged"))
    assert old_tally == 100 < 150


def test_seed_backfills_from_surviving_records(env):
    """Upgrading must not reset the badges to zero."""
    pr_reviews = {
        "o/r#1": {"merged": True},
        "o/r#2": {"merged": True, "auto_merged": True},
        "o/r#3": {"approved": True},
        "o/r#4": "not-a-dict",
    }
    led = env["_seed_merge_ledger"]({}, pr_reviews)
    assert led["merged"] == {"o/r#1", "o/r#2"}
    assert led["auto_merged"] == {"o/r#2"}


def test_seed_is_a_union_not_a_replace(env):
    """Re-seeding on every boot must not drop keys already in the ledger whose
    records have since aged out of the ring buffer."""
    existing = {"merged": {"o/r#old"}}
    led = env["_seed_merge_ledger"](existing, {"o/r#new": {"merged": True}})
    assert led["merged"] == {"o/r#old", "o/r#new"}


def test_seed_survives_a_junk_store(env):
    assert env["_seed_merge_ledger"]({}, None) == {}
    assert env["_seed_merge_ledger"]({}, {"k": None}) == {}
