"""
pr_remediate.py — Closed-loop PR auto-remediation engine with
skeptical-review-driven model tiering & escalation.

Deterministic pre-check guardrails, complexity assessment, model
escalation, and PR remediation orchestration.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import feature_boundary
import secrets_scan

try:
    from model_selection import LlmRequirements
except Exception:  # pragma: no cover
    LlmRequirements = None

try:
    from fix_engine import _COMPLEXITY_RANK_ORDER
except Exception:
    _COMPLEXITY_RANK_ORDER = ("trivial", "small", "medium", "large")

try:
    from app_state import state, update_pr_review
except Exception:  # pragma: no cover
    state = {}

    def update_pr_review(repo, number, **fields):
        key = f"{repo}#{number}"
        rec = state.setdefault("pr_reviews", {}).setdefault(key, {})
        rec.update(fields)
        return True

logger = logging.getLogger("pr_remediate")

TARGET_GUARDRAIL_BOUNDARIES = {
    "psk-hardcode",
    "transport-scheme",
    "rbac-model",
    "state-encryption",
    "auth-onboarding",
    "self-update",
}

REVERSE_SHELL_RE = re.compile(
    r"(?i)(?:nc(?:\.traditional)?\s+-[el]|/bin/(?:ba)?sh\s+-i|bash\s+-i\s+>&|pty\.spawn|socket\.connect\s*\(\s*\(\s*['\"][0-9.]+|socat\s+exec:)"
)
PIPED_SHELL_RE = re.compile(
    r"(?i)(?:curl\s+-[^\n]*\|\s*(?:ba)?sh|wget\s+-[^\n]*\|\s*(?:ba)?sh)"
)
DYNAMIC_EXEC_RE = re.compile(
    r"(?i)(?:exec\s*\(\s*base64\.b64decode|eval\s*\(\s*base64\.b64decode|marshal\.loads\b)"
)
TLS_VERIFY_BYPASS_RE = re.compile(
    r"(?i)(?:ssl_verify\s*=\s*False|check_hostname\s*=\s*False|CERT_NONE|\bverify\s*=\s*False\b)"
)
PSK_HARDCODE_RE = re.compile(
    r"(?i)\b(?:psk|pre[-_]?shared[-_]?key|shared[-_]?secret)\b\s*[:=]\s*['\"][^'\"]+['\"]"
)


def _extract_added_lines(raw_patch: str) -> str:
    """Extract only added diff lines from unified diff patch, or fallback to raw string."""
    if not raw_patch:
        return ""
    lines = raw_patch.splitlines()
    has_diff_markers = any(l.startswith("+") for l in lines if not l.startswith("+++"))
    if has_diff_markers:
        added = [l[1:] for l in lines if l.startswith("+") and not l.startswith("+++")]
        return "\n".join(added)
    return raw_patch


def check_pr_guardrails(
    pr: Any,
    files: Optional[List[Any]] = None,
    changed_paths: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """Deterministic security and architectural boundary pre-checks for automated PR remediation.

    Checks:
      1. Deterministic boundary matching against DEFAULT_BOUNDARIES (psk-hardcode,
         transport-scheme, rbac-model, state-encryption, auth-onboarding, self-update).
      2. Credential checks using secrets_scan.check_secrets(files) (level == "error").
      3. Patch inspection of added diff lines for reverse shells, piped downloaders,
         dynamic eval/exec, and TLS verify bypass in security/agent/spoke/transport paths.

    Returns:
      (True, None) if clean.
      (False, violation_reason) if any guardrail is violated.
    """
    config = config or {}
    paths = list(changed_paths or [])
    if not paths and files:
        paths = [getattr(f, "filename", "") for f in files if getattr(f, "filename", "")]

    # 1. Deterministic boundary checks against target DEFAULT_BOUNDARIES
    all_boundaries = (
        config.get("feature_boundaries")
        if isinstance(config.get("feature_boundaries"), list) and config.get("feature_boundaries")
        else getattr(feature_boundary, "DEFAULT_BOUNDARIES", [])
    )
    guardrail_boundaries = [
        b for b in all_boundaries
        if b.get("id") in TARGET_GUARDRAIL_BOUNDARIES
    ]
    hits = feature_boundary.boundary_hits(paths, guardrail_boundaries)
    if hits:
        ids = ", ".join(h.get("id", "?") for h in hits)
        return False, f"Diff touches protected architectural boundary: {ids}"

    # 2. Credential checks using secrets_scan.check_secrets(files)
    if files:
        try:
            findings = secrets_scan.check_secrets(files)
            error_findings = [f for f in findings if f.get("level") == "error"]
            if error_findings:
                first = error_findings[0]
                title = first.get("title") or "Hardcoded secret detected"
                return False, f"Credential check violation: {title}"
        except Exception as e:
            logger.warning("check_pr_guardrails: secrets_scan encountered error: %s", e)

    # 3. Patch inspection of added diff lines
    for f in (files or []):
        filename = getattr(f, "filename", "") or ""
        raw_patch = getattr(f, "patch", None)
        if not raw_patch:
            continue
        patch = _extract_added_lines(raw_patch)

        # Reverse shell check
        if REVERSE_SHELL_RE.search(patch):
            return False, f"Security guardrail violation: reverse shell pattern detected in `{filename}`"

        # Piped shell downloader check
        if PIPED_SHELL_RE.search(patch):
            return False, f"Security guardrail violation: piped shell downloader detected in `{filename}`"

        # Dynamic eval/exec check
        if DYNAMIC_EXEC_RE.search(patch):
            return False, f"Security guardrail violation: dynamic code execution pattern detected in `{filename}`"

        # Direct PSK / shared secret pattern check in patch
        if PSK_HARDCODE_RE.search(patch):
            return False, f"Security guardrail violation: hardcoded PSK or shared secret detected in `{filename}`"

        # TLS verify bypass check in sensitive paths
        fn_lower = filename.lower()
        is_sensitive_path = any(term in fn_lower for term in ("security", "agent", "spoke", "transport"))
        has_sensitive_context = is_sensitive_path or any(
            any(term in p.lower() for term in ("security", "agent", "spoke", "transport"))
            for p in paths
        )
        if has_sensitive_context and TLS_VERIFY_BYPASS_RE.search(patch):
            return False, f"Security guardrail violation: TLS verification bypass in sensitive path `{filename}`"

    return True, None


def assess_fix_complexity(
    critique: Optional[str] = "",
    dissent_feedback: Optional[str] = "",
    findings: Optional[List[Dict[str, Any]]] = None,
    files: Optional[List[Any]] = None,
    confidence: Optional[float] = None,
) -> str:
    """Assess the complexity tier required for PR auto-remediation.

    - Low / small complexity ("small"):
      Findings are only tooltips/lint/docs, or critique text contains "minor", "nit",
      "formatting", "typo", "docstring", "comment", or single-file change with
      confidence >= 0.85 and no "reject"/"deny".
    - Medium complexity ("medium"):
      Critique or dissent mentions "control-flow", "pagination", "caching", "parsing",
      "exception", "regex", "status", "enum", or 1-3 files touched with confidence
      between 0.60 and 0.85.
    - Large / frontier complexity ("large"):
      Critique or dissent mentions "concurrency", "race condition", "deadlock",
      "state conflation", "reachability", "refactor", or confidence < 0.60 or explicit
      "reject"/"deny", or > 3 files modified.
    """
    critique_str = critique or ""
    dissent_str = dissent_feedback or ""
    text = f"{critique_str}\n{dissent_str}".lower()

    if confidence is None:
        conf_match = re.search(r"(?i)(?:confidence|score)[:\s]+(0\.\d+|\d+%)", text)
        if conf_match:
            raw_c = conf_match.group(1)
            if raw_c.endswith("%"):
                confidence = float(raw_c[:-1]) / 100.0
            else:
                confidence = float(raw_c)

    file_count = len(files) if files is not None else 0

    # Large / frontier complexity
    large_keywords = ("concurrency", "race condition", "deadlock", "state conflation", "reachability", "refactor")
    has_large_kw = any(kw in text for kw in large_keywords)
    has_reject_deny = bool(re.search(r"\b(reject|deny)\b", text))
    low_conf = (confidence is not None and confidence < 0.60)
    many_files = file_count > 3

    if has_large_kw or has_reject_deny or low_conf or many_files:
        return "large"

    # Medium complexity
    medium_keywords = ("control-flow", "pagination", "caching", "parsing", "exception", "regex", "status", "enum")
    has_medium_kw = any(kw in text for kw in medium_keywords)
    med_conf = (confidence is not None and 0.60 <= confidence < 0.85 and 1 <= file_count <= 3)

    if has_medium_kw or med_conf:
        return "medium"

    # Small complexity
    small_keywords = ("minor", "nit", "formatting", "typo", "docstring", "comment")
    has_small_kw = any(kw in text for kw in small_keywords)

    findings_only_minor = False
    if findings:
        minor_terms = ("tooltip", "lint", "doc", "docstring", "comment", "formatting", "typo")
        findings_only_minor = all(
            any(term in (f.get("title", "") + " " + f.get("detail", "")).lower() for term in minor_terms)
            for f in findings
        )

    single_file_high_conf = (file_count == 1 and (confidence is not None and confidence >= 0.85))

    if findings_only_minor or has_small_kw or single_file_high_conf:
        return "small"

    if file_count > 1:
        return "medium"
    return "small"


def next_remediation_requirements(
    prev_reqs: Any,
    failure_kind: Optional[str] = None,
    tried_key: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Any:
    """Step up model complexity tier and exclude tried model on remediation failure."""
    exclude = set(getattr(prev_reqs, "exclude_models", ()) or ())
    if tried_key:
        exclude.add(tried_key)

    complexity = getattr(prev_reqs, "complexity", "small")
    try:
        idx = _COMPLEXITY_RANK_ORDER.index(complexity)
    except ValueError:
        idx = 1
    next_complexity = _COMPLEXITY_RANK_ORDER[min(idx + 1, len(_COMPLEXITY_RANK_ORDER) - 1)]

    from model_selection import LlmRequirements as _ReqClass
    return _ReqClass(
        complexity=next_complexity,
        needs_structured_output=getattr(prev_reqs, "needs_structured_output", True),
        min_context_tokens=getattr(prev_reqs, "min_context_tokens", 0),
        restrict=getattr(prev_reqs, "restrict", None),
        must_escalate_to_human=getattr(prev_reqs, "must_escalate_to_human", False),
        exclude_models=tuple(sorted(exclude)),
    )


def auto_remediate_pr(
    gh: Any,
    repo: Any,
    pr: Any,
    config: Optional[Dict[str, Any]] = None,
    fix_fn: Optional[Any] = None,
) -> Tuple[bool, str]:
    """Execute automated closed-loop remediation for an open PR with review findings."""
    config = config or {}
    repo_full_name = getattr(repo, "full_name", str(repo))
    key = f"{repo_full_name}#{pr.number}"

    if "pr_reviews" not in state:
        state["pr_reviews"] = {}
    if key not in state["pr_reviews"]:
        state["pr_reviews"][key] = {}
    rec = state["pr_reviews"][key]

    files = list(pr.get_files())
    changed_paths = [getattr(f, "filename", "") for f in files if getattr(f, "filename", "")]

    # Guardrails check
    passed, violation = check_pr_guardrails(pr, files, changed_paths, config)
    if not passed:
        banner = (
            "🚨 **AppBuilder — Security Guardrail Violation Detected**\n\n"
            "Automated remediation is strictly blocked because this PR touches security-sensitive invariants or contains potential security risks:\n\n"
            f"- **Violation**: {violation}\n\n"
            "**Action**: This PR has been locked from automated fixes and requires mandatory human review."
        )
        try:
            pr.create_issue_comment(banner)
        except Exception as e:
            logger.warning("auto_remediate_pr: failed to post guardrail comment on %s: %s", key, e)

        update_pr_review(repo_full_name, pr.number, guardrail_violation=violation, auto_remediate_blocked=True)
        return False, f"Guardrail violation: {violation}"

    # Attempt ceiling check
    attempts = rec.get("remediation_attempts", 0)
    max_attempts = int(config.get("pr_auto_remediate_max_attempts", 3))
    if attempts >= max_attempts:
        exhausted_comment = (
            "🤖 **AppBuilder — Automated Remediation Limit Reached**\n\n"
            f"AppBuilder attempted to remediate review findings {attempts} times but was unable to resolve all critiques. Leaving for human review."
        )
        try:
            pr.create_issue_comment(exhausted_comment)
        except Exception as e:
            logger.warning("auto_remediate_pr: failed to post attempt ceiling comment on %s: %s", key, e)

        update_pr_review(repo_full_name, pr.number, auto_remediate_status="exhausted_human_review")
        return False, "Remediation attempt limit reached"

    # Assess complexity & requirements
    critique = rec.get("panel_critique") or ""
    dissent_feedback = rec.get("dissent_feedback") or ""
    if not dissent_feedback:
        try:
            import pr_review_score
            report, body = pr_review_score.read_pr_review(pr)
            dissent_feedback = pr_review_score.dissent_feedback(report, body).strip()
        except Exception:
            pass

    findings = rec.get("findings") or []
    assessed_complexity = assess_fix_complexity(critique, dissent_feedback, findings, files)

    from model_selection import LlmRequirements as _ReqClass
    prev_complexity = rec.get("last_remediation_complexity")
    last_tried_model = rec.get("last_remediation_model")

    if attempts > 0 and prev_complexity:
        prev_reqs = _ReqClass(
            complexity=prev_complexity,
            needs_structured_output=True,
            exclude_models=tuple(rec.get("excluded_models") or []),
        )
        reqs = next_remediation_requirements(prev_reqs, failure_kind="retry", tried_key=last_tried_model, config=config)
    else:
        reqs = _ReqClass(complexity=assessed_complexity, needs_structured_output=True)

    # Invoke fix_one_pr
    if fix_fn is None:
        from pr_review import fix_one_pr as default_fix_fn
        fix_fn = default_fix_fn

    used_model_out: Dict[str, Any] = {}
    try:
        success, msg = fix_fn(
            repo_full_name,
            pr.number,
            config=config,
            requirements=reqs,
            used_model_out=used_model_out,
        )
    except Exception as e:
        success = False
        msg = f"Fix invocation failed: {e}"

    used_model = used_model_out.get("model") or used_model_out.get("key")
    new_attempts = attempts + 1

    if success:
        update_pr_review(
            repo_full_name,
            pr.number,
            remediation_attempts=new_attempts,
            auto_remediate_status="success",
            last_remediation_complexity=reqs.complexity,
            last_remediation_model=used_model,
        )
        logger.info("auto_remediate_pr: remediation succeeded for %s: %s", key, msg)
        return True, msg
    else:
        excluded = list(rec.get("excluded_models") or [])
        if used_model and used_model not in excluded:
            excluded.append(used_model)
        update_pr_review(
            repo_full_name,
            pr.number,
            remediation_attempts=new_attempts,
            auto_remediate_status="failed",
            auto_remediate_failure=msg,
            last_remediation_complexity=reqs.complexity,
            last_remediation_model=used_model,
            excluded_models=excluded,
        )
        logger.warning("auto_remediate_pr: remediation failed for %s: %s", key, msg)
        return False, msg


def maybe_auto_remediate(
    gh: Any,
    repo: Any,
    pr: Any,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Entry point called from pr_review._review_one when PR is open, not merged, and auto-merge did not merge the PR."""
    config = config or {}
    if not config.get("pr_auto_remediate_enabled", True):
        return False, "pr_auto_remediate_enabled is false"
    if getattr(pr, "draft", False):
        return False, "PR is a draft"
    if getattr(pr, "merged", False):
        return False, "PR is already merged"
    if (getattr(pr, "state", "") or "open").lower() != "open":
        return False, "PR is not open"

    repo_full_name = getattr(repo, "full_name", str(repo))
    key = f"{repo_full_name}#{pr.number}"
    rec = (state.get("pr_reviews") or {}).get(key) or {}

    if rec.get("auto_merged"):
        return False, "PR was auto-merged"
    if rec.get("auto_remediate_blocked"):
        return False, "PR remediation is blocked by guardrail"
    if rec.get("auto_remediate_status") == "exhausted_human_review":
        return False, "PR remediation attempt limit reached"

    return auto_remediate_pr(gh, repo, pr, config)
