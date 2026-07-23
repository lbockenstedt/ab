"""GitHub API integration: repo resolution, label/version ops, duplicate detection, and automated issue creation (extracted from main.py)."""
import json, os, re, requests
from datetime import datetime, timezone
from dedup import _is_duplicate_match as _is_duplicate_match_impl, _jaccard as _jaccard_impl, _normalize_for_dedup as _normalize_for_dedup_impl, _token_set as _token_set_impl
from dedup import _body_signal_match, _body_containment_match

from main import (
    _in_update_cooldown,
    load_config,
    logger,
    resolve_self_diagnosis_repo,
)


def clean_repo_name(name):
    """Converts a full GitHub URL or a 'user/repo' string into 'user/repo' format."""
    name = name.strip()
    if name.startswith("http"):
        name = name.replace("https://", "").replace("http://", "")
        name = name.replace("github.com/", "")
        if name.endswith(".git"):
            name = name[:-4]
        name = name.rstrip("/")
    return name


def get_monitored_repos(config):
    """Extracts and normalizes a list of monitored repositories from config,
    always including the self-diagnosis repository if it can be resolved.
    """
    raw_repos = config.get("monitored_repos", [])
    monitored_repos = []
    if isinstance(raw_repos, list):
        for r in raw_repos:
            for split_r in r.replace("\\n", ",").split(","):
                cleaned = clean_repo_name(split_r)
                if cleaned: monitored_repos.append(cleaned)
    elif isinstance(raw_repos, str):
        for split_r in raw_repos.replace("\\n", ",").split(","):
            cleaned = clean_repo_name(split_r)
            if cleaned: monitored_repos.append(cleaned)

    sd_repo = resolve_self_diagnosis_repo(config)
    if sd_repo:
        monitored_repos.append(sd_repo)

    return list(set(monitored_repos))


def resolve_module_repo(module, monitored_repos, config):
    """Maps a Hub log module name to the GitHub repo its issues should be filed in.

    Routing precedence (first match wins):
      1. Explicit 'module_repo_map' config key: {module_name: "owner/repo"}.
         Case-insensitive module lookup; lets the user override auto-matching
         for aliases or modules with no name-matching repo (e.g. "hub" -> "owner/lm").
      2. Auto-match: a monitored repo whose basename (the segment after the final
         '/') equals the module name, case-insensitive. e.g. module "pxmx" ->
         "lbockenstedt/pxmx".
      3. None if nothing matches — the caller should skip filing (NOT dump into
         the self-diagnosis repo, which is the behaviour the user explicitly
         wants to avoid).

    The returned repo is always a member of monitored_repos (auto-match) or a
    user-declared repo (explicit map); it is never invented.
    """
    if not module:
        return None
    mod_key = str(module).strip().lower()
    if not mod_key:
        return None

    # 1. Explicit user-provided mapping.
    module_map = config.get("module_repo_map") or {}
    if isinstance(module_map, dict):
        for k, v in module_map.items():
            if str(k).strip().lower() == mod_key and v and str(v).strip():
                resolved = clean_repo_name(str(v).strip())
                if resolved:
                    return resolved

    # 2. Auto-match against monitored repo basenames.
    for repo_name in monitored_repos:
        basename = str(repo_name).strip().split('/')[-1].lower()
        if basename == mod_key:
            return repo_name

    return None


def parse_module_repo_map(value):
    """Normalises a module_repo_map setting into {module: "owner/repo"}.

    Accepts a dict, a JSON object string, or a newline/comma-separated list of
    'module=owner/repo' pairs, so the Settings form can send any of these shapes.
    Values are cleaned via clean_repo_name; entries with empty module or repo are
    dropped. Module keys are stored as-is (case-insensitive lookup happens in
    resolve_module_repo), so callers see the original casing.
    """
    result = {}
    if value is None:
        return result
    if isinstance(value, dict):
        pairs = value.items()
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return result
        # Try JSON object first; fall back to line/separated 'module=repo' pairs.
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                pairs = obj.items()
            else:
                return result
        except Exception:
            pairs = []
            for part in s.replace(",", "\n").split("\n"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                mod, _, repo = part.partition("=")
                pairs.append((mod.strip(), repo.strip()))
    else:
        return result

    for mod, repo in pairs:
        mod_s = str(mod).strip()
        repo_s = clean_repo_name(str(repo).strip()) if repo else ""
        if mod_s and repo_s:
            result[mod_s] = repo_s
    return result


def discover_labels(gh_current, monitored_repos):
    """Fetches all unique labels from all monitored repositories, including built-in defaults."""
    all_labels = {"automated-fix", "bug", "Bug", "critical", "high-priority"}
    for repo_name in monitored_repos:
        try:
            repo = gh_current.get_repo(repo_name)
            labels = repo.get_labels()
            for label in labels:
                all_labels.add(label.name)
        except Exception as e:
            logger.error(f"Error discovering labels for {repo_name}: {e}")
    return sorted(list(all_labels))


_LABEL_COLORS = {
    "automated-fix": "0e8a16",  # green
    "Bug": "b60205",            # red (classic GitHub bug color)
    "bug": "b60205",
    "log-detected": "fbca04",   # yellow
    "critical": "b60205",
    "high-priority": "d93f0b",
}


def _ensure_label(gh_repo, name):
    """Create a label on the repo if it doesn't already exist.

    GitHub issue creation raises if a label name is absent, so this is called
    before create_issue for every label we intend to apply. No-op if it exists.
    """
    try:
        gh_repo.get_label(name)
        return True
    except Exception:
        # 404 / UnknownObjectException -> create it.
        try:
            color = _LABEL_COLORS.get(name, "ededed")
            gh_repo.create_label(name=name, color=color)
            logger.info(f"Created missing label '{name}' on {gh_repo.full_name}")
            return True
        except Exception as e:
            logger.warning(f"Could not create label '{name}' on {gh_repo.full_name}: {e}")
            return False


def bump_repo_version(repo_path):
    """Increment the target repo's VERSION by bumping the LAST numeric run in place,
    preserving its zero-pad width — EXACTLY mirroring the version-bump.yml CI so the
    target's scheme is never clobbered.

    The fleet scheme is ``n.nn`` (e.g. ``0.01`` → ``0.02`` … ``0.99``; production is
    set to ``1.00`` by hand). This function is scheme-agnostic and also handles
    ``N.N.N`` (``0.0.1`` → ``0.0.2``) and legacy leading-dot (``.1219`` → ``.1220``).

    Previously it ONLY understood 3-part ``N.N.N`` and RESET every other scheme to
    ``0.0.1`` — which silently clobbered a hub on the ``.NNNN`` scheme (``.1219`` →
    ``0.0.1``). Incrementing the last run in place (zero-pad preserved) keeps
    ``0.09`` → ``0.10``. It does NOT carry (``0.99`` → ``0.100``), exactly like the
    CI — the ``1.00`` production milestone is set by hand, well before 0.99."""
    version_file = os.path.join(repo_path, "VERSION")
    current_version = ""
    if os.path.exists(version_file):
        try:
            with open(version_file, "r") as f:
                current_version = f.read().strip()
        except Exception as e:
            logger.error(f"Error reading version file: {e}")

    nums = list(re.finditer(r"\d+", current_version))
    if not nums:
        # No numeric segment yet — seed the n.nn scheme.
        new_version = "0.01"
    else:
        last = nums[-1]
        width = len(last.group())
        # int(..., 10) avoids octal interpretation of a leading-zero run (e.g. "01").
        val = int(last.group(), 10) + 1
        new_version = current_version[:last.start()] + str(val).zfill(width) + current_version[last.end():]

    try:
        with open(version_file, "w") as f:
            f.write(new_version + "\n")
        return new_version
    except Exception as e:
        logger.error(f"Error writing version file: {e}")
        return None


def trigger_infrastructure_update():
    url = os.getenv("UPDATE_API_URL")
    if not url or "your-netbox" in url: return "URL not configured"
    try:
        resp = requests.post(url, json={}, timeout=10)
        return "SUCCESS: Sync Triggered" if resp.status_code == 200 else f"FAILED: {resp.status_code}"
    except Exception as e: return f"ERROR: {str(e)}"


def _normalize_for_dedup(text):
    """Aggressively normalize text for duplicate comparison.

    Thin wrapper around the strengthened implementation in ``dedup.py`` (which
    additionally strips the automated-issue boilerplate wrapper and applies
    module aliases such as ``opns`` -> ``opnsense``). Kept here as a shim so
    existing call sites that import it from main continue to work.
    """
    return _normalize_for_dedup_impl(text)


def _token_set(text):
    return _token_set_impl(text)


def _jaccard(a, b):
    return _jaccard_impl(a, b)


def _is_duplicate_match(new_title, new_body, ex_title, ex_body):
    """Returns True if a new error matches an existing issue, using normalized +
    fuzzy comparison so LLM rephrasing, timestamp drift, boilerplate wrapper,
    and module-name variants (opns/opnsense) don't defeat dedup."""
    return _is_duplicate_match_impl(new_title, new_body, ex_title, ex_body)


DEDUP_CLOSED_WINDOW_DAYS = 60


GLOBAL_FALLBACK_JACCARD = 0.8


def find_global_duplicate_issue(gh_current, monitored_repos, error_data):
    """Searches across monitored repositories for an existing issue matching the error.

    Searches OPEN issues AND recently-CLOSED issues (within
    DEDUP_CLOSED_WINDOW_DAYS), because a recurring error whose prior issue was
    closed (the bot merged a "fix") must still be recognised so it can be
    REOPENED rather than re-filed — this is what previously caused the
    opnsense 'time' import storm (#25 -> #55 -> #78 -> #90).

    The target repository (error_data['repo']) is searched first; other
    monitored repos are searched as a fallback with a stricter title-level
    threshold (GLOBAL_FALLBACK_JACCARD) to avoid cross-module false positives.

    Returns a tuple (issue, repo_name, was_closed). ``was_closed`` is True when
    the matched issue is currently closed, signalling the caller to reopen it
    rather than treat it as an open duplicate. Returns (None, None, False) when
    no duplicate is found.

    Safely handles error_data payloads that may be missing the 'title' or 'body'
    keys (the LLM may omit them). Missing fields are treated as empty strings so
    that the deduplication search degrades gracefully instead of raising a
    KeyError.
    """
    # Defensive: ensure error_data is a dict before calling .get()
    if not isinstance(error_data, dict):
        logger.warning(f"find_global_duplicate_issue received non-dict error_data: {type(error_data)}")
        return None, None, False

    new_title = error_data.get('title') or ''
    new_body = error_data.get('body') or ''

    if not str(new_title).strip() and not str(new_body).strip():
        return None, None, False

    target_repo = error_data.get('repo')

    def _search_repo(repo_name, require_strict_global=False, is_self_diag=False):
        try:
            repo = gh_current.get_repo(repo_name)
            # state='all' so we see recently-closed recurrences too; newest-first
            # so the most relevant (recently updated) issues are scanned first.
            issues = repo.get_issues(state='all', sort='updated', direction='desc')
            # tz-aware: PyGithub returns tz-aware closed_at/updated_at. A naive
            # utcnow() here raises "can't subtract offset-naive and offset-aware
            # datetimes", which the outer except swallows → the whole dedup search
            # returns None → a CLOSED recurrence (e.g. a resolved bug reopening)
            # is never matched and a duplicate issue is filed instead.
            now = datetime.now(timezone.utc)
            for issue in issues:
                # Skip closed issues older than the recurrence window — they are
                # unlikely to be the same recurrence and would risk stale matches.
                if issue.state == 'closed':
                    closed_at = getattr(issue, 'closed_at', None) or issue.updated_at
                    if closed_at is not None and closed_at.tzinfo is None:
                        # Older PyGithub versions returned naive UTC — coerce so the
                        # subtraction below never raises.
                        closed_at = closed_at.replace(tzinfo=timezone.utc)
                    if closed_at and (now - closed_at).days > DEDUP_CLOSED_WINDOW_DAYS:
                        continue
                issue_body = issue.body or ""

                # Special case: Self-Diagnosis. Relax the match to rely primarily on title
                # since JSON error messages often vary by exactly one character (line number).
                if is_self_diag:
                    nt = _normalize_for_dedup(new_title)
                    et = _normalize_for_dedup(issue.title or "")
                    if nt and et and _jaccard(set(nt.split()), set(et.split())) >= 0.7:
                        return issue, repo_name, (issue.state == 'closed')

                if _is_duplicate_match(new_title, new_body, issue.title or "", issue_body):
                    is_closed = (issue.state == 'closed')
                    # Reopening a CLOSED issue is expensive (re-triggers an
                    # ai-fix branch) and destructive if wrong, so it demands a
                    # BODY-level signal — a title-only match (e.g. a generic
                    # "NameError in X" title) is not enough to resurrect a closed
                    # issue. An OPEN duplicate can still match on title alone.
                    if is_closed and not _body_signal_match(new_body, issue_body):
                        continue
                    # Global fallback (non-target repo): require a strong
                    # title-level signal so we don't cross-match unrelated
                    # modules on incidental body-wording overlap.
                    if require_strict_global:
                        nt = _normalize_for_dedup(new_title)
                        et = _normalize_for_dedup(issue.title or "")
                        if not (nt and et and
                                _jaccard(set(nt.split()), set(et.split())) >= GLOBAL_FALLBACK_JACCARD):
                            continue
                    return issue, repo_name, is_closed
        except Exception as e:
            # WARNING (not debug): a swallowed failure here silently defeats
            # dedup — the caller files a duplicate / re-files a dismissed error.
            logger.warning(f"Duplicate search failed for {repo_name}; may file a duplicate: {e}")
        return None

    # 1. Target repo first — the recurrence almost always lands in the same repo.
    if target_repo:
        config = load_config()
        self_diag_repo = config.get("self_diagnosis_repo")
        is_self_diag = (target_repo == self_diag_repo)

        if target_repo in monitored_repos:
            hit = _search_repo(target_repo, is_self_diag=is_self_diag)
            if hit:
                return hit
        elif is_self_diag:
            # If it's the self-diagnosis repo, search it even if it's not explicitly
            # in the monitored_repos list (though it usually is).
            hit = _search_repo(target_repo, is_self_diag=True)
            if hit:
                return hit


    # 2. Global fallback across the other monitored repos, stricter threshold.
    for repo_name in monitored_repos:
        if repo_name == target_repo:
            continue
        hit = _search_repo(repo_name, require_strict_global=True)
        if hit:
            return hit

    return None, None, False


def create_automated_issue(gh_current, monitored_repos, gh_repo, error_data, labels=None, raw=False):
    """Creates a GitHub issue for a log-detected error, deduplicating globally across monitored repos.

    The 'body' field is required to create a meaningful issue. If it is missing or
    empty, the function logs a warning and returns None instead of raising a
    KeyError, which previously crashed automated issue creation with: 'body'.

    Additionally validates that error_data is a dict and that both 'title' and
    'body' are present and non-empty strings before any GitHub API call is made.
    """
    try:
        in_cooldown, remaining = _in_update_cooldown()
        if in_cooldown:
            logger.info(
                f"Post-update cooldown active ({remaining / 60:.1f} min remaining) — "
                f"suppressing issue: {error_data.get('title', 'unknown') if isinstance(error_data, dict) else repr(error_data)}"
            )
            return None

        # Defensive: ensure error_data is a dict; if the LLM returned a malformed
        # payload (e.g., a string or None), .get() would itself raise AttributeError.
        if not isinstance(error_data, dict):
            logger.warning(
                f"Skipping automated issue creation: error_data is not a dict "
                f"(type={type(error_data).__name__}). Value: {error_data!r}"
            )
            return None

        title_text = error_data.get('title')
        body_text = error_data.get('body')

        # Validate body FIRST — this is the field that was causing the KeyError crash.
        # We explicitly check for None, empty string, or whitespace-only strings.
        if body_text is None or not str(body_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'body' field is missing or empty. "
                f"Title was: {title_text!r}. Full error_data: {error_data}"
            )
            return None

        # Validate title as well — a GitHub issue cannot be created without a title.
        if title_text is None or not str(title_text).strip():
            logger.warning(
                f"Skipping automated issue creation: 'title' field is missing or empty. "
                f"Body was: {str(body_text)[:120]!r}"
            )
            return None

        # Normalise to strings (the LLM might return non-string types).
        title_text = str(title_text)
        body_text = str(body_text)

        current_repo_name = error_data.get('repo') or gh_repo.full_name

        logger.info(f"[dedup] Checking for an existing issue matching {title_text[:80]!r} "
                    f"(target {current_repo_name}, +{len(monitored_repos)} monitored repos)…")
        existing_issue, duplicate_repo_name, was_closed = find_global_duplicate_issue(
            gh_current, monitored_repos, error_data
        )
        if existing_issue:
            logger.info(f"[dedup] MATCH → #{existing_issue.number} in "
                        f"{duplicate_repo_name or current_repo_name} "
                        f"(state={existing_issue.state}, was_closed={was_closed}) — will "
                        f"{'reopen' if was_closed else 'add evidence to'} it, not file a duplicate.")
        else:
            logger.info(f"[dedup] NO MATCH for {title_text[:80]!r} — filing a NEW issue.")

        if existing_issue:
            duplicate_repo_display = duplicate_repo_name or current_repo_name

            # If the matching issue still carries the 'bugfixer-dismissed' label,
            # it was intentionally marked as not a real issue. Only suppress when
            # the NEW error's body is near-identical to the dismissed issue's
            # (normalized containment) — a mere title/0.7-Jaccard match is too
            # weak to silence a genuinely-DIFFERENT bug that happens to share a
            # title or some body wording with a dismissed one. When it is not a
            # body-near-identical match, we treat the dismissed issue as NOT a
            # match and fall through to file a fresh issue (and never reopen the
            # dismissed one). A human removing the label resumes normal handling.
            existing_labels = [lbl.name for lbl in (existing_issue.labels or [])]
            if "bugfixer-dismissed" in existing_labels:
                if _body_containment_match(body_text, existing_issue.body or ""):
                    logger.info(
                        f"Suppressing new issue for #{existing_issue.number} in "
                        f"{duplicate_repo_display} — 'bugfixer-dismissed' label is "
                        f"still present and the body is near-identical."
                    )
                    return existing_issue
                logger.info(
                    f"Dismissed issue #{existing_issue.number} in {duplicate_repo_display} "
                    f"matched only weakly (no body-level near-identity) — treating as a new "
                    f"error and filing a fresh issue instead of suppressing."
                )
                existing_issue = None

            if existing_issue and was_closed:
                # The matching issue was closed (typically the bot merged a "fix"
                # for it). Reopen it and record the recurrence instead of filing a
                # brand-new issue + spawning another ai-fix-issue-* branch. This
                # is the core fix for the recurring-error storm.
                logger.info(
                    f"Recurring CLOSED issue #{existing_issue.number} in "
                    f"{duplicate_repo_display} matched; reopening instead of filing a duplicate."
                )
                try:
                    existing_issue.edit(state='open')
                except Exception as reopen_err:
                    logger.warning(f"Could not reopen issue #{existing_issue.number}: {reopen_err}")
                try:
                    existing_issue.create_comment(
                        f"🔁 **Recurrence detected — reopening instead of filing a duplicate**\n\n"
                        f"BugFixer re-detected this error in **{current_repo_name}** after the "
                        f"issue was closed.\n\n"
                        f"```\n{body_text}\n```"
                    )
                    logger.info(f"Reopened issue #{existing_issue.number} for {current_repo_name}")
                except Exception as comment_err:
                    logger.warning(f"Could not add recurrence comment to #{existing_issue.number}: {comment_err}")
                return existing_issue

            # OPEN duplicate — keep the existing evidence-comment behavior.
            # Guarded by existing_issue because the dismissed-label branch above
            # may have cleared it (weak match) to fall through to a fresh issue.
            if existing_issue:
                logger.info(f"Global duplicate issue detected: #{existing_issue.number} in {duplicate_repo_display}. Adding info.")

                existing_body = existing_issue.body or ""
                if body_text.lower() not in existing_body.lower():
                    existing_issue.create_comment(
                        f"🤖 **BugFixer Update**\n\nAdditional instance of this error detected in repository **{current_repo_name}:**\n\n"
                        f"```\n{body_text}\n```"
                    )
                    logger.info(f"Added additional evidence from {current_repo_name} to issue #{existing_issue.number}")

                return existing_issue

        full_title = f"🤖 Log Alert: {title_text}"
        # Hidden module marker so the fix step can pull the source module's
        # related logs from the local hub_logs mirror as fix context (see
        # log_scan._module_log_fix_context). Mirrors the File-a-Bug
        # <!-- bug-report-id --> marker pattern. Only on the non-raw path —
        # raw (scan_bugs) carries its own bug-report-id marker instead. The
        # marker is appended AFTER body_text so it never participates in the
        # dedup/containment match (which compares body_text, not full_body).
        module_marker = ""
        _mod = error_data.get("module")
        if not raw and _mod and str(_mod).strip():
            module_marker = f"\n\n<!-- bf-module: {str(_mod).strip()} -->"
        full_body = (
            f"**Automated Error Detection**\n\n"
            f"The BugFixer Hub analysis detected a potential issue in the logs:\n\n"
            f"### Log Evidence:\n```\n{body_text}\n```\n\n"
            f"This issue has been automatically created for fixing."
            f"{module_marker}"
        )
        # raw=True (used by scan_bugs for user-filed "File a Bug" reports): use
        # the caller-provided title/body verbatim — no "Log Alert" prefix and no
        # Log Evidence wrapping — so the public issue body stays clean (just the
        # user's explanation + context + a hidden bug-report-id reference).
        if raw:
            full_title = title_text
            full_body = body_text
        applied_labels = labels if labels is not None else ["automated-fix", "log-detected"]
        # Ensure each label exists on the target repo first — create_issue
        # raises UnknownObjectException if a label is absent (e.g. the "Bug"
        # label on a freshly-monitored repo like lbockenstedt/lm).
        for lbl in applied_labels:
            _ensure_label(gh_repo, lbl)
        issue = gh_repo.create_issue(
            title=full_title,
            body=full_body,
            labels=applied_labels
        )
        logger.info(f"Created automated issue #{issue.number} for {current_repo_name}")
        return issue
    except Exception as e:
        logger.error(f"Failed to handle automated issue creation: {e}")
        logger.debug(f"create_automated_issue error_data was: {error_data!r}")
        return None


def find_existing_pull_request(repo_obj, target_branch, base_branch):
    """Checks whether an open pull request already exists for the given head/base pair."""
    existing_pr = None

    owner = repo_obj.owner.login
    head_param = f"{owner}:{target_branch}"

    try:
        existing_prs = repo_obj.get_pulls(state='open', head=head_param, base=base_branch)
        for pr_item in existing_prs:
            existing_pr = pr_item
            break
    except Exception as e:
        logger.warning(f"Filtered PR check failed for {target_branch} -> {base_branch}: {e}")

    if not existing_pr:
        try:
            all_open_prs = repo_obj.get_pulls(state='open')
            for pr_item in all_open_prs:
                if pr_item.head.ref == target_branch and pr_item.base.ref == base_branch:
                    existing_pr = pr_item
                    break
        except Exception as e:
            logger.warning(f"Manual PR scan failed for {target_branch} -> {base_branch}: {e}")

    return existing_pr


def _ensure_bugfixer_closed_label(repo):
    """Ensure the `bugfixer-closed` label exists in repo (create if missing). Best-effort.
    Mirrors the bugfixer-dismissed ensure-pattern used in delete_issue."""
    try:
        repo.get_label("bugfixer-closed")
    except Exception:
        try:
            repo.create_label("bugfixer-closed", "6b7280",
                               "Issue resolved and closed by BugFixer")
        except Exception as e:
            logger.warning(f"Could not create bugfixer-closed label: {e}")


def _apply_closed_label(repo, issue, issue_id):
    """Best-effort add the `bugfixer-closed` label to an issue being closed (existing
    labels are kept). Failure to label is non-fatal — the close + local transition still
    proceed."""
    try:
        _ensure_bugfixer_closed_label(repo)
        issue.add_to_labels("bugfixer-closed")
    except Exception as e:
        logger.warning(f"Could not apply bugfixer-closed label to {issue_id}: {e}")


__all__ = [
    'clean_repo_name',
    'get_monitored_repos',
    'resolve_module_repo',
    'parse_module_repo_map',
    'discover_labels',
    '_ensure_label',
    'bump_repo_version',
    'trigger_infrastructure_update',
    'find_global_duplicate_issue',
    'create_automated_issue',
    'find_existing_pull_request',
    '_ensure_bugfixer_closed_label',
    '_apply_closed_label',
    '_normalize_for_dedup',
    '_token_set',
    '_jaccard',
    '_is_duplicate_match',
    '_LABEL_COLORS',
    'DEDUP_CLOSED_WINDOW_DAYS',
    'GLOBAL_FALLBACK_JACCARD',
]
