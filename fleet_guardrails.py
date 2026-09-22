"""
fleet_guardrails.py — Extended Fleet Invariant & Architectural Guardrails.

Deterministic pre-checks enforcing Lab Manager fleet invariants:
  1. VERSION file protection: branch-owned, incremented only by promotion automation.
  2. Vanilla JS / No-NPM purity: dependency-free vanilla JS, no npm manifests or JSX/TSX.
  3. Global DOM container protection: prevent deletion or mutation of core DOM anchors.
  4. Subprocess shell execution safety: prohibit shell=True with variables/formatting.
  5. External CDN / egress blocker: prevent references to remote CDNs/fonts in air-gapped environments.
  6. Twin-parity enforcement: ensure agent/src/ and src/ remain identical in twin repos like pxmx.
"""
import ast
import os
import re
from typing import Any, List, Optional, Tuple

VERSION_VIOLATION = (
    "Fleet invariant violation: `VERSION` is branch-owned and incremented by promotion automation. "
    "Hand-editing `VERSION` is prohibited."
)
VANILLA_JS_VIOLATION = (
    "Fleet invariant violation: Lab Manager WebUI is dependency-free vanilla JS. "
    "No npm manifests, lockfiles, or JSX/TSX build steps are allowed."
)
DOM_PROTECTION_VIOLATION = (
    "Architectural guardrail violation: deletion or mutation of core global DOM container detected."
)
SHELL_EXEC_VIOLATION = (
    "Security guardrail violation: unsafe `shell=True` subprocess execution detected. "
    "Commands must use tokenized argument lists without shell interpolation."
)
CDN_EGRESS_VIOLATION = (
    "Security guardrail violation: external CDN or unauthenticated remote egress URL detected in isolated datacenter environment."
)

BLOCKED_CDNS = (
    "cdn.jsdelivr.net",
    "unpkg.com",
    "cdnjs.cloudflare.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)

CORE_DOM_ANCHORS = (
    "#_lmToastRegion",
    "#app",
    "window._lmToastRegion",
    "_lmToastRegion",
)


def _is_unified_diff(lines: List[str]) -> bool:
    """Return True if the lines constitute a unified diff (hunk or file headers)."""
    has_hunk = any(l.startswith("@@") for l in lines)
    has_file_headers = (
        any(l.startswith("--- ") for l in lines)
        and any(l.startswith("+++ ") for l in lines)
    )
    return has_hunk or has_file_headers


def _extract_added_lines(raw_patch: str) -> str:
    """Extract added lines from a unified diff patch."""
    if not raw_patch:
        return ""
    lines = raw_patch.splitlines()
    has_plus = any(l.startswith("+") for l in lines if not l.startswith("+++"))
    if has_plus:
        added = [l[1:] for l in lines if l.startswith("+") and not l.startswith("+++")]
        return "\n".join(added)
    if _is_unified_diff(lines):
        # Confirmed unified diff with no additions (pure deletion or context-only hunk)
        return ""
    # Non-diff raw content fallback
    return raw_patch


def _extract_removed_lines(raw_patch: str) -> str:
    """Extract removed lines from a unified diff patch."""
    if not raw_patch:
        return ""
    lines = raw_patch.splitlines()
    has_diff_markers = any(l.startswith("-") for l in lines if not l.startswith("---"))
    if has_diff_markers:
        removed = [l[1:] for l in lines if l.startswith("-") and not l.startswith("---")]
        return "\n".join(removed)
    return ""


def _is_unsafe_subprocess_call(added_text: str) -> bool:
    """Scan added Python code for unsafe subprocess calls with shell=True."""
    if "shell" not in added_text or "True" not in added_text:
        return False

    shell_true_pattern = re.compile(r"\bshell\s*=\s*True\b")
    if not shell_true_pattern.search(added_text):
        return False

    # Check for subprocess calls
    subproc_pattern = re.compile(
        r"subprocess\.(?:Popen|run|check_output|check_call|call)\s*\((.*?)\)",
        re.DOTALL,
    )
    matches = list(subproc_pattern.finditer(added_text))
    if not matches:
        # If shell=True is present with subprocess reference in added lines
        if "subprocess." in added_text:
            return True
        return False

    for m in matches:
        call_content = m.group(1).strip()
        if not shell_true_pattern.search(call_content):
            continue

        # Extract first argument (command)
        first_arg = call_content.split(",")[0].strip()

        # Unsafe if f-string, format call, % format, concatenation (+), or variable identifier
        if first_arg.startswith("f'") or first_arg.startswith('f"') or 'f"""' in first_arg:
            return True
        if "%" in first_arg or ".format(" in first_arg or "+" in first_arg:
            return True

        # Check if first_arg is a static string literal without variables
        is_literal_str = (
            (first_arg.startswith('"') and first_arg.endswith('"') and len(first_arg) >= 2)
            or (first_arg.startswith("'") and first_arg.endswith("'") and len(first_arg) >= 2)
        )
        if not is_literal_str:
            # It's a variable or expression
            return True

    return False


def _is_dom_mutation_or_deletion(raw_patch: str) -> bool:
    """Inspect JS diff patch for removal or overwrite of critical global DOM elements."""
    if not raw_patch:
        return False

    removed_text = _extract_removed_lines(raw_patch)
    added_text = _extract_added_lines(raw_patch)

    mutation_patterns = (
        r"(?:document\.(?:getElementById|querySelector)\s*\(\s*['\"]#?_lmToastRegion['\"]\s*\)|window\._lmToastRegion|_lmToastRegion)\s*\.(?:remove|empty)\s*\(",
        r"(?:document\.(?:getElementById|querySelector)\s*\(\s*['\"]#?_lmToastRegion['\"]\s*\)|window\._lmToastRegion|_lmToastRegion)\s*\.(?:innerHTML|textContent)\s*=\s*['\"]\s*['\"]",
        r"window\._lmToastRegion\s*=\s*(?:null|undefined|false)",
        r"delete\s+window\._lmToastRegion",
        r"(?:document\.(?:getElementById|querySelector)\s*\(\s*['\"]#app['\"]\s*\)|#app)\s*\.(?:remove|empty)\s*\(",
    )

    # 1. Removal of DOM anchors from existing code (excluding lines deleting mutation calls)
    clean_removed = [
        line for line in removed_text.splitlines()
        if not any(re.search(pat, line) for pat in mutation_patterns)
    ]
    clean_removed_text = "\n".join(clean_removed)

    added_only = "\n".join(
        l[1:] for l in raw_patch.splitlines()
        if l.startswith("+") and not l.startswith("+++")
    )
    for anchor in CORE_DOM_ANCHORS:
        if anchor in clean_removed_text and anchor not in added_only:
            return True

    # 2. Overwrite / deletion calls in added lines
    for pat in mutation_patterns:
        for line in added_text.splitlines():
            if line.startswith("-"):
                continue
            if re.search(pat, line):
                return True

    return False


def check_fleet_invariants(
    pr: Any,
    files: Optional[List[Any]] = None,
    changed_paths: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str]]:
    """Deterministic validation of Lab Manager fleet invariants and guardrails.

    Checks:
      a) VERSION protection: Any path where os.path.basename(p) == "VERSION" or p.endswith("/VERSION").
      b) Vanilla JS / No-NPM Purity: Any path named package.json, package-lock.json, in node_modules/,
         or ending in .jsx/.tsx in WebUI/.
      c) Global DOM Protection: In JS/UI files, inspect patch for removal or overwrite of critical
         global DOM elements (#_lmToastRegion, #app, window._lmToastRegion).
      d) Unsafe Subprocess Shell Execution: In Python files, scan added patch lines for shell=True in
         subprocess calls with dynamic formatting or variables.
      e) External CDN / Egress Blocker: Scan patch lines for external CDN or hosted URLs.
      f) Twin-Parity Check: For PRs on repositories like pxmx, if files in agent/src/ are modified,
         check if the corresponding twin file in src/ is present in the PR files and identical.

    Returns:
      (True, None) if clean.
      (False, violation_message) if any guardrail is violated.
    """
    paths = list(changed_paths or [])
    if not paths and files:
        paths = [getattr(f, "filename", "") for f in files if getattr(f, "filename", "")]

    head_ref = getattr(pr, "head_ref", None) or getattr(getattr(pr, "head", None), "ref", "") or ""
    pr_title = str(getattr(pr, "title", "") or "").lower()
    pr_user = str(getattr(getattr(pr, "user", None), "login", "") or "").lower()
    is_promotion = (
        head_ref.startswith("promote/")
        or pr_title.startswith("promote:")
        or "promote-bot" in pr_user
    )

    # a) VERSION protection
    if not is_promotion:
        for p in paths:
            bname = os.path.basename(p)
            if bname == "VERSION" or p.endswith("/VERSION") or p == "VERSION":
                return False, VERSION_VIOLATION

    # b) Vanilla JS / No-NPM Purity
    for p in paths:
        bname = os.path.basename(p)
        p_lower = p.lower()
        if bname in ("package.json", "package-lock.json"):
            return False, VANILLA_JS_VIOLATION
        if "node_modules/" in p or p.startswith("node_modules/"):
            return False, VANILLA_JS_VIOLATION
        if (p.endswith(".jsx") or p.endswith(".tsx")) and ("webui" in p_lower or p_lower.startswith("webui")):
            return False, VANILLA_JS_VIOLATION

    # Iterate over files for content/patch based checks
    for f in (files or []):
        filename = getattr(f, "filename", "") or ""
        raw_patch = getattr(f, "patch", None) or ""
        fn_lower = filename.lower()

        # c) Global DOM Protection in JS / WebUI files
        is_ui_file = fn_lower.endswith(".js") or fn_lower.endswith(".html") or "webui" in fn_lower or "/ui/" in fn_lower
        if is_ui_file and raw_patch:
            if _is_dom_mutation_or_deletion(raw_patch):
                return False, DOM_PROTECTION_VIOLATION

        # d) Unsafe Subprocess Shell Execution in Python files
        if fn_lower.endswith(".py") and raw_patch:
            added_patch = _extract_added_lines(raw_patch)
            if _is_unsafe_subprocess_call(added_patch):
                return False, SHELL_EXEC_VIOLATION

        # e) External CDN / Egress Blocker
        if raw_patch:
            added_patch = _extract_added_lines(raw_patch)
            for cdn in BLOCKED_CDNS:
                if cdn in added_patch:
                    return False, CDN_EGRESS_VIOLATION

    # f) Twin-Parity Check for pxmx
    repo_name = ""
    if pr is not None:
        repo_name = getattr(pr, "repo_name", "") or ""
        if not repo_name:
            repo_obj = getattr(pr, "repo", None) or getattr(getattr(pr, "base", None), "repo", None)
            if repo_obj:
                repo_name = getattr(repo_obj, "full_name", "") or getattr(repo_obj, "name", "")
        if not repo_name and isinstance(pr, str):
            repo_name = pr

    if "pxmx" in str(repo_name).lower() and files:
        file_map = {getattr(f, "filename", ""): f for f in files}
        for fn, f in file_map.items():
            if fn.startswith("agent/src/"):
                twin_path = "src/" + fn[len("agent/src/"):]
                if twin_path not in file_map:
                    return False, f"Twin-parity violation: modified file `{fn}` is missing matching twin `{twin_path}` in pxmx."
                twin_f = file_map[twin_path]
                if (getattr(f, "patch", "") or "") != (getattr(twin_f, "patch", "") or ""):
                    return False, f"Twin-parity violation: twin files `{fn}` and `{twin_path}` diff contents are not identical."
            elif fn.startswith("src/"):
                twin_path = "agent/src/" + fn[len("src/"):]
                if twin_path not in file_map:
                    return False, f"Twin-parity violation: modified file `{fn}` is missing matching twin `{twin_path}` in pxmx."
                twin_f = file_map[twin_path]
                if (getattr(f, "patch", "") or "") != (getattr(twin_f, "patch", "") or ""):
                    return False, f"Twin-parity violation: twin files `{fn}` and `{twin_path}` diff contents are not identical."

    return True, None
