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
# Regular expressions describe every credential shape this scan looks for.
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
    # An at-rest encryption key (Fernet keys are exactly 43 urlsafe-base64 chars
    # + '='). Named explicitly so the finding says what was leaked: exposing one
    # means every encrypted state file must be re-encrypted, not just the key
    # replaced. Quotes optional -- these almost always appear in a .env line.
    ("Encryption key (Fernet-shaped)", re.compile(
        r"(?i)\b[a-z0-9_-]*(?:fernet|encryption)[a-z0-9_-]*key\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9_-]{43}=['\"]?"), None),
]

# Generic "assignment of a secret-shaped value to a secret-shaped name" — needs
# the placeholder/lookup exclusion below to stay usably low-noise.
_GENERIC_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|secret([_-]?key)?|access[_-]?token|auth[_-]?token|"
    r"password|passwd|pwd|client[_-]?secret|private[_-]?key)\b\s*[:=]\s*"
    r"['\"]([^'\"\s]{8,})['\"]"
)

# Unquoted ``NAME=value`` — the dotenv/shell/systemd/YAML shape.
# ``_GENERIC_ASSIGN`` above misses this class entirely for two independent
# reasons, and that is exactly how an at-rest encryption key can reach a diff
# unnoticed:
#   1. it REQUIRES quotes, but a .env line never has them; and
#   2. it anchors each credential word with ``\b``, which cannot match after an
#      underscore — so a vendor/product prefix hides the name completely
#      (``LM_FERNET_KEY``, ``LM_HUB_SECRET`` and ``DB_PASSWORD`` all slip past).
# So match the credential word as a SUFFIX of the identifier, and make the
# quotes optional. The value charset deliberately excludes ``:``, ``/``, ``(``,
# ``$``, ``{`` and whitespace, so a URL, a filesystem path, a function call and
# a ``${VAR}`` expansion are all structurally unable to match; together with the
# placeholder + lookup-context exclusions that keeps ordinary config lines quiet.
_ENV_ASSIGN = re.compile(
    r"(?i)(?:^|[\s;,{])([A-Za-z][A-Za-z0-9_-]*"
    r"(?:key|secret|token|password|passwd|pwd))\s*[:=]\s*"
    r"['\"]?([A-Za-z0-9+/=_.~-]{12,})['\"]?\s*(?:[#;].*)?$"
)

# Values that are obviously placeholders (changeme, xxx, <your-key>, ${VAR}, booleans,
# ...) and must not be reported as real secrets.
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

# Only scan the first 40 files of a PR to keep the scan cheap on huge PRs.
_MAX_FILES = 40
_MAX_FINDINGS = 15  # cap so a vendored-file accident doesn't produce a wall of findings


def _added_lines(patch):
    """Yield (line_no_in_patch, text) for '+' lines in a unified diff patch,
    skipping the '+++' file-header line. line_no is 1-based within the patch
    text, only used for a stable, cheap per-file dedup key."""
    # No diff text (binary or too large): nothing to yield.
    if not patch:
        return
    # Walk the diff with line indices so each hit gets a stable position key.
    for i, line in enumerate(patch.splitlines()):
        # Skip the "+++ b/file" header, which is not an added line.
        if line.startswith("+++"):
            continue
        # Yield added lines without their leading "+".
        if line.startswith("+"):
            yield i, line[1:]


def _scan_line(text):
    """Return a list of (signature_name, matched_text) for one added line."""
    # Every (pattern name, matched text) found on this line.
    hits = []
    # Try each high-specificity signature pattern against the line.
    for name, pat, _ in _SIGNATURE_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append((name, m.group(0)))
    # Then try the generic quoted "secret_name = 'value'" rule.
    m = _GENERIC_ASSIGN.search(text)
    if m:
        # Group 3 is the quoted value being assigned.
        value = m.group(3)
        # Report it unless the value is a placeholder or the line reads from env/config.
        if not _PLACEHOLDER_RE.match(value.strip()) and not _LOOKUP_CONTEXT_RE.search(text):
            hits.append(("Hardcoded credential-shaped assignment", m.group(0)))
    elif not any(n.startswith("Encryption key") for n, _ in hits):
        # Only when the quoted rule and the Fernet signature both stayed silent,
        # so one .env line never yields three findings for the same secret.
        # Try the unquoted NAME=value (.env / shell / YAML) rule.
        m = _ENV_ASSIGN.search(text)
        if m:
            # Group 2 is the assigned value.
            value = m.group(2)
            # Same placeholder / lookup-context exclusions as the quoted rule.
            if (not _PLACEHOLDER_RE.match(value.strip())
                    and not _LOOKUP_CONTEXT_RE.search(text)):
                hits.append(("Hardcoded credential in an unquoted assignment",
                             m.group(0).strip()))
    # Return every hit for this line (empty list when the line is clean).
    return hits


def _redact(matched_text, keep=4):
    """Never echo the actual secret value back into a PUBLIC PR comment —
    show only enough to identify the finding (first `keep` chars + length)."""
    # Work on the trimmed match text.
    s = matched_text.strip()
    # Very short matches are fully masked.
    if len(s) <= keep:
        return "*" * len(s)
    # Show the first few characters plus how many characters were hidden.
    return s[:keep] + "…(%d more chars, redacted)" % (len(s) - keep)


def check_secrets(files):
    """Scan a PR's changed files (PyGithub File objects, same list pr_review.py
    already fetched via pr.get_files()) for hardcoded credentials in ADDED
    lines only. Returns a list of {level, title, detail} findings, level
    always 'error'. Best-effort: a file with no .patch (binary/too-large) is
    silently skipped, never raises."""
    # Findings for the whole PR, across all scanned files.
    findings = []
    # Scan files in order, up to the per-PR file limit.
    for f in list(files)[:_MAX_FILES]:
        # Too many findings already: add one "capped" notice and stop scanning.
        if len(findings) >= _MAX_FINDINGS:
            findings.append({
                "level": "error",
                "title": "Secrets scan capped",
                "detail": "Hit the %d-finding cap for this PR — there may be more. "
                          "Review the full diff manually before merging." % _MAX_FINDINGS,
            })
            break
        # Read the file's name and diff text; files without a diff are skipped.
        filename = getattr(f, "filename", "?")
        patch = getattr(f, "patch", None)
        if not patch:
            continue
        # Avoid reporting the same secret twice within one file.
        seen_on_this_file = set()
        # Check every added line in this file's diff.
        for _, line in _added_lines(patch):
            # Report each distinct hit on the line.
            for name, matched in _scan_line(line):
                # A finding is identified by its pattern name plus the exact matched text.
                key = (name, matched)
                if key in seen_on_this_file:
                    continue  # same secret repeated in the file — one finding is enough
                # Record the hit, redacting the secret so it is not republished in the PR comment.
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
                # Stop scanning this file's hits once the cap is reached ...
                if len(findings) >= _MAX_FINDINGS:
                    break
            # ... and stop scanning further lines of this file too (the outer loop adds the notice).
            if len(findings) >= _MAX_FINDINGS:
                break
    # Return all findings (empty list means nothing credential-shaped was added).
    return findings
