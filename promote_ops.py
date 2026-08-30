"""Scheduled branch promotion for AppBuilder.

This module lets AppBuilder open promotion PRs on a fixed *cadence* instead of
only when someone clicks a promotion button in the WebUI. It changes nothing
about what a promotion IS: exactly like the buttons, a scheduled promotion only
DISPATCHES ``.github/workflows/promote.yml`` in each target repo, which prepares
a ``promote/<src>-to-<tgt>`` branch and OPENS a pull request. Nothing here
merges and nothing here pushes to dev/qa/main -- the promotion PR is still
reviewed (AppBuilder's own pre-review panels run on it) and merged by a human.
The schedule only decides *when* to open those PRs, so dev->qa (and, if enabled,
qa->main) batches on a regular interval.

The dispatch itself is shared with the WebUI path (routes.py) via
``dispatch_promote_workflow`` so there is one implementation, not two that drift.

Config (config.json, editable from Settings -> Automation):
  promote_schedule_enabled            bool  master switch                 (default False)
  promote_schedule_dev_to_qa_hours    int   dev->qa cadence, 0 = off      (default 24)
  promote_schedule_qa_to_main_hours   int   qa->main cadence, 0 = off     (default 0)
  promote_schedule_repo               str   repo scope; the "__all__"
                                            sentinel = every promotable
                                            repo                          (default "__all__")
  promote_schedule_check_interval_min int   how often the worker wakes to
                                            re-check whether a route is due (default 30)

qa->main defaults OFF: pushing to production on a timer is a deliberate choice
the operator has to opt into, whereas batching dev->qa is low-risk (it only ever
opens a PR into qa).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("appbuilder.promote")

#: Sentinel meaning "every promotable repo". Mirrors routes.PROMOTE_ALL; kept as
#: a literal here so this module carries no import-time dependency on routes.py
#: (routes.py imports THIS module, so the reverse would be a cycle). Asserted
#: equal to routes.PROMOTE_ALL in the tests to catch drift.
PROMOTE_ALL = "__all__"

#: (source, target) -> config key holding that route's cadence in hours.
#: Only the two forward, non-override routes are schedulable. The dev->main
#: emergency override is intentionally NOT here: skipping qa is a human
#: decision, never something a timer should do on its own.
ROUTE_INTERVAL_KEYS = {
    ("dev", "qa"): "promote_schedule_dev_to_qa_hours",
    ("qa", "main"): "promote_schedule_qa_to_main_hours",
}

_DEFAULT_INTERVALS = {
    "promote_schedule_dev_to_qa_hours": 24,
    "promote_schedule_qa_to_main_hours": 0,
}


def _route_key(source, target):
    return f"{source}->{target}"


def _as_int(val, default):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Shared dispatch (used by BOTH the WebUI buttons and the scheduler).
# ---------------------------------------------------------------------------
def dispatch_promote_workflow(token, repo_name, source, target, is_override):
    """Dispatch promote.yml in ``repo_name`` for the ``source -> target`` hop.

    Returns the workflow URL on success and raises on failure. This ONLY opens a
    PR -- it never merges. Shared with routes.promote_branch so the manual and
    scheduled paths behave identically.
    """
    from github import Github
    repo = Github(token).get_repo(repo_name)
    wf = repo.get_workflow("promote.yml")
    # workflow_dispatch runs the workflow file as it exists on the ref it is
    # dispatched from; the promotion logic lives on the default branch, which is
    # also what promote.yml's concurrency note assumes.
    ref = repo.default_branch
    # Send `target` ONLY for the override. A sibling repo's promote.yml may still
    # declare just the `source` input, and GitHub rejects a dispatch carrying an
    # input the workflow does not declare -- so always sending `target` would
    # break the ordinary dev->qa / qa->main dispatch on those repos. The
    # scheduler never uses the override, so this stays omitted for it.
    inputs = {"source": source}
    if is_override:
        inputs["target"] = target
    ok = wf.create_dispatch(ref, inputs)
    # PyGithub returns False (rather than raising) when GitHub rejects the
    # dispatch. Treat that as a failure instead of a false success.
    if ok is False:
        extra = (" This repo's promote.yml may predate the 'target' input "
                 "that the dev -> main override requires.") if is_override else ""
        raise RuntimeError(
            f"GitHub rejected the workflow dispatch for {repo_name} (ref '{ref}').{extra}")
    return f"https://github.com/{repo_name}/actions/workflows/promote.yml"


# ---------------------------------------------------------------------------
# Schedule decision (pure -- unit-tested without any GitHub/IO).
# ---------------------------------------------------------------------------
def due_routes(cfg, last_runs, now):
    """Which forward routes are due to be promoted, given the last-run times.

    ``last_runs`` maps ``"src->tgt"`` -> aware ``datetime`` of the last dispatch
    (or missing/None if never). A route is due when its cadence is > 0 hours and
    either it has never run or at least that many hours have elapsed. Returns a
    list of ``(source, target)`` in a stable order (dev->qa before qa->main).
    """
    due = []
    for (src, tgt), key in ROUTE_INTERVAL_KEYS.items():
        hours = _as_int(cfg.get(key, _DEFAULT_INTERVALS[key]), _DEFAULT_INTERVALS[key])
        if hours <= 0:
            continue
        last = last_runs.get(_route_key(src, tgt))
        if last is None or (now - last) >= timedelta(hours=hours):
            due.append((src, tgt))
    return due


# ---------------------------------------------------------------------------
# Last-run state (small JSON file next to config.json).
# ---------------------------------------------------------------------------
def _state_path():
    from main import CONFIG_DIR
    return os.path.join(CONFIG_DIR, "promote_schedule_state.json")


def load_schedule_state():
    """route-key -> aware datetime of last dispatch. Empty on any read problem."""
    try:
        with open(_state_path(), "r") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    out = {}
    for k, v in (raw or {}).items():
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[k] = dt
        except (TypeError, ValueError):
            continue
    return out


def save_schedule_state(runs):
    """Persist {route-key: datetime} atomically as ISO-8601 UTC strings."""
    path = _state_path()
    payload = {k: (v.astimezone(timezone.utc).isoformat()) for k, v in runs.items()}
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# One scheduler cycle: dispatch every due route across the configured repos.
# ---------------------------------------------------------------------------
def _clean_repo(name):
    """Minimal 'owner/name' normaliser for the single-repo schedule scope.

    Deliberately does NOT import github_ops.clean_repo_name: that module imports
    `main` at load time, which would drag the whole app (and its worker threads)
    into anything that merely resolves the schedule's repo scope. The schedule
    value is a plain config string, so a light strip of a URL/`.git`/slash is
    enough here."""
    s = (name or "").strip()
    for pre in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    if s.endswith(".git"):
        s = s[:-4]
    return s.strip("/")


def _resolve_repos(cfg):
    """The repo list a scheduled promotion fans out to.

    Honours ``promote_schedule_repo``: the PROMOTE_ALL sentinel (default) means
    every promotable repo -- exactly the set the WebUI "All" button uses -- while
    a concrete "owner/name" restricts the schedule to that single repo.
    """
    scope = (cfg.get("promote_schedule_repo") or PROMOTE_ALL).strip()
    if scope and scope != PROMOTE_ALL:
        one = _clean_repo(scope)
        return [one] if one else []
    from routes import _promotable_repos
    repos, _ = _promotable_repos(cfg)
    return repos


def run_scheduled_promotions(cfg, now=None):
    """Dispatch promote.yml for every route whose cadence has elapsed.

    Returns a summary dict {dispatched, skipped, results}. A route is marked as
    run (its timer resets) as long as at least one repo dispatched, so a single
    flaky repo does not wedge the whole schedule; per-repo failures are logged
    and reported. Never raises for a per-repo failure -- only truly fatal setup
    problems (no token) short-circuit the cycle.
    """
    from routes import PROMOTE_ROUTES
    now = now or datetime.now(timezone.utc)

    token = cfg.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        logger.warning("promote scheduler: no GitHub token configured — skipping cycle")
        return {"dispatched": 0, "skipped": 0, "results": [], "reason": "no-token"}

    last_runs = load_schedule_state()
    due = due_routes(cfg, last_runs, now)
    if not due:
        return {"dispatched": 0, "skipped": 0, "results": []}

    repos = _resolve_repos(cfg)
    if not repos:
        logger.warning("promote scheduler: no promotable repositories resolved — nothing to do")
        return {"dispatched": 0, "skipped": 0, "results": [], "reason": "no-repos"}

    results = []
    dispatched_total = 0
    for src, tgt in due:
        is_override = PROMOTE_ROUTES.get((src, tgt), False)
        ok_count = 0
        for name in repos:
            try:
                url = dispatch_promote_workflow(token, name, src, tgt, is_override)
                results.append({"repo": name, "source": src, "target": tgt, "ok": True, "url": url})
                ok_count += 1
            except Exception as e:  # noqa: BLE001 — one repo must not abort the fan-out
                logger.error("promote scheduler: %s %s -> %s failed: %s", name, src, tgt, e)
                results.append({"repo": name, "source": src, "target": tgt, "ok": False, "error": str(e)})
        if ok_count:
            # Reset the timer for this route only when something actually went
            # out, so a fully-failed hop is retried next tick rather than
            # silently waiting a whole interval.
            last_runs[_route_key(src, tgt)] = now
            dispatched_total += ok_count
            logger.warning("promote scheduler: %s -> %s dispatched for %d/%d repo(s)",
                           src, tgt, ok_count, len(repos))

    try:
        save_schedule_state(last_runs)
    except OSError as e:
        logger.error("promote scheduler: could not persist schedule state: %s", e)

    return {"dispatched": dispatched_total,
            "skipped": len(due) - len({(r["source"], r["target"]) for r in results if r["ok"]}),
            "results": results}
