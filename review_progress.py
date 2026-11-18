"""One definition of review progress for AppBuilder's PR remediation
(app_state.record_pr_review, pr_remediate).

A reviewed head is scored [panels approved, lowest panel confidence]. It makes
progress when it approves more panels than the best so far, or as many with a
lowest confidence at least ``min_gain`` above the best's. Anything else is a
stall and the best stays: a +0.005 creep is noise, and letting it reset the
count kept point fixes going round after round. ``stalls >= stall_limit`` is
thrash; the loop then gets ``rework_limit`` consolidated reworks (one pass
over every round's findings) before a human takes over.

Pure Python with no AppBuilder imports, so every caller can import it without
a cycle."""

STALL_LIMIT = 2      # pr_remediate_stall_limit: heads in a row without progress = thrash (0 = off)
MIN_GAIN = 0.03      # pr_remediate_min_gain: confidence gain that counts as progress
REWORK_LIMIT = 1     # pr_remediate_rework_limit: consolidated reworks before hand-off (0 = none, at most 3)
HISTORY_KEEP = 6     # scored heads kept in a PR record's review_history
CRITIQUE_CAP = 3000  # characters kept of each panel critique in review_history


def _knob(cfg, key, default, cast, lo, hi=None):
    """``cfg[key]`` cast and clamped to lo..hi; ``default`` when missing or malformed."""
    try:
        value = max(lo, cast((cfg or {}).get(key, default)))
    except (TypeError, ValueError, OverflowError):
        return default
    return value if hi is None else min(hi, value)


def stall_limit(cfg=None):
    """Heads in a row without progress that count as thrash (0 turns the stop off)."""
    return _knob(cfg, "pr_remediate_stall_limit", STALL_LIMIT, int, 0)


def min_gain(cfg=None):
    """Lowest-confidence gain over the best that counts as progress, 0..0.5."""
    return _knob(cfg, "pr_remediate_min_gain", MIN_GAIN, float, 0.0, 0.5)


def rework_limit(cfg=None):
    """Consolidated reworks allowed before a thrashing change goes to a human,
    0..3. A rework is not charged as an attempt, so this cap is what bounds
    them (fleet rule 10.3), like the premium and barren budgets' 3."""
    return _knob(cfg, "pr_remediate_rework_limit", REWORK_LIMIT, int, 0, 3)


def made_progress(score, best, gain_needed=MIN_GAIN):
    """True when ``score`` beats ``best`` by the progress rule (module docstring).

    No best yet means the first score is progress. The gain is rounded because
    0.83 - 0.80 is 0.0299... in floats; ``gain > 0`` keeps a margin of 0 equal
    to the old strict "beat the best" rule."""
    if best is None:
        return True
    if score[0] != best[0]:
        return score[0] > best[0]
    gain = round(score[1] - best[1], 4)
    return gain > 0 and gain >= gain_needed


def upsert(rounds, key, entry, keep=None):
    """``rounds`` with ``entry`` replacing the round whose ``key`` matches, else
    appended, then the last ``keep`` (all when None). Re-recording a round
    (a re-scan, or panel 2 arriving later) updates it and never adds one."""
    rounds = list(rounds or [])
    hit = [i for i, r in enumerate(rounds) if r.get(key) == entry[key]]
    if hit:
        rounds[hit[0]] = entry
    else:
        rounds.append(entry)
    return rounds[-keep:] if keep else rounds


def rework_rounds(rec, head_sha):
    """The rounds a consolidated rework works from, oldest first: the PR
    record's review_history, with the head being fixed carrying its live
    critiques from ``rec`` in full instead of the copy capped at CRITIQUE_CAP.

    A head gets a history entry only once it is scored, and a reviewed head can
    stay unscored (panel 2 queued for retry, a verdict with no confidence), so
    that head is added last as "not scored". Without it the rework would drop
    the newest objections, which are the ones the fixer most needs."""
    # The head being fixed, as history keys it, and its live critiques.
    head = (head_sha or "")[:12]
    live = {k: (rec.get(k) or "").strip() for k in ("panel_critique", "panel2_critique")}
    # Its round, if it has one, takes the live critiques; an empty one keeps the stored copy.
    rounds = [dict(r, **{k: v for k, v in live.items() if v}) if r.get("head") == head else r
              for r in rec.get("review_history") or []]
    # An unscored head still has critiques to resolve: it is the last round.
    if any(live.values()) and not any(r.get("head") == head for r in rounds):
        rounds.append(dict(live, head=head, score="not scored"))
    return rounds
