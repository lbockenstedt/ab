"""
perf_auditor.py — Performance Hotpath & Async Concurrency Auditor.

Audits changes for common concurrency and event-loop performance pitfalls:
- Blocking time.sleep() inside async def functions (should be await asyncio.sleep()).
- Loops containing sequential await calls that could run concurrently with asyncio.gather.
- Missing cache TTL or repeated decryption/secret retrieval in polling loops.
"""
# Regular expressions detect loop headers and await calls in diff lines.
import re
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, List


def audit_performance_hotpaths(files: List[Any]) -> List[Dict[str, Any]]:
    """Audit files for performance hotpaths, blocking event loop calls, and loop inefficiencies."""
    # Every problem found across all files, returned to the caller at the end.
    findings: List[Dict[str, Any]] = []

    # Inspect each changed file in the PR (tolerating a None list).
    for f in (files or []):
        # Only Python files are audited; skip everything else.
        filename = getattr(f, "filename", "") or ""
        if not filename.endswith(".py"):
            continue

        # Files without a diff (binary, renamed only) have nothing to scan.
        raw_patch = getattr(f, "patch", None) or ""
        if not raw_patch:
            continue

        # Work line by line through the unified diff.
        lines = raw_patch.splitlines()

        # State tracking through diff lines
        async_indents: List[int] = []
        loop_indents: List[int] = []
        in_poll_context = False

        # Scan each diff line, keeping a rough idea of the enclosing async-def and loop blocks.
        for raw_line in lines:
            # A new hunk header jumps to a different part of the file, so reset all scope tracking.
            if raw_line.startswith("@@"):
                async_indents = []
                loop_indents = []
                in_poll_context = False
                continue

            # Strip diff prefix if present
            # Classify the line as added ("+") or deleted ("-"),
            # ignoring the "+++"/"---" file headers.
            is_added = raw_line.startswith("+") and not raw_line.startswith("+++")
            is_deleted = raw_line.startswith("-") and not raw_line.startswith("---")
            # Removed lines are no longer in the code, so they cannot introduce problems.
            if is_deleted:
                continue

            # Drop the one-character diff prefix to get the real source line.
            line = raw_line[1:] if (is_added or raw_line.startswith(" ")) else raw_line

            # Blank lines and comments do not affect scope or performance; skip them.
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            # Measure indentation, then close any loop/async blocks this line has dedented out of.
            current_indent = len(line) - len(line.lstrip())
            while loop_indents and current_indent <= loop_indents[-1]:
                loop_indents.pop()
            while async_indents and current_indent <= async_indents[-1]:
                async_indents.pop()

            # Track async function scope
            if stripped.startswith("async def "):
                async_indents.append(current_indent)

            # Track loop scope
            if re.search(r"^\s*(?:for\s+\w+|\s*while\b)", line):
                loop_indents.append(current_indent)

            # Remember whether this line sits inside a loop and/or inside an async function.
            in_loop = bool(loop_indents)
            in_async_def = bool(async_indents)

            # Track polling context
            if any(term in stripped.lower() for term in ("def poll", "poll_loop", "schedule_poll", "polling")):
                in_poll_context = True

            # 1. Check blocking time.sleep inside async def
            if is_added and in_async_def and "time.sleep(" in line:
                findings.append({
                    "type": "blocking_sleep_in_async",
                    "file": filename,
                    "title": "Blocking time.sleep inside async function",
                    "detail": "Blocking `time.sleep()` used inside `async def` function. Use `await asyncio.sleep()` instead.",
                    "level": "warning",
                })

            # 2. Check sequential await in loop
            if is_added and in_loop and re.search(r"\bawait\s+", line):
                findings.append({
                    "type": "sequential_await_in_loop",
                    "file": filename,
                    "title": "Sequential await inside loop",
                    "detail": "Sequential `await` call detected inside loop. Consider concurrent execution using `asyncio.gather()`.",
                    "level": "warning",
                })

            # 3. Repeated decryption or missing cache TTL in polling loops
            if is_added and (in_loop or in_poll_context) and any(term in line for term in ("decrypt(", "fernet.", ".decrypt", "get_secret(")):
                # Lines that mention a cache or TTL are assumed to already handle repetition.
                if "cache" not in line.lower() and "ttl" not in line.lower():
                    findings.append({
                        "type": "repeated_decryption_in_polling_loop",
                        "file": filename,
                        "title": "Repeated decryption in polling loop",
                        "detail": "Repeated decryption or secret retrieval detected inside polling loop without cached TTL.",
                        "level": "warning",
                    })

    # Return all findings (empty list means nothing suspicious was found).
    return findings
