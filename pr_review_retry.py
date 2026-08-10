"""pr_review_retry.py — standalone (no app/main import) helper for
pr_review._review_one's head-SHA review cache.

Split out from pr_review.py so it's unit-testable in isolation: pr_review.py
pulls in github_ops -> main -> log_scan, a circular-import chain that only
resolves inside the running app, not a standalone test script (the same
constraint check_unattended_mutation.py / attr_definition_lookup.py were
built standalone to avoid).
"""


def is_queued_for_retry_stale(prior_review, head_sha):
    """True when the LAST recorded review for this PR, at the SAME head_sha,
    was a "queue_for_retry" (every skeptical-panel reviewer was transiently
    offline/failed) — meaning it must NOT count as already_current.

    _review_one's head-SHA cache only checks whether a comment exists for the
    current head, never what verdict that comment recorded — so without this,
    a PR that hit queue_for_retry once stayed stuck there FOREVER: every later
    poll saw "a comment exists for this head" and skipped re-running the panel
    entirely, and no new commit was ever going to arrive on its own to bust
    the cache. This costs nothing extra to check: it only re-enables the NEXT
    regularly-scheduled poll (hourly by default, POLL_INTERVAL_SECONDS) to
    actually retry the panel, rather than forcing an immediate retry — so a
    persistently-down panel just gets retried once per poll cycle like any
    other PR, never hammered.

    A different head_sha means the PR moved since the queued review — that's
    already a fresh review via the normal (not-already_current) path, so this
    returns False rather than double-triggering."""
    prior = prior_review or {}
    return bool(prior.get("panel_status") == "queue_for_retry"
                and prior.get("head") == head_sha)
