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
        "diagnostic_identify_models",
        ("scout", "claude-sonnet-5.5*", "claude-opus-5*", "claude-opus-6*"),
        "Diagnostic issues: find the files",
        "Models allowed to decide which files a health-check finding points at. The default "
        "ordinary file-finding step uses the cheapest capable model, which can pick the wrong repo."),
    "diag_fix": (
        "diagnostic_fix_models", ("coding", "claude-opus-5*", "claude-opus-6*"),
        "Diagnostic issues: write the fix",
        "Models allowed to write the first fix attempt for a health-check finding (root cause "
        "spans hub, spokes and feeders). Ordinary issues keep the normal fix ladder."),
}


# Pool tasks (free, local) and the stage each suits; shown next to the stage so the
# operator knows what to add. A pool entry's model is its task name.
SUITED = {
    "diag_identify": "Best: scout (Qwen 3.8, read-only search). Cloud: Sonnet, Opus.",
    "diag_fix": "Best: coding (Qwen 3.6 coder). Cloud: Opus-class only. Add reasoning (DeepSeek R1, slow) only to a stage that needs it.",
}


def ladder(config: Dict[str, Any], stage: str) -> list:
    """The configured models this stage may use, in the order the picker tries them.

    The picker is cost-first: free (local) models, then cheap, then frontier;
    within a tier, the order of the patterns. A model that is down or has no
    credit is skipped at call time and is not shown as such here."""
    try:
        import llm_client
        import model_registry
        cands = llm_client._enumerate_candidates(config)
    except Exception as e:  # noqa: BLE001 - a settings page must render without it
        logger.debug("stage_models: could not enumerate candidates: %s", e)
        return []
    pats = patterns(config, stage)
    rows = []
    for c in cands:
        if not _allowed(c.get("model"), pats):
            continue
        tier = (c.get("caps") or {}).get("cost_tier", "unknown")
        m = str(c.get("model") or "").lower().split("/")[-1]
        idx = next(i for i, p in enumerate(pats) if fnmatch.fnmatchcase(m, p))
        rows.append((model_registry.COST_TIER_RANK.get(tier, 99), idx, {
            "model": c.get("model"), "provider": c.get("provider"), "tier": tier,
            "where": "Local (free)" if model_registry.is_local_provider(c.get("provider")) else "Cloud"}))
    rows.sort(key=lambda r: r[:2])
    return [dict(r[2], order=n) for n, r in enumerate(rows, 1)]


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


def exclusions(config: Dict[str, Any], stage: str, existing: Iterable[Any] = (),
               fallback: Any = None) -> tuple:
    """``existing`` plus every model key outside the stage's patterns.

    When no stage model is left (none configured, or all already tried) the
    result is ``fallback`` if given, else ``existing``: emptying the pool would
    turn the escalation into a dead end. The fix step passes the ordinary write
    floor as ``fallback``; without it, retries fell to any configured model and
    a weak model that returns empty answers wrote the last attempt."""
    existing = tuple(existing or ())
    fallback = existing if fallback is None else tuple(fallback)
    try:
        import llm_client
        import model_selection
        cands = llm_client._enumerate_candidates(config)
    except Exception as e:  # noqa: BLE001 - never block fixing on a policy lookup
        logger.debug("stage_models: could not enumerate candidates: %s", e)
        return fallback
    pats = patterns(config, stage)
    # A model already tried (by key or by name) is used up.
    ok = [c for c in cands
          if _allowed(c.get("model"), pats) and not model_selection.is_excluded(c, existing)]
    if not ok:
        logger.warning("stage_models: no untried model matches %s for %s; using the normal pool",
                       list(pats), stage)
        return fallback
    out = set(existing)
    out.update(c["key"] for c in cands if not _allowed(c.get("model"), pats))
    return tuple(sorted(out, key=str))
