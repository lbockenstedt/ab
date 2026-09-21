#!/usr/bin/env python3
"""Self-test for GET /api/task-details' no-task_id summary branch (the
"Active Tasks" dashboard card's live-refresh feed).

Run:  python3 test_active_tasks_live_summary.py

routes.py cannot be imported directly (it pulls in the full FastAPI app /
main.py's circular-import chain), so this extracts the SOURCE of the pure
route body plus its helpers via ast and execs them with a stubbed state dict.

Regression guard: the dashboard used to only refresh the "Active Tasks" card
via a 30s full-page reload, and the raw per-task dict (with a non-JSON-safe
python datetime start_time) was never actually consumed by the frontend. This
covers that the summary branch now returns, per task: an ISO-formatted
start_time string, a human duration string, and a "current_step" one-liner
derived from the tail of the task's streamed reasoning — all serializable and
small enough to poll every few seconds without the payload growing with the
full (often multi-KB) stream.
"""
import ast
from datetime import datetime, timedelta


def _load_ns():
    routes_src = open("routes.py").read()
    routes_tree = ast.parse(routes_src)

    segs = []
    want = {"get_task_details", "_duration_str", "_current_step"}
    for node in routes_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in want:
            segs.append(ast.get_source_segment(routes_src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {
        "datetime": datetime,
        "JSONResponse": lambda status_code, content: {"status_code": status_code, "content": content},
        "logger": _NoLog(),
    }
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


async def main():
    ok = True
    ns = _load_ns()
    get_task_details = ns["get_task_details"]

    now = datetime.now()
    ns["state"] = {
        "status": "running",
        "active_tasks": {
            "org/repo#42": {
                "name": "Fix drive_health thresholds",
                "kind": "fix",
                "start_time": now - timedelta(minutes=3, seconds=7),
                "stream": "Reading src/drive_health.py...\n\nApplying patch to thresholds\n",
            },
            "org/repo#PR-982": {
                "name": "PR review",
                "kind": "pr",
                "start_time": now - timedelta(seconds=5),
                "stream": "",
            },
        },
    }

    result = await get_task_details(task_id=None)

    ok &= _check("summary has active_tasks + count keys", "active_tasks" in result and result["count"] == 2)

    t1 = result["active_tasks"]["org/repo#42"]
    ok &= _check("task name passed through", t1["name"] == "Fix drive_health thresholds")
    ok &= _check("kind passed through", t1["kind"] == "fix")
    ok &= _check("start_time is an ISO string, not a datetime object",
                 isinstance(t1["start_time"], str) and "T" in t1["start_time"])
    ok &= _check("duration is a formatted string like '0h 3m 7s'",
                 t1["duration"].startswith("0h 3m") and t1["duration"].endswith("s"))
    ok &= _check("current_step is the last non-blank stream line",
                 t1["current_step"] == "Applying patch to thresholds")

    t2 = result["active_tasks"]["org/repo#PR-982"]
    ok &= _check("empty stream yields empty current_step (no crash)", t2["current_step"] == "")

    # Detail branch (task_id given) must still work unchanged.
    ns["state"]["active_tasks"]["org/repo#42"]["start_time"] = now - timedelta(seconds=90)
    detail = await get_task_details(task_id="org/repo#42")
    ok &= _check("detail branch still returns task/duration/stream keys",
                 set(["status", "task", "duration", "stream"]) <= set(detail.keys()))
    ok &= _check("detail branch duration format unchanged", detail["duration"].startswith("0h 1m"))

    missing = await get_task_details(task_id="does-not-exist")
    ok &= _check("unknown task_id returns 404", missing["status_code"] == 404)

    print("ALL PASS" if ok else "FAILURES DETECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(asyncio.run(main()))
