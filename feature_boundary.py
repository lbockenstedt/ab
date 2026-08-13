"""
feature_boundary.py — pure, standalone boundary-matching for BugFixer's
feature auto-drive classifier (feature_drive.py) and auto-merge gate
(pr_review.py's _automerge_decision).

No app imports (like secrets_scan.py / check_unattended_mutation.py) — this
module is the deterministic "Stage A" prefilter that runs before any LLM
call, and is ALSO reused, unmodified, as the auto-merge gate's re-check
against the actual PR diff. Being import-light keeps both call sites cheap
and keeps this file directly unit-testable outside the running app.

A "boundary" is one operator-authored rule describing something BugFixer
must never build without a human — e.g. "never hardcode a PSK for spoke-hub
comms". The list is stored in BugFixer's own config (key: feature_boundaries)
so the operator can add/remove/adjust it from Settings without a commit; see
DEFAULT_BOUNDARIES below for the seeded starting draft.

Boundary record shape (dict):
    {
        "id": "psk-hardcode",           # stable slug, used in flag comments
        "label": "Hardcoded credentials / PSKs",   # short human title
        "rule": "Never hardcode a PSK...",          # full sentence, quoted verbatim in flag comments
        "paths": ["**/hub_agent.py", "lm/core/src/security/**"],  # fnmatch globs
        "keywords": ["psk", "pre-shared", "hardcode", "shared secret"],
        "hard": true,      # True: any hit is an immediate flag, no LLM call
        "enabled": true,   # False: rule is kept but never evaluated
    }
"""
import fnmatch
import re

# Seeded starting draft — deliberately opinionated, not exhaustive. The
# operator owns this list from Settings once feature auto-drive ships; this
# constant only supplies config.setdefault's initial value for a fresh /
# upgrading install so the classifier isn't running with zero rules on day
# one. Drawn from this project's own standing architectural invariants.
DEFAULT_BOUNDARIES = [
    {
        "id": "psk-hardcode",
        "label": "Hardcoded credentials / PSKs / secrets",
        "rule": "Never hardcode a PSK, API key, password, or shared secret. "
                "Spoke↔hub auth material comes from the signed handshake "
                "(mTLS + HMAC signing), never a literal constant in code.",
        "paths": ["**/hub_agent.py", "**/security/**", "**/*signer*", "**/*mtls*"],
        "keywords": ["psk", "pre-shared key", "preshared key", "hardcode", "hard-code",
                     "shared secret", "hardcoded password", "hardcoded key"],
        "hard": True, "enabled": True,
    },
    {
        "id": "transport-scheme",
        "label": "Hub↔spoke transport / TLS scheme",
        "rule": "Never change the unified :443 WS+TLS transport, the verify-off "
                "loopback / verify-on remote split, or the message-signing scheme "
                "without a human designing the change.",
        "paths": ["**/hub_agent.py", "**/client.py", "**/security/signer.py", "**/ws_*"],
        "keywords": ["mtls", "wss://", "certificate", "signing scheme", "hmac"],
        "hard": True, "enabled": True,
    },
    {
        "id": "rbac-model",
        "label": "RBAC / permission model",
        "rule": "Never widen who can see or write tenant data, or change the "
                "is_admin / tenant_admin / read_scope / write_scope model, "
                "without a human designing the change.",
        "paths": ["**/rbac*", "**/auth*.py", "**/permissions*"],
        "keywords": ["is_admin", "tenant_admin", "read_scope", "write_scope", "rbac"],
        "hard": True, "enabled": True,
    },
    {
        "id": "logging-pipeline",
        "label": "Logging / log-relay infrastructure",
        "rule": "Never change how logs are captured, relayed, or persisted "
                "(HubLogHandler, log-shipping, the hub-log local sync) as a "
                "side effect of an unrelated feature.",
        "paths": ["**/logging_setup.py", "**/log_scan.py", "**/*log_handler*"],
        "keywords": ["loghandler", "log relay", "log pipeline"],
        "hard": True, "enabled": True,
    },
    {
        "id": "self-update",
        "label": "Self-update / watchdog mechanism",
        "rule": "Never change the self-update, watchdog, or rollback machinery "
                "— a bug here can brick a fleet-wide update.",
        "paths": ["**/watchdog.py", "**/update.sh", "**/update_recovery.py", "**/*self_update*"],
        "keywords": ["self-update", "watchdog", "hard reset", "git reset --hard"],
        "hard": True, "enabled": True,
    },
    {
        "id": "state-encryption",
        "label": "State encryption (Fernet) / data directory",
        "rule": "Never change how persisted state is encrypted or where it "
                "lives without a human designing the change.",
        "paths": ["**/state_manager*", "**/*fernet*"],
        "keywords": ["fernet", "encryption key", "data_dir"],
        "hard": True, "enabled": True,
    },
]


def _keyword_hit(text, keyword):
    """Case-folded whole-phrase match. Keywords may contain spaces (they are
    phrases, not single tokens), so this is a literal substring match on
    already-casefolded text rather than a \\b-word regex, which would fail on
    multi-word keywords like 'pre-shared key'."""
    return keyword.casefold() in text


def _path_hit(path, patterns):
    """True if `path` matches any fnmatch glob in `patterns`. fnmatch's `*`
    already matches path separators (it has no concept of '/' as special),
    so '**/x.py' and '*/x.py' behave identically here — '**' is kept in
    DEFAULT_BOUNDARIES purely as the conventional glob spelling operators
    will expect to type, not because this module treats it specially."""
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def boundary_hits(paths, boundaries):
    """Match a list of concrete file paths (e.g. an actual PR diff's changed
    files) against `boundaries`. Returns the list of matched boundary records
    (enabled only), each with a `_matched_paths` key added listing which of
    `paths` triggered it. Used both by prefilter() (path tokens mentioned in
    an issue) and by pr_review._automerge_decision (the REAL diff, which is
    the only check that actually gates auto-merge)."""
    hits = []
    for b in boundaries or []:
        if not b.get("enabled", True):
            continue
        matched = [p for p in (paths or []) if _path_hit(p, b.get("paths") or [])]
        if matched:
            hits.append({**b, "_matched_paths": matched})
    return hits


def prefilter(title, body, boundaries):
    """Deterministic Stage A — zero LLM cost, always runs first. Extracts no
    real file list (an issue body isn't a diff), so this only does a coarse
    keyword scan over the free text plus a path-glob check against any
    path-shaped tokens actually mentioned in the text. Returns:
        {"hard": bool, "hits": [boundary...], "soft_hits": [boundary...]}
    `hard=True` means: flag immediately, skip the LLM call entirely. A soft
    hit (keyword matched but no `hard` boundary) is evidence handed to the
    LLM classifier, not a verdict by itself — a legitimate bolt-on request
    can innocently share a word with a boundary's keyword list."""
    text = f"{title or ''}\n{body or ''}"
    text_cf = text.casefold()

    # Path-shaped tokens: anything that looks like a repo-relative path
    # (contains a '/' and a plausible extension or module segment), so a
    # request that pastes a stack trace or file reference gets checked.
    path_tokens = re.findall(r"[A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+", text)

    hits, soft_hits = [], []
    for b in boundaries or []:
        if not b.get("enabled", True):
            continue
        kw_hit = any(_keyword_hit(text_cf, kw) for kw in (b.get("keywords") or []))
        path_hit = bool(path_tokens) and _path_hit_any(path_tokens, b.get("paths") or [])
        if not (kw_hit or path_hit):
            continue
        record = {**b, "_kw_hit": kw_hit, "_path_hit": path_hit}
        if b.get("hard"):
            hits.append(record)
        else:
            soft_hits.append(record)
    return {"hard": bool(hits), "hits": hits, "soft_hits": soft_hits}


def _path_hit_any(paths, patterns):
    return any(_path_hit(p, patterns) for p in paths)


def render_boundaries_for_prompt(boundaries, max_chars=4000):
    """Render enabled boundaries as a numbered rule list for injection into
    the classifier LLM prompt. Empty string when there are no rules (an
    empty boundary list means the operator hasn't configured any — the
    caller should treat that as "nothing to check", not an error)."""
    enabled = [b for b in (boundaries or []) if b.get("enabled", True)]
    if not enabled:
        return ""
    lines = ["## Boundaries — flag (do not build) if the request requires touching any of these:"]
    for i, b in enumerate(enabled, 1):
        lines.append(f"{i}. [{b.get('id', '?')}] {b.get('rule', b.get('label', ''))}")
    return "\n".join(lines)[:max_chars]
