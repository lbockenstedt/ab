"""
check_unattended_mutation.py — Tier-1 deterministic check for a PR that adds an
UNATTENDED, machine-driven data-mutation path (a background loop that deletes/
purges/destroys something) with no accompanying test.

Motivating case: lm#151 added an automatic 15-min sweep that deletes client
registry records with no matching VM. AppBuilder's own skeptical review panel
correctly rejected it (55%) over a real safety-rail gap in that code — but the
gap was the kind of thing that only became verifiably fixed once a UNIT TEST
was written pinning the exact guard behavior. A one-line typo fix and an
unattended bulk-delete loop are different risk classes; this check flags the
second class so it gets asked for test coverage BEFORE panel review, the same
mechanical way check_secrets/check_parity/check_undefined_names already flag
their own risk classes — zero LLM cost, always on.

Heuristic and deliberately loose: this is advisory (never blocks, never
resolves), so a false positive just adds one note to the review comment. Two
signals, BOTH required, in the SAME changed file's ADDED lines:
  1. A periodic background loop: `while True` + `asyncio.sleep(` (or `sleep(`
     for non-python loops) — something that runs unattended on its own
     schedule, not in response to a single human-initiated request.
  2. A mutation call that deletes/purges/destroys/removes/scrubs/evicts state.

If both fire anywhere in the PR's changed files, and NO changed file in the
whole PR looks like a test (path contains a `test`/`tests` segment, or the
filename matches `test_*` / `*_test.*` / `*.test.*` / `*.spec.*`), emit one
advisory finding naming the file(s).
"""
# Regular expressions detect loops, sleeps, mutation calls and test file paths.
import re

# Signal 1a: a "while True:" loop.
_LOOP_RE = re.compile(r"\bwhile\s+True\s*:")
# Signal 1b: any sleep(...) call (time.sleep or asyncio.sleep).
_SLEEP_RE = re.compile(r"\bsleep\s*\(")
# Signal 2: a call whose name starts with a delete-like verb (delete_, purge, drop...).
_MUTATION_RE = re.compile(
    r"(?i)\b(delete|purge|destroy|remove|scrub|evict|shed|drop|wipe)\w*\s*\("
)
# Paths that look like tests: a test/tests directory, test_*.ext, *_test.ext,
# *.test.ext or *.spec.ext.
_TEST_PATH_RE = re.compile(
    r"(?i)(^|/)(tests?)(/|$)|(^|/)test_[^/]+\.\w+$|[^/]+_test\.\w+$|"
    r"[^/]+\.(test|spec)\.\w+$"
)

# Only the first 40 changed files are scanned, to bound work on huge PRs.
_MAX_FILES = 40


def _added_lines(patch):
    """Yield the text of every added ("+") line in a unified diff patch, without the "+"."""
    # No patch text (binary or oversized file): yield nothing.
    if not patch:
        return
    # Walk the diff line by line.
    for line in patch.splitlines():
        # Skip the "+++ b/file" header, which is not an added line.
        if line.startswith("+++"):
            continue
        # Added lines start with "+"; strip that marker.
        if line.startswith("+"):
            yield line[1:]


def check_unattended_mutation(files):
    """files: PyGithub File objects from pr.get_files() (same list pr_review.py
    already fetched). Returns a list of {level, title, detail} findings,
    level='advisory'. Best-effort: a file with no .patch (binary/too-large) is
    silently skipped; never raises."""
    # Whether the PR touches any test file, and which files trip both signals.
    has_test_file = False
    flagged = []
    # Look at each changed file (capped).
    for f in list(files)[:_MAX_FILES]:
        # Any test-looking file in the PR means tests were (probably) added.
        filename = getattr(f, "filename", "") or ""
        if _TEST_PATH_RE.search(filename):
            has_test_file = True
        # Files without a patch cannot be scanned.
        patch = getattr(f, "patch", None)
        if not patch:
            continue
        # Only added lines count.
        added = "\n".join(_added_lines(patch))
        # Flag the file if it adds a loop, a sleep and a mutation call.
        if _LOOP_RE.search(added) and _SLEEP_RE.search(added) and _MUTATION_RE.search(added):
            flagged.append(filename)
    # Nothing flagged, or tests are included: no finding.
    if not flagged or has_test_file:
        return []
    # List the flagged files in backticks.
    names = ", ".join("`%s`" % f for f in flagged)
    # One advisory finding asking for a test of the loop's safety rails.
    return [{
        "level": "advisory",
        "title": "Unattended background loop + data mutation, no test file in this PR",
        "detail": (
            "%s add%s both a periodic loop (`while True` + a `sleep(` call) and "
            "a delete/purge/destroy/remove/scrub-shaped call, with no changed "
            "file in this PR that looks like a test. This is a different risk "
            "class from a request-scoped or human-clicked change — nothing is "
            "watching it run, so a safety-rail bug (an over-broad guard, an "
            "off-by-one in a grace window, an ordering bug across sources) "
            "surfaces as silently-wrong production behavior, not a stack trace. "
            "Consider adding a unit test that directly exercises the loop's "
            "safety rail(s) — what it does and does NOT touch — before this "
            "merges. (Advisory only; this never blocks a merge on its own.)"
        ) % (names, "s" if len(flagged) > 1 else ""),
    }]
