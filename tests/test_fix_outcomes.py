"""Per-model fix outcomes (fix_outcomes.py) and the per-thread spend tally in ai_spend.py.

The ledger is a JSON Lines file in CONFIG_DIR, so each test points CONFIG_DIR at
tmp_path and writes or reads real lines rather than mocking the file.
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main  # noqa: F401 - loads workers in import order

# isort: split
import ai_spend
import config_store
import fix_outcomes


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    """Point CONFIG_DIR at a fresh tmp_path so each test has its own fix_outcomes.jsonl."""
    monkeypatch.setattr(config_store, "CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_summary_rolls_up_attempts_passes_feedback_cost_and_reasons():
    """Two m1 attempts and one m2 attempt roll up per model, sorted by model, with rates and costs."""
    fix_outcomes.record("org/lm", 7, 1, "m1", "pass", usd=0.10)
    fix_outcomes.record("org/lm", 7, 2, "m1", "fail", ["wrong_answer"], usd=0.30, from_feedback=True)
    fix_outcomes.record("org/lm", 8, 1, "m2", "pass", usd=None)

    rows = fix_outcomes.summary(30)

    assert [r["model"] for r in rows] == ["m1", "m2"]
    m1, m2 = rows
    assert (m1["attempts"], m1["passes"], m1["pass_rate"]) == (2, 1, 0.5)
    assert (m1["feedback_attempts"], m1["feedback_passes"], m1["feedback_pass_rate"]) == (1, 0, 0.0)
    assert m1["usd"] == pytest.approx(0.4)
    assert m1["usd_per_pass"] == pytest.approx(0.4)
    assert m1["reasons"] == {"wrong_answer": 1}
    assert (m2["attempts"], m2["passes"], m2["pass_rate"]) == (1, 1, 1.0)
    assert m2["feedback_pass_rate"] is None
    assert m2["usd"] == 0.0 and m2["usd_per_pass"] == 0.0


def test_summary_skips_garbage_lines_and_records_older_than_the_window():
    """An unparsable line and a record from 2000 are ignored; only the recent record counts."""
    fix_outcomes.record("org/lm", 1, 1, "m1", "pass")
    with open(fix_outcomes._file(), "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps({"ts": "2000-01-01T00:00:00", "model": "old", "result": "pass"}) + "\n")

    rows = fix_outcomes.summary(30)

    assert [r["model"] for r in rows] == ["m1"]
    assert rows[0]["attempts"] == 1


def test_summary_of_a_missing_file_is_empty():
    """With no outcomes recorded yet the summary is an empty list, not an error."""
    assert fix_outcomes.summary(30) == []


def test_tally_counts_this_threads_paid_calls_only(monkeypatch):
    """start_tally() sums the paid calls made on this thread; another thread sees no tally."""
    monkeypatch.setattr(ai_spend, "metered", lambda provider, model, config: True)
    monkeypatch.setattr(ai_spend, "price_of", lambda model, prices: {"input": 1.0, "output": 1.0})
    monkeypatch.setattr(ai_spend, "policy", lambda: {"gate_usd": 100.0, "prices": {}})
    monkeypatch.setattr(ai_spend, "_ledger", None)
    config = {"AI_SPEND_GATE": False}
    other = {}

    def other_thread():
        """Record a call on a second thread and note its tally there."""
        other["tally"] = ai_spend.tally()

    ai_spend.start_tally()
    ai_spend.record("p", "m", {"input_tokens": 1_000_000, "output_tokens": 0}, config)
    ai_spend.record("p", "m", {"input_tokens": 1_000_000, "output_tokens": 0}, config)
    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join()

    assert ai_spend.tally() == pytest.approx(2.0)
    assert other["tally"] is None


def test_record_tolerates_missing_note(tmp_path, monkeypatch):
    """A panel reject with no critique (note None) is still recorded, never raised."""
    monkeypatch.setattr(config_store, "CONFIG_DIR", str(tmp_path))
    fix_outcomes.record("o/r", 1, 1, "m", "fail", ["wrong_answer"], None, True, None)
    assert fix_outcomes.summary()[0]["attempts"] == 1


def test_unscored_attempt_counts_cost_but_not_attempts_passes_or_reasons():
    """An unscored attempt (review or verification never ran) adds its cost and an
    unscored count; it is neither a pass nor a fail, so the pass rate ignores it."""
    fix_outcomes.record("org/lm", 1, 1, "m1", "pass", usd=0.10)
    fix_outcomes.record("org/lm", 1, 2, "m1", "unscored", ["other"], usd=0.25)
    fix_outcomes.record("org/lm", 2, 1, "m2", "unscored", ["other"], usd=0.50)

    m1, m2 = fix_outcomes.summary(30)

    assert (m1["attempts"], m1["passes"], m1["pass_rate"], m1["unscored"]) == (1, 1, 1.0, 1)
    assert m1["reasons"] == {}
    assert m1["usd"] == pytest.approx(0.35)
    assert m1["usd_per_pass"] == pytest.approx(0.35)
    assert (m2["attempts"], m2["pass_rate"], m2["unscored"]) == (0, None, 1)
    assert m2["usd"] == pytest.approx(0.5) and m2["usd_per_pass"] is None


def test_malformed_fields_never_raise_and_good_record_still_counts():
    """A non-numeric usd, a non-list reasons and a non-string result are ignored
    one by one; the well-formed record for the same model still counts."""
    today = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(fix_outcomes._file(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": today, "model": "m", "result": "fail", "usd": "abc", "reasons": "x"}) + "\n")
        fh.write(json.dumps({"ts": today, "model": "m", "result": 5}) + "\n")

    rows = fix_outcomes.summary(30)

    assert [r["model"] for r in rows] == ["m"]
    assert (rows[0]["attempts"], rows[0]["passes"], rows[0]["usd"], rows[0]["reasons"]) == (1, 0, 0.0, {})


def test_file_is_trimmed_to_the_newest_records_once_it_passes_the_line_cap():
    """Past _MAX_LINES the file is cut to the newest _KEEP_LINES, the new record included."""
    path = fix_outcomes._file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Each line is padded to about 250 bytes so the file passes _CHECK_BYTES and the trim runs.
    pad = "x" * 200
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps({"ts": "2000-01-01T00:00:00", "note": f"{i:06d}{pad}"}) + "\n"
                      for i in range(fix_outcomes._MAX_LINES + 1))

    fix_outcomes.record("org/lm", 9, 1, "m1", "pass")

    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    first_kept = fix_outcomes._MAX_LINES + 1 - fix_outcomes._KEEP_LINES + 1
    assert len(lines) == fix_outcomes._KEEP_LINES
    assert json.loads(lines[-1])["pr"] == "org/lm#9"
    assert json.loads(lines[0])["note"].startswith(f"{first_kept:06d}")


def test_summary_skips_unknown_result_and_unknown_reason():
    """A record with an unknown result is not an attempt; an unknown reason is not counted."""
    fix_outcomes.record("org/lm", 1, 1, "m1", "partial", usd=0.5)
    fix_outcomes.record("org/lm", 1, 2, "m1", "fail", ["wrong_answer", "bogus"], usd=0.1)

    (row,) = fix_outcomes.summary(30)

    assert row["attempts"] == 1
    assert row["usd"] == 0.1
    assert row["reasons"] == {"wrong_answer": 1}


def test_knob_off_records_nothing_and_summary_is_empty(config_dir, monkeypatch):
    """FIX_OUTCOMES_ENABLED false: record writes no file; an existing file is not summarised."""
    fix_outcomes.record("org/lm", 1, 1, "m1", "pass")
    assert fix_outcomes.summary(30)
    monkeypatch.setattr(config_store, "load_config", lambda: {"FIX_OUTCOMES_ENABLED": False})
    fix_outcomes.record("org/lm", 2, 1, "m2", "pass")

    assert fix_outcomes.summary(30) == []
    assert len((config_dir / "fix_outcomes.jsonl").read_text().splitlines()) == 1
