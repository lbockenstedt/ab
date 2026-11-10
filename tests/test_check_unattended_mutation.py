#!/usr/bin/env python3
"""Self-test for ab/check_unattended_mutation.py.

Run:  python3 ab/test_check_unattended_mutation.py

Standalone: imports only check_unattended_mutation (stdlib-only, no app init,
no GitHub/network). Uses a minimal fake PyGithub File (.filename, .patch).

Cases mirror the motivating incident (lm#151): an automatic 15-min sweep
loop that deletes client registry records, reviewed by AppBuilder's own panel
at 55%/Reject over a safety-rail gap only closed by adding a real test.
"""
import sys

from check_unattended_mutation import check_unattended_mutation


class _F:
    def __init__(self, filename, patch):
        self.filename = filename
        self.patch = patch


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


_LOOP_DELETE_PATCH = """@@ -1,0 +1,6 @@
+async def _clients_scrub_loop():
+    while True:
+        stale = find_stale()
+        for h in stale:
+            await delete_client(h)
+        await asyncio.sleep(900)
"""

_REQUEST_SCOPED_DELETE_PATCH = """@@ -1,0 +1,3 @@
+async def cs_delete_client(hostname):
+    removed = await registry.delete_one(hostname)
+    return {"removed": removed}
"""

_LOOP_NO_DELETE_PATCH = """@@ -1,0 +1,4 @@
+async def _health_loop():
+    while True:
+        await ping()
+        await asyncio.sleep(30)
"""


def main():
    print("Running ab check_unattended_mutation self-test...")
    ok = True

    # (1) loop + delete, no test file anywhere in the PR -> flagged.
    files = [_F("core/src/simulations/routes.py", _LOOP_DELETE_PATCH)]
    findings = check_unattended_mutation(files)
    ok &= _check("loop+delete with no test file is flagged", len(findings) == 1)
    ok &= _check("finding level is advisory (never blocking)",
                bool(findings) and findings[0]["level"] == "advisory")

    # (2) loop + delete, but SOME changed file in the PR looks like a test
    # (this exact scenario — lm#151's follow-up commit added
    # core/tests/test_clients_scrub.py alongside the loop) -> NOT flagged.
    files = [
        _F("core/src/simulations/routes.py", _LOOP_DELETE_PATCH),
        _F("core/tests/test_clients_scrub.py", "+def test_x(): pass\n"),
    ]
    ok &= _check("loop+delete WITH a test file in the same PR is not flagged",
                check_unattended_mutation(files) == [])

    # (3) a plain human-clicked, request-scoped delete (no loop) -> not flagged.
    # This is the Remove-button shape; it runs once, on demand, with a human
    # watching the result — a different risk class this check isn't for.
    files = [_F("core/src/simulations/routes.py", _REQUEST_SCOPED_DELETE_PATCH)]
    ok &= _check("request-scoped delete with no loop is not flagged",
                check_unattended_mutation(files) == [])

    # (4) a background loop with no mutation at all (e.g. a health-check
    # heartbeat) -> not flagged.
    files = [_F("core/src/main.py", _LOOP_NO_DELETE_PATCH)]
    ok &= _check("loop with no mutation is not flagged",
                check_unattended_mutation(files) == [])

    # (5) no patch (binary/oversized file) -> skipped, never raises.
    files = [_F("some/binary.bin", None)]
    ok &= _check("file with no patch is skipped without raising",
                check_unattended_mutation(files) == [])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
