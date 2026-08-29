"""
feature_build.py — Phase 2 of feature auto-drive: the mutating build stage.

Picks up a "build" verdict from feature_drive.classify() and turns it into a
PR: a throwaway temp checkout, AppBuilder's NEW mutating claude_cli profile
(claude_cli_native_tools.BUILD_* — see that module's docstring; Edit/Write/
Skill-enabled, still denies commit/push/reset/checkout/clean/rm/sudo/curl),
a deterministic docs-completeness gate, then commit+push+PR. ALWAYS a PR,
never direct-to-main — a feature adds new surface area, not a fix for known-
broken behavior, so it always gets a human (or, in a later phase, the
existing PR-pre-review panel) in the loop before it ships.

Ground truth for "what changed" is always the actual git diff
(`git diff --cached --name-only` after `git add -A`), never the agent's own
self-reported file list — the self-report is narrative context for the PR
body, not something anything here trusts for a decision.
"""
import os
import tempfile
import threading
from datetime import datetime

import git
from github import GithubException

from main import (
    logger,
    load_processed,
    save_processed,
    recompute_issue_counters,
    call_llm,
)
from fix_engine import _authenticated_remote, find_existing_pull_request, _robust_json_loads
import llm_client
from model_selection import LlmRequirements, select_model
import skills_loader
import feature_boundary
from branch_policy import auto_branch_name, integration_branch, may_force_push
from github_ops import _ensure_label

# Exactly one build at a time, globally — a mutating agentic build is
# expensive (minutes, real tokens) and there is no reason to run two
# concurrently; the intake worker's own feature_drive_max_per_cycle cap
# (default 1) means this is mostly redundant defense-in-depth, not the
# primary throttle.
_BUILD_LOCK = threading.Lock()

_FEATURE_DRIVE_MARKER = "<!-- ab-feature-drive: {repo}#{number} -->"

_BUILD_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "touchpoints_done": {"type": "array", "items": {"type": "string"}},
        "touchpoints_skipped": {"type": "array", "items": {"type": "string"}},
        "pr_body": {"type": "string", "description": "A few sentences describing what was built and why."},
    },
    "required": ["pr_body"],
}

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

_DOCS_NUDGE_TMPL = (
    "You touched these file(s) but no docs/*.md file changed: {files}. "
    "Please also update the relevant documentation page under docs/ to "
    "describe the new feature, then return the JSON summary again."
)


def _build_prompt(issue_title, issue_body, skill_name, skill_text, boundaries_block):
    return (
        f"## Feature request (issue title)\n{issue_title}\n\n"
        f"## Feature request (issue body)\n{issue_body}\n\n"
        f"## Recipe to follow — skill \"{skill_name}\"\n{skill_text}\n\n"
        f"## Boundaries — do NOT touch any of these even incidentally\n{boundaries_block or '(none configured)'}\n"
    )


def _materialize_skill(skills_root, skill_name):
    """Writes the skill's SKILL.md/reference.md under skills_root/skill_name/
    so the agent can Read the full reference.md on demand via --add-dir
    instead of it all burning prompt budget up front (it's also injected
    in-prompt via _build_prompt, so this is a convenience, not the only
    delivery path). Returns the directory path, or None if the skill has no
    files loaded (skills_loader cache miss / not yet fetched)."""
    files = skills_loader.skill_files(skill_name)
    if not files:
        return None
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
    out = repo_git.git.diff("--cached", "--name-only")
    return [line for line in out.splitlines() if line.strip()]


def _docs_touched(changed_files):
    return any(p.startswith("docs/") and p.endswith(".md") for p in changed_files)


def _non_doc_files(changed_files):
    return [p for p in changed_files if not (p.startswith("docs/") and p.endswith(".md"))]


def _run_build_agent(prompt, config, checkout_path, extra_add_dirs, timeout_s):
    """One call_llm invocation on the mutating build profile. needs_mutating_agent=True
    is a HARD capability filter satisfied only by claude_cli registry rules
    (see model_registry.py) — the picker naturally lands on a claude_cli
    model the same way _find_claude_cli_slot used to hand-search for one,
    but now surfaces "no model has this capability" instead of "no
    claude_cli slot configured" when nothing qualifies."""
    build_config = dict(config)
    build_config["LLM_TIMEOUT"] = timeout_s
    reqs = LlmRequirements(complexity="large", needs_mutating_agent=True,
                           needs_structured_output=True, min_context_tokens=len(prompt) // 4)
    raw = call_llm(
        prompt, system_prompt=_BUILD_SYSTEM, requirements=reqs,
        enable_native_tools=True, profile="build",
        repo_checkout_path=checkout_path, extra_add_dirs=extra_add_dirs,
        json_schema=_BUILD_JSON_SCHEMA,
    )
    return raw


def _mark_failed(issue_id, reason):
    processed = load_processed()
    processed[issue_id] = {"status": "feature_failed", "reason": reason,
                           "timestamp": datetime.now().isoformat()}
    save_processed(processed)
    recompute_issue_counters(processed)


def _mark_built(issue_id, pr_url):
    processed = load_processed()
    processed[issue_id] = {"status": "feature_built", "pr_url": pr_url,
                           "timestamp": datetime.now().isoformat()}
    save_processed(processed)
    recompute_issue_counters(processed)


def _flag_incomplete(gh_repo, issue, reason):
    """A build that ran but never produced a docs update after the one
    corrective turn — not a boundary crossing, but the same "no code
    shipped, queued for a human" outcome as feature_drive._flag_issue, with
    its own reason text. Kept local rather than imported from feature_drive
    to avoid a circular import (feature_drive will import feature_build to
    dispatch "build" verdicts once this module lands)."""
    _ensure_label(gh_repo, "ab-needs-human")
    try:
        issue.add_to_labels("ab-needs-human")
    except Exception as e:
        logger.warning(f"feature_build: could not label {issue.number} needs-human: {e}")
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
    processed = load_processed()
    issue_id = f"{gh_repo.full_name}:{issue.number}"
    processed[issue_id] = {"status": "feature_flagged", "reason": reason,
                           "timestamp": datetime.now().isoformat()}
    save_processed(processed)
    recompute_issue_counters(processed)


def build_feature(gh, repo_obj, issue, classify_result, config):
    """The Phase 2 orchestrator. Returns (ok: bool, message: str). Never
    raises — the caller (feature_drive.scan_feature_requests) treats a build
    as best-effort per issue, same as the rest of the intake loop, so any
    unexpected error here is caught and turned into a feature_failed record
    rather than crashing the whole scan cycle."""
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
    _availability_reqs = LlmRequirements(complexity="large", needs_mutating_agent=True)
    _candidates = llm_client._enumerate_candidates(config)
    _perf = llm_client.get_llm_perf_snapshot()
    if select_model(_availability_reqs, _candidates, _perf) is None:
        _flag_incomplete(repo_obj, issue, "Feature building requires a claude_cli provider slot "
                                          "configured in the LLM Vault, and none is set up.")
        return False, "no claude_cli slot configured"

    if not _BUILD_LOCK.acquire(blocking=False):
        logger.info(f"feature_build: another build is already in progress — deferring {issue_id}")
        return False, "build lock busy"

    try:
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
        if not token:
            _flag_incomplete(repo_obj, issue, "No GitHub token configured.")
            return False, "no GitHub token"

        boundaries_block = ""
        try:
            boundaries_block = feature_boundary.render_boundaries_for_prompt(config.get("feature_boundaries") or [])
        except Exception:
            pass

        skill_text = skills_loader.skill_instructions(skill_name)
        if not skill_text:
            _flag_incomplete(repo_obj, issue, f"Skill \"{skill_name}\" was chosen but its recipe "
                                              f"could not be loaded.")
            return False, "skill text unavailable"

        timeout_s = int(config.get("feature_build_timeout_s", 1800) or 1800)
        branch_name = auto_branch_name("feature", issue=issue)
        # dev, never main -- features promote dev -> qa -> main like everything else.
        base_branch, base_why = integration_branch(config, repo_obj)
        logger.info(f"feature_build: targeting {base_branch} -- {base_why}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout_path = os.path.join(tmp_dir, "repo")
            skills_root = os.path.join(tmp_dir, "skills")
            clone_url = repo_obj.clone_url.replace("https://", f"https://{token}@")
            logger.info(f"feature_build: cloning {repo_name} for {issue_id}...")
            repo_git = git.Repo.clone_from(clone_url, checkout_path)
            # Strip the token back out immediately — never let it sit in
            # .git/config for the duration of an agentic build with shell
            # access to this same directory. Re-applied only transiently via
            # _authenticated_remote() right before the push below.
            repo_git.remotes.origin.set_url(repo_obj.clone_url)

            try:
                repo_git.create_head(branch_name).checkout()
            except Exception as e:
                logger.error(f"feature_build: could not create branch {branch_name} for {issue_id}: {e}")
                _mark_failed(issue_id, f"Could not create build branch: {e}")
                return False, f"branch creation failed: {e}"

            skill_dir = _materialize_skill(skills_root, skill_name)
            extra_add_dirs = [skill_dir] if skill_dir else None

            prompt = _build_prompt(issue.title or "", issue.body or "", skill_name, skill_text, boundaries_block)
            try:
                raw = _run_build_agent(prompt, config, checkout_path, extra_add_dirs, timeout_s)
            except Exception as e:
                logger.error(f"feature_build: build agent call failed for {issue_id}: {e}")
                _mark_failed(issue_id, f"Build agent call failed: {e}")
                return False, f"build agent failed: {e}"

            try:
                agent_summary = _robust_json_loads(raw) or {}
                if not isinstance(agent_summary, dict):
                    agent_summary = {}
            except Exception:
                agent_summary = {}

            repo_git.git.add(A=True)
            changed = _changed_files(repo_git)
            if not changed:
                logger.info(f"feature_build: agent made no changes for {issue_id}")
                _mark_failed(issue_id, "The build agent ran but made no file changes.")
                return False, "no changes made"

            # Deterministic docs-completeness gate — one corrective turn,
            # then give up and flag rather than ship an undocumented feature
            # silently. This is build-stage completeness, not the multi-
            # attempt verification retry loop (that's explicitly NOT part of
            # this plan — the panel stays advisory-only, unmodified).
            if config.get("feature_require_docs", True) and not _docs_touched(changed):
                nudge = _DOCS_NUDGE_TMPL.format(files=", ".join(_non_doc_files(changed)))
                try:
                    raw2 = _run_build_agent(prompt + "\n\n" + nudge, config, checkout_path,
                                            extra_add_dirs, timeout_s)
                    agent_summary2 = _robust_json_loads(raw2) or {}
                    if isinstance(agent_summary2, dict):
                        agent_summary = agent_summary2
                except Exception as e:
                    logger.warning(f"feature_build: docs corrective turn failed for {issue_id}: {e}")
                repo_git.git.add(A=True)
                changed = _changed_files(repo_git)
                if not _docs_touched(changed):
                    _flag_incomplete(repo_obj, issue,
                                     "The build touched " + ", ".join(_non_doc_files(changed)) +
                                     " but never updated any docs/*.md page, even after one "
                                     "corrective attempt.")
                    return False, "docs gate failed"

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
                with _authenticated_remote(repo_git.remotes.origin, repo_obj.clone_url, token):
                    repo_git.remotes.origin.push(branch_name, force=_force_ok)
            except Exception as e:
                logger.error(f"feature_build: push failed for {issue_id}: {e}")
                _mark_failed(issue_id, f"Push failed: {e}")
                return False, f"push failed: {e}"

            pr_title = f"AI Feature #{issue.number}: {(issue.title or '')[:60]}"
            marker = _FEATURE_DRIVE_MARKER.format(repo=repo_name, number=issue.number)
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

            _ensure_label(repo_obj, "ab-feature-drive")
            existing_pr = find_existing_pull_request(repo_obj, branch_name, base_branch)
            if existing_pr:
                pr = existing_pr
            else:
                try:
                    pr = repo_obj.create_pull(title=pr_title, body=pr_body, head=branch_name, base=base_branch)
                except GithubException as ge:
                    if ge.status == 422:
                        existing_pr = find_existing_pull_request(repo_obj, branch_name, base_branch)
                        if not existing_pr:
                            raise
                        pr = existing_pr
                    else:
                        raise
            try:
                pr.add_to_labels("ab-feature-drive")
            except Exception as e:
                logger.warning(f"feature_build: could not label PR #{pr.number}: {e}")

            _mark_built(issue_id, pr.html_url)
            logger.info(f"feature_build: {issue_id} built -> {pr.html_url}")
            return True, pr.html_url
    except Exception as e:
        logger.exception(f"feature_build: unexpected error building {issue_id}: {e}")
        try:
            _mark_failed(issue_id, f"Unexpected error: {e}")
        except Exception:
            pass
        return False, f"unexpected error: {e}"
    finally:
        _BUILD_LOCK.release()
