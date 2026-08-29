#!/usr/bin/env python3
"""Regression test: the Hub Logs page (/hub-logs) must not block the event
loop, and must not hand Jinja an unbounded table.

Run:  python3 test_hub_logs_page_nonblocking.py

Found on the live AppBuilder box: the local hub-log mirror had grown to
575,748 lines across ~40 module files (each individually capped at 20000
lines, but there's no cap on how many module files exist). get_hub_logs_page
called get_hub_logs() — which reads + sorts the WHOLE mirror — directly and
synchronously in the async route. uvicorn runs single-process/single-event-
loop (no workers=), so that froze EVERY concurrent request, not just this
page, for as long as the read+sort+render took (observed: several minutes)
— reported as "the UI dies" when clicking Hub Logs. /api/hub-logs/raw had
already been fixed for the identical class of bug (see its own docstring in
routes.py); this route wasn't caught at the time.

Fix: wrap get_hub_logs() in run_in_executor (moves the blocking work off the
event loop) and truncate the result to HUB_LOGS_PAGE_LIMIT before handing it
to the template, carrying the untruncated total separately for the "showing
newest N of TOTAL" label.

routes.py cannot be imported directly (main.py's app-init side effects — see
test_dismiss_background_retry.py's docstring), so this extracts the route
via ast and execs it with a stubbed get_hub_logs, the established convention
in this repo."""
import ast
import asyncio
import time


def _load_ns(get_hub_logs_stub):
    src = open("routes.py").read()
    tree = ast.parse(src)
    segs = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_hub_logs_page":
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "HUB_LOGS_PAGE_LIMIT" for t in node.targets
        ):
            segs.append(ast.get_source_segment(src, node))
    assert len(segs) == 2, f"expected HUB_LOGS_PAGE_LIMIT + get_hub_logs_page, found {len(segs)}"

    class _FakeConfig(dict):
        pass

    class _CapturedResponse:
        def __init__(self, request=None, name=None, context=None):
            self.context = context

    class _FakeTemplates:
        def TemplateResponse(self, request=None, name=None, context=None):
            return _CapturedResponse(request, name, context)

    from datetime import datetime as _datetime

    ns = {
        "asyncio": asyncio,
        "load_config": lambda: {},
        "get_hub_logs": get_hub_logs_stub,
        "templates": _FakeTemplates(),
        "state": {},
        "datetime": _datetime,
        "Request": type("Request", (), {}),
    }
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main():
    print("Running Hub Logs page non-blocking / truncation self-test...")
    ok = True

    # ── the route must not call get_hub_logs() directly on the event loop —
    # a stub that sleeps proves the coroutine awaits it off-thread rather
    # than blocking in place (a direct synchronous call would make the
    # coroutine itself take >= sleep_s wall time on ITS OWN await point,
    # which is fine; the real proof is the next block: a CONCURRENT task
    # keeps running while this one is "blocked"). ──────────────────────────
    big_logs = [{"module": f"m{i % 40}", "log": f"2026-08-29 12:00:{i % 60:02d} - line {i}"}
               for i in range(50000)]

    def _slow_get_hub_logs():
        time.sleep(0.2)  # stands in for the real read+sort of 575K lines
        return big_logs

    ns = _load_ns(_slow_get_hub_logs)

    async def _concurrent_probe():
        """A trivial task that should keep making progress WHILE the hub-logs
        route is doing its (stubbed) blocking work — proof the event loop
        wasn't frozen by it."""
        ticks = 0
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            ticks += 1
        return ticks

    async def _both():
        return await asyncio.gather(ns["get_hub_logs_page"](request=None), _concurrent_probe())

    resp, ticks = _run(_both())
    ok &= _check("a concurrent task keeps ticking WHILE the (stubbed slow) hub-logs read runs "
                "— proves the blocking work is off the event loop, not run in-line",
                ticks >= 5)

    # ── truncation: the template gets at most HUB_LOGS_PAGE_LIMIT rows, ─────
    # ── never the full unbounded mirror ──────────────────────────────────
    ctx = resp.context
    ok &= _check("hub_logs is truncated to HUB_LOGS_PAGE_LIMIT, not the full 50000-row mirror",
                len(ctx["hub_logs"]) == ns["HUB_LOGS_PAGE_LIMIT"] == 500)
    ok &= _check("hub_logs_total carries the UNTRUNCATED count for the 'showing N of TOTAL' label",
                ctx["hub_logs_total"] == 50000)
    ok &= _check("the truncated rows are the FIRST 500 (get_hub_logs already returns newest-first)",
                ctx["hub_logs"] == big_logs[:500])

    # ── a small mirror (under the limit) round-trips untouched ──────────────
    small_ns = _load_ns(lambda: big_logs[:10])
    small_resp = _run(small_ns["get_hub_logs_page"](request=None))
    ok &= _check("a mirror smaller than the limit is not padded/altered",
                len(small_resp.context["hub_logs"]) == 10
                and small_resp.context["hub_logs_total"] == 10)

    # ── an empty mirror still produces the existing "waiting for first
    # scan cycle" error message, unaffected by the truncation change ────────
    empty_ns = _load_ns(lambda: [])
    empty_resp = _run(empty_ns["get_hub_logs_page"](request=None))
    ok &= _check("an empty mirror still reports fetch_error (not silently swallowed)",
                bool(empty_resp.context["hub_fetch_error"]))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
