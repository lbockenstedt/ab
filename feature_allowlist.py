"""
feature_allowlist.py — pure, standalone DEFAULT-DENY allowlist matcher for
AppBuilder's auto-merge gate (pr_review.py's _automerge_decision).

WHY THIS EXISTS (design intent, operator-authored)
---------------------------------------------------
feature_boundary.py answers "does this diff touch something we must NEVER
build without a human?" — a deny-list. That is necessary but not sufficient:
the operator's policy is DEFAULT-DENY, i.e. *nothing* auto-merges unless it
is a small, additive, provably-safe change. This module answers the opposite,
positive question: "is this diff one of the few additive shapes we are
willing to merge with no human?".

The gate composition in _automerge_decision is therefore:

    auto-merge  ==  (all existing panel/confidence/state gates pass)
                AND (boundary_hits is empty)          # deny-list  (feature_boundary)
                AND (classify(...) is auto-approvable) # allow-list (this module)

Because this is added as an *additional required* condition, it can only ever
make auto-merge MORE restrictive. A false negative here (we fail to recognise
a genuinely-safe change) costs only "a human approves it" — which is the
operator's explicit safe default for anything behaviour-changing. A false
positive is the dangerous direction, so every detector below is deliberately
conservative and fails closed on ANY ambiguity (missing patch text, mixed
categories, an unexpected line shape → not auto-approvable).

WHAT IS AUTO-APPROVABLE (must be provable from the diff alone)
-------------------------------------------------------------
  - docs-only     : every changed file is documentation (*.md/*.rst/docs/**).
  - log-only      : purely additive logging — only added `logger.*` calls,
                    zero deletions, no other added code.
  - tooltip-only  : only tooltip / label copy attributes changed
                    (title=/aria-label=/placeholder=/label text), no logic.

WHAT IS DELIBERATELY *NOT* AUTO-APPROVABLE HERE (routes to human)
----------------------------------------------------------------
  - new-client-simulation : a real sim touches shared orchestrators/config
        (see the add-simulation skill — ~15 coordinated edits, many of them
        MODIFICATIONS to existing shared files). "additive & isolated" cannot
        be proven from the diff, so a new-sim PR is built + reviewed and then
        waits for a human. Promoting it to auto-approve needs a semantic
        classifier signal, not a path heuristic — intentionally future work.
  - report-only : a "read-only" report cannot be proven side-effect-free from
        a diff alone, so it also routes to human approval.

Navigation / button / form-handler / route-logic changes are, by
construction, none of the categories above → not auto-approvable → human.
That is the operator rule "behaviour-changing → build but gate" enforced
mechanically rather than by trust.

No app imports (mirrors feature_boundary.py / secrets_scan.py): this is a
deterministic, import-light, directly-unit-testable pure module.

File record shape (dict) — a normalised view of one PyGithub PR file:
    {
        "path": "ab/docs/foo.md",      # filename
        "status": "modified",          # added|modified|removed|renamed
        "additions": 3,
        "deletions": 0,
        "patch": "@@ ... @@\n+...\n",   # unified diff hunk text, may be None
    }
"""
import fnmatch

# ── category ids (stable slugs, referenced in config + merge reasons) ────────
DOCS_ONLY = "docs-only"
LOG_ONLY = "log-only"
TOOLTIP_ONLY = "tooltip-only"

# The categories this diff-level classifier is willing to auto-approve. The
# operator narrows (never widens beyond) this from config key
# `feature_automerge_allowlist`; an empty/absent config list means "use this
# default set". Categories the classifier can never *prove* from a diff
# (new-client-simulation, report-only) are intentionally not in this set.
DEFAULT_ALLOWLIST = [DOCS_ONLY, LOG_ONLY, TOOLTIP_ONLY]

# Documentation file shapes — edits here cannot change runtime behaviour.
_DOC_GLOBS = [
    "*.md", "**/*.md", "*.rst", "**/*.rst", "*.txt", "**/*.txt",
    "**/docs/**", "docs/**", "README*", "**/README*", "CHANGELOG*", "**/CHANGELOG*",
]


def _path_hit(path, patterns):
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def _added_lines(patch):
    """Content lines the diff ADDS (leading '+', excluding the '+++' header).
    Returns each line's text WITHOUT the leading '+'."""
    out = []
    for ln in (patch or "").splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(ln[1:])
    return out


def _removed_lines(patch):
    """Content lines the diff REMOVES (leading '-', excluding the '---'
    header), each WITHOUT the leading '-'."""
    out = []
    for ln in (patch or "").splitlines():
        if ln.startswith("-") and not ln.startswith("---"):
            out.append(ln[1:])
    return out


# A logging call: logger./logging./log./self.log. + a level method + '('.
# Deliberately anchored to the START of the (stripped) added line so a line
# that merely *mentions* logging inside other code does not qualify.
_LOG_PREFIXES = ("logger.", "logging.", "log.", "self.logger.", "self.log.", "_log.", "LOG.")
_LOG_METHODS = ("debug(", "info(", "warning(", "warn(", "error(", "critical(", "exception(")


def _is_pure_log_added_line(text):
    """True if an added line is blank, a comment, or a single logging call.
    Conservative: the line must START with a known logger prefix AND contain a
    level method call — assignments, conditionals, or logger *configuration*
    (addHandler/setLevel/getLogger) do NOT qualify."""
    s = text.strip()
    if s == "" or s.startswith("#"):
        return True
    if not s.startswith(_LOG_PREFIXES):
        return False
    return any(m in s for m in _LOG_METHODS)


# Attribute-copy tokens that carry no behaviour — pure human-visible text.
_TOOLTIP_TOKENS = ("title=", "aria-label=", "aria-describedby=", "placeholder=",
                   "data-tooltip=", "data-bs-title=", "label=", "helptext=", "help_text=")


def _is_pure_tooltip_line(text):
    """True if a changed (added or removed) line only carries tooltip/label
    copy. Blank/comment lines pass; otherwise the line must contain a tooltip
    token AND must not contain code-flow punctuation that would indicate real
    logic (assignment to a variable, a call other than the attribute, etc.).
    Conservative by design — anything it is unsure about fails the category."""
    s = text.strip()
    if s == "":
        return True
    if not any(tok in s.casefold() for tok in _TOOLTIP_TOKENS):
        return False
    # Reject lines that look like control flow / real statements rather than a
    # markup attribute or a simple copy assignment.
    lowered = s.casefold()
    for bad in ("def ", "return ", "import ", "if ", "for ", "while ", "lambda",
                "os.", "subprocess", "eval(", "exec(", "->"):
        if bad in lowered:
            return False
    return True


def _all_docs(files):
    return bool(files) and all(_path_hit(f.get("path", ""), _DOC_GLOBS) for f in files)


def _all_log_only(files):
    """Purely additive logging across EVERY changed file: no deletions
    anywhere, and every added line is blank/comment or a bare logging call.
    A file with no patch text (binary / patch unavailable) fails closed."""
    if not files:
        return False
    for f in files:
        if f.get("status") in ("removed", "renamed"):
            return False
        patch = f.get("patch")
        if not patch:
            return False
        if _removed_lines(patch):
            return False
        added = _added_lines(patch)
        if not added:
            return False
        if not all(_is_pure_log_added_line(a) for a in added):
            return False
    return True


def _all_tooltip_only(files):
    """Only tooltip/label copy changed across EVERY changed file. Both added
    and removed lines must be pure tooltip lines; a missing patch fails
    closed; there must be at least one real tooltip line so an empty/no-op
    diff does not masquerade as this category."""
    if not files:
        return False
    saw_real = False
    for f in files:
        if f.get("status") in ("removed", "renamed"):
            return False
        patch = f.get("patch")
        if not patch:
            return False
        changed = _added_lines(patch) + _removed_lines(patch)
        if not changed:
            return False
        for c in changed:
            if not _is_pure_tooltip_line(c):
                return False
            if c.strip():
                saw_real = True
    return saw_real


def classify(files, allowlist=None):
    """Classify a PR's real changed-file records into at most ONE additive
    allowlist category, honouring the operator's enabled-category `allowlist`
    (defaults to DEFAULT_ALLOWLIST). Returns:

        {"category": str|None, "auto_approvable": bool, "reason": str}

    `auto_approvable` is True ONLY when a category matched AND that category
    is enabled in `allowlist`. Any ambiguity, mixed categories, missing patch
    text, or unrecognised shape → category None, auto_approvable False, and a
    reason explaining the safe fallback to human approval."""
    enabled = list(allowlist) if allowlist else list(DEFAULT_ALLOWLIST)

    if not files:
        return {"category": None, "auto_approvable": False,
                "reason": "no changed files to classify (fail closed)"}

    # Order matters only for reason clarity; the detectors are mutually
    # exclusive in practice (a docs file is not a .py logging change).
    if _all_docs(files):
        cat = DOCS_ONLY
    elif _all_log_only(files):
        cat = LOG_ONLY
    elif _all_tooltip_only(files):
        cat = TOOLTIP_ONLY
    else:
        return {"category": None, "auto_approvable": False,
                "reason": "diff is not a recognised additive allowlist shape "
                          "(docs-only / log-only / tooltip-only) — routes to human approval"}

    if cat not in enabled:
        return {"category": cat, "auto_approvable": False,
                "reason": f"matched additive category '{cat}' but it is not enabled "
                          f"in feature_automerge_allowlist — routes to human approval"}

    return {"category": cat, "auto_approvable": True,
            "reason": f"additive allowlist category '{cat}' (auto-approvable)"}


def files_from_pr_files(pr_files):
    """Normalise an iterable of PyGithub PR file objects (pr.get_files())
    into the plain-dict records classify() expects. Kept here (not in
    pr_review) so the normalisation is unit-testable without importing the
    live app."""
    out = []
    for f in pr_files or []:
        out.append({
            "path": getattr(f, "filename", "") or "",
            "status": getattr(f, "status", "") or "",
            "additions": getattr(f, "additions", 0) or 0,
            "deletions": getattr(f, "deletions", 0) or 0,
            "patch": getattr(f, "patch", None),
        })
    return out
