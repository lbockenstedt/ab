#!/usr/bin/env python3
"""Self-test for pr_review_retry.is_queued_for_retry_stale.

Run:  python3 ab/test_queue_for_retry_stale.py

Standalone: imports only pr_review_retry (no app/main init — pr_review.py
itself can't be imported outside the running app due to a circular import
via github_ops -> main -> log_scan).

Motivating bug: _review_one's already_current cache only checked whether a
GitHub comment existed for the PR's current head_sha — never what verdict
that comment recorded. A PR whose skeptical panel returned "queue_for_retry"
(reviewers transiently offline) got that comment posted once, and every
later poll then saw "comment exists for this head" and skipped re-running
the panel forever — with no new commit, nothing would ever bust the cache.
is_queued_for_retry_stale is the fix: it flags exactly that case so
already_current is forced False and the next poll actually retries.
"""
import sys

from pr_review_retry import is_queued_for_retry_stale


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


def main():
    print("Running ab is_queued_for_retry_stale self-test...")
    ok = True

    # (1) The exact bug: prior review at the SAME head was queue_for_retry —
    # must be treated as stale so a retry is forced.
    ok &= _check(
        "queue_for_retry at the same head is stale (forces a retry)",
        is_queued_for_retry_stale(
            {"panel_status": "queue_for_retry", "head": "abc123"}, "abc123") is True)

    # (2) A prior review that actually completed (real verdict, no
    # panel_status) is NOT stale — the normal cache behavior is preserved.
    ok &= _check(
        "a completed review (no panel_status) is not stale",
        is_queued_for_retry_stale(
            {"panel_verdict": "Approve", "panel_status": "", "head": "abc123"}, "abc123") is False)

    # (3) queue_for_retry recorded for a DIFFERENT (older) head — the PR has
    # since moved, so the normal not-already_current path already handles a
    # fresh review; this must not double-trigger.
    ok &= _check(
        "queue_for_retry at a DIFFERENT (stale) head is not flagged",
        is_queued_for_retry_stale(
            {"panel_status": "queue_for_retry", "head": "old-sha"}, "new-sha") is False)

    # (4) No prior record at all (first-ever scan of this PR) — not stale,
    # nothing to retry.
    ok &= _check("no prior record at all is not stale",
                is_queued_for_retry_stale(None, "abc123") is False)
    ok &= _check("empty prior record dict is not stale",
                is_queued_for_retry_stale({}, "abc123") is False)

    # (5) A different panel_status value (not queue_for_retry) — e.g. a
    # future status this function doesn't know about — is not flagged; only
    # the exact "queue_for_retry" string retries.
    ok &= _check(
        "an unrelated panel_status value is not flagged",
        is_queued_for_retry_stale(
            {"panel_status": "some_other_status", "head": "abc123"}, "abc123") is False)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
