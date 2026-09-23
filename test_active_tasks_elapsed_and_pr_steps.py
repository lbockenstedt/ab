#!/usr/bin/env python3
"""Self-test for the Active Tasks card's elapsed timer and per-step reporting.

Run:  python3 test_active_tasks_elapsed_and_pr_steps.py

Regression guards (two bugs that made the card near-useless during PR review):

1. "0h 0m 0s" forever. update_task_state(action="start") unconditionally reset
   start_time, but the SAME task_id is re-started for every sub-step (Triaging →
   Fix Attempt → Reviewing → Verifying, each repo of a scan, each PR of a review
   pass). The clock therefore restarted every few seconds and never showed the
   real elapsed time. start_time must now survive a re-start, while phase_start
   tracks the current sub-step.

2. "What is it even doing?" Nothing reported which PR was under review or which
   stage it was in, because the card's one-liner was derived only from the tail
   of streamed LLM reasoning — blank for every static/GitHub-API stage.
   set_task_step() now records an explicit line, and the summary payload
   exposes it.

Neither app_state.py nor routes.py can be imported directly (both pull in
main.py's circular-import chain), so their functions' SOURCE is extracted via
ast and exec'd against stubs, exactly as test_active_tasks_live_summary.py does.
"""
import ast
import re
import time
from datetime import datetime, timedelta


class _NoLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _extract(path, names, ns):
    src = open(path).read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            segs.append(ast.get_source_segment(src, node))
    assert len(segs) == len(names), f"{path}: expected {names}, found {len(segs)}"
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def _app_state_ns():
    import threading
    ns = {
        "datetime": datetime,
        "logger": _NoLog(),
        "_task_state_lock": threading.Lock(),
        "state": {"active_tasks": {}, "status": "running"},
    }
    return _extract("app_state.py", ["update_task_state", "set_task_step"], ns)


def _routes_ns(state):
    ns = {
        "datetime": datetime,
        "logger": _NoLog(),
        "JSONResponse": lambda status_code, content: {"status_code": status_code, "content": content},
        "state": state,
    }
    return _extract("routes.py", ["get_task_details", "_duration_str", "_current_step"], ns)


async def main():
    ok = True

    # ---- app_state: elapsed time spans the whole job -------------------------
    ns = _app_state_ns()
    update_task_state = ns["update_task_state"]
    set_task_step = ns["set_task_step"]
    state = ns["state"]

    update_task_state("PRReview", "PR pre-review — scanning 3 repos", "start", kind="pr")
    # Backdate as if this job had been running for 12 minutes already.
    real_start = datetime.now() - timedelta(minutes=12)
    state["active_tasks"]["PRReview"]["start_time"] = real_start

    time.sleep(0.01)
    update_task_state("PRReview", "Reviewing org/repo PR #7 (1/3)", "start", kind="pr")
    task = state["active_tasks"]["PRReview"]

    ok &= _check("re-starting the same task_id preserves start_time",
                 task["start_time"] == real_start)
    ok &= _check("phase_start advances to the new phase",
                 task["phase_start"] > real_start)
    ok &= _check("phase name is updated", task["name"] == "Reviewing org/repo PR #7 (1/3)")
    ok &= _check("kind is preserved as 'pr'", task["kind"] == "pr")
    ok &= _check("a new phase starts with a blank step", task["step"] == "")

    # ---- set_task_step ------------------------------------------------------
    set_task_step("PRReview", "org/repo #7 — scanning changed files for secrets")
    ok &= _check("set_task_step records the step",
                 task["step"] == "org/repo #7 — scanning changed files for secrets")

    set_task_step("PRReview", "x" * 500)
    ok &= _check("step is truncated to 200 chars", len(task["step"]) == 200)

    set_task_step("no-such-task", "ignored")
    set_task_step(None, "ignored")
    ok &= _check("set_task_step no-ops for unknown/empty task_id (no raise)",
                 "no-such-task" not in state["active_tasks"])

    set_task_step("PRReview", "org/repo #7 — rendering review comment")
    update_task_state("PRReview", "Reviewing org/repo PR #7 (1/3)", "start", kind="pr")
    ok &= _check("re-starting the SAME phase keeps its step",
                 state["active_tasks"]["PRReview"]["step"] == "org/repo #7 — rendering review comment")

    # ---- routes: the summary payload the dashboard polls ---------------------
    rns = _routes_ns(state)
    result = await rns["get_task_details"](task_id=None)
    t = result["active_tasks"]["PRReview"]

    ok &= _check("summary exposes elapsed_seconds spanning the whole job (~12m)",
                 isinstance(t["elapsed_seconds"], int) and 700 <= t["elapsed_seconds"] <= 740)
    ok &= _check("summary duration string reflects the whole job, not the phase",
                 t["duration"].startswith("0h 12m"))
    ok &= _check("summary exposes a separate phase_duration",
                 bool(re.match(r"^\d+h \d+m \d+s$", t["phase_duration"])) and t["phase_duration"].startswith("0h 0m"))
    ok &= _check("summary exposes the explicit step",
                 t["step"] == "org/repo #7 — rendering review comment")
    ok &= _check("legacy current_step key retained for existing dashboard JS",
                 t["current_step"] == t["step"])
    ok &= _check("name identifies WHICH PR is under review",
                 "PR #7" in t["name"] and "org/repo" in t["name"])
    ok &= _check("start_time is an ISO string the browser can tick from",
                 isinstance(t["start_time"], str) and "T" in t["start_time"])

    # The explicit step must win over the streamed-reasoning fallback.
    state["active_tasks"]["PRReview"]["stream"] = "some trailing llm reasoning line"
    result = await rns["get_task_details"](task_id=None)
    ok &= _check("explicit step takes priority over the stream tail",
                 result["active_tasks"]["PRReview"]["step"] == "org/repo #7 — rendering review comment")

    state["active_tasks"]["PRReview"]["step"] = ""
    result = await rns["get_task_details"](task_id=None)
    ok &= _check("stream tail is still the fallback when no step is set",
                 result["active_tasks"]["PRReview"]["step"] == "some trailing llm reasoning line")

    # Detail branch gets the same treatment.
    detail = await rns["get_task_details"](task_id="PRReview")
    ok &= _check("detail branch exposes elapsed_seconds + phase + step",
                 {"elapsed_seconds", "phase", "step"} <= set(detail.keys()))
    ok &= _check("detail branch elapsed_seconds spans the whole job",
                 700 <= detail["elapsed_seconds"] <= 740)

    # ---- ending a task still clears it --------------------------------------
    update_task_state("PRReview", "done", "end")
    ok &= _check("action='end' removes the task", "PRReview" not in state["active_tasks"])

    print("ALL PASS" if ok else "FAILURES DETECTED")
    return 0 if ok else 1


def test_active_tasks_elapsed_and_pr_steps():
    """pytest entry point (CI runs `pytest -q .`)."""
    import asyncio
    assert asyncio.run(main()) == 0


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(asyncio.run(main()))
