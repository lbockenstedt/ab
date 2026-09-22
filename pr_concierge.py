"""
pr_concierge.py — Developer Concierge & Plain-English Remediation Guidance.

Translates technical guardrail violations and review panel critiques into
operator-friendly markdown with clear architectural context and copy-paste
ready AI assistant prompts.
"""
from typing import List, Optional


def generate_user_guidance(
    repo_name: str,
    pr_number: int,
    violation: Optional[str] = None,
    review_report: Optional[str] = None,
    attempts: int = 0,
    max_attempts: int = 3,
    changed_files: Optional[List[str]] = None,
) -> str:
    """Generate developer-friendly markdown explaining guardrails, architectural context,

    and an IDE-ready copy-paste prompt.
    """
    violation_str = violation or ""
    report_str = review_report or ""

    # 1. 🚨 What Was Flagged
    flagged_desc = ""
    why_rule_exists = ""
    specific_action = ""

    v_lower = violation_str.lower()

    if "version" in v_lower:
        flagged_desc = (
            "The Pull Request modified the `VERSION` file. Hand-editing `VERSION` is prohibited."
        )
        why_rule_exists = (
            "In the Lab Manager hub-and-spoke ecosystem, `VERSION` is strictly branch-owned "
            "and monotonically incremented by promotion automation (`promote.sh`). Manual edits cause "
            "branch divergence between `dev`, `qa`, and `main`, corrupting release telemetry and version tracking."
        )
        specific_action = "Revert all modifications to `VERSION` (`git checkout origin/dev -- VERSION`)."

    elif "vanilla js" in v_lower or "package.json" in v_lower or "node_modules" in v_lower or "jsx" in v_lower:
        flagged_desc = (
            "The Pull Request introduced npm manifests (`package.json`, `package-lock.json`), "
            "`node_modules/`, or JSX/TSX build artifacts into the WebUI."
        )
        why_rule_exists = (
            "Lab Manager's WebUI is dependency-free vanilla JS (ES6+). Zero build steps or npm packages "
            "are allowed anywhere in the fleet. This ensures spokes can be served directly from Python FastAPI "
            "or embedded servers in air-gapped datacenter infrastructure with zero supply chain risk."
        )
        specific_action = (
            "Remove all `package.json`, `node_modules/`, and `.jsx`/`.tsx` files. "
            "Implement UI components using native browser DOM APIs and vanilla ES6 modules."
        )

    elif "dom container" in v_lower or "toast" in v_lower or "#app" in v_lower or "_lmtoastregion" in v_lower:
        flagged_desc = (
            "The Pull Request removed or mutated a core global DOM container (`#_lmToastRegion`, `#app`, or `window._lmToastRegion`)."
        )
        why_rule_exists = (
            "Global DOM containers are system-wide lifecycle anchors. Specifically, `#_lmToastRegion` is "
            "required by the hub notification and telemetry layer to surface operational alerts across all views. "
            "Mutating or deleting these elements breaks global alerting and UI navigation."
        )
        specific_action = (
            "Preserve `#_lmToastRegion` and `#app` intact. Mount views and widgets into designated child containers "
            "without overwriting the global toast host."
        )

    elif "shell=true" in v_lower or "subprocess" in v_lower:
        flagged_desc = (
            "The Pull Request introduced unsafe `subprocess` execution using `shell=True` with string formatting or variable interpolation."
        )
        why_rule_exists = (
            "Spokes run with host or container privileges in datacenter networks. Shell interpolation (`shell=True`) "
            "exposes the spoke to shell injection attacks. Fleet policy mandates tokenized argument lists (`['cmd', 'arg1', ...]`) "
            "with default `shell=False`."
        )
        specific_action = (
            "Replace `subprocess.run(..., shell=True)` with a list of arguments: `subprocess.run(['command', arg1, arg2])`."
        )

    elif "cdn" in v_lower or "egress" in v_lower:
        flagged_desc = (
            "The Pull Request included links or references to external public CDNs (e.g. jsdelivr, unpkg, cdnjs, Google Fonts)."
        )
        why_rule_exists = (
            "Lab Manager runs in air-gapped, isolated datacenter environments where outbound internet egress is strictly blocked. "
            "Referencing public CDNs causes immediate asset load failures and violates security boundary rules."
        )
        specific_action = "Remove external CDN references. Vendor any required assets locally within the spoke repository or use inline SVG/CSS."

    elif "twin" in v_lower:
        flagged_desc = (
            f"Twin drift was detected: {violation_str}"
        )
        why_rule_exists = (
            "In twin repositories like `pxmx`, host-side agents (`agent/src/`) and spoke services (`src/`) share identical "
            "wire contracts, discovery algorithms, and utility helpers. Changes must remain byte-identical across twins to avoid protocol mismatch."
        )
        specific_action = "Mirror all changes between `agent/src/` and `src/` so both paths stay in sync."

    elif violation_str:
        flagged_desc = violation_str
        why_rule_exists = (
            "Lab Manager fleet guardrails enforce deterministic architectural boundaries, credential isolation, "
            "and protocol consistency across all 16 repositories."
        )
        specific_action = "Address the flagged guardrail violation before resubmitting the Pull Request."

    elif report_str:
        flagged_desc = f"Automated remediation could not resolve the following review critique:\n\n{report_str}"
        why_rule_exists = (
            "AppBuilder requires approval from both the Skeptical and State-Logic review panels before merging. "
            "Edge cases, error handling, and state reachability must be verified."
        )
        specific_action = "Review the critique findings below, update the code, and verify tests locally before pushing."

    else:
        flagged_desc = "Review panel findings require human developer attention."
        why_rule_exists = "Lab Manager requires deterministic validation and high reliability across all components."
        specific_action = "Review recent panel comments on the PR and push an updated commit."

    # Status / Attempt note
    attempt_note = ""
    if attempts >= max_attempts:
        attempt_note = (
            f"\n> [!CAUTION]\n> **Automated Remediation Limit Reached ({attempts}/{max_attempts})**\n> "
            "AppBuilder has stopped automated fixes to avoid cycle thrashing. Manual operator intervention is required.\n"
        )

    # Format files list
    files_section = ""
    if changed_files:
        files_section = "Changed Files:\n" + "\n".join(f"- {f}" for f in changed_files) + "\n"

    # Build prompt for AI assistant
    prompt_text = f"""Please fix my Pull Request according to the Lab Manager architectural rules:

Repository: {repo_name}
PR Number: #{pr_number}
{files_section}
Violation / Findings:
{violation_str or report_str or flagged_desc}

Core Lab Manager Invariants:
1. Never edit `VERSION` — it is branch-owned and incremented by promotion automation.
2. WebUI must be pure vanilla JS — no npm, node_modules, package.json, or JSX/TSX build steps.
3. Global DOM containers (#_lmToastRegion, #app, window._lmToastRegion) must NEVER be removed or mutated.
4. Subprocess execution must always use tokenized argument lists (`subprocess.run(['cmd', 'arg'])`), never `shell=True`.
5. Zero external CDNs or remote egress URLs (isolated datacenter environment).
6. In `pxmx`, maintain exact twin parity between `agent/src/` and `src/`.

Required Action:
{specific_action}

Please inspect the changed files, correct the issues to comply with all invariants, and ensure all tests pass with pytest."""

    guidance = f"""## 🚨 What Was Flagged
{flagged_desc}
{attempt_note}
## 💡 Why This Rule Exists
{why_rule_exists}

## 📋 Copy-Paste Prompt for your IDE / AI Assistant

```text
{prompt_text}
```
"""
    return guidance.strip()
