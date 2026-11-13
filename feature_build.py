"""
feature_build.py — Phase 2 of feature auto-drive: the mutating build stage.

Picks up a "build" verdict from feature_drive.classify() and turns it into a
PR: a throwaway temp checkout, AppBuilder's NEW mutating claude_cli profile
(claude_cli_native_tools.BUILD_* — see that module's docstring; Edit/Write/
Skill-enabled, still denies commit/push/reset/checkout/clean/rm/sudo/curl) or,
when no claude_cli model is approved, a tool-calling API model (Copilot)
driving repo_tools.run_mutating_agent; a deterministic docs-completeness
gate; the same pre-PR guardrails a fix passes (syntax, reviewer panel,
repository tests, with correction rounds); then commit+push+PR. ALWAYS a PR,
never direct-to-main — a feature adds new surface area, not a fix for known-
broken behavior, so it always gets a human (or, in a later phase, the
existing PR-pre-review panel) in the loop before it ships.

Ground truth for "what changed" is always the actual git diff
(`git diff --cached --name-only` after `git add -A`), never the agent's own
self-reported file list — the self-report is narrative context for the PR
body, not something anything here trusts for a decision.
"""
# Standard library: paths, temp checkouts, the build lock, and timestamps.
import os
import tempfile
import threading
from datetime import datetime

# GitPython drives the checkout; GithubException reports GitHub API errors.
import git
from github import GithubException

# Shared logger, the processed-issue store and counters, and the LLM entry point from main.
from main import (
    logger,
    load_processed,
    save_processed,
    recompute_issue_counters,
    call_llm,
)
# Fix-pipeline helpers reused for pushing, PR lookup, review, tests and cleanup.
from fix_engine import (_authenticated_remote, push_or_raise, find_existing_pull_request, _loads_llm_json_object,
                        process_single_issue, review_fix, prepare_environment, verify_fix,
                        is_verification_infra_failure, _worktree_state, discard_verification_artifacts)
# Agent tooling, LLM client and model selection, skills, boundaries, branches and labels.
import repo_tools
import llm_client
from model_selection import LlmRequirements, select_model
import skills_loader
import fleet_checks
import feature_boundary
from branch_policy import auto_branch_name, integration_branch, may_force_push
from github_ops import _ensure_label

# Exactly one build at a time, globally — a mutating agentic build is
# expensive (minutes, real tokens) and there is no reason to run two
# concurrently; the intake worker's own feature_drive_max_per_cycle cap
# (default 1) means this is mostly redundant defense-in-depth, not the
# primary throttle.
_BUILD_LOCK = threading.Lock()

# Hidden marker added to the PR body linking it back to the feature request issue.
_FEATURE_DRIVE_MARKER = "<!-- ab-feature-drive: {repo}#{number} -->"

# JSON shape the build agent must return: touch-points done/skipped and a PR description.
_BUILD_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "touchpoints_done": {"type": "array", "items": {"type": "string"}},
        "touchpoints_skipped": {"type": "array", "items": {"type": "string"}},
        "pr_body": {"type": "string", "description": "A few sentences describing what was built and why."},
    },
    "required": ["pr_body"],
}

# System prompt for the claude_cli mutating agent (native Edit/Write tools).
_BUILD_SYSTEM = (
    "You are AppBuilder's feature-build agent. You have Read/Grep/Glob/Edit/Write/"
    "Bash(narrow, read-only)/Skill access scoped to a throwaway git checkout on "
    "a dedicated branch. Build EXACTLY the feature request below by following "
    "the named skill's recipe, in order, completely — a silent skip reads as "
    "\"done\" when it isn't, so if you intentionally skip a touch-point, say so "
    "in touchpoints_skipped rather than leaving it out silently.\n\n"
    "Do NOT run git commit, git push, git reset, git checkout, git clean, sudo, "
    "curl, or pip install — these are blocked, and AppBuilder commits + pushes "
    "your working-tree changes itself once you're done. Do not touch anything "
    "outside the checkout you were given.\n\n"
    "When finished, return the required JSON summary."
)

# The same build, driven by a function-calling API model (Copilot) through
# repo_tools.BUILD_TOOLS when no claude_cli mutating agent is approved for the
# deployment. repo_tools appends the tool-usage rules to this prompt.
_BUILD_SYSTEM_TOOLS = (
    "You are AppBuilder's feature-build agent, working in a throwaway git checkout "
    "on a dedicated branch. Build EXACTLY the feature request below by following "
    "the named skill's recipe, in order, completely — a silent skip reads as "
    "\"done\" when it isn't, so if you intentionally skip a touch-point, say so "
    "in touchpoints_skipped rather than leaving it out silently.\n\n"
    "Match the existing code's conventions and helpers exactly (find a similar "
    "existing feature with grep and mirror it). AppBuilder commits and pushes your "
    "working-tree changes itself, then runs a skeptical reviewer panel and the "
    "repository's tests before any PR is opened.\n\n"
    "When finished, reply with ONLY this JSON object: "
    '{"touchpoints_done": [..], "touchpoints_skipped": [..], "pr_body": "..."}'
)

# Prompt sent back to the agent when a pre-PR check fails, asking it to fix in place.
_GUARDRAIL_FEEDBACK_TMPL = (
    "## AppBuilder guardrail feedback — correction round {round} of {rounds}\n"
    "Your previous changes are still in the checkout, but they failed the "
    "{gate} gate:\n\n{detail}\n\n"
    "Fix exactly these problems in place (read the current file content first, "
    "keep the parts that are already correct), then reply with the JSON summary."
)

# Human-readable names for each failed check, used in the feedback prompt.
_GATE_LABELS = {
    "syntax": "syntax check",
    "fleet_rules": "fleet code rules",
    "review_rejected": "skeptical reviewer panel",
    "tests_failed": "repository test suite",
}

# Prompt asking the agent to add documentation when it changed code but no docs page.
_DOCS_NUDGE_TMPL = (
    "You touched these file(s) but no docs/*.md file changed: {files}. "
    "Please also update the relevant documentation page under docs/ to "
    "describe the new feature, then return the JSON summary again."
)


def _build_prompt(issue_title, issue_body, skill_name, skill_text, boundaries_block):
    """Build the agent's user prompt: the issue title and body, the skill recipe, and the
    boundaries it must not touch.
    """
    # One markdown section per piece of context.
    return (
        f"## Feature request (issue title)\n{issue_title}\n\n"
        f"## Feature request (issue body)\n{issue_body}\n\n"
        f"## Recipe to follow — skill \"{skill_name}\"\n{skill_text}\n\n"
        f"## Boundaries — do NOT touch any of these even incidentally\n{boundaries_block or '(none configured)'}\n"
        + ("\n" + fleet_checks.rules_text() + "\n" if fleet_checks.rules_text() else "")
        + _agents_block(issue_title, issue_body)
    )


def _agents_block(issue_title, issue_body):
    """The checklists of the specialist agents matching this request, as a
    prompt section ("" when none match or agents aren't loaded)."""
    try:
        text = skills_loader.agents_context("%s\n%s" % (issue_title, issue_body))
    except AttributeError:  # an older skills_loader without agent support
        return ""
    return "\n" + text + "\n" if text else ""


def _materialize_skill(skills_root, skill_name):
    """Writes the skill's SKILL.md/reference.md under skills_root/skill_name/
    so the agent can Read the full reference.md on demand via --add-dir
    instead of it all burning prompt budget up front (it's also injected
    in-prompt via _build_prompt, so this is a convenience, not the only
    delivery path). Returns the directory path, or None if the skill has no
    files loaded (skills_loader cache miss / not yet fetched)."""
    # Files of the skill; nothing to write if the skill is not loaded.
    files = skills_loader.skill_files(skill_name)
    if not files:
        return None
    # Create the skill's directory and write each file into it.
    skill_dir = os.path.join(skills_root, skill_name)
    os.makedirs(skill_dir, exist_ok=True)
    for fname, content in files.items():
        with open(os.path.join(skill_dir, fname), "w", encoding="utf-8") as f:
            f.write(content)
    return skill_dir


def _changed_files(repo_git):
    """The REAL changed-file list — staged vs HEAD — after `git add -A`.
    This, never the agent's self-report, is what every downstream decision
    (docs gate, PR body, "did anything even change") is based on."""
    # Names of staged files that differ from HEAD, without blank lines.
    out = repo_git.git.diff("--cached", "--name-only")
    return [line for line in out.splitlines() if line.strip()]


def _docs_touched(changed_files):
    """True if any changed file is a markdown page under docs/."""
    # Docs pages are docs/*.md.
    return any(p.startswith("docs/") and p.endswith(".md") for p in changed_files)


def _non_doc_files(changed_files):
    """Return the changed files that are not docs/*.md pages."""
    # Everything except docs pages.
    return [p for p in changed_files if not (p.startswith("docs/") and p.endswith(".md"))]


def _build_mode(config):
    """Which build agent this deployment can run:
    "mutating_agent" — a claude_cli model (needs_mutating_agent, native Edit/Write);
    "tool_agent"     — a function-calling API model (e.g. Copilot) driving
                       repo_tools.BUILD_TOOLS;
    None             — neither is configured."""
    # Available models and their measured performance.
    candidates = llm_client._enumerate_candidates(config)
    perf = llm_client.get_llm_perf_snapshot()
    # Prefer a claude_cli mutating agent if one qualifies.
    if select_model(LlmRequirements(complexity="large", needs_mutating_agent=True), candidates, perf):
        return "mutating_agent"
    # Otherwise use a tool-calling API model, unless the operator turned that off.
    if (config.get("feature_build_tool_agent", True)
            and select_model(LlmRequirements(complexity="large", needs_tools=True), candidates, perf)):
        return "tool_agent"
    # No suitable model.
    return None


def _run_build_agent(prompt, config, checkout_path, extra_add_dirs, timeout_s,
                     mode="mutating_agent", used_model_out=None):
    """One call_llm invocation on the build profile.

    mode="mutating_agent": needs_mutating_agent=True is a HARD capability
    filter satisfied only by claude_cli registry rules (see model_registry.py).
    mode="tool_agent": needs_tools=True picks the most capable function-calling
    model; llm_client runs repo_tools.run_mutating_agent for profile="build"."""
    # Copy the config so the longer build timeout does not leak to other callers.
    build_config = dict(config)
    build_config["LLM_TIMEOUT"] = timeout_s
    # Choose model requirements and system prompt for the selected build mode.
    if mode == "tool_agent":
        reqs = LlmRequirements(complexity="large", needs_tools=True, needs_structured_output=True,
                               prefer_capable=True, min_context_tokens=len(prompt) // 4)
        system_prompt = _BUILD_SYSTEM_TOOLS
    else:
        reqs = LlmRequirements(complexity="large", needs_mutating_agent=True,
                               needs_structured_output=True, min_context_tokens=len(prompt) // 4)
        system_prompt = _BUILD_SYSTEM
    # Run the agent in the checkout with native tools and the JSON schema; return its reply.
    raw = call_llm(
        prompt, system_prompt=system_prompt, requirements=reqs,
        enable_native_tools=True, profile="build",
        repo_checkout_path=checkout_path, extra_add_dirs=extra_add_dirs,
        json_schema=_BUILD_JSON_SCHEMA, config=build_config, used_model_out=used_model_out,
    )
    return raw


def _syntax_errors(checkout_path, changed):
    """Syntax-check each changed file that still exists and return a list of error strings."""
    errors = []
    # Check every changed file; deleted files are skipped.
    for rel in changed:
        full = os.path.join(checkout_path, rel)
        if os.path.isfile(full):
            res = repo_tools.check_file_syntax(full)
            if not res.get("ok"):
                # Record which checker failed and its output.
                errors.append(f"{rel} ({res.get('checker')}): {res.get('output')}")
    return errors


def _run_guardrails(checkout_path, repo_git, repo_name, issue, issue_id, config, builder_key):
    """The checks a normal fix passes before AppBuilder opens a PR, applied to
    the staged feature build: syntax of every changed file, the skeptical
    reviewer panel (fix_engine.review_fix, builder excluded), then the
    repository's own tests (prepare_environment + verify_fix).

    Returns (ok, kind, detail, review_confidence). kind is "passed",
    "syntax", "review_rejected", "tests_failed" (correctable by the agent),
    or "review_unavailable" / "sandbox_unavailable" (not the code's fault)."""
    # Stage everything, list the real changed files, and syntax-check them.
    repo_git.git.add(A=True)
    changed = _changed_files(repo_git)
    errors = _syntax_errors(checkout_path, changed)
    if errors:
        return False, "syntax", "\n".join(errors), None
    # Fleet code rules (vault, storage, deps, docstrings, ...).
    fleet = fleet_checks.gate(checkout_path, repo_name, config)
    if fleet:
        return False, "fleet_rules", fleet, None

    # Run the reviewer panel unless reviews are turned off in config.
    review_conf = None
    if not config.get("skip_review", False):
        issue_text = f"{issue.title or ''}\n\n{issue.body or ''}"
        review = review_fix(checkout_path, issue_text, {}, task_id=issue_id, builder_key=builder_key) or {}
        # Reviewers could not run: not the code's fault, retry later.
        if review.get("status") == "queue_for_retry":
            return False, "review_unavailable", str(review.get("reason") or "reviewers unavailable"), None
        # Anything other than an approval is a rejection the agent can try to fix.
        review_conf = review.get("confidence")
        if review.get("verdict", "Reject") != "Approve":
            return False, "review_rejected", str(review.get("critique") or "(no critique given)"), review_conf

    # Run the repository's own tests in the checkout, unless QA is turned off.
    if config.get("qa_enabled", True):
        # Snapshot the worktree so test artifacts can be removed afterwards.
        before = _worktree_state(checkout_path)
        try:
            # Set up the environment, then run the tests if setup succeeded.
            failure = prepare_environment(checkout_path)
            if failure:
                verified = False
            else:
                verified, failure = verify_fix(checkout_path, repo_name, config)
        # Always remove files the tests created, so they are not committed.
        finally:
            discard_verification_artifacts(checkout_path, before)
        # Failed tests: blame the sandbox if it was an infrastructure problem, else the code.
        if not verified:
            if is_verification_infra_failure(failure):
                return False, "sandbox_unavailable", str(failure or ""), review_conf
            return False, "tests_failed", str(failure or "tests failed"), review_conf
    # Every check passed.
    return True, "passed", "", review_conf


def _identity(prior) -> dict:
    """Carry a record's title/link across a status change."""
    return {k: v for k, v in (prior or {}).items() if k in ("title", "issue_url") and v}


def _mark_failed(issue_id, reason):
    """Record the issue as a failed feature build, with the reason, and refresh the counters."""
    # Overwrite the issue's record with the failure status and save.
    processed = load_processed()
    processed[issue_id] = {**_identity(processed.get(issue_id)), "status": "feature_failed", "reason": reason,
                           "timestamp": datetime.now().isoformat()}
    save_processed(processed)
    recompute_issue_counters(processed)


def _mark_built(issue_id, pr_url, extra=None):
    """Record the issue as a built feature with its PR URL (plus any extra fields) and
    refresh the counters.
    """
    # Overwrite the issue's record with the built status and save.
    processed = load_processed()
    processed[issue_id] = {**_identity(processed.get(issue_id)), **(extra or {}), "status": "feature_built", "pr_url": pr_url,
                           "timestamp": datetime.now().isoformat()}
    save_processed(processed)
    recompute_issue_counters(processed)


def _flag_incomplete(gh_repo, issue, reason, flag_kind="build_incomplete", extra=None):
    """A build that ran but never produced a docs update after the one
    corrective turn — not a boundary crossing, but the same "no code
    shipped, queued for a human" outcome as feature_drive._flag_issue, with
    its own reason text. Kept local rather than imported from feature_drive
    to avoid a circular import (feature_drive will import feature_build to
    dispatch "build" verdicts once this module lands)."""
    # Make sure the needs-human label exists, then add it to the issue.
    _ensure_label(gh_repo, "ab-needs-human")
    try:
        issue.add_to_labels("ab-needs-human")
    except Exception as e:
        logger.warning(f"feature_build: could not label {issue.number} needs-human: {e}")
    # Explain on the issue why no PR was opened.
    try:
        issue.create_comment(
            "🤖 **AppBuilder — Feature Auto-Drive**\n\n"
            "An automatic build was attempted but could not be completed cleanly, so "
            "**no PR was opened**. It stays open here for a human.\n\n"
            f"**Reason:** {reason}\n\n"
            "<!-- ab-boundary-flag: v1 -->"
        )
    except Exception as e:
        logger.warning(f"feature_build: could not comment on {issue.number}: {e}")
    # Record the issue as flagged for a human and refresh the counters.
    processed = load_processed()
    issue_id = f"{gh_repo.full_name}:{issue.number}"
    processed[issue_id] = {**_identity(processed.get(issue_id)),
                           "title": (issue.title or "")[:200], "issue_url": getattr(issue, "html_url", "") or "",
                           **(extra or {}), "status": "feature_flagged", "flag_kind": flag_kind,
                           "reason": reason, "timestamp": datetime.now().isoformat()}
    save_processed(processed)
    recompute_issue_counters(processed)


def _handoff_to_fix_pipeline(repo_obj, issue, classify_result, why):
    """Run a classified-buildable request through the SAME pipeline a normal
    GitHub issue takes (fix_engine.process_single_issue: triage, fix
    generation, cross-provider reviewer panel, syntax/boundary/secrets gates,
    PR to the integration branch). Used when no mutating-agent model exists,
    instead of parking a request the classifier already cleared. The
    classifier's verdict is stamped on the record first so the Status table
    shows why it went this way; process_single_issue keeps those fields
    until it writes its own terminal status."""
    # Stamp the classifier's verdict on the issue's record so the Status page shows it.
    issue_id = f"{repo_obj.full_name}:{issue.number}"
    processed = load_processed()
    processed[issue_id] = {
        **(processed.get(issue_id) or {}),
        "status": "processing",
        "pipeline": "fix_engine",
        "handoff_reason": why,
        "feature_skill": classify_result.get("skill"),
        "classifier_reason": classify_result.get("reason", ""),
        "timestamp": datetime.now().isoformat(),
    }
    save_processed(processed)
    recompute_issue_counters(processed)
    logger.info(f"feature_build: {issue_id} — {why}; handing off to the standard fix pipeline")
    # Run the normal fix pipeline; any error marks the issue failed instead of raising.
    try:
        return process_single_issue(repo_obj.full_name, issue.number)
    except Exception as e:
        logger.exception(f"feature_build: fix-pipeline hand-off raised for {issue_id}: {e}")
        _mark_failed(issue_id, f"Fix-pipeline hand-off failed: {e}")
        return False, f"fix pipeline hand-off failed: {e}"


def build_feature(gh, repo_obj, issue, classify_result, config):
    """The Phase 2 orchestrator. Returns (ok: bool, message: str). Never
    raises — the caller (feature_drive.scan_feature_requests) treats a build
    as best-effort per issue, same as the rest of the intake loop, so any
    unexpected error here is caught and turned into a feature_failed record
    rather than crashing the whole scan cycle."""
    # Issue identifiers and the skill chosen by the classifier.
    repo_name = repo_obj.full_name
    issue_id = f"{repo_name}:{issue.number}"
    skill_name = classify_result.get("skill")
    if not skill_name:
        # A "build" verdict with no resolvable skill is exactly the
        # half-shipped outcome skills exist to prevent — refuse and flag
        # rather than let a recipe-less agent improvise.
        _flag_incomplete(repo_obj, issue, "Classified as buildable, but no matching skill/recipe "
                                          "was available to build it safely.")
        return False, "no skill resolved"

    # Fast-fail before the (expensive) clone+lock below if no model has the
    # mutating-agent capability configured at all -- same early-exit UX
    # _find_claude_cli_slot used to give, now derived from the picker's own
    # capability filter instead of a hardcoded provider name.
    mode = _build_mode(config)
    if mode is None:
        if config.get("feature_build_fix_pipeline_fallback", True):
            return _handoff_to_fix_pipeline(
                repo_obj, issue, classify_result,
                "no build-capable model (claude_cli agent or tool-calling model) is configured")
        _flag_incomplete(repo_obj, issue, "Feature building requires a claude_cli agent or a "
                                          "tool-calling model in the LLM Vault, and none is set up.")
        return False, "no build-capable model configured"
    logger.info(f"feature_build: {issue_id} — build agent mode {mode}")

    # Only one build at a time; defer if another is running.
    if not _BUILD_LOCK.acquire(blocking=False):
        logger.info(f"feature_build: another build is already in progress — deferring {issue_id}")
        return False, "build lock busy"

    try:
        # Everything below runs while holding the build lock (released in finally).
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
        if not token:
            _flag_incomplete(repo_obj, issue, "No GitHub token configured.")
            return False, "no GitHub token"

        # Boundary rules for the prompt; a rendering error just means none are shown.
        boundaries_block = ""
        try:
            boundaries_block = feature_boundary.render_boundaries_for_prompt(config.get("feature_boundaries") or [])
        except Exception:
            pass

        # The skill's recipe text is required.
        skill_text = skills_loader.skill_instructions(skill_name)
        if not skill_text:
            _flag_incomplete(repo_obj, issue, f"Skill \"{skill_name}\" was chosen but its recipe "
                                              f"could not be loaded.")
            return False, "skill text unavailable"

        # Build timeout, and a fresh branch name for this feature.
        timeout_s = int(config.get("feature_build_timeout_s", 1800) or 1800)
        branch_name = auto_branch_name("feature", issue=issue)
        # dev, never main -- features promote dev -> qa -> main like everything else.
        base_branch, base_why = integration_branch(config, repo_obj)
        logger.info(f"feature_build: targeting {base_branch} -- {base_why}")

        # Work in a temp directory removed automatically when done.
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Clone into tmp/repo using a token-bearing URL; skills go in tmp/skills.
            checkout_path = os.path.join(tmp_dir, "repo")
            skills_root = os.path.join(tmp_dir, "skills")
            clone_url = repo_obj.clone_url.replace("https://", f"https://{token}@")
            logger.info(f"feature_build: cloning {repo_name} for {issue_id}...")
            # Clone the integration branch: the default branch is main, and a
            # feature forked from main carries main's VERSION into its dev PR.
            repo_git = git.Repo.clone_from(clone_url, checkout_path, branch=base_branch)
            # Strip the token back out immediately — never let it sit in
            # .git/config for the duration of an agentic build with shell
            # access to this same directory. Re-applied only transiently via
            # _authenticated_remote() right before the push below.
            repo_git.remotes.origin.set_url(repo_obj.clone_url)

            # Create and switch to the feature branch.
            try:
                repo_git.create_head(branch_name).checkout()
            except Exception as e:
                logger.error(f"feature_build: could not create branch {branch_name} for {issue_id}: {e}")
                _mark_failed(issue_id, f"Could not create build branch: {e}")
                return False, f"branch creation failed: {e}"

            # Write the skill files where the agent can read them.
            skill_dir = _materialize_skill(skills_root, skill_name)
            extra_add_dirs = [skill_dir] if skill_dir else None

            # Build the prompt and run the agent; used_model records which model ran.
            prompt = _build_prompt(issue.title or "", issue.body or "", skill_name, skill_text, boundaries_block)
            used_model = {}
            try:
                raw = _run_build_agent(prompt, config, checkout_path, extra_add_dirs, timeout_s,
                                       mode=mode, used_model_out=used_model)
            except Exception as e:
                logger.error(f"feature_build: build agent call failed for {issue_id}: {e}")
                _mark_failed(issue_id, f"Build agent call failed: {e}")
                return False, f"build agent failed: {e}"

            # Parse the agent's JSON summary; anything unparseable becomes an empty summary.
            try:
                agent_summary = _loads_llm_json_object(raw) or {}
                if not isinstance(agent_summary, dict):
                    agent_summary = {}
            except Exception:
                agent_summary = {}

            # Stage everything and check the real diff; no changes means the build failed.
            repo_git.git.add(A=True)
            changed = _changed_files(repo_git)
            if not changed:
                logger.info(f"feature_build: agent made no changes for {issue_id}")
                _mark_failed(issue_id, "The build agent ran but made no file changes.")
                return False, "no changes made"

            # Deterministic docs-completeness gate — one corrective turn,
            # then give up and flag rather than ship an undocumented feature
            # silently. The syntax/review/test guardrails follow below.
            if config.get("feature_require_docs", True) and not _docs_touched(changed):
                # Ask the agent once more to add docs, keeping its new summary if it returns one.
                nudge = _DOCS_NUDGE_TMPL.format(files=", ".join(_non_doc_files(changed)))
                try:
                    raw2 = _run_build_agent(prompt + "\n\n" + nudge, config, checkout_path,
                                            extra_add_dirs, timeout_s, mode=mode,
                                            used_model_out=used_model)
                    agent_summary2 = _loads_llm_json_object(raw2) or {}
                    if isinstance(agent_summary2, dict):
                        agent_summary = agent_summary2
                except Exception as e:
                    logger.warning(f"feature_build: docs corrective turn failed for {issue_id}: {e}")
                # Re-check the real diff; still no docs means flag for a human.
                repo_git.git.add(A=True)
                changed = _changed_files(repo_git)
                if not _docs_touched(changed):
                    _flag_incomplete(repo_obj, issue,
                                     "The build touched " + ", ".join(_non_doc_files(changed)) +
                                     " but never updated any docs/*.md page, even after one "
                                     "corrective attempt.")
                    return False, "docs gate failed"

            # Same guardrails a normal fix passes before its PR: syntax,
            # skeptical reviewer panel, repository tests. A correctable
            # failure goes back to the agent in this same checkout (its work
            # so far is kept) for up to feature_build_correction_rounds turns.
            rounds = max(0, int(config.get("feature_build_correction_rounds", 3) or 0))
            guard_history = []
            review_conf = None
            # Each round runs the checks; on failure, record it and either give up or ask for a fix.
            for rnd in range(rounds + 1):
                ok, kind, detail, review_conf = _run_guardrails(
                    checkout_path, repo_git, repo_name, issue, issue_id, config, used_model.get("key"))
                guard_history.append({"round": rnd, "gate": kind, "model": used_model.get("model"),
                                      "detail": detail[:1500]})
                logger.info(f"feature_build: {issue_id} guardrails round {rnd}: {kind}"
                            + (f" — {detail[:300]}" if detail else ""))
                # All checks passed: stop looping.
                if ok:
                    break
                # Details saved on the issue record if this build is flagged.
                record = {"build_mode": mode, "build_model": used_model.get("model"),
                          "guardrail_rounds": guard_history, "failure_gate": kind,
                          "feature_skill": skill_name}
                # Checks could not run at all: flag for a human rather than retry.
                if kind in ("review_unavailable", "sandbox_unavailable"):
                    _flag_incomplete(repo_obj, issue,
                                     f"The build could not be checked before opening a PR "
                                     f"({kind.replace('_', ' ')}): {detail[:800]}",
                                     flag_kind=kind, extra=record)
                    return False, kind
                # Out of correction rounds: flag for a human.
                if rnd >= rounds:
                    _flag_incomplete(repo_obj, issue,
                                     f"The build still failed the {_GATE_LABELS.get(kind, kind)} "
                                     f"after {rounds} correction round(s): {detail[:1500]}",
                                     flag_kind="guardrails_failed", extra=record)
                    return False, f"guardrails failed: {kind}"
                # Send the failure back to the agent and let it fix things in place.
                feedback = _GUARDRAIL_FEEDBACK_TMPL.format(
                    round=rnd + 1, rounds=rounds, gate=_GATE_LABELS.get(kind, kind),
                    detail=detail[:6000])
                try:
                    raw_fix = _run_build_agent(prompt + "\n\n" + feedback, config, checkout_path,
                                               extra_add_dirs, timeout_s, mode=mode,
                                               used_model_out=used_model)
                    summary_fix = _loads_llm_json_object(raw_fix) or {}
                    if isinstance(summary_fix, dict) and summary_fix.get("pr_body"):
                        agent_summary = summary_fix
                except Exception as e:
                    logger.warning(f"feature_build: correction round {rnd + 1} failed for {issue_id}: {e}")
            # Stage and list the final changes after the last round.
            repo_git.git.add(A=True)
            changed = _changed_files(repo_git)

            # Commit the build to the feature branch.
            commit_msg = f"AI Feature #{issue.number}: {(issue.title or '')[:50]}"
            repo_git.index.commit(commit_msg)

            try:
                # branch_name comes from auto_branch_name("feature", ...), so
                # this normally force-pushes AppBuilder's own branch. The
                # guard keeps that true if the naming ever changes --
                # force-pushing a shared branch discards other people's
                # commits.
                _force_ok, _force_why = may_force_push(
                    branch_name, config,
                    repo_default_branch=getattr(repo_obj, "default_branch", None))
                if not _force_ok:
                    logger.info(f"feature_build: pushing {branch_name} without --force — {_force_why}")
                # Push using a temporary token-bearing remote URL.
                with _authenticated_remote(repo_git.remotes.origin, repo_obj.clone_url, token):
                    push_or_raise(repo_git.remotes.origin, branch_name, force=_force_ok)
            # A failed push marks the issue failed.
            except Exception as e:
                logger.error(f"feature_build: push failed for {issue_id}: {e}")
                _mark_failed(issue_id, f"Push failed: {e}")
                return False, f"push failed: {e}"

            # PR title and the hidden marker linking the PR to the issue.
            pr_title = f"AI Feature #{issue.number}: {(issue.title or '')[:60]}"
            marker = _FEATURE_DRIVE_MARKER.format(repo=repo_name, number=issue.number)
            # Shaped to the fleet PR template. AGENTS.md requires every PR to
            # carry an explicit Intent & Problem Statement, and the skeptical
            # panel is told to lower confidence when one is missing -- so a body
            # that opens with "Automated feature build for #N" was asking to be
            # rejected on a rule this repo wrote itself. The issue is the
            # authoritative statement of intent; quote it.
            try:
                import pr_template
                _touch_done = ', '.join(agent_summary.get('touchpoints_done') or []) or '(not reported)'
                _touch_skip = ', '.join(agent_summary.get('touchpoints_skipped') or []) or '(none reported)'
                pr_body = pr_template.render(
                    intent=pr_template.from_issue(
                        issue.number, issue.title or "", issue.body or "",
                        getattr(issue, "html_url", None)),
                    solution=(
                        f"Built by AppBuilder's feature drive.\n\n"
                        f"- **Skill used:** {skill_name}\n"
                        f"- **Classifier reason:** {classify_result.get('reason', '')}\n"
                        f"- **Files changed:** {', '.join(changed)}\n\n"
                        f"**Agent's own account:**\n"
                        f"{agent_summary.get('pr_body', '(none provided)')}"),
                    guardrails=(
                        f"- Touch-points done: {_touch_done}\n"
                        f"- Touch-points skipped: {_touch_skip}\n"
                        f"- Build agent: {mode} ({used_model.get('model') or 'unknown model'})"),
                    verification=(
                        f"Before this PR was opened: syntax check passed, skeptical reviewer panel "
                        f"approved"
                        + (f" (confidence {review_conf:.0%})" if isinstance(review_conf, (int, float)) else "")
                        + f", repository tests passed — after {len(guard_history) - 1} correction "
                        f"round(s). Repository CI runs on this PR."),
                ) + f"\n{marker}"
            except Exception:  # noqa: BLE001 — never block a build on body rendering
                # Plain fallback PR body if the template could not be rendered.
                pr_body = (
                    f"Automated feature build for #{issue.number}.\n\n"
                    f"**Classifier reason:** {classify_result.get('reason', '')}\n\n"
                    f"**Skill used:** {skill_name}\n\n"
                    f"**Files changed:** {', '.join(changed)}\n\n"
                    f"**Agent's own account:**\n{agent_summary.get('pr_body', '(none provided)')}\n\n"
                    f"Touch-points done: {', '.join(agent_summary.get('touchpoints_done') or []) or '(not reported)'}\n"
                    f"Touch-points skipped: {', '.join(agent_summary.get('touchpoints_skipped') or []) or '(none reported)'}\n\n"
                    f"{marker}"
                )

            # Reuse an open PR for this branch, or create one.
            _ensure_label(repo_obj, "ab-feature-drive")
            existing_pr = find_existing_pull_request(repo_obj, branch_name, base_branch)
            if existing_pr:
                pr = existing_pr
            else:
                try:
                    pr = repo_obj.create_pull(title=pr_title, body=pr_body, head=branch_name, base=base_branch)
                # 422 means a PR already exists (e.g. created concurrently); reuse it.
                except GithubException as ge:
                    if ge.status == 422:
                        existing_pr = find_existing_pull_request(repo_obj, branch_name, base_branch)
                        if not existing_pr:
                            raise
                        pr = existing_pr
                    else:
                        raise
            # Label the PR as a feature-drive build; a labelling error is only logged.
            try:
                pr.add_to_labels("ab-feature-drive")
            except Exception as e:
                logger.warning(f"feature_build: could not label PR #{pr.number}: {e}")

            # Record success with build details and return the PR URL.
            _mark_built(issue_id, pr.html_url, extra={
                "build_mode": mode, "build_model": used_model.get("model"),
                "guardrail_rounds": guard_history, "review_confidence": review_conf,
                "feature_skill": skill_name})
            logger.info(f"feature_build: {issue_id} built -> {pr.html_url}")
            return True, pr.html_url
    # Any unexpected error is logged and recorded instead of raised.
    except Exception as e:
        logger.exception(f"feature_build: unexpected error building {issue_id}: {e}")
        try:
            _mark_failed(issue_id, f"Unexpected error: {e}")
        except Exception:
            pass
        return False, f"unexpected error: {e}"
    # Always release the build lock.
    finally:
        _BUILD_LOCK.release()
