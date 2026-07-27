"""
pr_review.py — BugFixer PR PRE-REVIEW (review-only; a human is the sole gate).

Polls OPEN pull requests on the monitored repos and runs the dual-copy-guard
parity checks against each PR's changed-file set, then posts a COMMENT-type
summary (upserted in place, keyed by head SHA so it never spams) plus a
NON-required informational `bugfixer/review` commit status.

INVARIANTS (see memory pr-gate-bugfixer-prereview):
  * NEVER approves/denies a PR and NEVER pushes to the branch. It posts findings
    as a comment; only a human approves/denies.
  * The status check is informational only (always `success`, count in the
    description). It must stay a NON-required check so it can never block a merge.
  * This is the cheap, deterministic Tier-1 (zero LLM). The skeptical-review
    panel + skill-completeness + QA layers get added on top later.

Cross-repo caveat: several mirror pairs span SEPARATE GitHub repos (lm <-> cs).
A PR lives in ONE repo, so those can only be flagged ADVISORY ("ensure the twin
PR exists"); within-repo pairs are hard findings.

TODO: the pair rules below duplicate the dual-copy-guard skill's reference. Once
proven, load them from `.claude/skills/dual-copy-guard/reference.md` (or a shared
machine-readable pairs file) so there is a single source of truth.
"""
import logging

from github_ops import get_monitored_repos
from app_state import update_task_state

logger = logging.getLogger(__name__)

PR_REVIEW_MARKER = "<!-- bugfixer-pr-review -->"
STATUS_CONTEXT = "bugfixer/review"
_LEVEL_ORDER = {"error": 0, "warning": 1, "advisory": 2}
_LEVEL_ICON = {"error": "\U0001F534", "warning": "\U0001F7E0", "advisory": "\U0001F535"}


def _basename(path):
    return path.rsplit("/", 1)[-1]


def check_parity(repo_full_name, changed):
    """Deterministic dual-copy/parity findings for a PR's changed-file set.

    ``changed`` = repo-root-relative paths in the PR. Returns a list of
    {level, title, detail} dicts (level: error | warning | advisory).
    """
    findings = []
    changed = set(changed)
    repo = (repo_full_name or "").lower()
    is_cs = repo.endswith("/cs") or repo == "cs"
    is_lm = repo.endswith("/lm") or repo == "lm"

    # ---- cs (client-sim) WITHIN-REPO pairs -------------------------------
    if is_cs:
        # canonical common.sh -> generated linux copy (byte-identical)
        if "clients/lib/common.sh" in changed and "clients/linux/common.sh" not in changed:
            findings.append({
                "level": "error",
                "title": "common.sh canonical changed but generated copy not updated",
                "detail": "`clients/lib/common.sh` (canonical) is in this PR but its byte-identical copy "
                          "`clients/linux/common.sh` is not. Regenerate it: "
                          "`cp clients/lib/common.sh clients/linux/common.sh` (verify with `cmp`).",
            })
        if "clients/linux/common.sh" in changed and "clients/lib/common.sh" not in changed:
            findings.append({
                "level": "error",
                "title": "generated common.sh edited without its canonical source",
                "detail": "`clients/linux/common.sh` is GENERATED from `clients/lib/common.sh`. Edit the "
                          "canonical file and re-`cp` — never edit the generated copy directly.",
            })
        # linux <-> windows client parity (basename .sh <-> .ps1). Some scripts
        # are intentionally single-platform — exclude them so we don't nag.
        _LINUX_ONLY = {"agent", "recovery"}                      # no Windows twin by design
        _WINDOWS_ONLY = {"launch-terminals", "sim_log_monitor", "sys_log_monitor"}
        lin = {_basename(p)[:-3] for p in changed if p.startswith("clients/linux/") and p.endswith(".sh")} - _LINUX_ONLY
        win = {_basename(p)[:-4] for p in changed if p.startswith("clients/windows/") and p.endswith(".ps1")} - _WINDOWS_ONLY
        for base in sorted(lin - win):
            findings.append({
                "level": "warning",
                "title": "linux `%s.sh` changed without windows `%s.ps1`" % (base, base),
                "detail": "Client parity: `clients/linux/%s.sh` is in this PR but `clients/windows/%s.ps1` "
                          "is not. A sim/behavior must land on BOTH platforms (see the add-simulation / "
                          "dual-copy-guard skills). If this is intentional, note why in the PR." % (base, base),
            })
        for base in sorted(win - lin):
            findings.append({
                "level": "warning",
                "title": "windows `%s.ps1` changed without linux `%s.sh`" % (base, base),
                "detail": "Client parity: `clients/windows/%s.ps1` is in this PR but `clients/linux/%s.sh` "
                          "is not." % (base, base),
            })
        # dns_fail.txt triple copy
        dns_copies = ["configs/dns_fail.txt", "clients/linux/dns_fail.txt", "clients/windows/dns_fail.txt"]
        present = [p for p in dns_copies if p in changed]
        if present and len(present) < len(dns_copies):
            missing = [p for p in dns_copies if p not in changed]
            findings.append({
                "level": "error",
                "title": "dns_fail.txt copies out of sync",
                "detail": "The bogus-domains list has 3 copies that must match. This PR updates "
                          + ", ".join("`%s`" % p for p in present) + " but not "
                          + ", ".join("`%s`" % p for p in missing) + ".",
            })
        # cross-repo advisories (twins live in the lm repo)
        if "lm-spoke/static/sim-views.js" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim-views.js twin lives in the lm repo",
                "detail": "This PR changes `lm-spoke/static/sim-views.js`; its twin `WebUI/sim-views.js` is in "
                          "the **lm** repo (separate PR). Ensure a matching lm PR keeps the two in lockstep.",
            })
        if "lm-spoke/src/sim_quota.py" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim_quota.py twin lives in the lm repo",
                "detail": "This PR changes `lm-spoke/src/sim_quota.py`; its hub twin "
                          "`core/src/simulations/sim_quota.py` is in the **lm** repo. Keep SIM_QUOTA_KEYS/logic "
                          "matched via a corresponding lm PR.",
            })

    # ---- lm cross-repo advisories ----------------------------------------
    if is_lm:
        if "WebUI/sim-views.js" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim-views.js twin lives in the cs repo",
                "detail": "This PR changes `WebUI/sim-views.js`; its twin `lm-spoke/static/sim-views.js` is in "
                          "the **cs** repo. Ensure a matching cs PR.",
            })
        if "core/src/simulations/sim_quota.py" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim_quota.py spoke twin lives in the cs repo",
                "detail": "This PR changes the hub `core/src/simulations/sim_quota.py`; its spoke twin "
                          "`lm-spoke/src/sim_quota.py` is in the **cs** repo.",
            })

    return findings


def _render(findings, head_sha):
    lines = [
        PR_REVIEW_MARKER,
        "<!-- head: %s -->" % head_sha,
        "## \U0001F916 BugFixer PR pre-review",
        "",
        "_Automated pre-review — **informational only**. A human is the sole approver; "
        "this bot never approves, denies, or edits the branch._",
        "",
    ]
    if not findings:
        lines += [
            "✅ **Parity checks passed** — no dual-copy / cross-platform drift detected in the changed files.",
            "",
            "_(Correctness panel + QA layers are not enabled in this first cut.)_",
        ]
    else:
        for f in sorted(findings, key=lambda x: _LEVEL_ORDER.get(x["level"], 9)):
            icon = _LEVEL_ICON.get(f["level"], "•")
            lines += ["%s **%s — %s**" % (icon, f["level"].upper(), f["title"]), "", f["detail"], ""]
    return "\n".join(lines)


def _find_marker_comment(pr):
    for c in pr.get_issue_comments():
        if PR_REVIEW_MARKER in (c.body or ""):
            return c
    return None


def _review_one(repo, pr):
    if getattr(pr, "draft", False):
        return  # skip WIP drafts
    head_sha = pr.head.sha
    existing = _find_marker_comment(pr)
    # Dedup: already reviewed THIS exact head -> nothing to do (never spam).
    if existing and ("<!-- head: %s -->" % head_sha) in (existing.body or ""):
        return
    changed = [f.filename for f in pr.get_files()]
    findings = check_parity(repo.full_name, changed)
    body = _render(findings, head_sha)
    if existing:
        existing.edit(body)
        action = "updated"
    else:
        pr.create_issue_comment(body)
        action = "created"
    # Informational, NON-blocking status. Always 'success' so it can never block a
    # merge (the human is the gate); the count rides the description. Best-effort:
    # needs statuses:write on the token/app — if absent, the comment still posts.
    try:
        n = len(findings)
        desc = "no parity issues" if n == 0 else "%d parity finding(s) — see review comment" % n
        repo.get_commit(head_sha).create_status(
            state="success", context=STATUS_CONTEXT, description=desc[:140])
    except Exception as e:  # noqa: BLE001
        logger.info("pr_review: status check skipped (%s) — token likely lacks statuses:write", e)
    logger.info("pr_review: %s PR #%s reviewed (%d findings, comment %s)",
                repo.full_name, pr.number, len(findings), action)


def scan_open_prs(gh, config):
    """Poll open PRs on monitored repos and post a parity pre-review on each.

    Gated behind ``pr_review_enabled`` (default False) so it is inert until turned
    on. Never raises out — one bad repo/PR must not break the scan cycle.
    """
    if not config.get("pr_review_enabled", False):
        return
    try:
        repos = get_monitored_repos(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("pr_review: could not resolve monitored repos: %s", e)
        return
    if not repos:
        return
    # Surface PR-review as a distinct 'pr'-kind task so the UI badges it apart
    # from bug scans/fixes (see templates/index.html Active Tasks).
    update_task_state("PRReview", "PR pre-review — scanning open PRs", "start", kind="pr")
    try:
        for repo_name in repos:
            try:
                repo = gh.get_repo(repo_name)
                for pr in repo.get_pulls(state="open"):
                    try:
                        _review_one(repo, pr)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("pr_review: PR #%s in %s failed: %s",
                                       getattr(pr, "number", "?"), repo_name, e)
            except Exception as e:  # noqa: BLE001
                logger.warning("pr_review: repo %s failed: %s", repo_name, e)
    finally:
        update_task_state("PRReview", action="end")
