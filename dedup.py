"""Pure, dependency-free duplicate-detection helpers for the BugFixer.

This module is intentionally stdlib-only (``re`` only) and performs NO app
initialization, so it can be imported standalone for unit testing — unlike
``main.py``, which runs FastAPI/logging setup at import time and therefore
cannot be imported in a test harness.

Strengthened over the original inline helpers in ``main.py``:

* ``strip_boilerplate`` removes the template wrapper that ``create_automated_issue``
  injects into EVERY automated issue (``🤖 Log Alert:``, ``Automated Error
  Detection``, ``BugFixer Update``, ``Log Evidence``, ``AI Fix`` …). Without
  stripping it, that boilerplate dominates the token comparison and the actual
  error content is drowned out.
* ``MODULE_ALIASES`` collapses module-name variants so ``opns`` and ``opnsense``
  (the same spoke, named differently across cycles) compare equal. Kept minimal
  and explicit — only aliases that are unambiguously the same module.

These two changes let ``_is_duplicate_match`` recognise a recurring error that
was rephrased / re-logged at a new timestamp as the same issue, so the caller can
reopen the existing (possibly closed) issue instead of filing a duplicate.
"""

import re

# Minimal, explicit module aliases — ONLY modules that are unambiguously the
# same spoke. Add new entries only when the two names refer to one codebase.
MODULE_ALIASES = {
    "opns": "opnsense",
}

# Boilerplate phrases injected by create_automated_issue (main.py) into every
# automated issue title/body, and by the recurrence/evidence comments. Stripped
# so comparisons reflect the underlying error rather than the template wrapper.
_BOILERPLATE_RE = re.compile(
    r'\b(?:'
    r'bug report|feature request|'
    r'log alert|automated error detection|bugfixer update|bugfixer hub analysis|'
    r'log evidence|ai fix|automated issue|automated fix|log detected|'
    r'detected a potential issue in the logs|'
    r'this issue has been automatically created for fixing|'
    r'additional instance of this error detected in repository|'
    r'recurrence detected reopening instead of filing a duplicate|'
    r'redetected this error after the issue was closed'
    r')\b',
    re.IGNORECASE,
)

# Emoji / pictographic characters (🤖 🔁 ✅ ⚠️ …). Stripped before the
# punctuation pass so they do not leave stray tokens behind.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols & pictographs
    "\U00002600-\U000027BF"   # misc symbols / dingbats
    "\U0001F600-\U0001F64F"   # emoticons
    "]+",
    flags=re.UNICODE,
)

# ISO-style timestamps: 2026-06-18 00:02:06,054  (optional comma-ms, T or space sep)
_TS_RE = re.compile(r'\b\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}:\d{2}(?:,\d+)?\b')
_PUNCT_RE = re.compile(r'[^\w\s]')
_WS_RE = re.compile(r'\s+')

# Body-level duplicate threshold. Raised from the old 0.7 to 0.85: two error
# snippets that share ~70% of their tokens are frequently DISTINCT bugs from the
# same module (same stack-trace scaffolding, different failure), so 0.7 folded
# genuinely-new bugs into an existing issue. 0.85 demands a much stronger core
# overlap before declaring a body-level duplicate.
_BODY_JACCARD_THRESHOLD = 0.85


def strip_boilerplate(text):
    """Remove the template wrapper + emoji common to every automated issue.

    Returns the text with boilerplate phrases and emoji replaced by spaces.
    Does not lowercase or touch punctuation/numbers — that is the caller's job.
    """
    if not text:
        return ""
    t = _EMOJI_RE.sub(" ", str(text))
    t = _BOILERPLATE_RE.sub(" ", t)
    return t


def _apply_aliases(token_text):
    """Map module-alias tokens to their canonical form, preserving order."""
    return " ".join(MODULE_ALIASES.get(tok, tok) for tok in token_text.split())


def _normalize_for_dedup(text):
    """Normalize text for duplicate comparison.

    Lowercases, strips emoji + the automated-issue boilerplate wrapper, strips
    full timestamps, drops punctuation, applies module aliases, and collapses
    whitespace. Log snippets that differ only by timestamp / boilerplate /
    ``opns``-vs-``opnsense`` naming compare equal, so the SAME recurring error
    (rephrased by the LLM or logged at a new time) is detected as a duplicate.

    NOTE: numbers are deliberately PRESERVED. Stripping every standalone number
    also erased the strongest discriminators between two DISTINCT errors —
    exception line numbers, error/exit codes, HTTP status codes, PIDs — so two
    unrelated bugs that differed only by such a number normalized identical and a
    genuinely-new bug got folded into an existing issue. Only whole timestamps
    (genuine noise) are removed; every other number is kept as a discriminator.
    """
    if not text:
        return ""
    t = str(text).lower()
    t = strip_boilerplate(t)
    t = _TS_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t)
    t = _apply_aliases(t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def _token_set(text):
    return set(_normalize_for_dedup(text).split())


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _body_containment_match(new_body, ex_body):
    """True when one normalized body fully contains the other — the strongest
    "same error" signal for recurring LOG snippets, and near-identical enough to
    treat the two as the same underlying bug. Length-guarded so trivially short
    bodies don't produce spurious substring hits."""
    nb = _normalize_for_dedup(new_body)
    eb = _normalize_for_dedup(ex_body)
    nb_tokens = nb.split()
    eb_tokens = eb.split()
    if not nb or not eb or len(nb_tokens) < 5 or len(eb_tokens) < 5:
        return False
    return nb in eb or eb in nb


def _body_signal_match(new_body, ex_body):
    """True when there is a BODY-level duplicate signal — containment OR high
    (>= _BODY_JACCARD_THRESHOLD) token overlap. Used to require actual body
    evidence (not a title-only coincidence) before reopening a closed issue."""
    if _body_containment_match(new_body, ex_body):
        return True
    nb_tokens = _normalize_for_dedup(new_body).split()
    eb_tokens = _normalize_for_dedup(ex_body).split()
    if len(nb_tokens) < 5 or len(eb_tokens) < 5:
        return False
    return _jaccard(set(nb_tokens), set(eb_tokens)) >= _BODY_JACCARD_THRESHOLD


def _is_duplicate_match(new_title, new_body, ex_title, ex_body):
    """Returns True if a new error matches an existing issue, using normalized +
    fuzzy comparison so LLM rephrasing, timestamp drift, boilerplate wrapper,
    and module-name variants (opns/opnsense) don't defeat dedup."""
    nt = _normalize_for_dedup(new_title)
    et = _normalize_for_dedup(ex_title)

    nt_tokens = nt.split()
    et_tokens = et.split()

    # Exact normalized title match (guard against tiny/generic titles).
    if nt and et and len(nt_tokens) >= 3 and len(et_tokens) >= 3 and nt == et:
        return True
    # Title containment (LLM added/trimmed a few words) — guard against tiny titles.
    if nt and et and len(nt_tokens) >= 3 and len(et_tokens) >= 3 and (nt in et or et in nt):
        return True
    # High title token overlap.
    if nt and et and len(nt_tokens) >= 2 and _jaccard(set(nt_tokens), set(et_tokens)) >= 0.7:
        return True
    # Body-level signal: containment (most reliable for recurring LOG errors,
    # matches even though timestamps differ) or high token overlap.
    if _body_signal_match(new_body, ex_body):
        return True
    return False