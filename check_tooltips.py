"""
check_tooltips.py — Tier-1 deterministic tooltip-completeness scan for PR
pre-review AND feature auto-drive's build stage.

Zero-LLM, zero-cost, always-on: scans only the ADDED lines of a PR's diff
(same `.patch` text pr_review.py already fetches via pr.get_files(), no extra
API calls) for a new interactive control (<button>, <input>, <select>) with
no `title=` attribute nearby. This is a UX-completeness nudge, not a
correctness/security issue — findings are always level="advisory" (the
lowest severity tier), unlike secrets_scan.py's always-"error".

Same {level, title, detail} shape as pr_review.check_parity / secrets_scan.
check_secrets, so it slots into the same findings list with no special
handling downstream.
"""
import re

_MAX_FILES = 40
_MAX_FINDINGS = 15  # same cap rationale as secrets_scan.py — one wall of
                    # findings from a vendored/generated file helps no one.

_TAG_RE = re.compile(r"<(button|input|select)\b", re.IGNORECASE)
_TITLE_RE = re.compile(r"\btitle\s*=", re.IGNORECASE)
# hidden inputs are never user-visible, so a tooltip on one is meaningless.
_HIDDEN_INPUT_RE = re.compile(r'type\s*=\s*["\']hidden["\']', re.IGNORECASE)


def _added_lines(patch):
    """Yield (index_in_patch, text) for '+' lines in a unified diff patch,
    skipping the '+++' file-header line. Mirrors secrets_scan._added_lines —
    duplicated rather than imported so this module stays independently
    standalone (no cross-module coupling two Tier-1 checks shouldn't need)."""
    if not patch:
        return
    for i, line in enumerate(patch.splitlines()):
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            yield i, line[1:]


def _has_tooltip_nearby(lines, start_idx, window=2):
    """True if `title=` appears on the tag's own added line or within the
    next `window` added lines — a crude but low-noise way to tolerate a
    control's attributes wrapping onto a following line, which this
    codebase's own templates do occasionally (a single-line same-line-only
    check would false-positive on those)."""
    for text in lines[start_idx:start_idx + 1 + window]:
        if _TITLE_RE.search(text):
            return True
        if ">" in text:
            break  # tag closed with no title= found in its own span
    return False


def find_missing_tooltips(patch):
    """Scan one file's patch for added <button>/<input>/<select> tags with
    no title= nearby. Returns a list of (tag_name, snippet) tuples — the
    caller (find_missing_tooltips_in_files) turns these into findings."""
    if not patch:
        return []
    added = [text for _, text in _added_lines(patch)]
    hits = []
    for idx, text in enumerate(added):
        m = _TAG_RE.search(text)
        if not m:
            continue
        if m.group(1).lower() == "input" and _HIDDEN_INPUT_RE.search(text):
            continue
        if _has_tooltip_nearby(added, idx):
            continue
        hits.append((m.group(1).lower(), text.strip()[:120]))
    return hits


def find_missing_tooltips_in_files(files):
    """Scan a PR's changed files (PyGithub File objects, same list pr_review.py
    already fetches via pr.get_files()) for added interactive controls with no
    title= tooltip nearby. HTML files only. Returns a list of {level, title,
    detail} findings, level always 'advisory'. Best-effort: a file with no
    .patch (binary/too-large) is silently skipped, never raises."""
    findings = []
    for f in list(files or [])[:_MAX_FILES]:
        if len(findings) >= _MAX_FINDINGS:
            findings.append({
                "level": "advisory",
                "title": "Tooltip scan capped",
                "detail": "Hit the %d-finding cap for this PR — there may be more "
                          "controls missing a tooltip." % _MAX_FINDINGS,
            })
            break
        filename = getattr(f, "filename", "?") or "?"
        if not filename.lower().endswith(".html"):
            continue
        patch = getattr(f, "patch", None)
        for tag, snippet in find_missing_tooltips(patch):
            findings.append({
                "level": "advisory",
                "title": f"New <{tag}> in `{filename}` has no title= tooltip",
                "detail": (
                    f"Added: `{snippet}`. Not a hard requirement, but every other "
                    f"interactive control in this project carries a `title=` "
                    f"explaining what it does — consider adding one for consistency."
                ),
            })
    return findings
