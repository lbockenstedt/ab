"""attr_definition_lookup.py — best-effort GitHub code-search lookup for the
REAL definition of a ``getattr(x, "name", default)`` access the diff itself
doesn't show.

Motivating case: cs#74 added `getattr(deploy, "proxmox_states", {})` and
`getattr(cp, "connected_agents", {})` — wiring a new module onto existing
spoke/control-plane/deploy objects it never touches the definition of. The
skeptical reviewer panel (claude_cli, tools-blind) had no way to confirm those
attributes actually exist with the shape the new code assumes, and correctly
flagged that as unverifiable — an honest "I can't tell" that reads as a false
alarm once a human checks and finds the wiring is fine (or a REAL bug when it
isn't). Proactive full-file embedding (see fix_engine._run_reviewer_turn)
already covers files the diff itself CHANGES; this covers the harder case —
an attribute defined in a file the diff never touches at all.

`getattr(...)` is a deliberately narrow, high-signal target: the pattern
itself is defensive/uncertain attribute access — literally the author hedging
against "I'm not 100% sure this exists". A bare `x.attr` chain is far too
common (false-positive-prone) to be worth the same treatment.

Uses GitHub's code-search API (``Github.search_code``), which needs the repo
to be indexed — normal for any repo with commit history. Everything here is
best-effort and silently degrades to "found nothing" on any error (rate
limit, search unavailable, huge repo, weird auth scope) — this only ever
ADDS context for the reviewer to react to; it must never block or fail loud
enough to break the review itself.
"""
import re

_GETATTR_RE = re.compile(r'getattr\(\s*[\w\.]+\s*,\s*["\'](\w+)["\']')
_MAX_ATTRS = 6                 # cap distinct attr names looked up per PR
_MAX_HITS_PER_ATTR = 2         # cap files fetched per attr name
_SNIPPET_MAX_CHARS = 1200
_SNIPPET_CONTEXT_LINES = 6     # lines of context around the matched line


def _added_lines(patch):
    if not patch:
        return
    for line in patch.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            yield line[1:]


def extract_getattr_names(files):
    """files: PyGithub File objects (pr.get_files()). Returns a deduped,
    order-preserving list of attribute names accessed via getattr(...) in
    ADDED lines across the PR's changed files — capped at _MAX_ATTRS so a
    diff with dozens of getattr calls doesn't turn into dozens of searches.
    Never raises: a file with no .patch (binary/too large) is skipped."""
    seen = []
    for f in files or []:
        patch = getattr(f, "patch", None)
        if not patch:
            continue
        added = "\n".join(_added_lines(patch))
        for m in _GETATTR_RE.finditer(added):
            name = m.group(1)
            if name not in seen:
                seen.append(name)
    return seen[:_MAX_ATTRS]


def _snippet_around(content, needle):
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            lo = max(0, i - _SNIPPET_CONTEXT_LINES)
            hi = min(len(lines), i + _SNIPPET_CONTEXT_LINES + 1)
            snippet = "\n".join(lines[lo:hi])
            return snippet[:_SNIPPET_MAX_CHARS]
    return content[:_SNIPPET_MAX_CHARS]


def find_attr_definitions(gh, repo, attr_names, changed_paths=None):
    """For each attr name, GitHub-code-search THIS repo for its real
    assignment (``self.<name> =``) or method/property definition
    (``def <name>(``), skipping hits inside files the diff already changed
    (already covered by full-file embedding, and re-showing them here would
    just be noise). Returns ``{attr_name: [{"path", "snippet"}, ...]}`` for
    only the names that actually turned up a hit — an attr name that finds
    NOTHING is deliberately left out of the dict (not reported as "confirmed
    missing"): code search can legitimately miss dynamic assignment
    (``setattr``, kwargs-based `__init__`, a metaclass) that's real but not
    textually greppable, so absence of a hit is NOT proof the attribute
    doesn't exist — only a hit is actionable signal.

    Best-effort throughout: gh=None, no attr_names, or any search/fetch error
    for one name just skips that name — never raises, never blocks the
    caller."""
    if gh is None or not attr_names:
        return {}
    changed = set(changed_paths or [])
    full_name = getattr(repo, "full_name", None)
    if not full_name:
        return {}
    out = {}
    for name in attr_names:
        hits = []
        for pattern in ('self.%s =' % name, 'def %s(' % name):
            try:
                query = '"%s" repo:%s' % (pattern, full_name)
                results = gh.search_code(query)
                for item in list(results[:_MAX_HITS_PER_ATTR * 2]):
                    path = getattr(item, "path", None)
                    if not path or path in changed:
                        continue
                    raw = getattr(item, "decoded_content", None)
                    if raw is None:
                        continue
                    try:
                        content = raw.decode("utf-8")
                    except (UnicodeDecodeError, AttributeError):
                        continue
                    hits.append({"path": path, "snippet": _snippet_around(content, pattern)})
                    if len(hits) >= _MAX_HITS_PER_ATTR:
                        break
            except Exception:  # noqa: BLE001 — search/rate-limit/auth: degrade silently
                continue
            if hits:
                break  # the assignment pattern found it — no need for the def pattern too
        if hits:
            out[name] = hits
    return out


def format_wiring_context(defs):
    """Render find_attr_definitions()'s result as a prompt-ready block, or ''
    if nothing was found. Framed as VERIFIED ground truth (not "maybe this
    helps") so the reviewer treats it as settling the question, the same
    framing fix_engine's full-file-context addendum uses."""
    if not defs:
        return ""
    blocks = []
    for name, hits in defs.items():
        for h in hits:
            blocks.append(
                "\n--- REAL DEFINITION of `%s` (found via repo code search, "
                "NOT part of this diff): %s ---\n%s\n" % (name, h["path"], h["snippet"])
            )
    return (
        "\n\nThis diff accesses some attributes via getattr(...) that are "
        "defined in files OUTSIDE this diff. The block(s) below are the "
        "ACTUAL definitions found by searching the repo — verify the new "
        "code's assumptions (shape, keys, types) against them instead of "
        "flagging the access as unverifiable:\n" + "".join(blocks)
    )
