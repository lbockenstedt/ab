"""One JSON line per PR fix attempt, in CONFIG_DIR/fix_outcomes.jsonl.

Each record (one per attempt counted against fix_one_pr's limit, free models
included) says which model wrote a fix attempt, whether it passed, why it
failed, what it cost, and whether it was a retry fed by the reviewer panel's
critique. The shape matches the fleet model ledger
(lm/.claude/skills/model-ledger/localstat.py outcomes.jsonl: ts, agent, model,
task, result, reasons, redo, minutes, usd, repo, note, source) plus ``pr``,
``attempt`` and ``from_feedback``, so the two can be compared.

The FIX_OUTCOMES_ENABLED setting (default on) turns recording off; summary() then
returns nothing. A record whose result is not pass, fail or unscored is skipped
entirely, and only reasons in REASONS are counted.

``unscored`` is the result for a generated fix whose review or verification
could not run. Its cost counts, but it is neither a pass nor a fail, so it adds
to the model's cost and ``unscored`` count only, not to attempts, passes,
feedback counts or reasons.

summary() rolls the file up per model for the Settings page and GET /fix_outcomes.
The file is bounded: once it passes _MAX_LINES, only the newest _KEEP_LINES are kept.
"""

import json
import math
import os
import threading
import time

import config_store
from main import logger

_LOCK = threading.Lock()
RESULTS = ("pass", "fail", "unscored")
REASONS = ("corrupt_code", "wrong_answer", "incomplete", "off_spec", "tool_misuse", "crash", "other")
# summary() reads the whole file, so keep it bounded. Counting lines on every
# append would read the file each time; the size check makes that rare.
_CHECK_BYTES = 4_000_000
_MAX_LINES = 20_000
_KEEP_LINES = 10_000


def _file():
    """Where the fix attempts are kept (CONFIG_DIR is read at call time so tests can move it)."""
    return os.path.join(config_store.CONFIG_DIR, "fix_outcomes.jsonl")


def enabled():
    """True unless the FIX_OUTCOMES_ENABLED setting is off (default on)."""
    return bool(config_store.load_config().get("FIX_OUTCOMES_ENABLED", True))


def record(repo, number, attempt, model, result, reasons=(), usd=None, from_feedback=False, note=""):
    """Append one fix attempt's outcome. Never raises: bookkeeping must not break a fix."""
    if not enabled():
        return
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": "ab-fixer", "model": model or "unknown",
           "task": "fix", "result": result, "reasons": list(reasons), "redo": None, "minutes": None,
           "usd": None if usd is None else round(usd, 4), "repo": (repo or "").rsplit("/", 1)[-1],
           "pr": f"{repo}#{number}", "attempt": attempt, "from_feedback": bool(from_feedback),
           "note": str(note or "")[:300], "source": "ab"}
    path = _file()
    try:
        with _LOCK:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            if os.path.getsize(path) > _CHECK_BYTES:
                _trim(path)
    except OSError as e:
        logger.warning("fix outcomes: could not record attempt on %s: %s", path, e)


def _trim(path):
    """Keep only the newest _KEEP_LINES lines once the file has more than _MAX_LINES (caller holds _LOCK)."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    if len(lines) <= _MAX_LINES:
        return
    # Write beside the file and swap it in, so a crash mid-write leaves the old file intact.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines[-_KEEP_LINES:])
    os.replace(tmp, path)


def _read_records():
    """Every parsable record in the file; garbage lines are skipped, a missing file is empty."""
    try:
        with _LOCK, open(_file(), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _new_row(model):
    """An empty per-model roll-up row."""
    return {"model": model, "attempts": 0, "passes": 0, "feedback_attempts": 0, "feedback_passes": 0,
            "unscored": 0, "usd": 0.0, "reasons": {}}


def _usd(rec):
    """The record's paid cost as a float; a missing, non-numeric or non-finite value counts as 0."""
    try:
        value = float(rec.get("usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _add(row, rec):
    """Fold one record into its model's roll-up row; a malformed field is ignored, never raised."""
    result = rec.get("result")
    if result not in RESULTS:
        return
    if result == "unscored":
        # Cost only: the review or verification never ran, so it is neither a pass nor a fail.
        row["unscored"] += 1
        row["usd"] += _usd(rec)
        return
    row["attempts"] += 1
    passed = result == "pass"
    if passed:
        row["passes"] += 1
    if rec.get("from_feedback"):
        row["feedback_attempts"] += 1
        row["feedback_passes"] += int(passed)
    row["usd"] += _usd(rec)
    if not passed:
        reasons = rec.get("reasons") if isinstance(rec.get("reasons"), list) else []
        for reason in reasons:
            if reason in REASONS:
                row["reasons"][reason] = row["reasons"].get(reason, 0) + 1


def _finish(row):
    """Turn the counts into the rates and rounded totals the page shows."""
    attempts, passes = row["attempts"], row["passes"]
    fb_attempts, fb_passes = row["feedback_attempts"], row["feedback_passes"]
    # A model with only unscored attempts has no rate: it was never scored.
    row["pass_rate"] = round(passes / attempts, 2) if attempts else None
    row["feedback_pass_rate"] = round(fb_passes / fb_attempts, 2) if fb_attempts else None
    row["usd"] = round(row["usd"], 4)
    row["usd_per_pass"] = round(row["usd"] / passes, 4) if passes else None
    return row


def summary(days=30):
    """Per-model fix results for the last ``days`` days, sorted by model."""
    # Records are kept as ISO timestamps, so comparing the date prefix is enough.
    if not enabled():
        return []
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    rows = {}
    for rec in _read_records():
        if str(rec.get("ts") or "")[:10] < cutoff or rec.get("result") not in RESULTS:
            continue
        model = str(rec.get("model") or "unknown")
        _add(rows.setdefault(model, _new_row(model)), rec)
    return [_finish(rows[model]) for model in sorted(rows)]
