"""
secrets_scan.py — Tier-1 deterministic secrets/credential scan for PR pre-review.

Zero-LLM, zero-cost, always-on: scans only the ADDED lines of a PR's diff
(same `.patch` text pr_review.py already fetches via pr.get_files(), no extra
API calls) for common credential shapes. False positives are cheaper than
false negatives here, but the pattern set is curated to keep noise low —
placeholder/example values (``changeme``, ``xxx``, ``<your-key>``,
``os.environ[...]``) are excluded so a normal config-lookup line doesn't fire.

Findings use the SAME {level, title, detail} shape as pr_review.check_parity,
always level="error" (the most severe existing tier) so a real hit sorts to
the top of the review comment. This is advisory-only like everything else in
pr_review.py — it flags for human review, it never blocks or redacts.
"""
import re

# Patterns with high specificity — a matched prefix/shape that's essentially
# never legitimate source text. Each: (name, compiled regex, match-group index
# to report, or None to report the whole match).
_SIGNATURE_PATTERNS = [
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), None),
    ("AWS Secret Access Key (context)", re.compile(
        r"(?i)aws_secret_access_key\s*[:=]\s*['\"][A-Za-z0-9/+=]{40}['\"]"), None),
    ("GitHub Personal Access Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), None),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), None),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), None),
    ("Private Key Block", re.compile(
        r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), None),
    ("Connection string with embedded credentials", re.compile(
        r"(?i)\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s/'\"]+:[^@\s/'\"]+@"), None),
    ("Slack Webhook URL", re.compile(
        r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+"), None),
]

# Generic "assignment of a secret-shaped value to a secret-shaped name" — needs
# the placeholder/lookup exclusion below to stay usably low-noise.
_GENERIC_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|secret([_-]?key)?|access[_-]?token|auth[_-]?token|"
    r"password|passwd|pwd|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*"
    r"['\"]([^'\"\s]{8,})['\"]"
)

_PLACEHOLDER_RE = re.compile(
    r"(?i)^(changeme|change_me|xxx+|todo|fixme|example|placeholder|redacted|"
    r"your[_-].*|<.*>|\{\{.*\}\}|\$\{.*\}|none|null|false|true|\.\.\.)$"
)
# A line that PULLS a secret from config/env rather than hardcoding one —
# these are exactly the pattern we want engineers to use, never flag them.
_LOOKUP_CONTEXT_RE = re.compile(
    r"(?i)(os\.environ|getenv|os\.getenv|config\.get|process\.env|vault\.|"
    r"secret_manager|keyring\.|\.env\b|ENV\[|SecretStr|Depends\()"
)

_MAX_FILES = 40
_MAX_FINDINGS = 15  # cap so a vendored-file accident doesn't produce a wall of findings


def _added_lines(patch):
    """Yield (line_no_in_patch, text) for '+' lines in a unified diff patch,
    skipping the '+++' file-header line. line_no is 1-based within the patch
    text, only used for a stable, cheap per-file dedup key."""
    if not patch:
        return
    for i, line in enumerate(patch.splitlines()):
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            yield i, line[1:]


def _scan_line(text):
    """Return a list of (signature_name, matched_text) for one added line."""
    hits = []
    for name, pat, _ in _SIGNATURE_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append((name, m.group(0)))
    m = _GENERIC_ASSIGN.search(text)
    if m:
        value = m.group(3)
        if not _PLACEHOLDER_RE.match(value.strip()) and not _LOOKUP_CONTEXT_RE.search(text):
            hits.append(("Hardcoded credential-shaped assignment", m.group(0)))
    return hits


def _redact(matched_text, keep=4):
    """Never echo the actual secret value back into a PUBLIC PR comment —
    show only enough to identify the finding (first `keep` chars + length)."""
    s = matched_text.strip()
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "…(%d more chars, redacted)" % (len(s) - keep)


def check_secrets(files):
    """Scan a PR's changed files (PyGithub File objects, same list pr_review.py
    already fetched via pr.get_files()) for hardcoded credentials in ADDED
    lines only. Returns a list of {level, title, detail} findings, level
    always 'error'. Best-effort: a file with no .patch (binary/too-large) is
    silently skipped, never raises."""
    findings = []
    for f in list(files)[:_MAX_FILES]:
        if len(findings) >= _MAX_FINDINGS:
            findings.append({
                "level": "error",
                "title": "Secrets scan capped",
                "detail": "Hit the %d-finding cap for this PR — there may be more. "
                          "Review the full diff manually before merging." % _MAX_FINDINGS,
            })
            break
        filename = getattr(f, "filename", "?")
        patch = getattr(f, "patch", None)
        if not patch:
            continue
        seen_on_this_file = set()
        for _, line in _added_lines(patch):
            for name, matched in _scan_line(line):
                key = (name, matched)
                if key in seen_on_this_file:
                    continue  # same secret repeated in the file — one finding is enough
                seen_on_this_file.add(key)
                findings.append({
                    "level": "error",
                    "title": "Possible secret: %s in `%s`" % (name, filename),
                    "detail": (
                        "A line added by this PR looks like a hardcoded credential "
                        "(`%s`). If this is real, **rotate it immediately** — it is "
                        "now in git history even if removed in a later commit — then "
                        "replace it with an environment variable / secret-manager "
                        "lookup. If this is a false positive (e.g. a test fixture or "
                        "an already-public placeholder), no action needed."
                        % _redact(matched)
                    ),
                })
                if len(findings) >= _MAX_FINDINGS:
                    break
            if len(findings) >= _MAX_FINDINGS:
                break
    return findings
