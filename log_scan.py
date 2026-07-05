"""Hub log ingestion and scanning: error/heartbeat/bug detection and production-fix verification (extracted from main.py)."""
import json, os, re, requests, time
from datetime import datetime
from github import GithubException

from main import (
    _apply_closed_label,
    _get_hub_agent_client,
    call_llm,
    clean_repo_name,
    create_automated_issue,
    get_monitored_repos,
    load_config,
    logger,
    resolve_module_repo,
    resolve_self_diagnosis_repo,
    save_processed,
    state,
    update_task_state,
)


def get_hub_logs():
    """Fetches recent logs from the Hub for all modules via the Hub agent.

    BugFixer is now an authenticated WebSocket agent of the Hub (see hub_agent.py),
    so it no longer hits the admin-cookie-gated HTTP /setup/logs/all endpoint that
    always returned 401 for a static token. Logs come back as a signed HUB_RESPONSE
    to a GET_LOGS request. Returns a list of {"module","log"} entries sorted
    newest-first, or None when the agent isn't approved/connected yet.
    """
    client = _get_hub_agent_client()
    if not client:
        return None
    result = client.request_sync("GET_LOGS", {}, timeout=20)
    if not isinstance(result, dict):
        return None
    raw_logs = result.get("logs", [])
    if not isinstance(raw_logs, list):
        raw_logs = []
    # Try to parse a timestamp from each log line so we can sort newest-first.
    # Typical format: "2024-01-15 10:30:00 [INFO] …"
    _TS_PAT = re.compile(r'^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})')
    def _log_ts(entry):
        m = _TS_PAT.match((entry.get('log') or '').strip())
        return m.group(1) if m else ''
    return sorted(raw_logs, key=_log_ts, reverse=True)


def get_hub_state():
    """Fetches the current state of the hub for verification."""
    config = load_config()
    url = config.get("HUB_QUERY_URL") or os.getenv("HUB_QUERY_URL")
    if not url or "your-netbox" in url: return None
    try:
        resp = requests.get(url.rstrip('/'), timeout=15)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception as e:
        logger.error(f"Hub State Fetch Error: {e}")
        return None


def filter_error_logs(logs):
    """Scrubs raw logs down to error-relevant entries before sending to the LLM.

    Why: HubScan previously JSON-dumped the *entire* Hub log set (every INFO line,
    last-500-lines-per-module file logs, recurring duplicates) into the LLM
    prompt. That both bloated the prompt toward the model's context limit
    (a likely cause of upstream HTTP 500s) and buried actionable errors in noise.

    This keeps only entries whose 'log' text carries an error signature
    ([ERROR]/[CRITICAL]/Traceback/Exception/Error/Failed), dedupes identical
    lines per module (recurring errors appear dozens/hundreds of times in file
    logs), and caps the total to bounded entry/character budgets so the prompt
    can never overflow context regardless of log volume.

    Schema-agnostic: handles the Hub shape {"module":..., "log":...} and the
    SelfScan shape {"module":..., "timestamp":..., "log":...} equally, since it
    only inspects the 'log' field (falling back to the stringified entry).
    """
    import re
    if not logs:
        return []

    # ERROR/CRITICAL level tags plus common error signatures (tracebacks,
    # raised exceptions, explicit "Error:"/"Failed"). WARNINGs are excluded:
    # the LLM task is to find actionable *errors*, not routine warnings.
    error_pattern = re.compile(
        r'\[(ERROR|CRITICAL)\]|Traceback|Exception|Error[: ]|Failed|Traceback \(most recent call last\)',
        re.IGNORECASE
    )

    cfg = load_config()
    max_entries = int(cfg.get("LLM_LOG_MAX_ENTRIES", 200))
    max_chars = int(cfg.get("LLM_LOG_MAX_CHARS", 60000))

    seen = set()
    kept = []
    total_chars = 0
    for entry in logs:
        if isinstance(entry, dict):
            module = str(entry.get('module', '') or '')
            text = entry.get('log')
            text = str(text) if text is not None else json.dumps(entry)
        else:
            module = ''
            text = str(entry)

        if not error_pattern.search(text):
            continue

        key = (module, text.strip())
        if key in seen:
            continue
        seen.add(key)

        line_len = len(text) + len(module) + 16
        if total_chars + line_len > max_chars:
            logger.info(
                f"filter_error_logs: reached {max_chars}-char budget after "
                f"{len(kept)} entries; stopping."
            )
            break
        kept.append(entry if isinstance(entry, dict) else {"module": "", "log": text})
        total_chars += line_len
        if len(kept) >= max_entries:
            logger.info(f"filter_error_logs: reached {max_entries}-entry cap; stopping.")
            break

    return kept


def analyze_logs_for_errors(logs):
    """Uses LLM to identify actionable errors in aggregated logs.

    Robustly validates the LLM's JSON response: every entry must be a dict with
    non-empty 'module', 'title', and 'body' fields. Malformed entries are dropped
    so they never reach create_automated_issue(), preventing the 'body' KeyError.

    The 'module' field (carried through from the source log entry) is the
    authoritative key for routing an issue to the correct repository — see
    resolve_module_repo(). The LLM may also suggest a 'repo', but it is treated
    as a hint only and is not required.
    """
    if not logs: return []

    log_text = json.dumps(logs, indent=2)
    prompt = (
        f"Logs from Hub:\n{log_text}\n\n"
        "Analyze these logs for critical, recurring, or actionable errors that can be fixed in code. "
        "Ignore heartbeat messages or routine status updates. "
        "For each actionable error found, provide: \n"
        "1. The exact 'module' value from the source log entry the error came from.\n"
        "2. A concise summary of the bug ('title').\n"
        "3. The specific log snippet that proves the error ('body').\n\n"
        "Return ONLY a JSON array of objects: [{\"module\": \"module-name\", \"title\": \"Error Summary\", \"body\": \"Log snippet and description\"}]. "
        "Every object MUST include non-empty 'module', 'title', and 'body' fields. "
        "The 'module' MUST be copied verbatim from the source log entry's module field."
    )
    try:
        res = call_llm(prompt, system_prompt="You are a log analysis expert. Return only a JSON array.")
        import re
        match = re.search(r'\[.*\]', res, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            # Defensive: the LLM might return a single object instead of an array.
            if isinstance(parsed, dict):
                logger.warning(f"LLM returned a single JSON object instead of an array for log analysis. Wrapping in list.")
                parsed = [parsed]
            if not isinstance(parsed, list):
                logger.warning(f"LLM returned non-array JSON for log analysis: {type(parsed).__name__}. Discarding.")
                return []
            cleaned = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    logger.debug(f"Dropping malformed log-analysis entry (not a dict): {entry}")
                    continue
                module_val = entry.get('module')
                title_val = entry.get('title')
                body_val = entry.get('body')
                if not module_val or not str(module_val).strip():
                    logger.warning(f"Hub log analysis found an actionable error but it's missing a module identifier. Log snippet: {body_val[:200]!r}")
                    continue
                if not title_val or not str(title_val).strip():
                    logger.debug(f"Dropping malformed log-analysis entry (missing/empty title): {entry}")
                    continue
                if not body_val or not str(body_val).strip():
                    logger.debug(f"Dropping malformed log-analysis entry (missing/empty body): {entry}")
                    continue

                # Try to find the original source entry to preserve full context (host, path, etc.)
                source_entry = next((log for log in logs if isinstance(log, dict)
                                   and str(log.get('module')) == str(module_val)
                                   and str(body_val) in str(log.get('log', ''))), {})

                # Normalise all fields to strings so downstream code never receives None.
                cleaned.append({
                    'module': str(module_val),
                    'title': str(title_val),
                    'body': str(body_val),
                    'repo': str(entry.get('repo')) if entry.get('repo') and str(entry.get('repo')).strip() else '',
                    'source_data': source_entry
                })
            return cleaned
        return []
    except Exception as e:
        logger.error(f"Error analyzing logs: {e}")
        return []


def verify_production_fixes(gh_current, processed):
    """Verify issues that were 'fixed' but are awaiting log confirmation.
    Now implements a configurable 'cooling period' (PROD_VERIFICATION_DAYS).
    The issue is only closed if the error snippet has been absent for the full period.
    """
    config = load_config()
    days_required = int(config.get("PROD_VERIFICATION_DAYS", 7))

    # Fetch hub logs once for the whole verification pass.
    hub_logs_cache = get_hub_logs()

    for issue_id, info in list(processed.items()):
        if info.get("status") == "awaiting_prod_verification":
            repo_name, issue_num = issue_id.split(":")
            logger.info(f"Verifying production fix for {issue_id} (Required clean period: {days_required} days)...")
            try:
                repo_obj = gh_current.get_repo(repo_name)
                issue = repo_obj.get_issue(int(issue_num))

                logs = hub_logs_cache
                if logs:
                    module_name = repo_name.split('/')[-1]
                    relevant_logs = [l['log'] for l in logs if l.get('module') == module_name]
                    full_log_text = "\n".join(relevant_logs)

                    import re
                    match = re.search(r"### Log Evidence:\n```\n(.*?)\n```", issue.body, re.DOTALL)
                    if match:
                        snippet = match.group(1).strip()
                        if snippet.lower() not in full_log_text.lower():
                            # Snippet is gone. Check if we've been clean long enough.
                            clean_since = info.get("clean_since")
                            now = datetime.now()

                            if not clean_since:
                                logger.info(f"Issue {issue_id} is clean. Starting {days_required}-day cooling period.")
                                info["clean_since"] = now.isoformat()
                                processed[issue_id] = info
                                save_processed(processed)
                            else:
                                first_clean_ts = datetime.fromisoformat(clean_since)
                                days_clean = (now - first_clean_ts).days
                                if days_clean >= days_required:
                                    logger.info(f"Verified: Issue {issue_id} has been clean for {days_clean} days. Closing issue.")
                                    try:
                                        issue.create_comment(f"🤖 **BugFixer AI Verification**\n\nProduction logs have been scanned and the error is no longer detected. The issue has remained clean for {days_required} days. Closing issue.")
                                    except Exception as ce:
                                        logger.warning(f"Could not post verification comment to {issue_id}: {ce}")
                                    issue.edit(state='closed')
                                    _apply_closed_label(repo_obj, issue, issue_id)
                                    processed[issue_id]["status"] = "closed"
                                    # Leaves Resolved (was counted via the QA-pass increment when
                                    # first fixed) and enters Closed. This also avoids the old
                                    # double-count where awaiting_prod_verification issues got a
                                    # second success_count += 1 on verification.
                                    state["success_count"] = max(0, state["success_count"] - 1)
                                    state["closed_count"] = state.get("closed_count", 0) + 1
                                    save_processed(processed)
                                else:
                                    logger.info(f"Issue {issue_id} is clean, but only for {days_clean}/{days_required} days. Waiting...")
                        else:
                            # Error reappeared. Reset the clean timer.
                            if info.get("clean_since"):
                                logger.warning(f"Issue {issue_id} error reappeared in logs. Resetting cooling period.")
                                info["clean_since"] = None
                                processed[issue_id] = info
                                save_processed(processed)
                            logger.info(f"Issue {issue_id} still failing in production logs.")
            except Exception as e:
                logger.error(f"Error verifying {issue_id}: {e}")


MODULE_TYPE_REPO = {
    "firewall": "lbockenstedt/opnsense",
    "hypervisor": "lbockenstedt/pxmx",
    "nac": "lbockenstedt/cppm",
    "ipam": "lbockenstedt/netbox",
    "directory": "lbockenstedt/ldap",
    "simulation": "lbockenstedt/cs",
    "dhcp": "lbockenstedt/dhcp",
    "dns": "lbockenstedt/dns",
    "hub": "lbockenstedt/lm",
}


HEARTBEAT_STALE_S_DEFAULT = 300


def scan_heartbeats(gh_current, config, hub_logs):
    """Triage modules whose per-module heartbeat log line is missing or stale.

    Every spoke (via BaseControlPlane._health_heartbeat_task) and the Hub (via
    run_hub_heartbeat_loop) emit a greppable ``[heartbeat] ok module=...`` line
    through the telemetry pipeline every ~60s, which the Hub stamps with a
    receive timestamp and stores in agent_logs[spoke_id] / self.logs. This reads
    the RAW hub_logs (before filter_error_logs drops heartbeat lines) and, for
    every approved spoke (minus agent monitors and heartbeat_exclude) plus the
    Hub, checks that a fresh heartbeat line exists. Missing/stale -> file or
    reopen a GitHub issue via create_automated_issue, which automatically applies
    the post-update cooldown guard (so a spoke restarting after a fix push does
    not immediately trip) and the global dedup/reopen logic (a recurring missing
    heartbeat reopens the closed prior issue instead of spamming duplicates).
    """
    try:
        stale_s = int(config.get("HEARTBEAT_STALE_S", HEARTBEAT_STALE_S_DEFAULT) or HEARTBEAT_STALE_S_DEFAULT)
    except Exception:
        stale_s = HEARTBEAT_STALE_S_DEFAULT
    exclude_raw = config.get("heartbeat_exclude") or []
    if isinstance(exclude_raw, str):
        exclude_raw = [s.strip() for s in exclude_raw.replace(",", "\n").splitlines() if s.strip()]
    exclude = {str(x).strip().lower() for x in (exclude_raw or []) if str(x).strip()}

    # Approved spokes + their module_types. Agents (module_type "agent") are the
    # monitors, not monitored modules — they do not emit the BaseControlPlane
    # heartbeat, and self-triage is a chicken-and-egg — so they are excluded by
    # default. heartbeat_exclude adds intentional opt-outs (e.g. an undeployed
    # spoke whose approval lingers).
    client = _get_hub_agent_client()
    spoke_status = None
    if client:
        try:
            spoke_status = client.request_sync("GET_SPOKE_STATUS", {}, timeout=10)
        except Exception as e:
            logger.warning(f"scan_heartbeats: GET_SPOKE_STATUS failed: {e}")
    if not isinstance(spoke_status, dict):
        spoke_status = {}
    approved = spoke_status.get("approved") or {}
    module_types = spoke_status.get("module_types") or {}

    expected = {}  # spoke_id -> module_type
    for sid, is_approved in approved.items():
        if not is_approved:
            continue
        mtype = (module_types.get(sid) or "").strip().lower()
        if mtype == "agent":
            continue
        if sid.lower() in exclude or mtype in exclude:
            continue
        expected[sid] = module_types.get(sid) or ""
    if "hub" not in exclude:
        expected["hub"] = "hub"

    if not expected:
        logger.info("scan_heartbeats: no expected modules to check this cycle.")
        return

    # Group raw hub_logs by module; capture the latest [heartbeat] line's
    # Hub-stamped timestamp per module. hub_logs are newest-first, so the first
    # heartbeat line encountered per module is the latest.
    latest_hb = {}  # module -> timestamp_str
    ts_pat = re.compile(r'^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})')
    for entry in hub_logs or []:
        if not isinstance(entry, dict):
            continue
        mod = entry.get("module") or ""
        log = entry.get("log") or ""
        if "[heartbeat]" not in log:
            continue
        if mod in latest_hb:
            continue
        m = ts_pat.match(log.strip())
        latest_hb[mod] = m.group(1) if m else ""

    monitored_repos = get_monitored_repos(config)
    now = time.time()
    triaged = 0
    for sid, mtype in expected.items():
        hb = latest_hb.get(sid)
        age_s = None
        if hb:
            try:
                dt = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
                age_s = now - dt.timestamp()
            except Exception:
                age_s = None
        missing = not hb
        stale = (age_s is not None and age_s > stale_s)
        if not (missing or stale):
            continue

        # Hub-side recovery handoff. The Hub watchdog (run_spoke_recovery_loop)
        # already restarts stranded spoke units with backoff and exposes per-
        # spoke recovery state via GET_SPOKE_STATUS (included in the payload
        # fetched above). Don't double-act:
        #   - manual_pause : admin paused recovery from the WebUI -> suppress.
        #   - in_progress  : hub is actively recovering (attempt n/3) -> suppress
        #                    so bugfixer doesn't file a "missing heartbeat" while
        #                    the hub is bringing the spoke back.
        #   - gave_up      : hub tried and couldn't (a restart structurally can't
        #                    fix it — e.g. venv/interpreter missing, broken
        #                    installer) -> escalate ONCE per down-period with a
        #                    "Recovery exhausted" issue naming the cause, which is
        #                    far more actionable than a generic missing-heartbeat.
        # The Hub itself (sid == "hub") is not recovered by the watchdog, so it
        # falls through to the normal missing/stale triage below.
        recovery = {}
        if sid != "hub":
            recovery = (spoke_status.get("recovery") or {}).get(sid, {}) or {}
        rec_paused = bool(recovery.get("manual_pause"))
        rec_in_progress = bool(recovery.get("in_progress")) and not bool(recovery.get("gave_up"))
        rec_gave_up = bool(recovery.get("gave_up"))
        if rec_paused:
            logger.info(f"scan_heartbeats: {sid} recovery paused by admin; not triaging.")
            continue
        if rec_in_progress:
            logger.info(f"scan_heartbeats: {sid} hub recovery in progress "
                         f"(attempt {recovery.get('attempts', 0)}/3); suppressing triage to avoid double-action.")
            continue
        # Escalate a gave_up at most once per down-period: track the last gave_up
        # state per sid so we file on the transition, not every poll cycle (which
        # would otherwise reopen/comment the issue every ~5 min while the spoke is
        # still down). Clear when the spoke recovers (no longer gave_up).
        if not hasattr(scan_heartbeats, "_gave_up_filed"):
            scan_heartbeats._gave_up_filed = {}
        if rec_gave_up:
            if scan_heartbeats._gave_up_filed.get(sid):
                logger.debug(f"scan_heartbeats: {sid} already escalated (gave_up); skipping re-file.")
                continue
        else:
            scan_heartbeats._gave_up_filed.pop(sid, None)

        # Map module -> repo: static type map, then explicit module_repo_map
        # (keyed by spoke_id or module_type), then auto-match, then self-diagnosis.
        repo_name = MODULE_TYPE_REPO.get((mtype or "").lower()) if sid != "hub" else MODULE_TYPE_REPO.get("hub")
        if not repo_name:
            module_map = config.get("module_repo_map") or {}
            if isinstance(module_map, dict):
                for k, v in module_map.items():
                    if str(k).strip().lower() in (sid.lower(), (mtype or "").lower()) and v and str(v).strip():
                        repo_name = clean_repo_name(str(v).strip())
                        break
        if not repo_name:
            repo_name = resolve_module_repo(sid, monitored_repos, config)
        if not repo_name:
            repo_name = resolve_self_diagnosis_repo(config)
        if not repo_name:
            logger.warning(f"scan_heartbeats: no repo maps to {sid} (module_type={mtype!r}); skipping triage. Add a 'module_repo_map' entry if this module should be tracked.")
            continue

        if rec_gave_up:
            last_err = recovery.get("last_error") or "unknown"
            crash_sig = recovery.get("last_crash_sig") or "unknown"
            title = f"Recovery exhausted — {sid}"
            body = (f"**Automated Recovery Escalation**\n\nThe Hub watchdog attempted to recover module "
                    f"**{sid}** (type={mtype or 'unknown'}) by restarting its systemd unit, but gave up. "
                    f"A restart structurally cannot fix this — the cause is most likely a missing venv / "
                    f"interpreter, a broken installer path, or a configuration error (the same class of "
                    f"failure as the cs status=203/EXEC venv-wipe strand).\n\n"
                    f"**Give-up reason:** {last_err}\n**Last crash signature:** {crash_sig}\n\n"
                    f"Action needed: re-run the spoke's installer (`install_all.sh`, or the spoke's own "
                    f"installer, e.g. `/opt/lm/cs/install_cs.sh`) to recreate the venv / repair the unit, "
                    f"then restart the spoke. The Hub watchdog will clear its recovery state and resume "
                    f"monitoring once the spoke reconnects.")
            scan_heartbeats._gave_up_filed[sid] = True
            logger.info(f"scan_heartbeats: {sid} hub recovery GAVE_UP ({last_err}); escalating once.")
        elif missing:
            title = f"Missing heartbeat — {sid}"
            body = (f"**Automated Heartbeat Triage**\n\nNo `[heartbeat]` log line was found for module "
                    f"**{sid}** (type={mtype or 'unknown'}) in the latest Hub logs. The module may be "
                    f"stopped or its telemetry relay to the Hub is broken.\n\nThis issue was automatically created for triage.")
        else:
            title = f"Stale heartbeat — {sid}"
            body = (f"**Automated Heartbeat Triage**\n\nThe latest `[heartbeat]` log line for module "
                    f"**{sid}** (type={mtype or 'unknown'}) is {int(age_s)}s old (stale threshold {stale_s}s). "
                    f"Last heartbeat timestamp: {hb}.\n\nThe module may be hung or its telemetry relay is delayed. "
                    f"This issue was automatically created for triage.")
        error_data = {"module": sid, "title": title, "body": body, "repo": repo_name}
        try:
            repo_obj = gh_current.get_repo(repo_name)
            create_automated_issue(gh_current, monitored_repos, repo_obj, error_data)
            triaged += 1
            logger.info(f"scan_heartbeats: triaged {sid} (missing={missing}, age_s={age_s}) -> {repo_name}")
        except GithubException as ge:
            if ge.status == 404:
                logger.error(f"scan_heartbeats: repo {repo_name} not found (404) for {sid}; skipping.")
            else:
                logger.error(f"scan_heartbeats: failed to create issue for {sid} in {repo_name}: {ge}")
        except Exception as e:
            logger.error(f"scan_heartbeats: failed to triage {sid}: {e}")
    if triaged:
        logger.info(f"scan_heartbeats: triaged {triaged} module(s) with missing/stale heartbeats.")
    else:
        logger.debug("scan_heartbeats: all expected modules have fresh heartbeats.")


def scan_bugs(gh_current, config, hub_logs):
    """File GitHub issues for user-submitted "File a Bug" reports.

    The WebUI footer button POSTs an explanation + console + HTML + screenshot
    to the hub's /api/bug-report; the hub stores the full artifacts under
    data_dir/bugs/<id>/ and logs a short ``[bug-report] id=<id> ...`` marker
    line. This scans the RAW hub_logs (before filter_error_logs drops it) for
    those markers, pulls each report's metadata from the hub (GET_BUG_REPORTS),
    and for any not-yet-filed report:
      - fetches the full artifacts (GET_BUG_REPORT) — used ONLY to build a
        concise context block; the raw console/HTML/screenshot are kept out of
        the public issue body (they live on the hub and are pulled back by
        process_single_issue as AI-fix context via the bug-report-id marker).
      - files a clean-body GitHub issue (raw=True, labels automated-fix+bug)
        carrying the user's explanation + context + a hidden
        ``<!-- bug-report-id: <id> -->`` reference.
      - marks the report filed on the hub (MARK_BUG_FILED) so it isn't re-filed.

    The issue carries 'automated-fix', so scan_repo_issues -> process_single_issue
    will then attempt a fix (once lbockenstedt/lm is in monitored_repos).
    """
    if not config.get("bug_report_enabled", True):
        return
    if not hub_logs:
        return

    # Parse [bug-report] id=... markers out of the raw hub logs (newest-first).
    # The marker line format (hub api.py): "[bug-report] id=<rid> severity=...
    # view=... summary=...". Only the id is needed here; the full report is
    # fetched from the hub.
    id_pat = re.compile(r'\[bug-report\][^\n]*?\bid=([0-9a-fA-F]+)')
    seen_ids = []
    seen_set = set()
    for entry in hub_logs or []:
        if not isinstance(entry, dict):
            continue
        log = entry.get("log") or ""
        if "[bug-report]" not in log:
            continue
        m = id_pat.search(log)
        if not m:
            continue
        rid = m.group(1)
        if rid in seen_set:
            continue
        seen_set.add(rid)
        seen_ids.append(rid)
    if not seen_ids:
        return

    client = _get_hub_agent_client()
    if not client:
        logger.warning("scan_bugs: no hub agent client; skipping bug-report filing.")
        return

    # Reconcile against the hub's filed flag so a bugfixer restart (which
    # clears the in-memory _filed set) does not re-file reports already filed
    # in a prior process lifetime. _filed is the per-process dedup fast-path.
    if not hasattr(scan_bugs, "_filed"):
        scan_bugs._filed = set()
    filed_on_hub = set()
    try:
        reports = client.request_sync("GET_BUG_REPORTS", {}, timeout=10)
        for r in (reports.get("reports") if isinstance(reports, dict) else []) or []:
            if isinstance(r, dict) and r.get("filed"):
                filed_on_hub.add(r.get("id"))
    except Exception as e:
        logger.warning(f"scan_bugs: GET_BUG_REPORTS failed: {e}")
    scan_bugs._filed |= filed_on_hub

    monitored_repos = get_monitored_repos(config)
    repo_name = (config.get("bug_report_repo") or "").strip()
    if not repo_name:
        repo_name = MODULE_TYPE_REPO.get("hub") or resolve_self_diagnosis_repo(config)
    if not repo_name:
        logger.warning("scan_bugs: no bug_report_repo/hub repo resolved; skipping.")
        return

    filed_this_cycle = 0
    for rid in seen_ids:
        if rid in scan_bugs._filed:
            continue
        # Pull the full report from the hub. The body we file is clean; the
        # raw console/HTML/screenshot stay on the hub for fix context.
        try:
            report = client.request_sync("GET_BUG_REPORT", {"id": rid}, timeout=15)
        except Exception as e:
            logger.warning(f"scan_bugs: GET_BUG_REPORT {rid} failed: {e}")
            continue
        if not isinstance(report, dict) or not report.get("id"):
            logger.warning(f"scan_bugs: report {rid} not found on hub (may have been evicted); skipping.")
            continue
        if report.get("filed"):
            scan_bugs._filed.add(rid)
            continue

        explanation = (report.get("report_json") and _safe_json_field(report.get("report_json"), "explanation")) \
            or report.get("summary") or ""
        severity = (report.get("report_json") and _safe_json_field(report.get("report_json"), "severity")) \
            or report.get("severity") or "medium"
        ctx = report.get("context") or {}
        # Build a clean, public-safe issue body.
        ctx_lines = []
        if isinstance(ctx, dict):
            for k in ("currentView", "currentSubView", "currentTenant", "url",
                      "hubVersion", "webuiVersion", "username", "userAgent"):
                v = ctx.get(k)
                if v:
                    ctx_lines.append(f"- **{k}**: {v}")
        title = f"🤖 Bug Report: {str(explanation)[:80].strip()}"
        body = (
            f'**Filed via the LM WebUI "File a Bug" button**\n\n'
            f"### What's wrong\n{explanation}\n\n"
            f"### Context\n" + ("\n".join(ctx_lines) if ctx_lines else "_no context captured_") + "\n\n"
            f"**Severity:** {severity}\n\n"
            f"---\n"
            f"<!-- bug-report-id: {rid} -->\n"
            f"_Full console log, raw DOM, and screenshot are stored on the hub "
            f"(report id `{rid}`) and are NOT included in this public issue. "
            f"BugFixer pulls them from the hub as fix context._\n"
        )
        error_data = {"module": "hub", "title": title, "body": body, "repo": repo_name}
        try:
            gh_repo = gh_current.get_repo(repo_name)
            issue = create_automated_issue(gh_current, monitored_repos, gh_repo, error_data,
                                           labels=["automated-fix", "Bug"], raw=True)
            if issue is None:
                logger.info(f"scan_bugs: {rid} not filed this cycle (cooldown/dedup/no-op).")
                continue
            issue_url = getattr(issue, "html_url", "") or ""
            try:
                client.request_sync("MARK_BUG_FILED", {"id": rid, "issue_url": issue_url}, timeout=10)
            except Exception as me:
                logger.warning(f"scan_bugs: MARK_BUG_FILED {rid} failed: {me}")
            scan_bugs._filed.add(rid)
            filed_this_cycle += 1
            logger.info(f"scan_bugs: filed bug report {rid} -> {repo_name}#{getattr(issue, 'number', '?')} ({issue_url})")
        except Exception as e:
            logger.error(f"scan_bugs: failed to file bug report {rid} in {repo_name}: {e}")
    if filed_this_cycle:
        logger.info(f"scan_bugs: filed {filed_this_cycle} new bug report(s).")
    else:
        logger.debug("scan_bugs: no new unfilled bug reports this cycle.")


def _safe_json_field(json_str, field):
    """Parse a JSON string and return one field, or '' on any failure.

    Used by scan_bugs to pull explanation/severity out of the report.json blob
    the hub returns via GET_BUG_REPORT without crashing on a malformed string.
    """
    try:
        obj = json.loads(json_str) if isinstance(json_str, str) else None
        if isinstance(obj, dict):
            v = obj.get(field)
            return v if v is not None else ""
    except Exception:
        pass
    return ""


def _bug_report_fix_context(issue_body):
    """Pull a "File a Bug" report's full artifacts from the hub as fix context.

    The public GitHub issue body for a user-filed bug carries only the user's
    explanation + a hidden ``<!-- bug-report-id: <id> -->`` reference; the raw
    console/HTML/screenshot live on the hub. This detects that marker in the
    issue body, fetches GET_BUG_REPORT from the hub, and returns a context
    section (console errors + DOM excerpt + screenshot note) to append to the
    issue body fed to apply_ai_fix / review_fix — so the AI gets the rich
    artifacts WITHOUT them ever landing in the public issue. Returns "" if no
    marker is present or anything fails (never blocks the fix).
    """
    if not isinstance(issue_body, str) or not issue_body:
        return ""
    m = re.search(r'<!--\s*bug-report-id:\s*([0-9a-fA-F]+)\s*-->', issue_body)
    if not m:
        return ""
    rid = m.group(1)
    client = _get_hub_agent_client()
    if not client:
        return ""
    try:
        report = client.request_sync("GET_BUG_REPORT", {"id": rid}, timeout=15)
    except Exception as e:
        logger.warning(f"fix-context: GET_BUG_REPORT {rid} failed: {e}")
        return ""
    if not isinstance(report, dict) or not report.get("id"):
        return ""

    console = str(report.get("console") or "")
    # Surface only the error/exception lines from the console — the most useful
    # signal for a UI bug — capped to keep the prompt bounded.
    err_lines = [ln for ln in console.splitlines()
                 if re.search(r'\b(error|exception|failed|uncaught|traceback)\b', ln, re.IGNORECASE)]
    console_excerpt = "\n".join(err_lines[-40:]) if err_lines else "(no error-level console lines)"

    dom = str(report.get("dom") or "")
    dom_excerpt = dom[:4096] if dom else "(not captured)"
    if len(dom) > 4096:
        dom_excerpt += "\n…[DOM truncated]"

    has_shot = "present (stored on hub, not inlined)" if report.get("screenshot_b64") else "not captured"

    return (
        f"\n\n--- Additional fix context from File-a-Bug report `{rid}` ---\n"
        f"The user filed this bug from the LM WebUI. The captured browser state "
        f"(kept on the hub, not in this public issue) is below — use it to localize "
        f"the fault in the WebUI / hub code.\n\n"
        f"### Browser console (error/exception lines)\n```\n{console_excerpt}\n```\n\n"
        f"### DOM excerpt (first 4 KB)\n```html\n{dom_excerpt}\n```\n\n"
        f"### Screenshot: {has_shot}\n"
    )


def scan_hub_logs(gh_current, config):
    """Phase: Scan Hub for new errors and create GitHub issues."""
    global state
    update_task_state(task_id="HubScan", task_name="Scanning Hub Logs", action="start")
    logger.info("Scanning Hub for new errors...")
    try:
        hub_logs = get_hub_logs()
        if hub_logs:
            # Heartbeat triage runs on the RAW hub_logs (before filter_error_logs
            # drops heartbeat lines): if an expected module's [heartbeat] line is
            # missing or stale, file/reopen an issue so a dead or hung module is
            # caught even when it is emitting no errors.
            try:
                scan_heartbeats(gh_current, config, hub_logs)
            except Exception as hb_err:
                logger.error(f"Heartbeat scan failed: {hb_err}")
            # File-a-Bug triage: the WebUI footer button logs a short
            # [bug-report] marker line; scan_bugs filters the RAW hub_logs for
            # those markers (before filter_error_logs drops them), pulls the
            # full artifacts from the hub, files a clean-body GitHub issue, and
            # marks it filed so it isn't re-filed. The issue carries
            # 'automated-fix' so scan_repo_issues -> process_single_issue then
            # attempts a fix, pulling the same artifacts back as fix context.
            try:
                scan_bugs(gh_current, config, hub_logs)
            except Exception as bug_err:
                logger.error(f"Bug-report scan failed: {bug_err}")
            # Scrub to error-relevant entries only before paying for an LLM
            # call: keeps the prompt small (avoids context-overflow 500s) and
            # focuses the model on actionable errors instead of INFO noise.
            error_logs = filter_error_logs(hub_logs)
            logger.info(
                f"Hub logs scrubbed: {len(hub_logs)} entries -> {len(error_logs)} "
                f"error-relevant entries for LLM analysis."
            )
            actionable_errors = []
            if not error_logs:
                logger.info("No error-level Hub log entries this cycle. Skipping LLM analysis.")
            else:
                actionable_errors = analyze_logs_for_errors(error_logs)
            monitored_repos = get_monitored_repos(config)
            for error in actionable_errors:
                # Defensive: ensure error is a dict (analyze_logs_for_errors already
                # guarantees this, but we double-check to be absolutely safe).
                if not isinstance(error, dict):
                    logger.warning(f"Skipping non-dict actionable error: {error!r}")
                    continue
                if not error.get('body') or not str(error.get('body')).strip():
                    logger.warning(f"Skipping actionable error with no body specified: {error.get('title')}")
                    continue

                # Route the issue to the module's own repo rather than relying on
                # the LLM's repo guess (which previously dumped everything into the
                # self-diagnosis repo). The module is authoritative.
                module = error.get('module')
                repo_name = resolve_module_repo(module, monitored_repos, config)
                if not repo_name:
                    # Fall back to the LLM's repo hint only if it is itself a
                    # monitored repo (so we never file into an arbitrary repo).
                    llm_repo = error.get('repo') or ''
                    if llm_repo and llm_repo in monitored_repos:
                        repo_name = llm_repo
                    else:
                        source_info = error.get('source_data', {})
                        host_info = source_info.get('host', 'unknown host') if isinstance(source_info, dict) else 'unknown source'
                        logger.warning(
                            f"Skipping actionable error for module={module!r} (source host: {host_info}): no monitored repo "
                            f"maps to this module (LLM repo hint={llm_repo!r}). Add a "
                            f"'module_repo_map' entry in Settings if this module should be tracked."
                        )
                        continue
                # Make the resolved repo authoritative for downstream code.
                error['repo'] = repo_name
                try:
                    repo_obj = gh_current.get_repo(repo_name)
                    create_automated_issue(gh_current, monitored_repos, repo_obj, error)
                    logger.info(f"Handled automated issue for log error in {repo_name} (module={module})")
                except GithubException as ge:
                    if ge.status == 404:
                        logger.error(
                            f"Cannot create automated issue for '{repo_name}': repository not found (404). "
                            f"Verify that '{repo_name}' exists and the configured GITHUB_TOKEN has access. "
                            f"Skipping this error."
                        )
                    else:
                        logger.error(f"Failed to create auto-issue for {repo_name}: {ge}")
                except Exception as e:
                    logger.error(f"Failed to create auto-issue for {repo_name}: {e}")
        else:
            # Hub unreachable: get_hub_logs() returned None. If BugFixer has a
            # Hub agent client configured (so this is an outage, not a missing
            # config), triage a hub-down heartbeat miss against the self-diagnosis
            # repo so a silent Hub outage does not pass unreported. Dedup keeps
            # this to one issue across cycles; a 'bugfixer-dismissed' label
            # suppresses it permanently.
            if _get_hub_agent_client():
                try:
                    repo_name = resolve_self_diagnosis_repo(config)
                    if repo_name:
                        error_data = {
                            "module": "hub",
                            "title": "Missing heartbeat — hub",
                            "body": ("**Automated Heartbeat Triage**\n\nBugFixer could not fetch Hub logs this "
                                     "cycle (GET_LOGS returned no data). The Hub may be down or the BugFixer agent "
                                     "link is not approved/connected.\n\nThis issue was automatically created for triage."),
                            "repo": repo_name,
                        }
                        monitored_repos = get_monitored_repos(config)
                        repo_obj = gh_current.get_repo(repo_name)
                        create_automated_issue(gh_current, monitored_repos, repo_obj, error_data)
                        logger.warning("scan_heartbeats: Hub unreachable — triaged hub-down to self-diagnosis repo.")
                except Exception as e:
                    logger.error(f"scan_heartbeats: failed to triage hub-down: {e}")
            else:
                logger.info("Hub log scan skipped — no Hub agent client configured (set HUB_WS_URL).")
    except Exception as e:
        logger.error(f"Hub log scan failed: {e}")
    finally:
        update_task_state(task_id="HubScan", action="end")


__all__ = [
    'get_hub_logs',
    'get_hub_state',
    'filter_error_logs',
    'analyze_logs_for_errors',
    'scan_hub_logs',
    'scan_heartbeats',
    'scan_bugs',
    '_safe_json_field',
    '_bug_report_fix_context',
    'verify_production_fixes',
    'MODULE_TYPE_REPO',
    'HEARTBEAT_STALE_S_DEFAULT',
]
