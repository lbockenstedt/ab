"""
check_unattended_mutation.py — Tier-1 deterministic check for a PR that adds an
UNATTENDED, machine-driven data-mutation path (a background loop that deletes/
purges/destroys something) with no accompanying test.

Motivating case: lm#151 added an automatic 15-min sweep that deletes client
registry records with no matching VM. BugFixer's own skeptical review panel
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
import re

_LOOP_RE = re.compile(r"\bwhile\s+True\s*:")
_SLEEP_RE = re.compile(r"\bsleep\s*\(")
_MUTATION_RE = re.compile(
    r"(?i)\b(delete|purge|destroy|remove|scrub|evict|shed|drop|wipe)\w*\s*\("
)
_TEST_PATH_RE = re.compile(
    r"(?i)(^|/)(tests?)(/|$)|(^|/)test_[^/]+\.\w+$|[^/]+_test\.\w+$|"
    r"[^/]+\.(test|spec)\.\w+$"
)

_MAX_FILES = 40


def _added_lines(patch):
    if not patch:
        return
    for line in patch.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            yield line[1:]


def check_unattended_mutation(files):
    """files: PyGithub File objects from pr.get_files() (same list pr_review.py
    already fetched). Returns a list of {level, title, detail} findings,
    level='advisory'. Best-effort: a file with no .patch (binary/too-large) is
    silently skipped; never raises."""
    has_test_file = False
    flagged = []
    for f in list(files)[:_MAX_FILES]:
        filename = getattr(f, "filename", "") or ""
        if _TEST_PATH_RE.search(filename):
            has_test_file = True
        patch = getattr(f, "patch", None)
        if not patch:
            continue
        added = "\n".join(_added_lines(patch))
        if _LOOP_RE.search(added) and _SLEEP_RE.search(added) and _MUTATION_RE.search(added):
            flagged.append(filename)
    if not flagged or has_test_file:
        return []
    names = ", ".join("`%s`" % f for f in flagged)
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
