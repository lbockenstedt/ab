"""Per-stage model policy for diagnostic issues (hub health-check findings).

A health-check issue is a diagnosis problem: the hard part is finding which
repo and file a symptom comes from, so the file-finding step and the fix step
use stronger models than ordinary issues. Patterns are fnmatch globs matched
against the model name (a leading "vendor/" is ignored), so ``claude-opus-5*``
keeps covering new releases. Operators edit them in Settings (Model policy);
a blank field means "use the default below".
"""
import fnmatch
import logging
import re
from typing import Any, Dict, Iterable, Tuple

logger = logging.getLogger(__name__)

# Marker file_escalated_issue puts in the body of every health-check issue.
DIAGNOSTIC_MARKER = "verdict: `health-check`"

# stage key -> (config key, default patterns, label, help)
STAGES: Dict[str, Tuple[str, Tuple[str, ...], str, str]] = {
    "diag_identify": (
        "diagnostic_identify_models", ("claude-sonnet-5.5*", "claude-opus-5*", "claude-opus-6*"),
        "Diagnostic issues: find the files",
        "Models allowed to decide which files a health-check finding points at. The default "
        "ordinary file-finding step uses the cheapest capable model, which can pick the wrong repo."),
    "diag_fix": (
        "diagnostic_fix_models", ("claude-opus-5*", "claude-opus-6*"),
        "Diagnostic issues: write the fix",
        "Models allowed to write the first fix attempt for a health-check finding (root cause "
        "spans hub, spokes and feeders). Ordinary issues keep the normal fix ladder."),
}


def is_diagnostic(issue_body: Any) -> bool:
    """True when the issue was filed by the hub health check."""
    return DIAGNOSTIC_MARKER in str(issue_body or "")


def _split(raw: Any) -> Tuple[str, ...]:
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    return tuple(dict.fromkeys(str(p).strip().lower() for p in (raw or []) if str(p).strip()))


def patterns(config: Dict[str, Any], stage: str) -> Tuple[str, ...]:
    """Active patterns for a stage: the operator's list, else the default."""
    key, default, _, _ = STAGES[stage]
    raw = (config or {}).get(key)
    got = _split(raw) if raw is not None else ()
    return got or default


def _allowed(model: Any, pats: Iterable[str]) -> bool:
    m = str(model or "").lower().split("/")[-1]
    return any(fnmatch.fnmatchcase(m, p) for p in pats)


def exclusions(config: Dict[str, Any], stage: str, existing: Iterable[Any] = ()) -> tuple:
    """``existing`` plus every model key outside the stage's patterns.

    Unchanged when nothing configured matches the patterns (a hard filter that
    empties the pool would turn the escalation into a dead end), or when the
    candidates cannot be listed."""
    existing = tuple(existing or ())
    try:
        import llm_client
        cands = llm_client._enumerate_candidates(config)
    except Exception as e:  # noqa: BLE001 - never block fixing on a policy lookup
        logger.debug("stage_models: could not enumerate candidates: %s", e)
        return existing
    pats = patterns(config, stage)
    skip = set(existing)
    ok = [c for c in cands if _allowed(c.get("model"), pats) and c["key"] not in skip]
    if not ok:
        logger.warning("stage_models: no configured model matches %s for %s; using the normal pool",
                       list(pats), stage)
        return existing
    out = set(existing)
    out.update(c["key"] for c in cands if not _allowed(c.get("model"), pats))
    return tuple(sorted(out, key=str))
