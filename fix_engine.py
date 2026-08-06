"""AI fix pipeline: issue analysis, sandboxed fix generation/application, verification, and per-issue orchestration (extracted from main.py)."""
import contextlib, git, json, os, re, requests, tempfile, threading, time
from datetime import datetime
from github import Github, GithubException

from main import (
    CHAT_CONFIG_DEFAULTS,
    _apply_closed_label,
    _bug_report_fix_context,
    _CODE_SLOTS,
    _REVIEW_SLOTS,
    _module_log_fix_context,
    _find_claude_cli_slot,
    _get_provider_config,
    _get_escalation_models,
    _is_ollama,
    _ollama_base_url,
    _ollama_models_detailed,
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
    recompute_issue_counters,
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
_FIX_COMMIT_RE = re.compile(r'Commit:\s*`?([0-9a-f]{7,40})`?')


def _regression_triage_context(repo_git, issue, prior_commit=None, prior_files=None):
    """For a REOPENED, previously-fixed issue: figure out what changed the fix's
    files SINCE our fix landed, so the builder triages from the regression cause
    instead of re-analysing the whole error from scratch.

    Resolves the prior fix sha from the processed record or, failing that, BugFixer's
    "Commit: `<sha>`" resolved comment on the issue. Then, in the fresh clone, runs
    `git log <sha>..HEAD` and `git diff <sha>..HEAD` scoped to the fix's files.
    Returns a context block to append to fix_body (or "" if there's nothing useful —
    no known fix sha, sha not in history, or the files were untouched since)."""
    sha = (prior_commit or "").strip()
    if not sha:
        try:
            for c in issue.get_comments():
                m = _FIX_COMMIT_RE.search(c.body or "")
                if m:
                    sha = m.group(1)  # keep the LAST (most recent) fix commit cited
        except Exception:  # noqa: BLE001
            pass
    if not sha:
        return ""
    try:
        repo_git.git.cat_file("-e", f"{sha}^{{commit}}")  # sha present in this clone?
    except Exception:  # noqa: BLE001
        return ""
    files = [f for f in (prior_files or []) if f and "VERSION" not in f]
    if not files:
        try:
            files = [ln.strip() for ln in repo_git.git.show(
                sha, "--name-only", "--pretty=format:").splitlines()
                if ln.strip() and "VERSION" not in ln]
        except Exception:  # noqa: BLE001
            files = []
    try:
        rng = f"{sha}..HEAD"
        log = repo_git.git.log(rng, "--oneline", "--", *files) if files else repo_git.git.log(rng, "--oneline")
        diff = repo_git.git.diff(rng, "--", *files) if files else ""
    except Exception:  # noqa: BLE001
        return ""
    if not log.strip():
        return ""  # our fix's files are untouched since — nothing to trace back to
    if len(diff) > 8000:
        diff = diff[:8000] + "\n… [diff truncated] …"
    logger.info(f"Regression triage: fix {sha[:10]}, {len(files)} file(s), {len(log.splitlines())} commit(s) since.")
    return (
        "\n\n--- REGRESSION CONTEXT (issue was previously fixed, then reopened) ---\n"
        f"BugFixer previously fixed this as commit {sha[:10]}. Since then the file(s) it "
        "touched were changed again — that most likely REINTRODUCED the bug.\n"
        f"Commits to those files since the fix:\n{log}\n\n"
        f"Diff of those files since the fix:\n{diff or '(no line changes captured)'}\n"
        "Trace the regression to the change above and fix from there — do not re-solve "
        "the original error from scratch."
    )


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
    # A human explicitly filed this via "File a Bug" — that human IS the triage. Its
    # console/DOM/screenshot live on the hub and are pulled as fix context. Always attempt
    # it: skip the flaky LLM actionability check, which is non-deterministic and (when the
    # hub-stored artifacts are evicted) keeps demanding "the browser console output" we
    # can't re-supply. A genuinely unfixable report surfaces at the FIX step, not here.
    if _BUG_REPORT_ID_RE.search(issue.body or ""):
        logger.info("Triage: user-filed 'File a Bug' report — treating as actionable (skipping LLM triage).")
        return True, ""

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
        res = call_llm(prompt, system_prompt="You are a triage bot. Only return a JSON object.", task_kind="triage")
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


# Frequent words that can look identifier-ish in a report but are never the symbol
# we're hunting; keeps the bare-token pass from grepping for English prose. Includes
# browser/user-agent tokens that leak in from the captured console context.
_IDENT_STOPWORDS = frozenset({
    "error", "report", "issue", "severity", "context", "filed", "fixed",
    "webui", "https", "http", "github", "console", "trace", "traceback", "stack",
    "function", "variable", "undefined", "return", "async", "await", "const", "class",
    # browser / user-agent noise from the captured console/DOM
    "applewebkit", "mozilla", "gecko", "khtml", "safari", "chrome", "firefox",
    "edge", "version", "windows", "macintosh", "linux", "webkit", "x11", "intel",
    "wow64", "trident", "opera", "mobile", "android", "iphone", "ipad",
})

# Error-message shapes that explicitly name a missing/offending symbol. Highest
# signal — a "Can't find variable: X" / "X is not defined" names the exact token.
_IDENT_PATTERNS = (
    r"can'?t find variable:?\s*([A-Za-z_$][\w$]+)",
    r"\b([A-Za-z_$][\w$]+)\s+is not defined\b",
    r"name '([A-Za-z_][\w]+)' is not defined",
    r"has no attribute '([A-Za-z_][\w]+)'",
    r"cannot find (?:name|module|symbol|variable) '?([A-Za-z_$][\w$]+)'?",
    r"ReferenceError:\s*([A-Za-z_$][\w$]+)",
    r"NameError:[^']*'([A-Za-z_][\w]+)'",
    r"AttributeError:[^']*'([A-Za-z_][\w]+)'",
)

# Binary / asset extensions never worth reading for a symbol grep.
_GREP_SKIP_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".gz", ".tgz",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov", ".bin", ".so", ".pyc",
    ".lock", ".map", ".min.js", ".min.css",
})


def _looks_code_like(tok):
    """True if a bare token looks like a code symbol (camelCase, snake_case, has a
    digit or $) rather than an ordinary English word — so the grep pass targets real
    identifiers, not prose."""
    if "_" in tok or "$" in tok or any(c.isdigit() for c in tok):
        return True
    # camelCase / PascalCase boundary: an inner lower→UPPER transition.
    return any(a.islower() and b.isupper() for a, b in zip(tok, tok[1:]))


def _extract_issue_identifiers(issue_body):
    """Pull likely code symbols out of an issue body, most-specific first: explicit
    error-message captures, then quoted/backticked code-ish tokens, then bare
    code-like identifiers. Returns an ordered, de-duplicated, capped list."""
    import re
    text = issue_body or ""
    # Drop HTML-comment markers (<!-- bug-report-id: bd1f… -->, <!-- report-type: … -->,
    # <!-- bf-module: … -->) so their hex ids/values aren't mistaken for code symbols.
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    found = []

    def _add(tok):
        if not tok:
            return
        tok = tok.strip()
        if len(tok) < 4 or tok.lower() in _IDENT_STOPWORDS or tok in found:
            return
        # Pure-hex tokens ≥8 chars are IDs (bug-report ids, commit SHAs) — the
        # File-a-Bug body mentions its id in backticks too, past the HTML-comment strip.
        low = tok.lower()
        if len(low) >= 8 and all(c in "0123456789abcdef" for c in low):
            return
        found.append(tok)

    for pat in _IDENT_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            for g in m.groups():
                _add(g)
    for m in re.finditer(r"[`'\"]([A-Za-z_$][\w$.]{3,})[`'\"]", text):
        _add(m.group(1))
    for m in re.finditer(r"\b([A-Za-z_$][\w$]{3,})\b", text):
        tok = m.group(1)
        if _looks_code_like(tok):
            _add(tok)
    return found[:12]


def _grep_files_for_identifiers(repo_path, all_files, identifiers):
    """Rank repo files by how many of the issue's identifiers they literally contain.

    The LLM file-guesser biases toward plausible-looking files and misses the actual
    fix site (e.g. it kept picking backend .py for a `ensureLDAPTennants` typo that
    lives in WebUI/main.js). A literal grep for the named symbol is deterministic —
    the file that CONTAINS the token is almost always where the fix goes. Ranked by
    distinct-identifiers-matched, then total hits."""
    if not identifiers:
        return []
    scores = {}
    for rel in all_files:
        low = rel.lower()
        if any(low.endswith(ext) for ext in _GREP_SKIP_EXT):
            continue
        full = os.path.join(repo_path, rel)
        try:
            if os.path.getsize(full) > 2_000_000:  # skip huge/generated files
                continue
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        distinct = total = 0
        for ident in identifiers:
            c = content.count(ident)
            if c:
                distinct += 1
                total += c
        if distinct:
            scores[rel] = (distinct, total)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [rel for rel, _ in ranked]


def _extract_error_symbols(issue_body):
    """The highest-signal identifiers ONLY — the exact symbol named by an error message
    (ReferenceError / "X is not defined" / NameError / AttributeError / "can't find
    variable: X"). These pin the fix site precisely, unlike incidental code tokens that
    happen to appear in the report."""
    import re
    text = re.sub(r"<!--.*?-->", " ", issue_body or "", flags=re.DOTALL)
    out = []
    for pat in _IDENT_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            for g in m.groups():
                if not g:
                    continue
                g = g.strip()
                low = g.lower()
                if len(g) < 4 or low in _IDENT_STOPWORDS or g in out:
                    continue
                if len(low) >= 8 and all(c in "0123456789abcdef" for c in low):
                    continue
                out.append(g)
    return out


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

    # Deterministic anchor: grep the repo for the exact symbols named in the issue
    # (e.g. "Can't find variable: ensureLDAPTennants") and rank those files FIRST —
    # the file that literally contains the token is the fix site, regardless of what
    # the LLM guesses. Capped so it augments, not floods, the candidate list.
    identifiers = _extract_issue_identifiers(issue_body)
    grep_hits = _grep_files_for_identifiers(repo_path, all_files, identifiers)[:8]
    if identifiers:
        logger.info("File identification: issue identifiers %s → grep matched %d file(s)%s",
                    identifiers[:6], len(grep_hits),
                    (": " + ", ".join(grep_hits[:5])) if grep_hits else " (none)")

    # Precise anchor: if the issue names an exact error symbol (ReferenceError /
    # "X is not defined" …), the file(s) literally containing THAT symbol are the fix
    # site. Restrict the builder to them (skip the LLM guess + the incidental-token
    # matches) so models — especially the small CPU rungs — aren't diluted into editing
    # unrelated files. Only kicks in when the symbol resolves to a small set of files.
    error_symbols = _extract_error_symbols(issue_body)
    symbol_hits = _grep_files_for_identifiers(repo_path, all_files, error_symbols) if error_symbols else []
    if symbol_hits and len(symbol_hits) <= 4:
        focused = symbol_hits[:3]
        logger.info("File identification: error symbol(s) %s → focusing the fix on %s",
                    error_symbols[:3], ", ".join(focused))
        return focused

    file_list_str = "\n".join(all_files)
    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Repository File List:\n{file_list_str}\n\n"
        "Identify which files are most likely relevant to fixing this issue. "
        "Return ONLY a JSON array of file paths: [\"path/to/file1\", \"path/to/file2\"]"
    )
    llm_files = []
    try:
        res = call_llm(prompt, system_prompt="You are a repository analyzer. Only return a JSON array of paths.", task_kind="identify_files")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            llm_files = json.loads(match.group())
    except Exception as e:
        if is_llm_cooldown_error(e):
            logger.warning(f"File identification deferred — LLM providers cooling down: {e}")
        else:
            logger.error(f"Error identifying files: {e}")
        # A grep hit alone is still a usable answer even if the LLM leg failed.
        return grep_hits

    # Merge: grep hits first (literal symbol location beats a guess), then any
    # LLM-suggested files not already covered. De-duped, order preserved.
    merged = list(grep_hits)
    for f in (llm_files or []):
        if isinstance(f, str) and f not in merged:
            merged.append(f)
    return merged


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



# ── Review-panel tool access (fetch_repo_file) ──────────────────────────────
# The panel is normally fed only a diff/patch, which can be truncated or
# simply not show a referenced symbol's definition (it lives outside the
# changed hunk). Rather than reject on "can't verify" — the exact failure
# mode that produced a false-negative Reject on cs#65 — a reviewer can call
# this tool to pull the ACTUAL file content from the commit being reviewed
# and settle the question. Read-only, scoped to the SAME repo/commit already
# under review: this is the same content a human reviewer sees browsing the
# repo on GitHub, not a broader system-state probe — contrast chat.py's
# CHAT_TOOLS, which redact results (_sanitize_tool_result there) because they
# CAN reach other tenants'/system state a single source file never does.
#
# Only wired up when the caller passes repo+head_sha (i.e. pr_review.py's PR
# pre-review path). Every other review_fix caller (the bot-fix pipeline,
# which has no GitHub repo/commit context) is completely unaffected — falls
# through to the exact prior single-turn call, unchanged.
_REVIEW_TOOLS = [
    {"type": "function", "function": {
        "name": "fetch_repo_file",
        "description": "Fetch the FULL content of a file from this repo at the "
                        "commit being reviewed. Use this when the diff doesn't "
                        "show whether a referenced symbol (function, route, "
                        "constant) actually exists, or when the diff was "
                        "truncated and you need more of a file than you were "
                        "shown. Do not guess or default to rejecting when a "
                        "tool call would settle it — but don't call this for "
                        "every trivial PR either; most reviews don't need it.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string",
                                                "description": "repo-relative file path"}},
                       "required": ["path"]},
    }},
]
_REVIEW_TOOL_MAX_ITER = 3
_REVIEW_TOOL_MAX_FILES = 5
_REVIEW_FILE_MAX_CHARS = 20000
# Some local models emit a tool call as "<tool_call>{...}</tool_call>" TEXT
# instead of the structured tool_calls field (the same shape chat.py's agent
# loop already guards against). Kept as its own minimal copy rather than an
# import from chat.py, to keep this change isolated to the review path.
_REVIEW_TOOLCALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _parse_review_text_tool_calls(text):
    if not text or "<tool_call>" not in text:
        return text, []
    calls = []
    for m in _REVIEW_TOOLCALL_TAG_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        fn = obj.get("function") if isinstance(obj.get("function"), dict) else {}
        name = obj.get("name") or fn.get("name")
        args = obj.get("arguments") or fn.get("arguments") or {}
        if name:
            calls.append({"function": {"name": name, "arguments": args}})
    return _REVIEW_TOOLCALL_TAG_RE.sub("", text).strip(), calls


def _fetch_repo_file_for_review(repo, head_sha, path):
    """Executor for fetch_repo_file — read-only, bounded, never raises (a bad
    path/binary/oversized file degrades to an {"error": ...} the reviewer can
    react to instead of crashing the panel turn)."""
    try:
        c = repo.get_contents(path, ref=head_sha)
    except Exception as e:  # noqa: BLE001
        return {"error": f"could not fetch {path}@{head_sha}: {e}"}
    raw = getattr(c, "decoded_content", None)
    if raw is None:
        return {"error": f"{path} has no fetchable content (directory or binary?)"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"{path} is not valid UTF-8 (binary file?)"}
    return {"path": path, "content": _trunc(text, _REVIEW_FILE_MAX_CHARS),
            "truncated": len(text) > _REVIEW_FILE_MAX_CHARS}


def _run_reviewer_turn(prompt, system_prompt, provider_n, model, task_id, repo, head_sha):
    """One reviewer's turn. With repo+head_sha AND a provider that can actually
    receive `tools=`, runs a bounded tool-calling loop (fetch_repo_file, up to
    _REVIEW_TOOL_MAX_ITER turns / _REVIEW_TOOL_MAX_FILES files) before
    producing final text. Otherwise falls back to the exact prior single-turn
    call — this includes claude_cli, which silently DROPS `tools=`
    (_request_claude_cli never receives the param; the whole conversation is
    serialised into a plain CLI prompt) — telling it about a fetch_repo_file
    tool it has no way to invoke made it try to fake a tool call in prose
    instead of returning clean JSON (bugfixer#730/#731: "Expecting ':'
    delimiter" / "Extra data" parse errors on every claude_cli review), so the
    tool-primed addendum below is only added when the provider will really see
    it as a callable tool.

    Returns the reviewer's final text (same plain-string contract call_llm's
    non-tools return already had) so the caller's existing JSON-extraction
    regex needs no changes."""
    provider = None
    if provider_n:
        try:
            provider = _get_provider_config(provider_n, load_config())[0]
        except Exception:  # noqa: BLE001
            provider = None
    supports_tools = not (repo is None or head_sha is None or (provider or "").lower().strip() == "claude_cli")
    if not supports_tools:
        return call_llm(prompt, system_prompt=system_prompt, force_provider=provider_n,
                        task_id=task_id, model_override=model)

    prompt = prompt + (
        "\n\nThe diff above may be TRUNCATED (large files/diffs are capped). "
        "You have a fetch_repo_file tool that reads the ACTUAL file at this "
        "commit — use it to confirm whether a referenced symbol exists, or "
        "to see the rest of a file the diff cut off, INSTEAD OF rejecting "
        "because you can't verify something. Most reviews won't need it; "
        "use it when a specific, nameable uncertainty would change your "
        "verdict, not as a first step."
    )
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}]
    files_fetched = 0
    last_text = ""
    for _ in range(_REVIEW_TOOL_MAX_ITER):
        result = call_llm("", messages=messages, tools=_REVIEW_TOOLS,
                          force_provider=provider_n, task_id=task_id, model_override=model)
        if not isinstance(result, dict):
            return str(result or "")
        text = result.get("text") or ""
        tool_calls = result.get("tool_calls") or []
        if not tool_calls:
            text, tool_calls = _parse_review_text_tool_calls(text)
        last_text = text
        if not tool_calls:
            return text
        messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            raw_args = fn.get("arguments") if fn else tc.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:  # noqa: BLE001
                args = {}
            if name == "fetch_repo_file" and files_fetched < _REVIEW_TOOL_MAX_FILES:
                files_fetched += 1
                out = _fetch_repo_file_for_review(repo, head_sha, str(args.get("path") or ""))
            elif name == "fetch_repo_file":
                out = {"error": "file-fetch budget exhausted for this review (%d files) "
                                "— decide from what you've already seen" % _REVIEW_TOOL_MAX_FILES}
            else:
                out = {"error": f"unknown tool: {name}"}
            messages.append({"role": "tool", "name": name or "unknown",
                             "content": json.dumps(out)[:_REVIEW_FILE_MAX_CHARS + 500],
                             "tool_call_id": tc.get("id") or f"call_{name}"})
    # Iteration cap reached without a tool-free turn — use whatever text the
    # last turn produced (existing JSON-extraction just won't find a match if
    # it's incomplete, same as any other malformed reviewer response today).
    return last_text


def review_fix(repo_path, issue_body, proposed_fixes, force_cloud=None, task_id=None,
               builder_n=None, diff_override=None, repo=None, head_sha=None):
    """Run a cross-provider reviewer panel on a proposed fix.

    builder_n: which provider slot (1/2/3) generated the fix being reviewed.
    Reviewers are all OTHER configured providers — the builder is never asked
    to review its own work.  If builder_n is None, it's inferred from force_cloud.
    Pass builder_n=0 when there is no builder to exclude (e.g. a human-authored
    PR pre-review) so EVERY configured provider reviews.

    diff_override: a pre-computed diff string to review INSTEAD of computing one
    from ``repo_path``'s working tree. This lets a caller review a diff that is
    not checked out — e.g. pr_review feeding a GitHub PR's diff into the same
    panel. When given, ``repo_path`` may be None and ``proposed_fixes`` empty.

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

    # Build the reviewer panel. The dedicated review pool (7-8) wins when any of
    # it is configured: those slots exist so a strong judging model is not also
    # spent on triage and file identification. Nothing is excluded from it for
    # being the builder -- a review slot never builds, so the conflict the
    # exclusion guards against cannot arise there.
    #
    # With no review slot configured, fall back to the original behaviour: code
    # slots minus the builder, so an install that has not set up the new pool
    # behaves exactly as before.
    review_pool = tuple()
    try:
        review_pool = tuple(
            n for n in _REVIEW_SLOTS
            if _provider_configured(*_get_provider_config(n, config)[:3])
        )
    except Exception as e:
        logger.warning(f"Error checking review slots configuration: {e}")
        # Fall back to code slots if there's an error in review slot configuration
        pass

    # Ensure we always have a valid pool (fallback to _CODE_SLOTS)
    pool, skip_builder = (review_pool, False) if review_pool else (_CODE_SLOTS, True)

    reviewers = []
    for n in pool:
        if skip_builder and n == builder_n:
            continue
        try:
            provider, key, model, _ = _get_provider_config(n, config)
            if not _provider_configured(provider, key, model):
                continue
            reviewers.append({"name": f"Reviewer {n} ({provider})", "model": model, "provider_n": n})
        except Exception as e:
            logger.warning(f"Error configuring reviewer for slot {n}: {e}")
            # Continue to next reviewer instead of failing completely
            continue

    if not reviewers:
        logger.warning("No reviewers configured. Falling back to default LLM review.")
        reviewers = [{"name": "Default Reviewer", "model": None, "provider_n": None}]

    # Check if any provider is online at all.
    any_provider_online = any(
        state.get(f"provider_{n}_online", True) for n in pool
        if not (skip_builder and n == builder_n)
    )
    if not any_provider_online:
        logger.warning("All reviewer LLM providers appear offline. Signaling retry queue.")
        return {"status": "queue_for_retry", "reason": "all_reviewers_offline"}

    # Show reviewers the actual working-tree DIFF (parse_and_apply already wrote
    # the change) rather than dumping full file bodies — a targeted edit to a large
    # file would otherwise flood the review prompt with ~1MB of unchanged code and
    # blow the provider limit. Fall back to (capped) file bodies if no diff.
    fix_details = ""
    if diff_override is not None:
        # Caller supplied the diff (e.g. a PR diff not checked out locally) —
        # review it directly, skip the working-tree git diff.
        diff_text = str(diff_override or "")
    else:
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
        "NOTE: Small, targeted changes that correct obvious typos or naming mismatches are considered high-signal; "
        "if they correctly address the issue without regressions, they SHOULD be approved.\n\n"
        "Return ONLY a JSON object: {\"confidence\": float, \"verdict\": \"Approve\"|\"Reject\", \"critique\": \"detailed explanation\"}\n"
        "\"confidence\" MUST be a fraction between 0.0 and 1.0 — e.g. 0.95 for 95% confidence. Do NOT return a 0-100 percentage.\n"
        "CRITICAL RULES: Your confidence score IS the decision. If you believe the fix is correct with >= 0.90 confidence, you MUST return 'Approve'. A 'Reject' verdict is only valid when you genuinely doubt the fix (confidence < 0.90). Do NOT give a high confidence score alongside a 'Reject' — that's contradictory and will cause the fix to be unnecessarily kicked back."
    )
    # NOTE: the tool-primed addendum used to be appended here, to one shared
    # `prompt` handed to every reviewer regardless of provider. Moved into
    # _run_reviewer_turn, which resolves each reviewer's actual provider and
    # only adds it for providers that can actually receive `tools=` — see its
    # docstring for why (claude_cli silently drops tools and got confused).

    votes = []
    failed_reviewers = []
    for r in reviewers:
        try:
            logger.info(f"{r['name']} analyzing fix...")
            res = _run_reviewer_turn(
                prompt,
                "You are a skeptical senior engineer. Be critical. Only return JSON.",
                r["provider_n"], r.get("model"), task_id, repo, head_sha,
            )
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if match:
                _v = json.loads(match.group())
                _v["confidence"] = _norm_confidence(_v.get("confidence"))
                votes.append({**_v, "reviewer": r["name"]})
        except Exception as e:
            if is_llm_cooldown_error(e):
                logger.warning(f"{r['name']} deferred — LLM providers cooling down: {e}")
            else:
                logger.error(f"{r['name']} failed: {e}")
            failed_reviewers.append(r["name"])

    if not votes:
        # Every configured reviewer errored (transient network/provider issue,
        # a tool-calling edge case, a malformed response) — NOT the panel
        # actually judging the fix. This used to return a hard 0%-confidence
        # Reject, which reads identically to "the panel looked at this and
        # rejected it" everywhere it's rendered (PR row, GitHub comment) even
        # though no reviewer produced an opinion at all. Queue for retry
        # instead, same as the pre-check above for all-offline — the caller
        # (process_single_issue) already handles this status by retrying in an
        # hour; PR review's _render_panel already renders it as "Panel
        # unavailable this pass" rather than a false verdict.
        _names = ", ".join(failed_reviewers) if failed_reviewers else "reviewer(s)"
        logger.warning("review_fix: all %d reviewer(s) failed to produce a parseable response (%s).",
                       len(reviewers), _names)
        return {"status": "queue_for_retry", "reason": "all_reviewers_failed: %s" % _names}

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

    # If avg confidence >= 90%, approve regardless of vote split — high-confidence
    # Rejections are contradictory and cause unnecessary kickbacks.
    if avg_conf >= 0.90:
        final_verdict = "Approve"
    elif len(approvals) >= (len(votes) / 2 + 0.5):
        final_verdict = "Approve"
    else:
        final_verdict = "Reject"
    return {"confidence": avg_conf, "verdict": final_verdict, "critique": critiques}


def _crosscheck_review(repo_path, issue_body, slot, models, exclude_model, config, task_id=None):
    """CPU ensemble cross-check: run EVERY installed local model (on `slot`) as a
    reviewer of the working-tree diff, except the one that built it. Returns
    {confidence, verdict, votes, n} — the CPU-side consensus (majority Approve +
    average confidence). Confidence >= 90% is treated as implicit approval — reviewers
    who give high confidence but say "Reject" are contradictory and should not block the fix."""
    try:
        diff_text = git.Repo(repo_path).git.diff("HEAD")
    except Exception:  # noqa: BLE001
        diff_text = ""
    if len(diff_text) > 20000:
        diff_text = diff_text[:20000] + "\n… [diff truncated] …"
    prompt = (
        f"Issue Description: {issue_body}\n\n"
        f"Proposed Fix (working-tree diff vs HEAD):\n{diff_text}\n\n"
        "You are a pragmatic senior engineer reviewing this diff. APPROVE it if it "
        "plausibly addresses the described issue and does not introduce a regression — a "
        "small, targeted change that directly fixes the reported error (e.g. correcting a "
        "misspelled identifier / typo, or a clear off-by-one) is a GOOD fix and SHOULD be "
        "approved. You are only shown the diff, not the whole file, so do NOT reject "
        "merely because you can't see surrounding context — assume referenced symbols "
        "exist unless the diff itself is clearly wrong. REJECT only if the change is "
        "clearly incorrect, incomplete, or harmful. "
        "Return ONLY JSON: {\"confidence\": float, \"verdict\": \"Approve\"|\"Reject\", \"critique\": \"...\"}\n"
        "\"confidence\" MUST be a fraction between 0.0 and 1.0 — e.g. 0.95 for 95% confidence. Do NOT return a 0-100 percentage.\n"
        "IMPORTANT: Your confidence score drives the final decision — if you give >= 0.90 confidence, "
        "the fix is accepted regardless of verdict, so do NOT give a 'Reject' verdict with high confidence "
        "(that's contradictory and wastes time). Approve and give >= 0.90 if the fix looks correct."
    )
    reviewers = [m for m in models if not (m and m == exclude_model)]
    logger.info(f"CPU cross-check: reviewing the diff with {len(reviewers)} local model(s) "
                f"{reviewers} — each is a fresh model load, can be slow on CPU for big models.")
    votes = []
    for i, mdl in enumerate(reviewers, 1):
        logger.info(f"  cross-check reviewer {i}/{len(reviewers)}: {mdl}…")
        _t0 = time.monotonic()
        try:
            res = call_llm(prompt, system_prompt="You are a pragmatic senior code reviewer. Approve correct fixes; reject only real problems. Only return JSON.",
                           force_provider=slot, model_override=mdl, task_id=task_id)
            m = re.search(r"\{.*\}", res, re.DOTALL)
            if m:
                v = json.loads(m.group())
                v["confidence"] = _norm_confidence(v.get("confidence"))
                v["model"] = mdl
                votes.append(v)
                logger.info(f"  ↳ {mdl}: {v.get('verdict')} @ {float(v.get('confidence', 0)):.0%} "
                            f"({time.monotonic() - _t0:.0f}s)")
        except Exception as e:  # noqa: BLE001
            if is_llm_cooldown_error(e):
                logger.warning(f"cross-check reviewer {mdl} deferred (cooldown): {e}")
            else:
                logger.warning(f"cross-check reviewer {mdl} failed ({time.monotonic() - _t0:.0f}s): {e}")
    if not votes:
        return {"confidence": 0.0, "verdict": "Reject", "votes": [], "n": 0}
    approvals = [v for v in votes if v.get("verdict") == "Approve"]
    avg = sum(v.get("confidence", 0.0) for v in votes) / len(votes)
    # Confidence >= 90% is implicit approval — avoids kickbacks from high-confidence
    # contradictory Rejections (same fix applied at _review_votes line ~601).
    if avg >= 0.90:
        verdict = "Approve"
    elif len(approvals) >= (len(votes) / 2 + 0.5):
        verdict = "Approve"
    else:
        verdict = "Reject"
    logger.info(f"CPU cross-check: {len(approvals)}/{len(votes)} approve, avg confidence {avg:.0%}")
    return {"confidence": avg, "verdict": verdict, "votes": votes, "n": len(votes)}


#: Failure kind -> operator-facing label. The pipeline knows precisely why it
#: gave up; before this the UI showed a generic sentence and the reason lived
#: only in the logs.
_FAILURE_LABELS = {
    "review_rejected": "Reviewers rejected the fix",
    "low_confidence":  "Confidence below threshold",
    "qa_failed":       "Fix failed QA tests",
    "error":           "Error during fix",
    "unknown":         "No verified fix found",
}


def _failure_summary(last_failure, attempts):
    """One line naming why the run gave up, cause first.

    Shown truncated in the status table, so the distinguishing part -- the label
    and any confidence numbers -- has to come before the boilerplate, not after.
    """
    kind = (last_failure or {}).get("kind") or "unknown"
    label = _FAILURE_LABELS.get(kind, _FAILURE_LABELS["unknown"])
    conf = (last_failure or {}).get("confidence")
    thr = (last_failure or {}).get("threshold")
    bits = []
    try:
        if conf is not None:
            bits.append(f"confidence {float(conf):.0%}"
                        + (f" < required {float(thr):.0%}" if thr is not None else ""))
    except (TypeError, ValueError):
        pass
    detail = str((last_failure or {}).get("detail") or "").strip()
    if detail:
        bits.append(detail[:160])
    head = f"{label} after {attempts} attempt(s)"
    return head + (" — " + "; ".join(bits) if bits else "")


def _norm_confidence(value):
    """Coerce a reviewer's ``confidence`` to the 0.0–1.0 scale every consumer assumes.

    Models answer this prompt on BOTH scales — 0.95 and 95 alike — because "95%
    confidence" reads naturally either way. Left raw, a 0–100 answer sails past
    every gate keyed to a fraction (SKIP_CONFIDENCE_THRESHOLD 0.80, the
    ``avg >= 0.90`` implicit approval in both panels, the ``>= 0.999`` auto-commit
    gate) and renders as "confidence 9500%" in the PR comment. Anything above 1 is
    therefore read as a percentage; the result is clamped so a nonsense value can
    never exceed certainty. Non-numeric/missing → 0.0 (no confidence, not a crash).
    """
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.0
    if c != c or c in (float("inf"), float("-inf")):   # NaN / inf
        return 0.0
    if c > 1.0:
        c /= 100.0
    return max(0.0, min(1.0, c))


def _model_param_b(name):
    """Parse a model's parameter count in billions from its name, e.g.
    'qwen2.5-coder:14b' → 14.0, 'llama3.1:8b' → 8.0, 'deepseek-coder-v2:16b' → 16.0.
    None if not determinable (then it's never filtered out)."""
    import re
    m = re.search(r'(\d+(?:\.\d+)?)b\b', str(name).lower())
    return float(m.group(1)) if m else None


def _filter_ensemble_models(detailed, config):
    """Drop local models below the configured minimum parameter size
    (ensemble_min_model_b, default 0 = keep all) — e.g. set 14 to skip 7b/8b that
    reliably whiff. Models whose size can't be parsed are kept. Never returns empty:
    if the filter would drop everything, keep the single largest model."""
    try:
        min_b = float(config.get("ensemble_min_model_b", 0) or 0)
    except (TypeError, ValueError):
        min_b = 0
    if min_b <= 0:
        return detailed
    kept = [d for d in detailed
            if (_model_param_b(d.get("name")) is None or _model_param_b(d.get("name")) >= min_b)]
    if not kept and detailed:
        kept = [max(detailed, key=lambda d: d.get("size", 0))]
    dropped = len(detailed) - len(kept)
    if dropped:
        logger.info(f"Ensemble: skipping {dropped} local model(s) under {min_b:g}B "
                    f"(ensemble_min_model_b); {len(kept)} remain.")
    return kept


def _run_cpu_ensemble(path, repo_git, repo_name, fix_body, config, task_id,
                      local_models, external_slots, cpu_target, min_conf):
    """CPU-first ensemble fixer (P1 = local CPU, anything P2+ = external).

    Per external slot (ramped P2 → P3 → …, i.e. one CPU ratchet cycle each): build
    with P1's models smallest→largest; cross-check each candidate with EVERY local
    model; once a candidate clears ``cpu_target`` AND passes QA, confirm it with that
    cycle's single external slot. Approve → commit. Reject → next external slot (one
    more CPU ratchet). If every external slot rejects a CPU-confident fix → hold for
    human review (NO auto-commit).

    Returns (outcome, fixes, confidence):
      'commit' — working tree holds a cross-checked + externally-confirmed + QA'd fix.
      'human'  — a CPU-confident fix was externally rejected on every cycle.
      'fail'   — the CPU ensemble never reached the target (caller may punt to build)."""
    err = None
    reached_target_ever = False
    for cycle, ext_slot in enumerate(external_slots, start=1):
        logger.info(f"CPU ensemble cycle {cycle}/{len(external_slots)} — external confirmer = P{ext_slot}.")
        for mdl in local_models:
            try:
                repo_git.git.reset("--hard", "HEAD")
                repo_git.git.clean("-fd")
            except Exception:  # noqa: BLE001
                pass
            # Name the WORK, not just the mechanism. This used to read
            # "CPU build <model>", which says what the machine is doing but not
            # what it is doing it TO -- and the dashboard renders only the task
            # NAME, so an operator watching a multi-minute ensemble had no way to
            # tell which issue was in flight, or how far through the cycles it
            # was. task_id already identifies the issue; carry it, plus the file
            # under edit and the cycle position.
            # Only name the target when it is actually a FILE. `path` is
            # sometimes the repo root (the multi-file fix path passes the clone
            # dir), and basename() on that yields the directory name -- the task
            # read "· repo", which looks like a filename and tells you nothing.
            # A directory carries no more information than task_id already does,
            # so omit the segment entirely rather than print something wrong.
            _tgt = os.path.basename(path) if (path and os.path.isfile(path)) else ""
            update_task_state(
                task_id=task_id,
                task_name=(f"Fixing {task_id}" + (f" · {_tgt}" if _tgt else "") +
                           f" — CPU build {mdl} (cycle {cycle}/{len(external_slots)})"),
                action="start")
            fix_code = apply_ai_fix(path, fix_body, err, force_provider=1, model_override=mdl, task_id=task_id)
            ok, fixes, _conf = parse_and_apply(fix_code, path)
            if not ok:
                _why = "; ".join(getattr(parse_and_apply, "last_failures", []) or [])
                if _why:
                    err = (f"Your previous edits could NOT be applied: {_why}. The \"search\" "
                           f"value must be copied VERBATIM from the code shown to you. Note the "
                           f"code is shown as SEPARATE, NON-CONTIGUOUS regions — never build a "
                           f"snippet that spans two regions or includes a '... (showing lines ...)' "
                           f"marker, because that text does not exist in the file. Pick a shorter "
                           f"anchor from INSIDE one region.")
                else:
                    err = ("Your previous attempt returned an invalid or empty fix. Return ONLY "
                           "a JSON object in the required format, with real file paths inside "
                           "the repository.")
                logger.info("Build rejected — feeding apply-failure back: %s", (_why or "(no detail)")[:300])
                continue
            cross = _crosscheck_review(path, fix_body, 1, local_models, mdl, config, task_id)
            if cross["verdict"] != "Approve" or cross["confidence"] < cpu_target:
                # Carry the reviewers' ACTUAL critiques into err, which is passed to
                # the next apply_ai_fix as error_context ("Previous attempt failed
                # with error: ..."). This used to be the bare percentage, so each
                # retry re-solved the same problem blind — six build+review cycles
                # with no new information, which is why the score sat at an
                # identical value instead of converging. The external-rejection path
                # below already threaded its critique; this one dropped it.
                _crit = "; ".join(
                    f"[{v.get('model') or 'reviewer'}] {str(v.get('critique', '')).strip()}"
                    for v in (cross.get("votes") or [])
                    if str(v.get("critique", "")).strip()
                )
                err = (f"Your previous attempt was REJECTED by the local reviewer panel "
                       f"(confidence {cross['confidence']:.0%} from {cross['n']} reviewer(s), "
                       f"below the required {cpu_target:.0%}). Reviewer feedback: "
                       f"{_crit or '(no critique returned)'}. "
                       f"Address these specific points; do not repeat the same change.")
                logger.info("CPU cross-check %.0f%% (%d reviewers) below target %.0f%% — "
                            "ratchet up. Feeding critique forward: %s",
                            cross['confidence'] * 100, cross['n'], cpu_target * 100,
                            (_crit or '(none)')[:300])
                continue
            reached_target_ever = True
            if config.get("qa_enabled", True):
                prepare_environment(path)
                verified, vmsg = verify_fix(path, repo_name, config)
                if not verified:
                    err = f"CPU-confident fix failed tests: {vmsg}"
                    logger.info(err)
                    continue
            # Optional: a UNANIMOUS ~100% CPU consensus can skip the external check
            # entirely (opt-in). The local ensemble already agreed completely.
            if (config.get("ensemble_skip_external_at_full")
                    and cross["verdict"] == "Approve" and cross["confidence"] >= 0.999):
                logger.info(f"CPU cross-check unanimous ~100% ({cross['n']} reviewers) — "
                            "skipping external confirmation, auto-commit.")
                return "commit", fixes, cross["confidence"]
            # Single external confirmation with THIS cycle's slot. The external slot
            # is the AUTHORITY: the local pool generates candidates cheaply (a build
            # sees ~38k chars of windowed source and emits full file edits), while a
            # review sees only the capped diff and emits a short verdict — so the
            # paid/GPU provider spends a small fraction of the tokens while holding
            # the commit decision.
            ext = _crosscheck_review(path, fix_body, ext_slot, [None], None, config, task_id)
            _crit = "; ".join(str(v.get("critique", "")).strip()
                              for v in ext.get("votes", []) if v.get("critique"))
            # Confidence floor on the AUTHORITY. Until now the external stage checked
            # only the verdict word: an Approve at 55% committed exactly like one at
            # 99%, so the local models were held to a strict tunable bar while the
            # provider that actually decides was trusted unconditionally. Floored on
            # the external's OWN confidence, deliberately NOT on the blended mean —
            # blending would make the bar for the paid reviewer depend on how poorly
            # the local model scored, penalising the wrong side.
            try:
                _ext_floor = float(config.get("EXTERNAL_MIN_CONFIDENCE", 0.90) or 0.90)
            except (TypeError, ValueError):
                _ext_floor = 0.90
            _ext_floor = max(0.0, min(1.0, _ext_floor))
            if ext["verdict"] == "Approve" and ext["confidence"] >= _ext_floor:
                logger.info(f"External P{ext_slot} APPROVED (conf {ext['confidence']:.0%} "
                            f">= floor {_ext_floor:.0%}). Auto-commit.")
                return "commit", fixes, (cross["confidence"] + ext["confidence"]) / 2
            if ext["verdict"] == "Approve":
                # Approved, but not confident ENOUGH. Not a rejection — so iterate
                # with this reviewer's feedback and let the CPU rebuild against it,
                # keeping the SAME external slot as the judge. This is the loop that
                # lets the authority raise a candidate to standard instead of the
                # choice being a bare commit-or-abandon.
                err = (f"The reviewing model APPROVED your previous attempt but only at "
                       f"{ext['confidence']:.0%} confidence, below the required "
                       f"{_ext_floor:.0%}. Its feedback: {_crit or '(none given)'}. "
                       f"Revise to resolve those concerns so the reviewer can be "
                       f"confident; do not resubmit the same change.")
                logger.info("External P%s approved at %.0f%% but below floor %.0f%% — "
                            "ratcheting with its feedback: %s", ext_slot,
                            ext["confidence"] * 100, _ext_floor * 100,
                            (_crit or '(none)')[:300])
                continue  # next local builder, SAME external judge
            logger.warning(f"External P{ext_slot} REJECTED (conf {ext['confidence']:.0%}) a CPU-confident fix — "
                           f"one more CPU ratchet. Reason: {_crit or '(no critique given)'}")
            err = f"External P{ext_slot} rejected the CPU-confident fix: {_crit}"
            break  # end this cycle; escalate to the next external slot
    return ("human" if reached_target_ever else "fail"), None, 0.0


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


def _targeted_file_context(content, identifiers, max_chars, window=60, max_hits_per_id=4):
    """For a large file, return only the regions AROUND lines that mention the
    issue's identifiers — RAREST identifier first — verbatim (no line numbers, so
    the model can copy an exact search snippet). Returns None when no identifier
    matches, so the caller falls back to head-truncation.

    Ranking by rarity is essential: a distinctive symbol (e.g. the exact mistyped
    function name) usually pinpoints the bug AND sits next to its correctly-spelled
    twin, while a common word from the issue ("ldap", "user") can match hundreds of
    lines. Emitting windows in file order let the common word fill the whole budget
    with noise before ever reaching the buggy region, so the model saw only "symbol
    not found" and invented a no-op stub instead of fixing the typo. We instead emit
    the rarest identifiers' neighbourhoods first (capped per identifier), so the
    pinpoint region — with its correct twin — is always in-context."""
    if not identifiers:
        return None
    lines = content.split("\n")
    n = len(lines)
    id_hits = []
    for idn in identifiers:
        hits = [i for i, ln in enumerate(lines) if idn in ln]
        if hits:
            id_hits.append((len(hits), idn, hits))
    if not id_hits:
        return None
    id_hits.sort(key=lambda t: t[0])  # rarest (most distinctive) identifier first

    covered = []  # emitted [start, end) ranges — skip windows that overlap these

    def _overlaps(s, e):
        return any(s < ce and cs < e for cs, ce in covered)

    parts, total = [], 0
    for _cnt, _idn, hits in id_hits:
        for i in hits[:max_hits_per_id]:
            s, e = max(0, i - window), min(n, i + window + 1)
            if _overlaps(s, e):
                continue
            seg = f"\n... (showing lines {s + 1}-{e}) ...\n" + "\n".join(lines[s:e])
            if total + len(seg) > max_chars:
                # Budget exhausted. The rare/distinctive regions are emitted first,
                # so whatever is already collected is the most relevant context.
                if parts:
                    return "\n".join(parts)
                return seg[:max(0, max_chars)] + "\n... [window truncated] ..."
            parts.append(seg)
            covered.append((s, e))
            total += len(seg)
    return "\n".join(parts) if parts else None


def apply_ai_fix(repo_path, issue_body, error_context=None, force_cloud=None, task_id=None, force_provider=None, model_override=None):
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
        "- The code above is shown as SEPARATE, NON-CONTIGUOUS regions, each preceded "
        "by a '... (showing lines N-M) ...' marker. Those marker lines are NOT part of "
        "the file, and two regions shown next to each other are NOT adjacent in the "
        "file. A \"search\" must come from INSIDE a single region and must never span "
        "a marker or a region boundary — such a snippet cannot exist in the file and "
        "the edit will be discarded.\n"
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
        return call_llm(prompt, system_prompt="You are a master coder. Only return a JSON object.", force_cloud=force_cloud, task_id=task_id, force_provider=force_provider, model_override=model_override, task_kind="fix")
    except Exception as e:
        raise Exception(f"Fix generation failed: {e}")


def _relaxed_edit_span(haystack, needle):
    """Locate *needle* in *haystack* tolerating whitespace-only differences.

    Exact `str.count` matching drops an edit for a single trailing space, a
    tab-vs-spaces indent, or CRLF — differences a model reproducing a snippet by
    eye gets wrong constantly, and which have no semantic meaning here.

    Each line is matched by its stripped content with flexible surrounding
    whitespace. Returns the ORIGINAL (start, end) span so the real file text is
    what gets replaced. Returns None unless the relaxed match is UNIQUE — an
    ambiguous match must never be applied blind, which is the safety property the
    exact matcher gave for free.
    """
    import re as _re
    lines = (needle or "").replace("\r\n", "\n").split("\n")
    if not any(l.strip() for l in lines):
        return None
    parts = []
    for ln in lines:
        stripped = ln.strip()
        parts.append(r"[ \t]*" + _re.escape(stripped) + r"[ \t]*" if stripped else r"[ \t]*")
    try:
        matches = list(_re.finditer(r"\r?\n".join(parts), haystack))
    except _re.error:
        return None
    return matches[0].span() if len(matches) == 1 else None


def parse_and_apply(content, repo_path):
    import re as _re, ast as _ast
    parse_and_apply.last_failures = []   # reset per call; read by the retry loop
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
            # Reasons an edit could not be applied, published on the function
            # attribute below so the retry loop can tell the model exactly which
            # anchor missed. Kept off the return value to preserve the existing
            # (ok, fixes, confidence) contract used by three call sites.
            _miss = parse_and_apply.last_failures = []
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
                    # Exact match failed — try the whitespace-tolerant matcher before
                    # discarding the edit.
                    _span = _relaxed_edit_span(current, search)
                    if _span:
                        logger.info(f"Edit matched {filepath!r} only after whitespace "
                                    f"normalisation — applying (unique match).")
                        file_contents[full_path] = (current[:_span[0]] + str(replace)
                                                    + current[_span[1]:])
                        continue
                    # Record WHAT failed so the retry can be told. parse_and_apply
                    # returns only (ok, fixes, conf), so the detail was previously
                    # logged and lost — leaving the next attempt to guess.
                    _first = (search.strip().splitlines() or [""])[0][:160]
                    _miss.append(f"{filepath}: search snippet not found (starts with: {_first!r})")
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
            _detail = "; ".join(parse_and_apply.last_failures or []) or \
                      "all edits rejected as unsafe or out-of-repo"
            logger.error(f"No fixes could be applied ({_detail}).")
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
        # Was this issue reopened (by the operator or a recurrence)? If so, its
        # eventual close/resolve is tallied in the ReOpened buckets, and the flag
        # is carried into the terminal processed entry.
        was_reopened = bool(issue_info.get("reopened"))
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

        # Surface the issue in the Status table AS SOON AS work starts, so a long CPU
        # fix isn't invisible there (it's otherwise only an Active Task until it reaches
        # a terminal status). Terminal handlers + the exception path overwrite this.
        try:
            processed[issue_id] = {
                **issue_info,
                "status": "processing",
                "title": getattr(issue, "title", "") or issue_info.get("title", ""),
                "original_body": issue_info.get("original_body") or (getattr(issue, "body", "") or "")[:2000],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_processed(processed)
            state["processed"] = processed
        except Exception:  # noqa: BLE001
            pass

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
            # Confidence escalation: when the user hasn't pinned a provider, walk the
            # configured provider slots in order across attempts (a cheap first pass
            # → escalate) instead of retrying the SAME model. A fix that verifies but
            # whose confidence is below MIN_BUILDER_CONFIDENCE is not accepted — we
            # escalate to the next provider (e.g. CPU → GPU → Cloud). Last provider's
            # verified fix is accepted even if below the bar (best effort).
            min_conf = float(config.get("MIN_BUILDER_CONFIDENCE", 0.80) or 0.80)
            escalate = (force_cloud is None and force_provider is None)
            # Build ladder: ordered [(slot, model_override)] steps. Each slot expands
            # to its escalation_models (a manual ratchet like 7b→14b→32b), OR — when a
            # model entry is "*" on an ollama slot — to EVERY model installed on that
            # endpoint, sorted ASCENDING by size so it ramps smallest-first (never
            # starts at 32b). Only after the whole local ratchet fails does it move to
            # the next (GPU/cloud) slot.
            ladder = []
            if escalate:
                for _n in (1, 2, 3, 4):
                    _p, _k, _m, _u = _get_provider_config(_n, config)
                    if not _provider_configured(_p, _k, _m):
                        continue
                    _models = _get_escalation_models(_n, config)
                    if _is_ollama(_p) and any(x == "*" for x in _models):
                        detailed = sorted(_ollama_models_detailed(_ollama_base_url(_p, _u)),
                                          key=lambda d: d.get("size", 0))
                        detailed = _filter_ensemble_models(detailed, config)
                        _models = [d["name"] for d in detailed] or [_m]
                        logger.info(f"Slot P{_n}: ramp-by-size across {len(_models)} local model(s): {', '.join(_models)}")
                    for _mdl in _models:
                        ladder.append((_n, _mdl))
                if len(ladder) > 1:
                    max_attempts = max(max_attempts, len(ladder))
                else:
                    escalate = False  # nothing to escalate to
            success = False
            error_context = None
            # WHY the last attempt failed, structured. error_context is written for
            # the NEXT builder prompt, so it is phrased as instructions to a model
            # and gets buried behind boilerplate by the time it reaches the UI.
            # This is the operator-facing answer to "why did this fail", kept as
            # (kind, detail, confidence, threshold) so the UI can lead with the
            # cause instead of truncating it away.
            last_failure = {}
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
            # Inject the project skills ("agents") so the fix follows their recipes +
            # boundaries (dual-copy rules, add-simulation touch-points, etc.).
            try:
                from skills_loader import skills_context as _skills_ctx
                _sk = _skills_ctx()
                if _sk:
                    fix_body += "\n\n" + _sk
            except Exception:
                pass
            # Reopened + previously fixed → prepend "what changed since our fix" so the
            # builder triages from the regression cause, not from zero.
            if was_reopened:
                try:
                    _reg = _regression_triage_context(
                        repo_git, issue,
                        issue_info.get("prior_fix_commit") or issue_info.get("commit"),
                        issue_info.get("prior_fix_files") or issue_info.get("files"))
                    if _reg:
                        fix_body += _reg
                        logger.info(f"Added regression-triage context for reopened {issue_id}.")
                except Exception as _re:  # noqa: BLE001
                    logger.debug(f"regression triage context skipped for {issue_id}: {_re}")

            # ── CPU-first ensemble (opt-in via local_ensemble) ──────────────────
            # P1 = local CPU: ratchet its models smallest→largest, cross-check with
            # EVERY local model to a high bar, then confirm with one external slot
            # (ramp P2→P3). External-reject on every cycle → human review (no commit).
            _skip_normal_loop = False
            if escalate and bool(config.get("local_ensemble")):
                _p1, _k1, _m1, _u1 = _get_provider_config(1, config)
                _local_models = []
                if _provider_configured(_p1, _k1, _m1) and _is_ollama(_p1):
                    _local_models = [d["name"] for d in _filter_ensemble_models(sorted(
                        _ollama_models_detailed(_ollama_base_url(_p1, _u1)),
                        key=lambda d: d.get("size", 0)), config)] or [_m1]
                _external = [n for n in (2, 3, 4)
                             if _provider_configured(*_get_provider_config(n, config)[:3])]
                if _local_models and _external:
                    cpu_target = float(config.get("CPU_CROSSCHECK_TARGET", 0.90) or 0.90)
                    logger.info(f"CPU ensemble: {len(_local_models)} local model(s), external P{_external}, target {cpu_target:.0%}.")
                    _outcome, _ens_fixes, _ens_conf = _run_cpu_ensemble(
                        path, repo_git, repo_name, fix_body, config, issue_id,
                        _local_models, _external, cpu_target, min_conf)
                    if _outcome == "commit":
                        success, fixes, confidence = True, _ens_fixes, _ens_conf
                        final_confidence, final_verdict = _ens_conf, "Approve"
                        state["success_count"] += 1
                        _skip_normal_loop = True
                    elif _outcome == "human":
                        try:
                            repo_git.git.reset("--hard", "HEAD"); repo_git.git.clean("-fd")
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            issue.create_comment(
                                "🧑‍⚖️ **BugFixer — held for human review**\n\nThe CPU ensemble reached "
                                f"high confidence on a fix, but every external reviewer (P{_external}) "
                                "rejected it, so it was NOT auto-committed. A human should review.")
                            issue.add_to_labels("bugfixer-needs-human")
                        except Exception as ce:  # noqa: BLE001
                            logger.warning(f"human-review note failed for {issue_id}: {ce}")
                        processed = load_processed()
                        processed[issue_id] = {
                            "status": "awaiting_human",
                            "timestamp": datetime.now().isoformat(),
                            "reason": "external reviewers rejected a CPU-confident fix",
                            "original_body": issue.body or "",
                        }
                        save_processed(processed); state["processed"] = processed
                        recompute_issue_counters(processed)
                        update_task_state(task_id=issue_id, action="end")
                        return False, "Held for human review (external reviewers rejected the fix)"
                    # _outcome == "fail" → fall through to the normal slot loop.

            for attempt in range(1, (0 if _skip_normal_loop else max_attempts) + 1):
                try:
                    update_task_state(task_id=issue_id, task_name=f"Fix Attempt {attempt}/{max_attempts} for {issue_id}", action="start")
                    logger.info(f"AI Fix Attempt {attempt}/{max_attempts} for {repo_name}:{issue_num}...")

                    # Escalation: pick THIS attempt's builder from the ladder (cheap
                    # first, escalate on low confidence). Pins to the user's choice
                    # when one was given.
                    att_fc, att_fp, att_model = force_cloud, force_provider, None
                    if escalate and ladder:
                        att_fp, att_model = ladder[(attempt - 1) % len(ladder)]
                        att_fc = None
                        logger.info(f"Attempt {attempt}/{max_attempts}: builder = P{att_fp} model={att_model or 'slot default'}.")

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
                        fix_code = apply_ai_fix(path, fix_body, error_context, force_cloud=att_fc, task_id=issue_id, force_provider=att_fp, model_override=att_model)
                        success_applied, fixes, confidence = parse_and_apply(fix_code, path)

                    if not success_applied:
                        verified = False
                        failure_msg = "AI generated invalid JSON format"
                    else:
                        critique = ""
                        if config.get("skip_review", False):
                            logger.info("Skeptical Reviewer bypassed by configuration.")
                            review_conf = confidence
                            review_verdict = "Approve"
                        else:
                            update_task_state(task_id=issue_id, task_name=f"Reviewing {issue_id}", action="start")
                            review = review_fix(path, fix_body, fixes, force_cloud=att_fc, task_id=issue_id, builder_n=att_fp)

                            # --- Handle Queue for Retry ---
                            if isinstance(review, dict) and review.get("status") == "queue_for_retry":
                                _q_reason = review.get("reason") or "reviewers unavailable"
                                logger.info(f"Review queued for {issue_id}: {_q_reason}. Saving fix for retry in 1 hour.")
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
                                return False, f"Review queued for retry in 1 hour ({_q_reason})."

                            review_conf = review.get("confidence", 0.0)
                            review_verdict = review.get("verdict", "Reject")
                            critique = review.get("critique", "")

                            if review_verdict == "Reject":
                                logger.warning(f"Reviewer REJECTED fix for {issue_id}: {critique}")
                                error_context = f"Reviewer rejected the fix: {critique}"
                                last_failure = {
                                    "kind": "review_rejected",
                                    "detail": str(critique or "").strip(),
                                    "confidence": review_conf,
                                }
                                try:
                                    repo_git.git.reset("--hard", "HEAD")
                                    repo_git.git.clean("-fd")
                                except Exception: pass
                                continue

                        if config.get("qa_enabled", True):
                            prepare_environment(path)
                            update_task_state(task_id=issue_id, task_name=f"Verifying {issue_id}", action="start")
                            verified, failure_msg = verify_fix(path, repo_name, config)
                        else:
                            logger.info("QA Testing disabled. Assuming verified.")
                            verified, failure_msg = True, "QA disabled"

                        if verified:
                            final_confidence = (confidence + review_conf) / 2
                            # Low confidence + a stronger provider still untried →
                            # discard this fix and escalate to the next ladder slot.
                            if (escalate and final_confidence < min_conf
                                    and attempt < max_attempts and attempt < len(ladder)):
                                logger.info(
                                    f"Fix verified but confidence {final_confidence:.0%} < "
                                    f"{min_conf:.0%} — escalating to the next provider.")
                                error_context = (
                                    f"The previous provider's fix passed tests but only reached "
                                    f"{final_confidence:.0%} confidence. Review critique: {critique}. "
                                    f"Produce a more robust fix.")
                                last_failure = {
                                    "kind": "low_confidence",
                                    "detail": str(critique or "").strip(),
                                    "confidence": final_confidence,
                                    "threshold": min_conf,
                                }
                                try:
                                    repo_git.git.reset("--hard", "HEAD")
                                    repo_git.git.clean("-fd")
                                except Exception:  # noqa: BLE001
                                    pass
                                continue
                            success = True
                            state["success_count"] += 1
                            final_verdict = review_verdict
                            break
                        else:
                            error_context = failure_msg
                            last_failure = {"kind": "qa_failed",
                                            "detail": str(failure_msg or "").strip()}
                except Exception as inner_e:
                    if "No LLM providers" in str(inner_e):
                        logger.error(f"No LLM providers configured for issue {issue_id}: {inner_e}")
                        update_task_state(task_id=issue_id, action="end")
                        return False, "No LLM providers configured"
                    # A provider/apply/verify error on THIS attempt must NOT kill the
                    # whole run — record it and let the loop escalate to the next
                    # provider (e.g. a pinned P1 that's unreachable → try P2/P3).
                    logger.warning(f"Attempt {attempt} errored ({inner_e}); escalating to the next provider/attempt.")
                    error_context = f"Previous attempt failed with: {str(inner_e)[:300]}"
                    last_failure = {"kind": "error", "detail": str(inner_e)[:300]}
                    try:
                        repo_git.git.reset("--hard", "HEAD")
                        repo_git.git.clean("-fd")
                    except Exception:  # noqa: BLE001
                        pass
                    continue

            if not success:
                state["failure_count"] += 1
                # Lead with the CAUSE. This string is shown truncated in the
                # status table, and the old form spent its first 72 characters on
                # "AI failed to find a verified fix after max attempts. Last
                # attempt error: " -- so the operator saw boilerplate and had to
                # open the logs to learn anything, which is the whole complaint.
                _summary = _failure_summary(last_failure, max_attempts)
                failure_reason = _summary
                if error_context and last_failure.get("kind") != "error":
                    failure_reason += f" | detail: {error_context}"
                elif error_context and not last_failure:
                    failure_reason += f" | detail: {error_context}"

                try:
                    issue.create_comment(f"🤖 **BugFixer Failure**\n\nI attempted to fix this issue {max_attempts} times, but I could not find a solution that passed verification.\n\n**Final Error:** `{failure_reason}`")
                except Exception as ce:
                    logger.warning(f"Could not post failure comment to {issue_id}: {ce}")

                processed = load_processed()
                processed[f"{repo_name}:{issue_num}"] = {
                    "status": "failed",
                    "timestamp": datetime.now().isoformat(),
                    "error": failure_reason,
                    # Structured so the UI can render the cause as a label plus
                    # numbers rather than parsing it back out of a sentence.
                    "failure_kind": last_failure.get("kind") or "unknown",
                    "failure_detail": (last_failure.get("detail") or "")[:1000],
                    "failure_confidence": last_failure.get("confidence"),
                    "failure_threshold": last_failure.get("threshold"),
                    "attempts": max_attempts,
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
            # Only the direct-push attempt below sets this. Stays None when
            # can_direct_push is False (the common case — most repos/fixes go
            # via PR, not direct push), so the else branch's ternary further
            # down doesn't crash trying to read an unassigned local.
            decision_reason = None
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
                reason = "Skeptical Reviewer rejected" if final_verdict != "Approve" else (decision_reason if decision_reason is not None else "Trust/Ownership requirements not met")
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

            issue_id = f"{repo_name}:{issue_num}"
            # Auto-committed → close on GitHub, then hold in PENDING VERIFICATION: a
            # human opens BugFixer, confirms the issue is actually gone and clicks
            # Resolved (→ Resolved bucket), or clicks Re-open if it's still there.
            # EVERY direct-commit fix goes here — one consistent human-check gate.
            issue.edit(state='closed')
            _notify_bug_fixed(issue)  # LM "File a Bug" → hub → UI shows "Fixed"
            _apply_closed_label(repo_obj, issue, issue_id)
            new_status = "pending_verification"

            processed = load_processed()
            processed[issue_id] = {
                "status": new_status,
                "timestamp": datetime.now().isoformat(),
                "commit": commit_hash,
                "commit_msg": commit_msg,
                "files": list(fixes.keys()),
                "commit_type": commit_type,
                "decision_reason": decision_reason,
                "reopened": was_reopened,
                "original_body": issue.body
            }

            save_processed(processed)
            state["processed"] = processed
            recompute_issue_counters(processed)
            state["daily_fixes_count"] = state.get("daily_fixes_count", 0) + 1

            update_task_state(task_id=issue_id, action="end")
            return True, f"Fixed via {commit_type}"

    except Exception as e:
        logger.exception(f"Error in process_single_issue: {e}")
        try:
            update_task_state(task_id=issue_id, action="end")
        except Exception as cleanup_err:
            logger.error(f"Failed to clean up task state for {issue_id}: {cleanup_err}")
        # Record the errored issue as `failed` so it stays VISIBLE in the Status table
        # (Failed) and remains retryable. An unhandled exception (e.g. an ollama 400 /
        # timeout mid-fix) otherwise never lands in the processed store, so the issue
        # silently vanishes from the UI — which is exactly what happened to #103.
        try:
            processed = load_processed()
            prev = processed.get(issue_id, {})
            processed[issue_id] = {
                **prev,
                "status": "failed",
                "error": str(e)[:500],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_processed(processed)
            state["processed"] = processed
        except Exception as rec_err:  # noqa: BLE001
            logger.error(f"Failed to record failed status for {issue_id}: {rec_err}")
        return False, str(e)
    finally:
        # Always release the claim — on success, failure, or exception — so a later
        # legitimate re-trigger (e.g. operator Reopen) isn't permanently blocked.
        _release_issue(issue_id)
        # Reconcile the dashboard counters to the processed store after every run,
        # so any terminal-status change (closed/failed/resolved) is reflected exactly
        # once regardless of which path we took.
        try:
            recompute_issue_counters()
        except Exception:  # noqa: BLE001 — counters are cosmetic, never fail the run
            pass


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
