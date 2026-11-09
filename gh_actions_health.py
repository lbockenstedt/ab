"""GitHub Actions outage detection and backoff.

When GitHub Actions is down, every workflow run ends as a failure with ZERO jobs
(GitHub's "workflow file issue" / startup failure). No check run is ever posted,
so required checks stay "expected" forever. Pushing fixes or merging during that
window only produces PRs that can never go green, and the merge refusal used to
be recorded as a permanent "needs a human" block.

This module tells an outage apart from a real failure and holds AppBuilder's
commit work behind a growing backoff instead:

* ``outage(config)`` -- True while Actions looks down. Two signals, either one is
  enough: githubstatus.com reports the Actions component as degraded, or our own
  runs recently started with zero jobs (``note_infra_failure``).
* Backoff starts at ``gh_backoff_start_min`` and doubles on every check that still
  sees the outage, up to ``gh_backoff_max_min``. It resets on recovery.
* ``infra_failed_runs`` / ``rerun_infra_failed`` find and re-run the runs that
  never started, so a held PR's checks post once Actions is back.

State is in memory only: a restart simply re-checks.
"""
import logging
import threading
import time

import requests

logger = logging.getLogger("ab.gh_actions_health")

# Settings (overridable in config.json / the Settings page).
DEFAULTS = {
    "gh_outage_backoff_enabled": True,
    "gh_backoff_start_min": 5,
    "gh_backoff_max_min": 60,
}

_STATUS_URL = "https://www.githubstatus.com/api/v2/components.json"
_STATUS_TTL_S = 120          # how long a status-page answer is reused
_SYMPTOM_WINDOW_S = 900      # zero-job failures newer than this count as a signal
_SYMPTOM_MIN = 2             # ...and it takes this many to call it an outage

_lock = threading.Lock()
_state = {
    "status_at": 0.0,        # when the status page was last read
    "status_down": False,    # what it said
    "status_text": "",
    "symptoms": [],          # timestamps of zero-job failed runs we saw
    "backoff_until": 0.0,    # no commit work before this time
    "backoff_s": 0.0,        # current backoff step (0 = not backing off)
    "reason": "",
}


def _cfg(config, key):
    """Read one setting, falling back to the default."""
    # Missing or None keys use the built-in default.
    val = (config or {}).get(key)
    return DEFAULTS[key] if val is None else val


def _status_page_down():
    """Return (down, text) from githubstatus.com, cached for a short time.

    An unreachable status page is "no evidence", never "down"."""
    # Reuse a recent answer.
    now = time.time()
    with _lock:
        if now - _state["status_at"] < _STATUS_TTL_S:
            return _state["status_down"], _state["status_text"]
    # Ask the status page for the Actions component.
    down, text = False, ""
    try:
        resp = requests.get(_STATUS_URL, timeout=5)
        for comp in (resp.json() or {}).get("components") or []:
            if comp.get("name") == "Actions":
                status = str(comp.get("status") or "")
                down = status in ("partial_outage", "major_outage")
                text = status
                break
    except Exception as exc:  # noqa: BLE001 -- no evidence either way
        logger.debug("gh_actions_health: status page unreachable: %s", exc)
    # Remember the answer.
    with _lock:
        _state.update(status_at=now, status_down=down, status_text=text)
    return down, text


def note_infra_failure(count=1):
    """Record that we just saw ``count`` workflow run(s) fail without any job."""
    # Keep only recent sightings.
    now = time.time()
    with _lock:
        recent = [t for t in _state["symptoms"] if now - t < _SYMPTOM_WINDOW_S]
        recent.extend([now] * max(0, int(count)))
        _state["symptoms"] = recent


def _symptom_down():
    """True when enough zero-job failures were seen recently."""
    # Count sightings inside the window.
    now = time.time()
    with _lock:
        recent = [t for t in _state["symptoms"] if now - t < _SYMPTOM_WINDOW_S]
        _state["symptoms"] = recent
        return len(recent) >= _SYMPTOM_MIN


def outage(config=None):
    """Return (in_backoff, reason).

    True means: do not push commits, open PRs or merge right now. While the
    outage lasts each expired backoff is replaced by a longer one; once both
    signals clear, the backoff resets and work resumes."""
    # Feature switched off: never hold work.
    if not _cfg(config, "gh_outage_backoff_enabled"):
        return False, ""
    now = time.time()
    # Still inside the current backoff window.
    with _lock:
        if now < _state["backoff_until"]:
            left = int((_state["backoff_until"] - now) / 60) + 1
            return True, "%s -- retrying in ~%d min" % (_state["reason"], left)
    # Window over (or none): look again.
    page_down, page_text = _status_page_down()
    sym_down = _symptom_down()
    if not (page_down or sym_down):
        # Recovered: reset and say so once.
        with _lock:
            was = _state["backoff_s"]
            _state.update(backoff_s=0.0, backoff_until=0.0, reason="")
        if was:
            logger.info("gh_actions_health: GitHub Actions recovered -- resuming commit work")
        return False, ""
    # Still down: grow the backoff and hold.
    start = float(_cfg(config, "gh_backoff_start_min")) * 60
    cap = float(_cfg(config, "gh_backoff_max_min")) * 60
    why = ("GitHub Actions %s (githubstatus.com)" % page_text if page_down
           else "GitHub Actions runs are failing to start (zero jobs)")
    with _lock:
        step = min(cap, _state["backoff_s"] * 2) if _state["backoff_s"] else start
        _state.update(backoff_s=step, backoff_until=now + step, reason=why)
    logger.warning("gh_actions_health: %s -- holding commits/merges for %d min",
                   why, int(step / 60))
    return True, "%s -- retrying in ~%d min" % (why, int(step / 60))


def snapshot():
    """Current backoff state for the dashboard / diagnostics."""
    # Copy the public fields.
    with _lock:
        left = max(0.0, _state["backoff_until"] - time.time())
        return {"backing_off": left > 0, "seconds_left": int(left),
                "reason": _state["reason"], "status_page": _state["status_text"]}


def infra_failed_runs(repo_name, head_sha, token, timeout=10.0):
    """Return the ids of workflow runs on ``head_sha`` that failed without any job.

    That is the outage signature: a real test failure always has at least one job."""
    # Without a token or commit there is nothing to look up.
    if not (token and repo_name and head_sha):
        return []
    headers = {"Authorization": "token " + token, "Accept": "application/vnd.github+json"}
    base = "https://api.github.com/repos/%s/actions/runs" % repo_name
    ids = []
    try:
        # Completed runs for this commit that did not succeed.
        resp = requests.get(base, params={"head_sha": head_sha, "per_page": 50},
                            headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return []
        for run in (resp.json() or {}).get("workflow_runs") or []:
            if run.get("status") != "completed":
                continue
            if run.get("conclusion") not in ("failure", "startup_failure"):
                continue
            # A run with jobs failed for real; only zero-job runs are infra failures.
            jobs = requests.get("%s/%s/jobs" % (base, run.get("id")),
                                headers=headers, timeout=timeout)
            if jobs.status_code == 200 and int((jobs.json() or {}).get("total_count") or 0) == 0:
                ids.append(run.get("id"))
    except Exception as exc:  # noqa: BLE001 -- lookup problems are "no evidence"
        logger.debug("gh_actions_health: run lookup failed for %s@%s: %s",
                     repo_name, (head_sha or "")[:8], exc)
    return ids


def rerun_infra_failed(repo_name, head_sha, token, timeout=10.0):
    """Re-run every zero-job failed run on ``head_sha``; return how many were restarted."""
    # Restart each run that never started.
    headers = {"Authorization": "token " + token, "Accept": "application/vnd.github+json"}
    done = 0
    for run_id in infra_failed_runs(repo_name, head_sha, token, timeout=timeout):
        try:
            resp = requests.post(
                "https://api.github.com/repos/%s/actions/runs/%s/rerun" % (repo_name, run_id),
                headers=headers, timeout=timeout)
            if resp.status_code in (201, 204):
                done += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("gh_actions_health: rerun of %s failed: %s", run_id, exc)
    if done:
        logger.info("gh_actions_health: re-ran %d run(s) that never started on %s@%s",
                    done, repo_name, head_sha[:8])
    return done
