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
# Regular expressions find getattr(...) calls in diff text.
import re

# Matches getattr(obj, "name" ...) and captures the quoted attribute name.
_GETATTR_RE = re.compile(r'getattr\(\s*[\w\.]+\s*,\s*["\'](\w+)["\']')
_MAX_ATTRS = 6                 # cap distinct attr names looked up per PR
_MAX_HITS_PER_ATTR = 2         # cap files fetched per attr name
# Longest code snippet (in characters) shown to the reviewer per hit, to keep prompts small.
_SNIPPET_MAX_CHARS = 1200
_SNIPPET_CONTEXT_LINES = 6     # lines of context around the matched line


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
        # Added lines start with "+"; strip that marker and hand the code back.
        if line.startswith("+"):
            yield line[1:]


def extract_getattr_names(files):
    """files: PyGithub File objects (pr.get_files()). Returns a deduped,
    order-preserving list of attribute names accessed via getattr(...) in
    ADDED lines across the PR's changed files — capped at _MAX_ATTRS so a
    diff with dozens of getattr calls doesn't turn into dozens of searches.
    Never raises: a file with no .patch (binary/too large) is skipped."""
    # Ordered list of distinct attribute names found so far.
    seen = []
    # Look through every changed file in the PR.
    for f in files or []:
        # Files without a patch (binary / too large) cannot be scanned.
        patch = getattr(f, "patch", None)
        if not patch:
            continue
        # Only new code matters, so scan just the added lines.
        added = "\n".join(_added_lines(patch))
        # Record each getattr attribute name the first time we see it.
        for m in _GETATTR_RE.finditer(added):
            name = m.group(1)
            if name not in seen:
                seen.append(name)
    # Cap the list so we never run too many searches.
    return seen[:_MAX_ATTRS]


def _snippet_around(content, needle):
    """Return a few lines of context around the first line containing needle, size-capped."""
    # Search the file line by line for the matched text.
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            # Take up to _SNIPPET_CONTEXT_LINES lines either side, clamped to the file bounds.
            lo = max(0, i - _SNIPPET_CONTEXT_LINES)
            hi = min(len(lines), i + _SNIPPET_CONTEXT_LINES + 1)
            # Join them back up and cap the length.
            snippet = "\n".join(lines[lo:hi])
            return snippet[:_SNIPPET_MAX_CHARS]
    # Needle not found: fall back to the top of the file.
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
    # Nothing to search with or for.
    if gh is None or not attr_names:
        return {}
    # Paths already in the diff, which we skip.
    changed = set(changed_paths or [])
    # Code search needs the "owner/name" form of the repo.
    full_name = getattr(repo, "full_name", None)
    if not full_name:
        return {}
    # Result map: attribute name -> list of hits.
    out = {}
    # Search for each attribute name in turn.
    for name in attr_names:
        hits = []
        # Try the "self.name =" assignment first, then a "def name(" method/property.
        for pattern in ('self.%s =' % name, 'def %s(' % name):
            # Any search/rate-limit/auth error just skips this pattern.
            try:
                # Exact-phrase code search restricted to this repository.
                query = '"%s" repo:%s' % (pattern, full_name)
                results = gh.search_code(query)
                # Look at a few more results than needed, since some get filtered out.
                for item in list(results[:_MAX_HITS_PER_ATTR * 2]):
                    # Skip results with no path or in files the diff already shows in full.
                    path = getattr(item, "path", None)
                    if not path or path in changed:
                        continue
                    # Need the file contents to build a snippet.
                    raw = getattr(item, "decoded_content", None)
                    if raw is None:
                        continue
                    # Decode the bytes; skip files that are not valid UTF-8 text.
                    try:
                        content = raw.decode("utf-8")
                    except (UnicodeDecodeError, AttributeError):
                        continue
                    # Keep the path plus a snippet around the matching line.
                    hits.append({"path": path, "snippet": _snippet_around(content, pattern)})
                    # Stop once we have enough hits for this name.
                    if len(hits) >= _MAX_HITS_PER_ATTR:
                        break
            except Exception:  # noqa: BLE001 — search/rate-limit/auth: degrade silently
                continue
            if hits:
                break  # the assignment pattern found it — no need for the def pattern too
        # Only names that actually turned up a definition are reported.
        if hits:
            out[name] = hits
    return out


def format_wiring_context(defs):
    """Render find_attr_definitions()'s result as a prompt-ready block, or ''
    if nothing was found. Framed as VERIFIED ground truth (not "maybe this
    helps") so the reviewer treats it as settling the question, the same
    framing fix_engine's full-file-context addendum uses."""
    # Nothing found means nothing to add to the prompt.
    if not defs:
        return ""
    # One text block per hit, labelled with the attribute name and file path.
    blocks = []
    for name, hits in defs.items():
        for h in hits:
            blocks.append(
                "\n--- REAL DEFINITION of `%s` (found via repo code search, "
                "NOT part of this diff): %s ---\n%s\n" % (name, h["path"], h["snippet"])
            )
    # Prepend instructions telling the reviewer to trust these definitions.
    return (
        "\n\nThis diff accesses some attributes via getattr(...) that are "
        "defined in files OUTSIDE this diff. The block(s) below are the "
        "ACTUAL definitions found by searching the repo — verify the new "
        "code's assumptions (shape, keys, types) against them instead of "
        "flagging the access as unverifiable:\n" + "".join(blocks)
    )
