"""
lint_python.py — Tier-1 deterministic undefined-name check for PR pre-review.

pr_review.py's LLM panel only ever sees the DIFF text (patch hunks), not full
files — a name defined outside the visible hunk (e.g. an import at the top of
a file the diff didn't touch) reads as "undefined" to the LLM even when it
isn't. That produced a real false positive on lm#135 ("no `import time` added"
when the import already existed above the diff's visible range).

This runs BEFORE the LLM panel, on the FULL post-patch file content (fetched
via the GitHub contents API at the PR head SHA — no clone/checkout, so this
never executes PR code, only parses it), using ``ruff``'s F821/F822/F823
rules (undefined name / undefined name in __all__ / used-before-assignment).
Deterministic and free: a real hit here is unambiguous, so findings are
level="error"; it also means the LLM panel's own "undefined names" claims can
be cross-checked against this pass when triaging its output by hand.

Scope: Python files only (bugfixer's own runtime + this class of bug is what
prompted the check). JS/TS is a separate, larger undertaking — not attempted
here (see the state-logic panel in pr_review.py for the LLM-side JS coverage
that already exists via the render-crash / syntax-error prompt).
"""
import json
import logging
import subprocess
import tempfile
import os

logger = logging.getLogger(__name__)

_RUFF_RULES = "F821,F822,F823"  # undefined name / undefined in __all__ / used-before-assignment
_MAX_FILES = 25
_MAX_FILE_BYTES = 300_000  # skip pathological/generated files; ruff would time out uselessly
_TIMEOUT_S = 10


def _fetch_full_content(repo, path, ref):
    """Full post-patch content of one file at the PR head SHA, or None if it
    can't be fetched (deleted file, binary, oversized, API error) — callers
    must treat None as 'skip', never as 'file is empty'."""
    try:
        c = repo.get_contents(path, ref=ref)
    except Exception as e:  # noqa: BLE001 — deleted/renamed/binary/oversized are all normal
        logger.debug("lint_python: could not fetch %s@%s: %s", path, ref, e)
        return None
    raw = getattr(c, "decoded_content", None)
    if raw is None or len(raw) > _MAX_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _run_ruff(source, filename_hint):
    """Run ruff on one file's content in isolation (no repo config picked up,
    so bugfixer's OWN pyproject.toml/ruff.toml can't accidentally suppress or
    alter these specific rules for someone else's PR). Returns ruff's parsed
    JSON diagnostics list, or [] on any tooling failure (never raises — a
    missing/broken ruff binary must degrade to 'no findings', not crash the
    whole review)."""
    suffix = ".py"
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False,
                                          encoding="utf-8") as tf:
            tf.write(source)
            tmp_path = tf.name
        try:
            proc = subprocess.run(
                ["ruff", "check", "--isolated", "--select", _RUFF_RULES,
                 "--output-format=json", tmp_path],
                capture_output=True, text=True, timeout=_TIMEOUT_S,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        # ruff exits 1 when it finds lint violations — that's the expected
        # "found something" path, not an error. Only a missing binary / crash
        # (FileNotFoundError, non-JSON stdout) should be swallowed as "skip".
        if not proc.stdout.strip():
            return []
        diagnostics = json.loads(proc.stdout)
        for d in diagnostics:
            d["_filename_hint"] = filename_hint
        return diagnostics
    except FileNotFoundError:
        logger.info("lint_python: ruff not installed — skipping undefined-name pass")
        return []
    except subprocess.TimeoutExpired:
        logger.info("lint_python: ruff timed out on %s — skipping", filename_hint)
        return []
    except (json.JSONDecodeError, OSError) as e:  # noqa: BLE001
        logger.info("lint_python: ruff run failed on %s (%s) — skipping", filename_hint, e)
        return []


def check_undefined_names(repo, files, head_sha):
    """Deterministic Tier-1 pass: for every changed .py file in this PR,
    fetch its FULL content at head_sha and run ruff's undefined-name rules.
    Returns {level, title, detail} findings (level='error'). Deleted files
    are skipped (nothing to fetch); best-effort throughout — any failure
    degrades to 'no findings' for that file, never raises."""
    findings = []
    py_files = [f for f in files
                if getattr(f, "filename", "").endswith(".py")
                and getattr(f, "status", "") != "removed"][:_MAX_FILES]
    for f in py_files:
        path = f.filename
        source = _fetch_full_content(repo, path, head_sha)
        if source is None:
            continue
        for d in _run_ruff(source, path):
            code = (d.get("code") or "").strip()
            msg = (d.get("message") or "").strip()
            line = (d.get("location") or {}).get("row")
            findings.append({
                "level": "error",
                "title": "Undefined name in `%s`%s" % (path, (" (line %s)" % line) if line else ""),
                "detail": "ruff %s: %s. Checked against the FULL file at this PR's head "
                          "commit (not just the diff), so this is a real undefined-name "
                          "hit, not a diff-visibility artifact." % (code or "?", msg or "undefined name"),
            })
    return findings
