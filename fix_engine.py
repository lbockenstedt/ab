"""AI fix pipeline: issue analysis, sandboxed fix generation/application, verification, and per-issue orchestration (extracted from main.py)."""
import contextlib, git, json, os, re, requests, tempfile, threading, time
from datetime import datetime
from github import Github, GithubException

from main import (
    CHAT_CONFIG_DEFAULTS,
    _apply_closed_label,
    _bug_report_fix_context,
    _module_log_fix_context,
    _find_claude_cli_slot,
    _get_provider_config,
    _get_reviewer_model,
    _is_triage_only,
    _provider_configured,
    _trigger_spoke_updates,
    _trunc,
    _wait_for_spokes_online,
    bump_repo_version,
    call_llm,
    find_existing_pull_request,
    is_llm_cooldown_error,
    load_config,
    load_processed,
    logger,
    resolve_self_diagnosis_repo,
    save_processed,
    state,
    update_task_state,
)


class QueueLocalException(Exception):
    """Kept for backward compatibility with persisted 'awaiting_local' issue states."""
    pass


_BUG_REPORT_ID_RE = re.compile(r'<!--\s*bug-report-id:\s*([0-9a-fA-F]+)\s*-->')


def _notify_bug_fixed(issue):
    """If a GitHub issue came from an LM 'File a Bug' report (hidden bug-report-id
    marker in the body), tell the hub the issue is fixed so the LM bug-reports UI
    shows 'Fixed' + the issue link. Best-effort + non-fatal."""
    try:
        m = _BUG_REPORT_ID_RE.search(getattr(issue, "body", "") or "")
        if not m:
            return
        from hub_agent import hub_agent_client
        if not hub_agent_client:
            return
        hub_agent_client.request_sync(
            "MARK_BUG_FIXED",
            {"id": m.group(1), "issue_url": getattr(issue, "html_url", "") or ""},
            timeout=10)
        logger.info(f"MARK_BUG_FIXED sent for bug-report {m.group(1)} "
                    f"({getattr(issue, 'html_url', '')})")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"_notify_bug_fixed skipped: {e}")


@contextlib.contextmanager
def _authenticated_remote(remote, plain_url, token):
    """Re-embeds the GitHub token on a remote only for the duration of a push/pull,
    then strips it back out. Keeps the token out of .git/config the rest of the
    time, since that directory is mounted read/write into the Docker sandbox where
    untrusted repository code (deps, tests) runs with default network access."""
    remote.set_url(plain_url.replace("https://", f"https://{token}@"))
    try:
        yield remote
    finally:
        remote.set_url(plain_url)


def run_sandboxed_command(command, cwd):
    """Executes a command in a Docker sandbox. Fails closed (returns an error result)
    if Docker is unavailable — NEVER runs untrusted repository code on the host as root."""
    import subprocess
    from dataclasses import dataclass

    @dataclass
    class MockResult:
        stdout: str
        stderr: str
        returncode: int

    docker_available = False
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True, timeout=15)
        docker_available = True
    except Exception:
        pass

    if not docker_available:
        msg = ("Docker is not available; refusing to run untrusted repository commands on the host "
               "(fail-closed). Install Docker and retry.")
        logger.error("⚠️ " + msg)
        return MockResult("", msg, 127)

    image = "ubuntu:latest"
    try:
        files = os.listdir(cwd)
    except Exception as e:
        logger.error(f"Cannot read sandbox working directory {cwd}: {e}")
        return MockResult("", str(e), 1)
    if "package.json" in files: image = "node:18-slim"
    elif "requirements.txt" in files or "pyproject.toml" in files: image = "python:3.9-slim"
    elif "go.mod" in files: image = "golang:1.21-slim"

    logger.info(f"Running sandboxed command in Docker image {image}...")

    try:
        if os.geteuid() != 0:
            # Non-root (svc_bg): docker socket needs root, so delegate to the
            # root helper. The helper validates cwd is under /opt/bugfixer and
            # runs `docker run` as root; it exits with the docker rc and passes
            # stdout/stderr through. Image selection stays here (svc_bg picks
            # the image from repo files) and is passed as a single argv.
            result = subprocess.run(
                ["sudo", "-n", "/usr/local/bin/bugfixer-sandbox", image, cwd, command],
                capture_output=True, text=True, timeout=300,
            )
        else:
            docker_cmd = [
                "docker", "run", "--rm",
                "-v", f"{cwd}:/app",
                "-w", "/app",
                image,
                "sh", "-c", command
            ]
            result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=300)
        return MockResult(result.stdout, result.stderr, result.returncode)
    except Exception as e:
        logger.error(f"Docker execution error: {e}")
        return MockResult("", f"Docker execution error: {e}", 1)


def analyze_issue(issue):
    full_context = f"Issue Title: {issue.title}\nIssue Body: {issue.body}\n\n"
    comments = issue.get_comments()
    for i, comment in enumerate(comments, 1):
        full_context += f"Comment {i}: {comment.body}\n"

    config = load_config()
    strictness = config.get("TRIAGE_STRICTNESS", "Moderate")

    if strictness == "Strict":
        strictness_instruction = "Specifically, for UI or runtime errors, you MUST have full console logs or stack traces. If these are missing, it is non-actionable."
    elif strictness == "Lenient":
        strictness_instruction = "Be generous. If the issue describes a bug and the repository is accessible, mark it as actionable even if full logs are missing, provided there is a plausible lead."
    else:
        strictness_instruction = "Specifically, for UI or runtime errors, prefer console logs or stack traces, but if the description is detailed enough for a senior engineer to hypothesize the bug accurately, mark it as actionable."

    prompt = (
        f"{full_context}\n\n"
        f"Determine if this issue contains enough information to provide a code fix. \n"
        f"{strictness_instruction}\n"
        f"Note: If this is an automated log alert, the provided log snippet is the primary evidence. Do not request a stack trace if a clear error is already present in the logs.\n"
        f"If information is missing, specify exactly what is needed (e.g., 'Please provide the browser console output').\n\n"
        "Return ONLY a JSON object: {\"actionable\": boolean, \"request\": \"message if not actionable\"}"
    )
    try:
        res = call_llm(prompt, system_prompt="You are a triage bot. Only return a JSON object.")
        import re
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data.get("actionable", False), data.get("request", "More information is needed to proceed with a fix.")
        return False, "Information provided is not in a usable format. Please provide more details."
    except Exception as e:
        if is_llm_cooldown_error(e):
            logger.warning(f"Issue analysis deferred — LLM providers cooling down: {e}")
        else:
            logger.error(f"Error analyzing issue: {e}")
        return True, ""


def identify_files_to_fix(repo_path, issue_body):
    logger.info("Identifying relevant files for fix...")
    all_files = []
    for root, _, files in os.walk(repo_path):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, repo_path)
            if any(x in rel_path for x in [".git", "node_modules", "__pycache__", "venv", ".env"]):
                continue
            all_files.append(rel_path)
    file_list_str = "\n".join(all_files)
    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Repository File List:\n{file_list_str}\n\n"
        "Identify which files are most likely relevant to fixing this issue. "
        "Return ONLY a JSON array of file paths: [\"path/to/file1\", \"path/to/file2\"]"
    )
    try:
        res = call_llm(prompt, system_prompt="You are a repository analyzer. Only return a JSON array of paths.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []
    except Exception as e:
        if is_llm_cooldown_error(e):
            logger.warning(f"File identification deferred — LLM providers cooling down: {e}")
        else:
            logger.error(f"Error identifying files: {e}")
        return []


def prepare_environment(repo_path):
    logger.info("Preparing environment (installing dependencies)...")
    files = os.listdir(repo_path)
    if "package.json" in files:
        logger.info("Detected Node.js project. Running npm install...")
        run_sandboxed_command("npm install", repo_path)
    elif "requirements.txt" in files:
        logger.info("Detected Python project with requirements.txt. Running pip install...")
        run_sandboxed_command("pip install -r requirements.txt", repo_path)
    elif "pyproject.toml" in files:
        logger.info("Detected Python project with pyproject.toml. Running pip install .")
        run_sandboxed_command("pip install .", repo_path)
    elif "go.mod" in files:
        logger.info("Detected Go project. Running go mod download...")
        run_sandboxed_command("go mod download", repo_path)
    elif "Makefile" in files:
        logger.info("Detected Makefile. Attempting 'make install'...")
        run_sandboxed_command("make install", repo_path)
    else:
        logger.info("No known dependency file detected. Skipping installation.")


def review_fix(repo_path, issue_body, proposed_fixes, force_cloud=None, task_id=None, builder_n=None):
    """Run a cross-provider reviewer panel on a proposed fix.

    builder_n: which provider slot (1/2/3) generated the fix being reviewed.
    Reviewers are all OTHER configured providers — the builder is never asked
    to review its own work.  If builder_n is None, it's inferred from force_cloud.

    If a reviewer provider is unavailable (offline, credit-exhausted, or errored):
      - If surviving reviewers reach confidence >= 0.80 with Approve: skip missing reviewer, proceed.
      - Otherwise: return {"status": "pending_review", "reason": ...} so the caller
        can queue the issue for manual approval or retry once providers come back.
    """
    SKIP_CONFIDENCE_THRESHOLD = 0.80  # skip missing reviewer only above this confidence

    logger.info("Running Reviewer Panel pass...")
    config = load_config()

    # Determine which provider built the fix.
    if builder_n is None:
        builder_n = 2 if force_cloud is True else 1

    # Build reviewer panel from all providers EXCEPT the builder.
    reviewers = []
    for n in (1, 2, 3, 4):
        if n == builder_n:
            continue
        provider, key, model, _ = _get_provider_config(n, config)
        if not _provider_configured(provider, key, model):
            continue
        r_model = _get_reviewer_model(n, config) or model
        reviewers.append({"name": f"Reviewer {n} ({provider})", "model": r_model, "provider_n": n})

    if not reviewers:
        logger.warning("No reviewers configured. Falling back to default LLM review.")
        reviewers = [{"name": "Default Reviewer", "model": None, "provider_n": None}]

    # Check if any provider is online at all.
    any_provider_online = any(
        state.get(f"provider_{n}_online", True) for n in (1, 2, 3, 4) if n != builder_n
    )
    if not any_provider_online:
        logger.warning("All reviewer LLM providers appear offline. Signaling retry queue.")
        return {"status": "queue_for_retry", "reason": "all_reviewers_offline"}

    # Show reviewers the actual working-tree DIFF (parse_and_apply already wrote
    # the change) rather than dumping full file bodies — a targeted edit to a large
    # file would otherwise flood the review prompt with ~1MB of unchanged code and
    # blow the provider limit. Fall back to (capped) file bodies if no diff.
    fix_details = ""
    try:
        diff_text = git.Repo(repo_path).git.diff("HEAD")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"review_fix: git diff unavailable ({e}); using file bodies")
        diff_text = ""
    if diff_text.strip():
        if len(diff_text) > 20000:
            diff_text = diff_text[:20000] + "\n… [diff truncated for review] …"
        fix_details = f"\n--- DIFF (working tree vs HEAD) ---\n{diff_text}\n"
    else:
        for path, code in proposed_fixes.items():
            body = str(code) if len(str(code)) <= 8000 else _trunc(str(code), 8000)
            fix_details += f"\n--- FILE: {path} ---\n{body}\n"

    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Proposed Fixes:\n{fix_details}\n\n"
        "You are a Skeptical Senior Engineer. Your job is to review this proposed fix. "
        "Check for: \n"
        "1. Does it actually fix the described issue?\n"
        "2. Does it introduce new bugs or regressions?\n"
        "3. Is the code quality acceptable?\n"
        "4. Are there any obvious edge cases missed?\n\n"
        "Return ONLY a JSON object: {\"confidence\": float, \"verdict\": \"Approve\"|\"Reject\", \"critique\": \"detailed explanation\"}"
    )

    votes = []
    failed_reviewers = []
    for r in reviewers:
        try:
            logger.info(f"{r['name']} analyzing fix...")
            res = call_llm(
                prompt,
                system_prompt="You are a skeptical senior engineer. Be critical. Only return JSON.",
                force_provider=r["provider_n"],
                task_id=task_id,
                model_override=r.get("model"),
            )
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if match:
                votes.append({**json.loads(match.group()), "reviewer": r["name"]})
        except Exception as e:
            if is_llm_cooldown_error(e):
                logger.warning(f"{r['name']} deferred — LLM providers cooling down: {e}")
            else:
                logger.error(f"{r['name']} failed: {e}")
            failed_reviewers.append(r["name"])

    if not votes:
        return {"confidence": 0.0, "verdict": "Reject", "critique": "All reviewers failed."}

    avg_conf = sum(v.get("confidence", 0.0) for v in votes) / len(votes)
    approvals = [v for v in votes if v.get("verdict") == "Approve"]
    critiques = " | ".join(
        f"[{v.get('reviewer', '?')}] {v.get('critique', '')}" for v in votes
    )

    # If some reviewers were skipped, decide whether to proceed or queue.
    if failed_reviewers:
        if avg_conf >= SKIP_CONFIDENCE_THRESHOLD and len(approvals) == len(votes):
            logger.warning(
                f"Skipped unavailable reviewers {failed_reviewers} — "
                f"surviving panel approved with confidence {avg_conf:.2f} (>= {SKIP_CONFIDENCE_THRESHOLD}). Proceeding."
            )
        else:
            reason = (
                f"Reviewers {failed_reviewers} unavailable and surviving panel confidence "
                f"{avg_conf:.2f} < {SKIP_CONFIDENCE_THRESHOLD} or not unanimous."
            )
            logger.warning(f"Queuing for manual approval: {reason}")
            return {
                "status": "queue_for_retry",
                "reason": reason,
                "partial_confidence": avg_conf,
                "partial_votes": votes,
                "critique": critiques,
            }

    final_verdict = "Approve" if len(approvals) >= (len(votes) / 2 + 0.5) else "Reject"
    return {"confidence": avg_conf, "verdict": final_verdict, "critique": critiques}


def _safe_repo_target(repo_root, filepath):
    """Resolve *filepath* under *repo_root*, rejecting absolute paths, ``..``
    traversal, and symlinks that escape the repo. Returns the absolute path or
    None (logging the reason). Shared by the full-file and targeted-edit apply
    paths so both enforce identical containment."""
    if (not isinstance(filepath, str) or os.path.isabs(filepath)
            or ".." in filepath.replace("\\", "/").split("/")):
        logger.error(f"Refusing to apply fix with unsafe path: {filepath!r}")
        return None
    full_path = os.path.abspath(os.path.join(repo_root, filepath))
    try:
        if os.path.commonpath([repo_root, full_path]) != repo_root:
            logger.error(f"Refusing to apply fix escaping repo root: {filepath!r}")
            return None
    except ValueError:
        logger.error(f"Refusing to apply fix with unresolvable path: {filepath!r}")
        return None
    if os.path.islink(full_path):
        try:
            link_target = os.path.abspath(os.readlink(full_path))
            if os.path.commonpath([repo_root, link_target]) != repo_root:
                logger.error(f"Refusing to write through symlink escaping repo: {filepath!r}")
                return None
        except Exception:  # noqa: BLE001
            logger.error(f"Refusing to write through unresolvable symlink: {filepath!r}")
            return None
    return full_path


# Common English / report-boilerplate words that are NOT code identifiers — kept
# out of the file-windowing search so we anchor on real symbols (e.g. a mistyped
# function name) rather than noise words that match half the file.
_ISSUE_STOP_TOKENS = {
    "error", "cannot", "undefined", "reading", "variable", "reference",
    "referenceerror", "typeerror", "null", "function", "return", "const", "await",
    "async", "true", "false", "none", "type", "line", "view", "http", "https",
    "report", "context", "severity", "filed", "console", "unknown", "this", "that",
    "with", "from", "have", "hubversion", "webuiversion", "useragent", "currentview",
    "currentsubview", "currenttenant", "tenant", "user", "runtime", "browser",
    "button", "request", "feature", "what", "wrong", "auto", "find", "properties",
}


def _issue_identifiers(issue_body):
    """Pull candidate code identifiers from an issue body (prioritising the
    ``Error:`` line) so a large file can be windowed around the buggy region
    instead of blindly head-truncated. Returns identifiers most-relevant-first,
    de-noised of common English / markup words."""
    text = str(issue_body or "")
    seen, out = set(), []

    def _add(tok):
        k = tok.lower()
        if len(tok) < 4 or k in _ISSUE_STOP_TOKENS or k in seen:
            return
        seen.add(k)
        out.append(tok)

    err_lines = [ln for ln in text.splitlines()
                 if ln.strip().lower().lstrip("*").startswith("error")]
    for ln in err_lines:
        for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", ln):
            _add(t)
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text):
        _add(t)
    return out[:12]


def _targeted_file_context(content, identifiers, max_chars, window=60):
    """For a large file, return only the regions AROUND lines that mention one of
    *identifiers* (±*window* lines, overlapping windows merged), verbatim (no line
    numbers, so the model can copy an exact search snippet). Returns None when no
    identifier matches, so the caller falls back to head-truncation."""
    if not identifiers:
        return None
    lines = content.split("\n")
    n = len(lines)
    hits = [i for i, ln in enumerate(lines) if any(idn in ln for idn in identifiers)]
    if not hits:
        return None
    ranges = []
    for i in hits:
        s, e = max(0, i - window), min(n, i + window + 1)
        if ranges and s <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], e)
        else:
            ranges.append([s, e])
    parts, total = [], 0
    for s, e in ranges:
        seg = f"\n... (showing lines {s + 1}-{e}) ...\n" + "\n".join(lines[s:e])
        if total + len(seg) > max_chars:
            parts.append(seg[:max(0, max_chars - total)] + "\n... [window truncated] ...")
            break
        parts.append(seg)
        total += len(seg)
    return "\n".join(parts)


def apply_ai_fix(repo_path, issue_body, error_context=None, force_cloud=None, task_id=None, force_provider=None):
    config = load_config()
    max_files = int(config.get("FIX_MAX_FILES", CHAT_CONFIG_DEFAULTS["FIX_MAX_FILES"]) or CHAT_CONFIG_DEFAULTS["FIX_MAX_FILES"])
    max_file_chars = int(config.get("FIX_MAX_FILE_CHARS", CHAT_CONFIG_DEFAULTS["FIX_MAX_FILE_CHARS"]) or CHAT_CONFIG_DEFAULTS["FIX_MAX_FILE_CHARS"])
    max_ctx_chars = int(config.get("FIX_MAX_CONTEXT_CHARS", CHAT_CONFIG_DEFAULTS["FIX_MAX_CONTEXT_CHARS"]) or CHAT_CONFIG_DEFAULTS["FIX_MAX_CONTEXT_CHARS"])
    relevant_files = identify_files_to_fix(repo_path, issue_body)
    if not relevant_files:
        logger.warning(f"No specific files identified for issue. Attempting general fix.")
    # Bound the prompt: cap file count, per-file chars, and total chars so the
    # request stays under provider limits (groq 413, ollama truncation) and the
    # returned fix JSON parses cleanly instead of hitting "unmatched '}'".
    relevant_files = relevant_files[:max_files]
    identifiers = _issue_identifiers(issue_body)
    context_code = ""
    for f_path in relevant_files:
        full_p = os.path.join(repo_path, f_path)
        if os.path.exists(full_p):
            try:
                with open(full_p, 'r') as f:
                    content = f.read()
                if len(content) > max_file_chars:
                    # Large file: window around the buggy region (identifiers from
                    # the issue) so the model actually SEES the bug. A blind head
                    # truncation hides a bug that lives past the cutoff — e.g. line
                    # 20357 of a 22k-line file is never shown, so the model "fixes"
                    # blind and returns a truncated whole-file rewrite. Fall back to
                    # head-truncation only when no identifier matches.
                    windowed = _targeted_file_context(content, identifiers, max_file_chars)
                    if windowed is None:
                        content = _trunc(content, max_file_chars)
                        logger.info(f"apply_ai_fix: {f_path} head-truncated (no issue identifier matched)")
                    else:
                        content = windowed
                        logger.info(f"apply_ai_fix: {f_path} windowed around issue identifiers ({len(content)} chars)")
                context_code += f"\n--- FILE: {f_path} ---\n{content}\n"
                if len(context_code) >= max_ctx_chars:
                    context_code = context_code[:max_ctx_chars] + "\n…[context truncated to stay under provider limit]\n"
                    logger.info(f"apply_ai_fix context capped at {max_ctx_chars} chars across {len(relevant_files)} files")
                    break
            except Exception as e:
                logger.error(f"Could not read file {f_path}: {e}")
    # Ask for TARGETED search/replace edits, not a full-file rewrite: a local model
    # cannot faithfully reproduce a large file, and a truncated rewrite would delete
    # real code (the truncated-rewrite guard then aborts the whole fix). An edit
    # only needs the changed snippet, so it scales to any file size.
    fix_format = (
        "Return ONLY a JSON object with two keys:\n"
        "  \"confidence\": a float from 0.0 to 1.0, and\n"
        "  \"edits\": a list of targeted edits, each an object "
        "{\"file\": <path>, \"search\": <snippet>, \"replace\": <snippet>}.\n"
        "Rules for each edit:\n"
        "- \"search\" MUST be an EXACT substring of the current file content shown "
        "above — copy it character-for-character INCLUDING indentation, and include "
        "enough surrounding lines that it appears exactly once.\n"
        "- Make the SMALLEST change that fixes the issue. Do NOT return the whole "
        "file; return only the snippet(s) that change.\n"
        "- Multiple edits are allowed and may target different files.\n"
        "Example: {\"confidence\": 0.97, \"edits\": [{\"file\": \"WebUI/main.js\", "
        "\"search\": \"    await badName();\", \"replace\": \"    await goodName();\"}]}"
    )
    if error_context:
        prompt = (
            f"Issue: {issue_body}\n\n"
            f"Current Relevant Code:\n{context_code}\n\n"
            f"Previous attempt failed with error:\n{error_context}\n\n"
            f"{fix_format}"
        )
    else:
        prompt = (
            f"Issue: {issue_body}\n\n"
            f"Current Relevant Code:\n{context_code}\n\n"
            f"{fix_format}"
        )
    try:
        return call_llm(prompt, system_prompt="You are a master coder. Only return a JSON object.", force_cloud=force_cloud, task_id=task_id, force_provider=force_provider)
    except Exception as e:
        raise Exception(f"Fix generation failed: {e}")


def parse_and_apply(content, repo_path):
    import re as _re, ast as _ast
    # --- Locate a JSON / Python-dict object in the LLM response ---
    if not content or not content.strip():
        logger.debug("parse_and_apply: empty content — expected retry case.")
        return False, {}, 0.0

    try:
        match = _re.search(r'\{.*\}', content, _re.DOTALL)
        if not match:
            # LLM returned prose / "None" / refusal — no JSON object present.
            # This is a non-error transient failure; caller will retry.
            logger.debug(f"parse_and_apply: no JSON object in response (first 120 chars: {content[:120]!r})")
            return False, {}, 0.0

        raw = match.group()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: some LLMs (Gemini Flash) return Python-style dicts with
            # single quotes instead of JSON double quotes.  ast.literal_eval is
            # safe (only evaluates literals) and handles those cleanly.
            try:
                parsed = _ast.literal_eval(raw)
                if not isinstance(parsed, dict):
                    raise ValueError(f"Expected dict, got {type(parsed).__name__}")
                data = parsed
            except Exception as ast_err:
                logger.error(f"Error parsing or applying JSON fix: {ast_err}")
                logger.debug(f"Failed content: {content[:500]}")
                return False, {}, 0.0

        fixes = data.get("fixes", {}) or {}
        edits = data.get("edits", []) or []
        confidence = data.get("confidence", 0.0)
        repo_root = os.path.abspath(repo_path)
        applied = {}

        # --- 1) Targeted search/replace edits (preferred) -------------------
        # An edit changes only the snippet it names, so it scales to files the
        # model cannot reproduce whole and never trips the truncated-rewrite
        # guard. Edits to the same file compose in order. We only write (and mark
        # applied) files whose content actually changed.
        if isinstance(edits, list) and edits:
            orig_contents, file_contents = {}, {}
            for ed in edits:
                if not isinstance(ed, dict):
                    continue
                filepath = ed.get("file") or ed.get("filepath") or ed.get("path")
                search = ed.get("search")
                replace = ed.get("replace")
                if not isinstance(filepath, str) or not isinstance(search, str) or replace is None:
                    logger.error(f"Skipping malformed edit (need file/search/replace): {str(ed)[:160]!r}")
                    continue
                if search == "":
                    logger.error(f"Skipping edit with empty search for {filepath!r}")
                    continue
                full_path = _safe_repo_target(repo_root, filepath)
                if not full_path:
                    continue
                if not os.path.isfile(full_path):
                    logger.error(f"Skipping edit: target file does not exist: {filepath!r}")
                    continue
                if full_path not in file_contents:
                    try:
                        with open(full_path, encoding="utf-8", errors="replace") as ef:
                            file_contents[full_path] = ef.read()
                        orig_contents[full_path] = file_contents[full_path]
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"Could not read {filepath!r} for edit: {e}")
                        continue
                current = file_contents[full_path]
                count = current.count(search)
                if count == 0:
                    logger.error(f"Edit search snippet not found in {filepath!r}; skipping this edit")
                    continue
                if count > 1:
                    logger.warning(f"Edit search matches {count}× in {filepath!r}; applying to all occurrences")
                file_contents[full_path] = current.replace(search, str(replace))
            for full_path, new_content in file_contents.items():
                if new_content == orig_contents.get(full_path):
                    continue  # every edit for this file failed to match — no change
                rel = os.path.relpath(full_path, repo_root)
                with open(full_path, "w") as f:
                    f.write(new_content)
                applied[rel] = new_content
                logger.info(f"Applied targeted edit(s) to file: {rel}")

        # --- 2) Full-file replacements (legacy / new files) ------------------
        for filepath, code in fixes.items():
            # Confine writes to the cloned repo (abs/traversal/symlink escape).
            full_path = _safe_repo_target(repo_root, filepath)
            if not full_path:
                continue
            # SAFETY: never let a "fix" rewrite a large file into a stub. A model
            # given a TRUNCATED view of a big file sometimes returns only a
            # skeleton, or a placeholder like "rest of file unchanged", which would
            # DELETE the real code (e.g. a 22,832-line main.js collapsed to a
            # 28-line stub). Abort the WHOLE fix if the new content is a truncation
            # placeholder or drops a large fraction of an existing non-trivial file
            # — a truncated rewrite means the model's output can't be trusted.
            new_code = code.strip()
            existing = ""
            if os.path.isfile(full_path):
                try:
                    with open(full_path, encoding="utf-8", errors="replace") as ef:
                        existing = ef.read()
                except Exception:  # noqa: BLE001
                    existing = ""
            if existing:
                low = new_code.lower()
                trunc_markers = (
                    "too large to reproduce", "only the fix is shown", "rest of the file",
                    "rest of file unchanged", "unchanged from the original", "... unchanged",
                    "full file is too large", "truncated for brevity", "remainder of the file",
                    "existing code here", "keep the rest", "<full file", "rest of the code",
                    "// ... (rest", "# ... (rest",
                )
                if any(m in low for m in trunc_markers):
                    logger.error(
                        f"ABORTING fix: new content for {filepath} contains a truncation/placeholder "
                        f"marker — the model did not reproduce the whole file; applying it would delete "
                        f"real code."
                    )
                    return False, {}, 0.0
                old_lines = existing.count("\n") + 1
                new_lines = new_code.count("\n") + 1
                if old_lines >= 40 and new_lines < old_lines * 0.4:
                    logger.error(
                        f"ABORTING fix: writing {filepath} would shrink it from {old_lines} to "
                        f"{new_lines} lines (>60% deleted) — almost certainly a truncated rewrite, "
                        f"not a targeted fix."
                    )
                    return False, {}, 0.0
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(new_code)
            applied[filepath] = code
            logger.info(f"Applied fix to file: {filepath}")
        if not applied:
            logger.error("No fixes could be applied (all rejected as unsafe or out-of-repo).")
            return False, {}, 0.0
        return True, applied, confidence
    except Exception as e:
        logger.error(f"Error parsing or applying JSON fix: {e}")
        logger.debug(f"Failed content: {content[:500]}")
        return False, {}, 0.0


def _qa_service_verify(repo_name, config, timeout=120):
    """Call the QA service API to run targeted tests for a repo/module.

    Calls POST /api/run?module=<repo_name> and polls GET /api/status until
    COMPLETED or FAILED (or timeout).  Returns (passed: bool, summary: str).
    """
    qa_url = (config.get("QA_API_URL") or "").rstrip("/")
    if not qa_url:
        return None, "QA_API_URL not configured"

    # Map full repo name (owner/name) to just the module name the QA service knows.
    module = repo_name.split("/")[-1] if "/" in repo_name else repo_name

    try:
        # Trigger a targeted test run for this module.
        trigger = requests.post(
            f"{qa_url}/api/run",
            json={"module": module},
            timeout=15,
        )
        if trigger.status_code not in (200, 202):
            return None, f"QA service returned HTTP {trigger.status_code} on trigger"

        # Poll for completion.
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            status_resp = requests.get(f"{qa_url}/api/status", timeout=10)
            if status_resp.status_code != 200:
                continue
            data = status_resp.json()
            status = data.get("status", "")
            if status in ("COMPLETED", "FAILED", "IDLE"):
                results = data.get("results", [])
                passed = sum(1 for r in results if r.get("status") == "PASS")
                total = len(results)
                failed_names = [r["name"] for r in results if r.get("status") != "PASS"]
                summary = f"QA: {passed}/{total} passed"
                if failed_names:
                    summary += f" — failed: {', '.join(failed_names[:5])}"
                return status == "COMPLETED" and passed == total, summary

        return None, f"QA service timed out after {timeout}s"
    except Exception as e:
        return None, f"QA service error: {e}"


def verify_fix(repo_path, repo_name, config):
    logger.info(f"Verifying fix in {repo_path}...")

    # Priority 1: QA service API (when QA_API_URL is configured).
    if config.get("QA_API_URL"):
        passed, summary = _qa_service_verify(repo_name, config)
        if passed is not None:
            if passed:
                logger.info(f"QA service verification passed — {summary}")
                return True, None
            else:
                logger.warning(f"QA service verification failed — {summary}")
                return False, summary
        else:
            logger.warning(f"QA service unreachable ({summary}), falling back to local tests")

    # Priority 2: per-repo explicit test command from config.
    repo_tests = config.get("repo_tests", {})
    test_cmd = repo_tests.get(repo_name)
    if test_cmd:
        logger.info(f"Using per-repo test command for {repo_name}: {test_cmd}")
    else:
        qa_repo = os.getenv("QA_REPO")
        test_cmd = os.getenv("QA_TEST_COMMAND", "pytest")
        if qa_repo:
            logger.info(f"Using external QA repository: {qa_repo}")
            token = os.getenv("GITHUB_TOKEN")
            qa_path = os.path.join(os.path.dirname(repo_path), "qa_suite")
            if not os.path.exists(qa_path):
                url = f"https://{token}@github.com/{qa_repo}.git"
                qa_git = git.Repo.clone_from(url, qa_path)
                # Strip the token back out immediately — this repo is only ever read
                # from inside the sandbox (never pushed), so it has no reason to keep
                # a live credential sitting in .git/config.
                qa_git.remotes.origin.set_url(f"https://github.com/{qa_repo}.git")
                logger.info(f"Cloned QA repository to {qa_path}")
            logger.info(f"Executing QA command: {test_cmd}")
            full_cmd = f"{test_cmd} {repo_path}" if " " not in test_cmd else test_cmd
            result = run_sandboxed_command(full_cmd, qa_path)
            if result.returncode == 0:
                logger.info("External QA tests passed!")
                return True, None
            else:
                error_msg = result.stdout + result.stderr
                logger.error(f"External QA tests failed:\n{error_msg}")
                return False, error_msg
        else:
            if not test_cmd or test_cmd == "pytest":
                files = os.listdir(repo_path)
                if "package.json" in files: test_cmd = "npm test"
                elif "requirements.txt" in files or "pyproject.toml" in files: test_cmd = "python3 -m pytest"
                elif "go.mod" in files: test_cmd = "go test ./..."
                elif "Makefile" in files: test_cmd = "make test"
            if not test_cmd:
                logger.info("No standard test framework detected. Assuming success (blind apply).")
                return True, "No tests found, assuming success"
            logger.info(f"Executing test command: {test_cmd}")
            result = run_sandboxed_command(test_cmd, repo_path)
            if result.returncode == 0:
                logger.info("Tests passed successfully!")
                return True, None
            else:
                error_msg = result.stdout + result.stderr
                logger.error(f"Tests failed:\n{error_msg}")
                return False, error_msg
    result = run_sandboxed_command(test_cmd, repo_path)
    if result.returncode == 0:
        logger.info(f"Per-repo tests for {repo_name} passed!")
        return True, None
    else:
        error_msg = result.stdout + result.stderr
        logger.error(f"Per-repo tests for {repo_name} failed:\n{error_msg}")
        return False, error_msg


# In-flight claim: BugFixer runs as ONE process (background scan ThreadPoolExecutor
# + the FastAPI request thread that services the manual/reopen triggers), so a
# process-wide lock is enough to guarantee a given issue is worked by at most one
# worker at a time. Without it, the reopen re-queue and a concurrent scan both
# grabbed the same still-open issue, produced two competing fix commits (the loser
# couldn't fast-forward), and posted duplicate "resolved" comments — one citing a
# commit that never reached the default branch.
_inflight_lock = threading.Lock()
_inflight_issues = set()


def _claim_issue(issue_id):
    """Atomically claim *issue_id* for processing. Returns False if another worker
    already holds it (caller should skip — do NOT release what it didn't claim)."""
    with _inflight_lock:
        if issue_id in _inflight_issues:
            return False
        _inflight_issues.add(issue_id)
        return True


def _release_issue(issue_id):
    with _inflight_lock:
        _inflight_issues.discard(issue_id)


def process_single_issue(repo_name, issue_num, llm_preference=None):
    """Core logic to fix a single issue. Used by poller and manual triggers."""
    global state
    issue_id = f"{repo_name}:{issue_num}"
    if not _claim_issue(issue_id):
        logger.info(f"{issue_id}: already being processed by another worker — skipping duplicate.")
        return False, "Already being processed"
    try:
        config = load_config()
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            logger.error(f"Manual trigger failed: No GitHub Token configured.")
            return False, "No GitHub Token configured"

        gh_current = Github(token)
        try:
            repo_obj = gh_current.get_repo(repo_name)
            issue = repo_obj.get_issue(int(issue_num))
        except GithubException as ge:
            if ge.status == 410 or ge.status == 404:
                logger.warning(f"Issue {repo_name}:{issue_num} was deleted or not found. Removing from history.")
                processed = load_processed()
                if issue_id in processed:
                    del processed[issue_id]
                    save_processed(processed)
                    state["processed"] = processed
                return False, "Issue deleted"
            raise ge

        # --- Resume from awaiting_review ---
        processed = load_processed()
        issue_info = processed.get(issue_id, {})
        if issue_info.get("status") == "awaiting_review":
            last_attempt = issue_info.get("timestamp")
            if last_attempt:
                try:
                    ts = datetime.fromisoformat(last_attempt)
                    if (datetime.now() - ts).total_seconds() < 3600:
                        logger.info(f"Issue {issue_id} is awaiting review. Next retry in 1 hour.")
                        return False, "Review queued: Cloud LLM offline (retrying in 1 hour)"
                except:
                    pass
            logger.info(f"Resuming review for {issue_id} after 1 hour timeout.")
            # We will use the saved fixes later in the loop.

        update_task_state(task_id=issue_id, task_name=f"Triaging {issue_id}", action="start")
        actionable, request_msg = analyze_issue(issue)

        if not actionable:
            logger.info(f"Issue {repo_name}:{issue_num} is non-actionable: {request_msg}")
            try:
                issue.create_comment(f"🤖 **BugFixer Triage**\n\nThis issue is currently non-actionable. To help me fix this, please provide: {request_msg}")
            except Exception as ce:
                logger.warning(f"Could not post non-actionable comment to {issue_id}: {ce}")
            processed = load_processed()
            processed[f"{repo_name}:{issue_num}"] = {
                "status": "non-actionable",
                "timestamp": datetime.now().isoformat(),
                "reason": request_msg,
                "original_body": issue.body.strip() if issue.body else ""
            }
            save_processed(processed)
            state["processed"] = processed
            update_task_state(task_id=issue_id, action="end")
            return False, f"Non-actionable: {request_msg}"

        force_cloud = None
        force_provider = None
        if llm_preference == "cloud":
            force_cloud = True
        elif llm_preference == "local":
            force_cloud = False
        elif llm_preference == "claude":
            slot = _find_claude_cli_slot(config)
            if slot is None:
                logger.error("Claude CLI fix requested but no claude_cli provider is configured.")
                update_task_state(task_id=issue_id, action="end")
                return False, "Claude CLI is not configured in the LLM Vault."
            force_provider = slot
            logger.info(f"Claude CLI fix requested for {issue_id} — using provider slot {slot}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "repo")
            url = repo_obj.clone_url.replace("https://", f"https://{token}@")
            logger.info(f"Cloning {repo_name} for manual fix...")
            repo_git = git.Repo.clone_from(url, path)
            # Strip the token back out of .git/config right after cloning. The AI
            # fix, review, and test/verify steps below run untrusted repository code
            # inside run_sandboxed_command()'s Docker container, which mounts this
            # same directory with default network access — a hostile dependency or
            # test could otherwise read the live token straight out of the remote
            # URL and exfiltrate it. The token is re-applied only transiently, right
            # before each push/pull, via _authenticated_remote().
            repo_git.remotes.origin.set_url(repo_obj.clone_url)

            max_attempts = 3
            success = False
            error_context = None
            final_verdict = "Reject"
            final_confidence = 0.0
            base_branch = config.get("default_branch", "main")

            # File-a-Bug enrichment: if this issue was filed from the WebUI "File
            # a Bug" button, its body carries a hidden <!-- bug-report-id: <id>
            # --> marker. Pull the full console/HTML/screenshot from the hub and
            # append them to the body fed to the AI fix/review — the public
            # issue stays clean, but the AI gets the rich artifacts as context.
            # For auto-filed error issues, _module_log_fix_context appends the
            # source module's surrounding logs from the local hub-log mirror so
            # the fix is informed by related log data, not just the error snippet.
            fix_body = (issue.body or "") + _bug_report_fix_context(issue.body or "") + _module_log_fix_context(issue.body or "")

            for attempt in range(1, max_attempts + 1):
                try:
                    update_task_state(task_id=issue_id, task_name=f"Fix Attempt {attempt}/{max_attempts} for {issue_id}", action="start")
                    logger.info(f"AI Fix Attempt {attempt}/{max_attempts} for {repo_name}:{issue_num}...")

                    # --- Resume from awaiting_review ---
                    pending_fix = issue_info.get("pending_fix") if attempt == 1 and issue_info.get("status") == "awaiting_review" else None
                    if pending_fix:
                        logger.info(f"Resuming from queued review for {issue_id} using saved fix.")
                        success_applied, fixes, confidence = parse_and_apply(json.dumps(pending_fix), path)
                        # Clear the pending status now that we're processing it
                        processed = load_processed()
                        if issue_id in processed:
                            processed[issue_id]["status"] = "processing"
                            save_processed(processed)
                    elif not pending_fix:
                        fix_code = apply_ai_fix(path, fix_body, error_context, force_cloud=force_cloud, task_id=issue_id, force_provider=force_provider)
                        success_applied, fixes, confidence = parse_and_apply(fix_code, path)

                    if not success_applied:
                        verified = False
                        failure_msg = "AI generated invalid JSON format"
                    else:
                        if config.get("skip_review", False):
                            logger.info("Skeptical Reviewer bypassed by configuration.")
                            review_conf = confidence
                            review_verdict = "Approve"
                        else:
                            update_task_state(task_id=issue_id, task_name=f"Reviewing {issue_id}", action="start")
                            review = review_fix(path, fix_body, fixes, force_cloud=force_cloud, task_id=issue_id, builder_n=force_provider)

                            # --- Handle Queue for Retry ---
                            if isinstance(review, dict) and review.get("status") == "queue_for_retry":
                                logger.info(f"Review queued for {issue_id}: Cloud LLM offline. Saving fix for retry in 1 hour.")
                                processed = load_processed()
                                processed[issue_id] = {
                                    "status": "awaiting_review",
                                    "timestamp": datetime.now().isoformat(),
                                    "pending_fix": {"confidence": confidence, "fixes": fixes},
                                    "original_body": issue.body.strip() if issue.body else ""
                                }
                                save_processed(processed)
                                state["processed"] = processed
                                update_task_state(task_id=issue_id, action="end")
                                return False, "Cloud offline: Review queued for retry in 1 hour."

                            review_conf = review.get("confidence", 0.0)
                            review_verdict = review.get("verdict", "Reject")

                        if config.get("qa_enabled", True):
                            prepare_environment(path)
                            update_task_state(task_id=issue_id, task_name=f"Verifying {issue_id}", action="start")
                            verified, failure_msg = verify_fix(path, repo_name, config)
                        else:
                            logger.info("QA Testing disabled. Assuming verified.")
                            verified, failure_msg = True, "QA disabled"

                        if verified:
                            success = True
                            state["success_count"] += 1
                            final_confidence = (confidence + review_conf) / 2
                            final_verdict = review_verdict
                            break
                        else:
                            error_context = failure_msg
                except Exception as inner_e:
                    if "No LLM providers" in str(inner_e):
                        logger.error(f"No LLM providers configured for issue {issue_id}: {inner_e}")
                        update_task_state(task_id=issue_id, action="end")
                        return False, "No LLM providers configured"
                    raise

            if not success:
                state["failure_count"] += 1
                failure_reason = "AI failed to find a verified fix after max attempts."
                if error_context:
                    failure_reason += f" Last attempt error: {error_context}"

                try:
                    issue.create_comment(f"🤖 **BugFixer Failure**\n\nI attempted to fix this issue {max_attempts} times, but I could not find a solution that passed verification.\n\n**Final Error:** `{failure_reason}`")
                except Exception as ce:
                    logger.warning(f"Could not post failure comment to {issue_id}: {ce}")

                processed = load_processed()
                processed[f"{repo_name}:{issue_num}"] = {
                    "status": "failed",
                    "timestamp": datetime.now().isoformat(),
                    "error": failure_reason,
                    "original_body": issue.body
                }
                save_processed(processed)
                state["processed"] = processed
                update_task_state(task_id=issue_id, action="end")
                return False, failure_reason

            # Triage-only mode: discard the generated changes, post a comment, defer fix.
            if _is_triage_only():
                try:
                    repo_git.git.reset("--hard", "HEAD")
                    repo_git.git.clean("-fd")
                except Exception:
                    pass
                try:
                    mode_reason = "Blackout active" if state.get("blackout") else "Triage-only mode enabled"
                    issue.create_comment(
                        f"🔍 **BugFixer Triage** — A fix has been identified for this issue.\n\n"
                        f"Fix commit is being held back ({mode_reason}). "
                        f"BugFixer will apply the fix automatically once restrictions are lifted."
                    )
                    issue.add_to_labels("bugfixer-triaged")
                except Exception as ce:
                    logger.warning(f"Could not post triage comment to {issue_id}: {ce}")
                processed = load_processed()
                processed[f"{repo_name}:{issue_num}"] = {
                    "status": "triaged",
                    "timestamp": datetime.now().isoformat(),
                    "reason": "Fix identified; commit deferred (triage-only mode)",
                    "original_body": issue.body or "",
                }
                save_processed(processed)
                state["processed"] = processed
                update_task_state(task_id=issue_id, action="end")
                return True, "Triaged — fix identified, commit deferred"

            repo_git.git.add(A=True)

            confidence_threshold = 0.95
            is_trusted = (repo_name in config["trusted_repos"]) or (repo_name == resolve_self_diagnosis_repo(config))
            bot_user = gh_current.get_user().login
            is_owner = repo_obj.owner.login == bot_user
            direct_push_setting = config.get("direct_push_enabled")
            can_direct_push = direct_push_setting and is_trusted and is_owner

            logger.info(f"Deployment decision for {repo_name}: DirectPushSetting={direct_push_setting}, IsTrusted={is_trusted}, IsOwner={is_owner} -> can_direct_push={can_direct_push}")


            version_bumped = False
            new_v = None
            can_actually_direct_push = False
            new_v = None
            if can_direct_push and final_verdict == "Approve":
                new_v = bump_repo_version(path)
                if new_v:
                    version_bumped = True
                    logger.info(f"Bumped target repository {repo_name} version to {new_v}")

            # COMMIT the fix (+ any version bump) BEFORE any push. The direct-push
            # path below used to push HEAD *before* this commit ran, so it pushed
            # the pre-fix base (a no-op) and the fix commit only ever existed
            # LOCALLY — yet the issue was told "pushed to main / Commit <sha>" and
            # closed, while nothing landed on origin (the false-fix bug). Commit
            # first so the push actually carries the fix.
            commit_msg = f"AI Fix #{issue.number}: {issue.title[:50]}..."
            if version_bumped:
                commit_msg += f" (Version Bump to {new_v})"
            repo_git.git.add(A=True)  # re-stage so a version bump made after the earlier add(A=True) is included
            repo_git.index.commit(commit_msg)

            if can_direct_push and final_verdict == "Approve":
                try:
                    with _authenticated_remote(repo_git.remotes.origin, repo_obj.clone_url, token):
                        repo_git.remotes.origin.push(f"HEAD:{base_branch}")
                    can_actually_direct_push = True
                    decision_reason = "Trusted repo & approved"
                except Exception as pe:
                    logger.warning(f"Direct push failed for {repo_name} ({pe}). Attempting rebase...")
                    try:
                        with _authenticated_remote(repo_git.remotes.origin, repo_obj.clone_url, token):
                            repo_git.remotes.origin.pull(base_branch, rebase=True)
                            repo_git.remotes.origin.push(f"HEAD:{base_branch}")
                        can_actually_direct_push = True
                        decision_reason = "Trusted repo & approved (after rebase)"
                        logger.info(f"Push successful after rebase for {repo_name}")
                    except Exception as re_err:
                        logger.warning(f"Direct push failed even after rebase: {re_err}. Falling back to PR.")
                        decision_reason = f"Direct push failed: {re_err}"
                        can_actually_direct_push = False

            if can_actually_direct_push:
                logger.info(f"Decision: Direct Commit to {base_branch}. Reason: {decision_reason}")
                commit_type = "Direct Commit"
                detail_msg = f"The fix was verified and pushed directly to the {base_branch} branch. Avg Confidence: {final_confidence:.2%}"
                # Fix is live on main — tell every spoke to pull and restart, then let
                # the QA service verify against the updated code.
                _trigger_spoke_updates(config)
                _wait_for_spokes_online(config, min_count=1, timeout=90)
            else:
                reason = "Skeptical Reviewer rejected" if final_verdict != "Approve" else (decision_reason if not can_direct_push or "Direct push failed" in decision_reason else "Trust/Ownership requirements not met")
                decision_reason = reason
                logger.info(f"Decision: Pull Request. Reason: {reason}.")
                target_branch = config.get("dev_branch", "dev") if final_confidence < confidence_threshold else f"ai-fix-issue-{issue.number}"
                try:
                    repo_git.git.checkout(target_branch)
                except:
                    repo_git.create_head(target_branch).checkout()
                with _authenticated_remote(repo_git.remotes.origin, repo_obj.clone_url, token):
                    repo_git.remotes.origin.push(target_branch, force=True)
                base_branch = config.get("default_branch", "main")

                existing_pr = find_existing_pull_request(repo_obj, target_branch, base_branch)

                if existing_pr:
                    pr = existing_pr
                    logger.info(f"Found existing open PR for {target_branch} -> {base_branch}: {pr.html_url}")
                else:
                    try:
                        pr = repo_obj.create_pull(
                            title=f"AI Fix #{issue.number}",
                            body=f"Automated fix for issue #{issue.number}. Avg Confidence: {final_confidence:.2%}",
                            head=target_branch,
                            base=base_branch
                        )
                        logger.info(f"Created new PR for {target_branch} -> {base_branch}: {pr.html_url}")
                    except GithubException as ge:
                        if ge.status == 422:
                            logger.warning(
                                f"PR creation returned 422 (likely already exists for "
                                f"{target_branch} -> {base_branch}). Re-checking for existing PR..."
                            )
                            time.sleep(2)
                            existing_pr = find_existing_pull_request(repo_obj, target_branch, base_branch)
                            if existing_pr:
                                pr = existing_pr
                                logger.info(
                                    f"Found existing open PR after 422 error: {pr.html_url}"
                                )
                            else:
                                logger.error(
                                    f"Could not find existing PR after 422 error for "
                                    f"{target_branch} -> {base_branch}. Re-raising."
                                )
                                raise ge
                        else:
                            raise ge

                commit_type = "Pull Request"
                detail_msg = f"The fix was verified and a Pull Request has been created on branch {target_branch}: {pr.html_url}"


            files_list = ", ".join(fixes.keys()) if fixes else "No files changed"
            commit_hash = repo_git.head.commit.hexsha

            comment_body = (
                f"🤖 **BugFixer AI Update**\n\n"
                f"The issue has been successfully resolved via {commit_type}.\n"
                f"{detail_msg}\n\n"
                f"**Changes:**\n- Files modified: `{files_list}`\n- Commit: `{commit_hash[:7]}`\n\n"
                f"Verification: ✅ Tests passed successfully."
            )
            try:
                issue.create_comment(comment_body)
            except Exception as ce:
                logger.warning(f"Could not post success comment to {issue_id}: {ce}")

            is_log_detected = "log-detected" in [lbl.name for lbl in issue.get_labels()]
            issue_id = f"{repo_name}:{issue_num}"
            if not is_log_detected:
                # Resolved + closed immediately: apply the closed label, record the terminal
                # `closed` status, and move this issue out of Resolved into Closed. The
                # success_count += 1 above (QA pass) is undone here — the issue is Closed, not
                # Resolved. Log-detected issues stay open for the production verification period.
                issue.edit(state='closed')
                _notify_bug_fixed(issue)  # LM "File a Bug" → hub → UI shows "Fixed"
                _apply_closed_label(repo_obj, issue, issue_id)
                state["success_count"] = max(0, state["success_count"] - 1)
                state["closed_count"] = state.get("closed_count", 0) + 1
                new_status = "closed"
            else:
                new_status = "awaiting_prod_verification"

            processed = load_processed()
            processed[issue_id] = {
                "status": new_status,
                "timestamp": datetime.now().isoformat(),
                "commit": commit_hash,
                "commit_msg": commit_msg,
                "files": list(fixes.keys()),
                "commit_type": commit_type,
                "decision_reason": decision_reason,
                "original_body": issue.body
            }

            save_processed(processed)
            state["processed"] = processed
            state["daily_fixes_count"] = state.get("daily_fixes_count", 0) + 1

            update_task_state(task_id=issue_id, action="end")
            return True, f"Fixed via {commit_type}"

    except Exception as e:
        logger.exception(f"Error in process_single_issue: {e}")
        try:
            update_task_state(task_id=issue_id, action="end")
        except Exception as cleanup_err:
            logger.error(f"Failed to clean up task state for {issue_id}: {cleanup_err}")
        return False, str(e)
    finally:
        # Always release the claim — on success, failure, or exception — so a later
        # legitimate re-trigger (e.g. operator Reopen) isn't permanently blocked.
        _release_issue(issue_id)


__all__ = [
    'analyze_issue',
    'identify_files_to_fix',
    'prepare_environment',
    'review_fix',
    'apply_ai_fix',
    'parse_and_apply',
    'verify_fix',
    '_qa_service_verify',
    'process_single_issue',
    'run_sandboxed_command',
    '_authenticated_remote',
    'QueueLocalException',
]
