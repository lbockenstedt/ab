"""
pr_review.py — AppBuilder PR PRE-REVIEW (review-only; a human is the sole gate).

Polls OPEN pull requests on the monitored repos and runs a Tier-1 deterministic
pass (dual-copy-guard parity, secrets scan, undefined-name lint) against each
PR's changed-file set, then posts a COMMENT-type summary (upserted in place,
keyed by head SHA so it never spams) plus a NON-required informational
`ab/review` commit status.

INVARIANTS (see memory pr-gate-ab-prereview):
  * NEVER approves/denies a PR and NEVER pushes to the branch. It posts findings
    as a comment; only a human approves/denies.
    ONE DELIBERATE, NARROWLY-SCOPED EXCEPTION (added for feature auto-drive,
    broadened 2026-08-29): ``_maybe_auto_merge``/``_automerge_decision`` CAN
    approve+merge a PR unattended — but ONLY when its target branch is
    explicitly opted into ``feature_automerge_target_branches`` (defaults to
    empty — opt-in, not opt-out; e.g. ["dev", "qa"]). main/master (and the
    configured ``default_branch``) are refused structurally, so listing one
    there does nothing — merges into the release branch stay owner-only.
    Beyond that gate: both review
    panels Approve above a configurable confidence floor, the diff is a
    provably-additive shape (``feature_allowlist``), the diff touches no
    configured boundary, and the repo is explicitly opted into
    ``feature_automerge_repos``. This is no longer gated by PR authorship —
    a human-authored PR targeting an allowlisted branch CAN be auto-merged
    now, same as a bot-authored one. See ``_automerge_decision``'s own
    docstring for the complete, exhaustive gate list.
  * The status check is informational only (always `success`, count in the
    description). It must stay a NON-required check so it can never block a merge.
  * Tier-1 (zero LLM, always on, three checks — see ``_review_one``):
      - ``check_parity`` — dual-copy/cross-platform drift (see module list below).
      - ``check_secrets`` (secrets_scan.py) — regex scan of ADDED diff lines for
        hardcoded credentials. Never echoes the actual secret into the comment.
      - ``check_undefined_names`` (lint_python.py) — ruff F821/F822/F823 against
        the FULL post-patch file (fetched via the contents API, not the diff),
        for changed .py files. Exists specifically because the LLM panel below
        only sees diff hunks and can hallucinate "missing import" when the
        import is just outside the visible hunk — this is deterministic ground
        truth to catch (or debunk) that class of claim.
  * On a head change, also runs (each independently opt-in):
      - the LLM change-summary (always, if an LLM is configured).
      - ``pr_review_llm_enabled`` → the general cross-provider skeptical
        reviewer panel (``fix_engine.review_fix``, ≥0.80 confidence gate) the
        bug/feature fix pipeline uses.
      - ``pr_review_state_logic_enabled`` → a SECOND, narrow-scope panel pass
        (``_state_logic_review``) that ONLY checks two defect shapes: a
        status/enum value conflating distinct states (e.g. "any warning" read
        as "failed"), and new logic placed after an early-return that skips it
        on the path it was meant to cover. Added after both shapes hit the same
        PR (lm#135) in one review cycle.
    All panel verdicts render ADVISORY-only; none ever approve/deny.
    (skill-completeness + QA layers still get added on top later.)

Cross-repo caveat: several mirror pairs span SEPARATE GitHub repos (lm <-> cs,
dns <-> lm/dns, dhcp <-> lm/dhcp). A PR lives in ONE repo, so those can only be
flagged ADVISORY ("ensure the twin PR exists"); within-repo pairs are hard
findings. ``_resolve_cross_repo_twins`` auto-drops the advisory when a matching
open PR in the twin repo already touches the twin path.

TODO: the pair rules below duplicate the dual-copy-guard skill's reference. Once
proven, load them from `.claude/skills/dual-copy-guard/reference.md` (or a shared
machine-readable pairs file) so there is a single source of truth.
"""
import logging
import os
import re
from datetime import timedelta

from github_ops import get_monitored_repos
from app_state import update_task_state, record_pr_review, update_pr_review, mark_pr_approved, state
import feature_boundary
import feature_allowlist
from branch_policy import AUTO_BRANCH_PREFIXES_BY_KIND
from pr_actions import approve_pr, merge_pr
from secrets_scan import check_secrets
from check_tooltips import find_missing_tooltips_in_files
from lint_python import check_undefined_names
from check_unattended_mutation import check_unattended_mutation
from check_test_regressions import check_test_regressions
from attr_definition_lookup import (
    extract_getattr_names, find_attr_definitions, format_wiring_context)
from pr_review_retry import is_queued_for_retry_stale
from config_store import load_config

logger = logging.getLogger(__name__)

PR_REVIEW_MARKER = "<!-- ab-pr-review -->"
STATUS_CONTEXT = "ab/review"
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
    owner = (repo_full_name or "").split("/")[0] or "lbockenstedt"
    is_cs = repo.endswith("/cs") or repo == "cs"
    is_lm = repo.endswith("/lm") or repo == "lm"
    is_dns = repo.endswith("/dns") or repo == "dns"
    is_dhcp = repo.endswith("/dhcp") or repo == "dhcp"

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
        # cross-repo advisories (twins live in the lm repo). The `twin` field lets
        # _review_one verify the other repo and DROP the advisory when a matching PR
        # already updates it (only warn when the twin is genuinely NOT updated).
        if "lm-spoke/static/sim-views.js" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim-views.js twin lives in the lm repo",
                "detail": "This PR changes `lm-spoke/static/sim-views.js`; its twin `WebUI/sim-views.js` is in "
                          "the **lm** repo (separate PR). Ensure a matching lm PR keeps the two in lockstep.",
                "twin": {"repo": "%s/lm" % owner, "path": "WebUI/sim-views.js"},
            })
        if "lm-spoke/src/sim_quota.py" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim_quota.py twin lives in the lm repo",
                "detail": "This PR changes `lm-spoke/src/sim_quota.py`; its hub twin "
                          "`core/src/simulations/sim_quota.py` is in the **lm** repo. Keep SIM_QUOTA_KEYS/logic "
                          "matched via a corresponding lm PR.",
                "twin": {"repo": "%s/lm" % owner, "path": "core/src/simulations/sim_quota.py"},
            })

    # ---- lm cross-repo advisories ----------------------------------------
    if is_lm:
        if "WebUI/sim-views.js" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim-views.js twin lives in the cs repo",
                "detail": "This PR changes `WebUI/sim-views.js`; its twin `lm-spoke/static/sim-views.js` is in "
                          "the **cs** repo. Ensure a matching cs PR.",
                "twin": {"repo": "%s/cs" % owner, "path": "lm-spoke/static/sim-views.js"},
            })
        if "core/src/simulations/sim_quota.py" in changed:
            findings.append({
                "level": "advisory",
                "title": "sim_quota.py spoke twin lives in the cs repo",
                "detail": "This PR changes the hub `core/src/simulations/sim_quota.py`; its spoke twin "
                          "`lm-spoke/src/sim_quota.py` is in the **cs** repo.",
                "twin": {"repo": "%s/cs" % owner, "path": "lm-spoke/src/sim_quota.py"},
            })

    # ---- dns / dhcp dual-module-copy advisories ---------------------------
    # These two modules deliberately exist in TWO shapes: a standalone repo
    # (dns / dhcp, root = the module) and a copy nested under lm/ (lm/dns/,
    # lm/dhcp/). Unlike the single-file pairs above, a changed file inside the
    # module's PAYLOAD maps 1:1 to its twin path in the other repo — one
    # advisory per file (not one combined advisory) so _resolve_cross_repo_twins
    # can verify/drop each individually against the twin repo's open PRs.
    #
    # ONLY THE PAYLOAD IS TWINNED. The two shapes do NOT have the same file
    # set: of the 20 files in the dns repo only 4 have any counterpart under
    # lm/dns, so mapping *every* changed path produced warnings naming files
    # that cannot exist. The standalone repo owns its own CI, docs and tests;
    # `lm/dns` is a subdirectory of the lm repo and can never have its own
    # `.github/`. A PR that merely rolled shared CI plumbing into the dns repo
    # was reported as three "twin NOT updated" WARNINGs for
    # `lm/dns/.github/...` paths, none of which were real or actionable --
    # and an advisory that cannot be satisfied trains people to ignore the
    # ones that can.
    #
    # VERSION is excluded for the opposite reason: it exists on BOTH sides but
    # must NEVER be synced. Each repo (and each branch) owns its own version
    # sequence -- see promote.sh, which pins VERSION to the target branch.
    _DNS_DHCP_CAP = 15  # a module-wide rename/refactor could touch many files

    def _is_twinned_module_path(rel):
        """Is this module-relative path part of the shared payload?

        Payload = the runtime code and its dependency list. Everything else
        (VERSION, .github/, docs/, tests/, README/AGENTS/.gitignore/.env*) is
        per-repo plumbing that legitimately differs between the two shapes.
        """
        return rel == "requirements.txt" or rel.startswith("src/")

    if is_dns:
        for p in sorted(p for p in changed if _is_twinned_module_path(p))[:_DNS_DHCP_CAP]:
            findings.append({
                "level": "advisory",
                "title": "dns module twin lives in the lm repo",
                "detail": "This PR changes `%s`; its twin `lm/dns/%s` is in the **lm** repo "
                          "(dual-copy — see memory `vscode-root-is-nw-checkout-and-dns-dhcp-dual-shape`). "
                          "Ensure a matching lm PR." % (p, p),
                "twin": {"repo": "%s/lm" % owner, "path": "dns/%s" % p},
            })
    if is_dhcp:
        for p in sorted(p for p in changed if _is_twinned_module_path(p))[:_DNS_DHCP_CAP]:
            findings.append({
                "level": "advisory",
                "title": "dhcp module twin lives in the lm repo",
                "detail": "This PR changes `%s`; its twin `lm/dhcp/%s` is in the **lm** repo "
                          "(dual-copy — see memory `vscode-root-is-nw-checkout-and-dns-dhcp-dual-shape`). "
                          "Ensure a matching lm PR." % (p, p),
                "twin": {"repo": "%s/lm" % owner, "path": "dhcp/%s" % p},
            })
    if is_lm:
        _dns_changed = sorted(p for p in changed if p.startswith("dns/")
                              and _is_twinned_module_path(p[len("dns/"):]))[:_DNS_DHCP_CAP]
        for p in _dns_changed:
            stripped = p[len("dns/"):]
            findings.append({
                "level": "advisory",
                "title": "lm/dns twin lives in the standalone dns repo",
                "detail": "This PR changes `%s`; its twin `%s` is in the **dns** repo "
                          "(dual-copy — see memory `vscode-root-is-nw-checkout-and-dns-dhcp-dual-shape`). "
                          "Ensure a matching dns PR." % (p, stripped),
                "twin": {"repo": "%s/dns" % owner, "path": stripped},
            })
        _dhcp_changed = sorted(p for p in changed if p.startswith("dhcp/")
                               and _is_twinned_module_path(p[len("dhcp/"):]))[:_DNS_DHCP_CAP]
        for p in _dhcp_changed:
            stripped = p[len("dhcp/"):]
            findings.append({
                "level": "advisory",
                "title": "lm/dhcp twin lives in the standalone dhcp repo",
                "detail": "This PR changes `%s`; its twin `%s` is in the **dhcp** repo "
                          "(dual-copy — see memory `vscode-root-is-nw-checkout-and-dns-dhcp-dual-shape`). "
                          "Ensure a matching dhcp PR." % (p, stripped),
                "twin": {"repo": "%s/dhcp" % owner, "path": stripped},
            })

    return findings


_SUMMARY_HEADER = "### \U0001F4DD What changed"      # NB: kept in sync with _extract_summary's regex
_SUMMARY_MAX_FILES = 30
_SUMMARY_PATCH_CHARS = 1500
_SUMMARY_PROMPT_CHARS = 14000


def _summarize_changes(pr, files, config, repo=None):
    """Plain-language 'what changed' summary of the PR diff, via the LLM.

    Caller-gated to run only on a head change (not every scan), so the LLM cost is
    per-PR-update, not per-cycle. Best-effort: returns '' if disabled, the LLM is
    unavailable, or it yields nothing — the parity findings still post either way.

    Routed via requirements=LlmRequirements(batch_ok=True) (LLM Selection
    Redesign, Phase 5 site #15): this is the one call site fire-and-forget
    batch processing is safe for (best-effort, discards failures, gated to
    head-SHA changes already) — see _apply_batched_pr_summary below for what
    happens when the batch result actually arrives. A "" return here (batch
    queued, or genuinely no LLM available) just means THIS scan's comment
    posts without a summary section, same as any other best-effort miss.
    """
    if not config.get("pr_review_summary_enabled", True):
        return ""
    try:
        from llm_client import call_llm
        from model_selection import LlmRequirements
    except Exception:  # noqa: BLE001
        return ""
    parts = []
    for f in list(files)[:_SUMMARY_MAX_FILES]:
        fn = getattr(f, "filename", "?")
        stt = getattr(f, "status", "?")
        add = getattr(f, "additions", 0)
        dele = getattr(f, "deletions", 0)
        patch = getattr(f, "patch", None) or ""
        if len(patch) > _SUMMARY_PATCH_CHARS:
            patch = patch[:_SUMMARY_PATCH_CHARS] + "\n… (patch truncated)"
        parts.append("--- %s (%s, +%s/-%s)\n%s" % (fn, stt, add, dele, patch))
    digest = "\n\n".join(parts)
    n_files = len(list(files))
    if n_files > _SUMMARY_MAX_FILES:
        digest += "\n\n… (+%d more file(s))" % (n_files - _SUMMARY_MAX_FILES)
    digest = digest[:_SUMMARY_PROMPT_CHARS]
    if not digest.strip():
        return ""
    system = ("You are a senior engineer writing a concise, factual summary of a pull "
              "request's changes for a human reviewer. Output 2-6 short markdown bullets "
              "grouped by area/feature (e.g. 'DNS server list expanded to add more "
              "options', 'DNS latency updated to support app XYZ', 'New simulation module "
              "<name> added: <features>'). State ONLY what the diff shows; do not "
              "speculate, do not just list file names, no preamble or sign-off.")
    prompt = ("Summarize what this pull request changes, for the reviewer.\n\n"
              "PR title: %s\n\nDiff:\n%s" % (pr.title or "", digest))
    reqs = LlmRequirements(complexity="trivial", batch_ok=True,
                           deprioritize_local=True,
                           min_context_tokens=len(prompt) // 4)
    batch_context = None
    if repo is not None:
        batch_context = {"repo": repo.full_name, "pr": pr.number, "head_sha": pr.head.sha}
    try:
        out = call_llm(prompt, system_prompt=system, requirements=reqs,
                       batch_kind="pr_summary", batch_context=batch_context)
    except Exception as e:  # noqa: BLE001
        logger.info("pr_review: change-summary skipped (%s)", e)
        return ""
    if not isinstance(out, str):
        out = str(out or "")
    return out.strip()


def _apply_batched_pr_summary(context, text):
    """batch.py result handler for kind='pr_summary' (register_handler call
    below) — injects the plain-language change summary into the PR's existing
    marker comment once the batch result arrives, whenever poll_and_dispatch()
    next runs. Runs independently of pr_review's own scan cycle (minutes to
    hours after _summarize_changes queued it), so this re-fetches the PR from
    scratch rather than assuming any in-memory state is still valid.

    Best-effort/discardable exactly like the synchronous path it replaces: any
    failure here just means the PR comment never gets its summary section,
    same as if the LLM call had failed outright.
    """
    try:
        text = (text or "").strip()
        if not text or not context:
            return
        repo_full_name = context.get("repo")
        number = context.get("pr")
        head_sha = context.get("head_sha")
        if not repo_full_name or not number:
            return
        config = load_config()
        token = config.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return
        from github import Github
        gh = Github(token)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(int(number))
        # Discard if the head changed since this was queued — the comment
        # already reflects the NEW head's own findings, and grafting an old
        # summary onto it would be actively misleading.
        if head_sha and pr.head.sha != head_sha:
            logger.info("pr_review: discarding stale batched summary for %s#%s (head changed)",
                       repo_full_name, number)
            return
        existing = _find_marker_comment(pr)
        if not existing:
            return
        body = existing.body or ""
        if _SUMMARY_HEADER in body:
            return  # a synchronous re-scan already posted one — don't duplicate
        # Insert right where _render always places it, so a later synchronous
        # re-scan's _extract_summary() finds it in the same spot.
        anchor = "this bot never approves, denies, or edits the branch._\n\n"
        idx = body.find(anchor)
        if idx == -1:
            return
        insert_at = idx + len(anchor)
        new_body = body[:insert_at] + _SUMMARY_HEADER + "\n\n" + text + "\n\n" + body[insert_at:]
        existing.edit(new_body)
        logger.info("pr_review: applied batched change-summary to %s#%s", repo_full_name, number)
    except Exception as e:  # noqa: BLE001
        logger.info("pr_review: batched summary handler skipped (%s)", e)


try:
    from batch import register_handler as _register_batch_handler
    _register_batch_handler("pr_summary", _apply_batched_pr_summary)
except Exception:  # noqa: BLE001 - batch.py is optional/best-effort, per its own docstring
    pass


def _extract_summary(body):
    """Recover a previously-generated 'What changed' summary from an existing
    review comment (so a cached re-scan / post-restart keeps it without a new LLM
    call). Returns '' if absent."""
    if not body:
        return ""
    m = re.search(re.escape(_SUMMARY_HEADER) + r"\s*(.*?)(?:\n#{2,3} |\Z)", body, re.S)
    return m.group(1).strip() if m else ""


_PANEL_HEADER = "### \U0001F9E0 Skeptical review (panel)"   # 🧠 — kept in sync w/ _render
_PANEL_MAX_FILES = 40
# Raised from 4000/24000 after cs#65: a single legitimately-large-but-normal
# file diff (a ~34KB dual-copy-guard port, one file) got sliced to ~12% of its
# actual content by the OLD per-file cap alone — well before the total budget
# was even touched — and the panel rejected at 32% confidence reasoning purely
# from what it couldn't see, not from any real defect (verified: every
# specific claim in that review was false when checked against the full file).
# Both values are still well inside what every configured provider's context
# window supports (modest local Ollama models run num_ctx=32768 tokens by
# default, i.e. ~130K+ chars); a wasted reject costs more in human triage time
# and a burned LLM call than the extra review-time tokens do.
_PANEL_PATCH_CHARS = 20000
_PANEL_DIFF_CHARS = 60000


def _pr_diff_text(files):
    """Assemble a unified-diff-ish text from a PR's changed files for the review
    panel — a `--- <filename>` header + patch per file, capped so a huge PR can't
    blow the provider limit (review_fix caps again at 20k internally)."""
    parts = []
    for f in list(files)[:_PANEL_MAX_FILES]:
        fn = getattr(f, "filename", "?")
        patch = getattr(f, "patch", None) or ""
        if len(patch) > _PANEL_PATCH_CHARS:
            patch = patch[:_PANEL_PATCH_CHARS] + "\n… (patch truncated)"
        parts.append("--- %s\n%s" % (fn, patch))
    return "\n\n".join(parts)[:_PANEL_DIFF_CHARS]


def _skeptical_review(pr, files, config, repo=None, head_sha=None, gh=None):
    """Run the cross-provider skeptical reviewer panel on the PR diff — the SAME
    panel (``fix_engine.review_fix``, ≥0.80 confidence gate) the bug/feature fix
    pipeline uses, now unified so a human PR and a bot fix are judged by one
    mechanism. Returns its ``{confidence, verdict, critique}`` (or a ``status``
    dict when reviewers are offline).

    ADVISORY ONLY: the caller renders this into the review COMMENT; it is never
    turned into a real PR approval/denial or a blocking status — a human remains
    the sole gate (pr-gate INVARIANT). Gated behind ``pr_review_llm_enabled``
    (default False). Best-effort: None when disabled, no diff, or on any error.

    ``gh`` (the PyGithub client), when supplied, is used to resolve
    getattr(x, "name", ...) accesses in the diff against their REAL definition
    elsewhere in the repo (see attr_definition_lookup) — the class of doubt a
    diff-only or even full-file-of-changed-files view can't settle, because the
    defining file (e.g. the class `deploy`/`cp` is an instance of) is often
    NOT one this PR touches at all. cs#74's reviewer flagged exactly this
    shape (`getattr(deploy, "proxmox_states", {})`, `getattr(cp,
    "connected_agents", {})`) as unverifiable and landed on a 58% advisory
    reject over doubts a human later confirmed were both unfounded."""
    if not config.get("pr_review_llm_enabled", False):
        return None
    diff = _pr_diff_text(files)
    if not diff.strip():
        return None
    try:
        from fix_engine import review_fix
    except Exception as e:  # noqa: BLE001
        logger.info("pr_review: panel skipped (fix_engine import failed: %s)", e)
        return None
    # NOTE: the instruction block below contains literal Jinja examples (`{% for %}`,
    # `{{ }}`) whose `%` signs would be mis-parsed as %-format conversions. So only the
    # title/body header is %-formatted; the instructions are plain concatenation.
    issue_body = (
        "PR TITLE: %s\n\nPR DESCRIPTION:\n%s\n\n"
        % (pr.title or "", (pr.body or "").strip()[:4000])
    ) + (
        "NOTE: This is a HUMAN-authored pull request under pre-review — not a bot "
        "fix. Judge whether it is SAFE and CORRECT to merge as-is. In addition to "
        "correctness/regressions, weight these merge-safety defects heavily (they "
        "have broken prod before):\n"
        "1) TEMPLATE RENDER CRASHES — Jinja dot-access of a dict method (`x.items`, "
        "`x.keys`, `x.values`) inside `{{ }}` or `{% for %}`: dot-notation returns the "
        "bound METHOD, not the key, so it 500s the whole page; the fix is bracket "
        "access `x['items']`.\n"
        "2) Template/JS SYNTAX errors: unbalanced Jinja blocks, duplicate `const`/`let` "
        "in a <script>, leftover merge-conflict markers.\n"
        "3) Undefined names, obvious logic errors, security issues.\n"
        "Reject (lower confidence) if any such defect is present.")
    try:
        attr_names = extract_getattr_names(files)
        wiring_defs = find_attr_definitions(
            gh, repo, attr_names, changed_paths=[f.filename for f in files])
        issue_body += format_wiring_context(wiring_defs)
    except Exception as e:  # noqa: BLE001 — this only ever ADDS context; never block the review over it
        logger.info("pr_review: wiring-context lookup skipped (%s)", e)
    try:
        # builder_n=0 → no builder to exclude, so EVERY configured provider reviews
        # the human's diff (there is no bot author to leave out). repo+head_sha
        # (when available) let each reviewer fetch_repo_file instead of rejecting
        # on a truncated/incomplete diff view.
        review = review_fix(None, issue_body, {}, builder_n=0, diff_override=diff,
                            repo=repo, head_sha=head_sha)
    except Exception as e:  # noqa: BLE001
        logger.info("pr_review: panel skipped (review_fix error: %s)", e)
        return None
    return review if isinstance(review, dict) else None


_STATE_PANEL_HEADER = "### \U0001F500 State-logic / control-flow review (panel)"   # 🔀


def _state_logic_review(pr, files, config, repo=None, head_sha=None):
    """Second, narrower skeptical-panel pass focused specifically on the bug
    SHAPE that hit lm#135 twice in one review cycle: a status/enum value that
    conflates two distinct states (e.g. "any warning" silently treated as
    "failed"), and new logic placed after an early-return/guard that skips it
    on the exact path it was meant to cover. General-purpose review (see
    _skeptical_review) reads each hunk for local correctness; it does not
    reliably ask "trace every value this status variable can take" or "does
    this line actually execute on the failure path" — this pass asks nothing
    ELSE, so it can't get distracted the way a broad-scope reviewer can.

    Independent opt-in: gated behind ``pr_review_state_logic_enabled``
    (default False) — separate from ``pr_review_llm_enabled`` so a user can
    run the general panel without paying for this one, or vice versa. Same
    advisory-only contract as _skeptical_review: never blocks, never
    approves/denies. Best-effort: None when disabled, no diff, or on error."""
    if not config.get("pr_review_state_logic_enabled", False):
        return None
    diff = _pr_diff_text(files)
    if not diff.strip():
        return None
    try:
        from fix_engine import review_fix
    except Exception as e:  # noqa: BLE001
        logger.info("pr_review: state-logic panel skipped (fix_engine import failed: %s)", e)
        return None
    issue_body = (
        "PR TITLE: %s\n\nPR DESCRIPTION:\n%s\n\n"
        % (pr.title or "", (pr.body or "").strip()[:4000])
    ) + (
        "NOTE: This is a HUMAN-authored pull request under pre-review. Ignore style, "
        "naming, and general correctness — a SEPARATE broad reviewer already covers "
        "those. Your ONLY job is two specific defect shapes:\n\n"
        "1) STATE/STATUS COVERAGE — for every boolean/enum/status value this diff "
        "computes or changes the computation of: list every distinct state the "
        "underlying data can actually be in (not just the two the author had in "
        "mind), and check whether the new logic conflates states that should stay "
        "distinct (e.g. 'a warning present' vs 'the operation failed' vs 'the "
        "operation never ran' are three different things — treating any two of "
        "them as the same value is a defect even if each individually looks "
        "reasonable).\n\n"
        "2) REACHABILITY — for every new line of logic (a render call, a state "
        "update, a side effect): trace the function's control flow BACKWARD from "
        "that line to its entry. Does an early-return, guard clause, or "
        "short-circuit ABOVE it in the function actually let execution reach that "
        "line on the specific input/condition the author intended it for? A line "
        "that only runs when there's nothing left to act on (e.g. UI added after "
        "an empty-result early-return, when the UI's whole purpose is describing "
        "why the result is empty) is a defect even though the line itself is "
        "syntactically and logically correct in isolation.\n\n"
        "Report ONLY concrete instances of these two shapes, with the exact "
        "variable/line and which states/paths are conflated or unreachable. If you "
        "find neither, say so plainly — do not manufacture a finding to have "
        "something to report.")
    try:
        review = review_fix(None, issue_body, {}, builder_n=0, diff_override=diff,
                            repo=repo, head_sha=head_sha)
    except Exception as e:  # noqa: BLE001
        logger.info("pr_review: state-logic panel skipped (review_fix error: %s)", e)
        return None
    return review if isinstance(review, dict) else None


#: Matches the "[reviewer-name] " tag fix_engine prefixes onto each critique
#: when it joins them into the legacy single-string `critique` blob.
_REVIEWER_TAG_RE = re.compile(r"^\[([^\]]{1,60})\]\s*")

#: Splits that blob back apart. The lookahead for "[" matters: a critique's own
#: prose can contain " | " (table rows, shell pipes, code snippets), and
#: splitting on the bare separator would slice a single reviewer's text into
#: fragments attributed to nobody.
_REVIEWER_SPLIT_RE = re.compile(r"\s+\|\s+(?=\[[^\]]{1,60}\]\s)")

#: An enumerated point inside a critique: "1.", "2)", "(3)", "-", "*", "•".
#: Reviewers overwhelmingly write their findings as such a list, which the
#: whitespace-collapsed blob renders as one unbroken paragraph.
_ENUM_MARK_RE = re.compile(r"(?:(?<=^)|(?<=\s))(?:\(?\d{1,2}[\).:]|[-*\u2022])\s+")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'`])")


def _norm_conf_pct(value):
    """Confidence as a "58%" string, or None when it isn't a usable number."""
    try:
        from fix_engine import _norm_confidence
    except Exception:  # noqa: BLE001 — never let comment rendering fail on this
        def _norm_confidence(v):
            c = float(v)
            return max(0.0, min(1.0, c / 100.0 if c > 1.0 else c))
    try:
        return "%.0f%%" % (_norm_confidence(value) * 100)
    except (TypeError, ValueError):
        return None


def _reviewer_blocks(review):
    """[(name, verdict|None, confidence|None, critique)] for one panel result.

    Prefers the structured ``reviews`` list fix_engine now returns. Falls back
    to parsing the legacy " | "-joined ``critique`` string so PRs reviewed
    before that change — and any other producer of this shape — still render
    per-reviewer rather than as one blob.
    """
    structured = review.get("reviews")
    if isinstance(structured, list) and structured:
        out = []
        for r in structured:
            if isinstance(r, dict):
                out.append((str(r.get("reviewer") or "?"), r.get("verdict"),
                            r.get("confidence"), str(r.get("critique") or "").strip()))
        if out:
            return out

    text = str(review.get("critique") or "").strip()
    if not text:
        return []
    blocks = []
    for part in _REVIEWER_SPLIT_RE.split(text):
        part = part.strip()
        if not part:
            continue
        m = _REVIEWER_TAG_RE.match(part)
        if m:
            blocks.append((m.group(1), None, None, part[m.end():].strip()))
        else:
            # Untagged (a single-reviewer panel, or a caller that never tagged).
            blocks.append((None, None, None, part))
    return blocks


def _structure_critique(text):
    """(preamble, [points]) — turn one reviewer's prose into readable parts.

    Reviewers write findings as enumerated lists that arrive whitespace-
    collapsed into a single paragraph. Where that enumeration is present it is
    the reviewer's OWN topic split, so it is used rather than invented. Where
    it isn't, sentences are grouped into short paragraphs instead of guessing
    at topics — a wrong split reads worse than no split.
    """
    text = " ".join((text or "").split())
    if not text:
        return "", []

    marks = [m for m in _ENUM_MARK_RE.finditer(text)]
    if len(marks) >= 2:
        preamble = text[:marks[0].start()].strip(" :;,-")
        points = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            point = text[m.end():end].strip(" ;,")
            if point:
                points.append(point)
        return preamble, points

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 2:
        return text, []
    # Two sentences per paragraph: enough to break the wall without severing
    # a point from the sentence that qualifies it.
    paras = [" ".join(sentences[i:i + 2]) for i in range(0, len(sentences), 2)]
    return "", paras


def _render_review_body(review, header, blurb):
    """Shared renderer for both advisory panels.

    Leads with the recommendation and the concerns behind it, then the
    per-reviewer detail. Highlighting is driven by each reviewer's actual
    verdict, never by keyword-sniffing their prose, so it cannot invent a
    concern that no reviewer raised or miss one phrased unusually.
    """
    if not review:
        return []
    if review.get("status"):
        return [header, "",
                "_Panel unavailable this pass (%s)._" % (review.get("reason") or review.get("status")),
                ""]

    verdict = str(review.get("verdict") or "—")
    approved = verdict == "Approve"
    conf_str = _norm_conf_pct(review.get("confidence")) or "n/a"
    blocks = _reviewer_blocks(review)
    dissenting = [b for b in blocks if b[1] and b[1] != "Approve"]
    known_verdicts = [b for b in blocks if b[1]]

    out = [
        header, "",
        blurb, "",
        "%s **Recommendation: %s** · panel confidence **%s**" % (
            "\U0001F7E2" if approved else "\U0001F534",     # 🟢 / 🔴
            "APPROVE" if approved else "DENY",
            conf_str),
        "",
    ]

    # ── concerns, up front ────────────────────────────────────────────────
    if dissenting:
        out += ["#### \u26A0\uFE0F Concerns (%d of %d reviewers did not approve)" % (
            len(dissenting), len(known_verdicts)), ""]
        for name, v_verdict, v_conf, crit in dissenting:
            gist = _structure_critique(crit)
            headline = (gist[1][0] if gist[1] else gist[0]) or "(no critique text returned)"
            if len(headline) > 240:
                headline = headline[:237].rstrip() + "…"
            pct = _norm_conf_pct(v_conf)
            out.append("- \U0001F534 **%s**%s — %s" % (
                name or "reviewer", " (%s)" % pct if pct else "", headline))
        out.append("")
    elif known_verdicts:
        out += ["#### \u2705 No objections", "",
                "All %d reviewer(s) approved. Detail below." % len(known_verdicts), ""]
    elif not approved:
        # Legacy panel results (and any producer that doesn't report per-
        # reviewer verdicts) still recommend DENY, so say what the concern is
        # rather than silently showing none — but don't attribute it to a
        # particular reviewer, which this shape genuinely cannot support.
        out += ["#### \u26A0\uFE0F Concerns", "",
                "The panel did not approve. Per-reviewer verdicts weren't recorded "
                "for this review, so the objections are in the detail below.", ""]

    # ── per-reviewer detail ───────────────────────────────────────────────
    if blocks:
        out += ["#### Reviewer detail", ""]
        for name, v_verdict, v_conf, crit in blocks:
            bits = []
            if v_verdict:
                bits.append("%s %s" % (
                    "\U0001F7E2" if v_verdict == "Approve" else "\U0001F534", v_verdict))
            pct = _norm_conf_pct(v_conf)
            if pct:
                bits.append("confidence %s" % pct)
            suffix = " — %s" % " · ".join(bits) if bits else ""
            out += ["**%s**%s" % (name or "Reviewer", suffix), ""]
            preamble, points = _structure_critique(crit)
            if preamble:
                out += [preamble, ""]
            for p in points:
                out.append("- %s" % p)
            if points:
                out.append("")
            if not preamble and not points:
                out += ["_No critique text returned._", ""]
    return out


def _render_state_panel(review):
    """Render the state-logic panel section (empty list when no review) — same
    shape as _render_panel, distinct header/framing so the two advisory panels
    are never confused for one combined verdict."""
    return _render_review_body(
        review, _STATE_PANEL_HEADER,
        "_Narrow-scope panel: state/status coverage + control-flow reachability "
        "ONLY (see pr_review.py `_state_logic_review` for what it does/doesn't "
        "check). Advisory — a human still approves/denies._")


def _render_panel(review):
    """Render the advisory skeptical-panel section (empty list when no review)."""
    return _render_review_body(
        review, _PANEL_HEADER,
        "_Cross-provider skeptical panel (same engine as the bot-fix reviewer) — "
        "an **advisory** confidence signal. A human still approves/denies; this "
        "bot never does._")


def _render_route(base_ref, head_ref, default_branch=""):
    """The "where is this PR going" line.

    GitHub shows base<-head at the top of the PR page, but this comment is also
    read in email notifications and the WebUI, where that context is absent —
    and in a dev -> qa -> main chain, which branch a PR targets is the single
    most important thing about it. Protected targets are called out explicitly
    so a PR aimed at main is never mistaken for a routine one.
    """
    base = str(base_ref or "").strip()
    if not base:
        return []
    head = str(head_ref or "").strip()
    protected = base.lower() in ("main", "master") or (
        default_branch and base == default_branch)
    line = "\U0001F500 **Merging into `%s`**" % base          # 🔀
    if head:
        line += " \u2190 `%s`" % head                          # ←
    if protected:
        line += "  \u2014 \u26A0\uFE0F **protected branch**"    # — ⚠️
    return [line, ""]


def _render(findings, head_sha, summary="", review=None, state_review=None,
            base_ref="", head_ref="", default_branch=""):
    lines = [
        PR_REVIEW_MARKER,
        "<!-- head: %s -->" % head_sha,
        "## \U0001F916 AppBuilder PR pre-review",
        "",
    ]
    lines += _render_route(base_ref, head_ref, default_branch)
    lines += [
        "_Automated pre-review — **informational only**. A human is the sole approver; "
        "this bot never approves, denies, or edits the branch._",
        "",
    ]
    if summary:
        lines += [_SUMMARY_HEADER, "", summary, ""]
    if not findings:
        lines += [
            "### Tier-1 checks",
            "",
            "✅ **Passed** — no dual-copy/cross-platform drift, no likely hardcoded "
            "credentials, no undefined names detected in the changed files, and no "
            "unattended-mutation-without-tests pattern.",
            "",
        ]
    else:
        lines += ["### Tier-1 findings (parity + secrets + undefined-names + unattended-mutation)", ""]
        for f in sorted(findings, key=lambda x: _LEVEL_ORDER.get(x["level"], 9)):
            icon = _LEVEL_ICON.get(f["level"], "•")
            lines += ["%s **%s — %s**" % (icon, f["level"].upper(), f["title"]), "", f["detail"], ""]
    lines += _render_panel(review)
    lines += _render_state_panel(state_review)
    return "\n".join(lines)


def _find_marker_comment(pr):
    for c in pr.get_issue_comments():
        if PR_REVIEW_MARKER in (c.body or ""):
            return c
    return None


_TWIN_MERGED_SCAN_LIMIT = 20   # bounded: newest-updated-first, so a real merge is near the front


def _twin_open_pr_touches(gh, twin_repo, twin_path, since=None):
    """Does an OPEN PR in `twin_repo` modify `twin_path` — OR did a RECENTLY
    MERGED one already land it? A merged twin PR means the drift is ALREADY
    resolved on the twin repo's default branch; only checking OPEN PRs meant
    the warning kept firing for hours after the twin PR merged (confirmed:
    cs#64 kept saying "twin NOT updated" for lm's sim-views.js long after
    lm#135 — the actual twin PR — merged and the fix was already on lm's
    main). ``since`` (the reviewed PR's own created_at, with a 1-day grace
    buffer) bounds the merged-PR scan to merges concurrent with or after this
    PR — an OLD merge touching the same path is unrelated history, not proof
    THIS drift was addressed, so it must not silently clear a real warning.

    Returns True/False, or None if the twin repo can't be reached (so the
    caller keeps the advisory rather than falsely clearing OR falsely
    warning)."""
    try:
        r = gh.get_repo(twin_repo)
    except Exception as e:  # noqa: BLE001
        logger.debug("pr_review twin-check: repo %s unreachable: %s", twin_repo, e)
        return None
    try:
        for tpr in r.get_pulls(state="open"):
            try:
                for f in tpr.get_files():
                    if f.filename == twin_path:
                        return True
            except Exception:
                continue
        # No open PR touches it — check recently MERGED ones too (bounded,
        # newest-updated-first) before concluding the twin is un-updated.
        if since is not None:
            # PyGithub's tz-awareness has varied across versions — normalize
            # both sides to naive UTC before comparing so this never raises
            # "can't compare offset-naive and offset-aware datetimes" and
            # silently kills the whole twin check via the outer except.
            since_naive = since.replace(tzinfo=None) if since.tzinfo else since
            cutoff = since_naive - timedelta(days=1)
            checked = 0
            for tpr in r.get_pulls(state="closed", sort="updated", direction="desc"):
                if checked >= _TWIN_MERGED_SCAN_LIMIT:
                    break
                checked += 1
                if not tpr.merged or not tpr.merged_at:
                    continue
                merged_at = tpr.merged_at.replace(tzinfo=None) if tpr.merged_at.tzinfo else tpr.merged_at
                if merged_at < cutoff:
                    continue
                try:
                    for f in tpr.get_files():
                        if f.filename == twin_path:
                            return True
                except Exception:
                    continue
    except Exception as e:  # noqa: BLE001
        logger.debug("pr_review twin-check: listing PRs for %s failed: %s", twin_repo, e)
        return None
    return False


def _twin_path_exists(gh, twin_repo, twin_path):
    """Does ``twin_path`` exist in ``twin_repo``? True / False / None (unknown).

    The dual-copy shapes are not file-for-file identical, so a twin advisory
    can name a path that simply has no counterpart. Escalating those to
    "twin NOT updated" produces a WARNING no PR can ever satisfy."""
    if not twin_repo or not twin_path:
        return None
    try:
        gh.get_repo(twin_repo).get_contents(twin_path)
        return True
    except Exception as e:  # noqa: BLE001
        if getattr(e, "status", None) == 404:
            return False
        logger.debug("pr_review twin-check: contents(%s:%s) failed: %s", twin_repo, twin_path, e)
        return None


def _resolve_cross_repo_twins(gh, findings, since=None):
    """Verify each cross-repo twin advisory against the OTHER repo.

    If a matching PR there already updates the twin file (open, OR merged
    since ``since``) → DROP the advisory (the pair IS in lockstep, no
    reminder needed). If NOT → escalate to a WARNING naming the file to
    update. If the twin repo can't be checked → leave the advisory as-is.
    Findings without a `twin` key pass through untouched; the key is stripped
    so the render/record layers never see it."""
    out = []
    exists_cache = {}
    for f in (findings or []):
        twin = (f or {}).get("twin")
        if not twin:
            out.append(f)
            continue
        f = dict(f)
        f.pop("twin", None)
        # A path with no counterpart cannot be "not updated in lockstep", and
        # scanning the twin repo's PRs for it would always come back False.
        # Say what is actually true instead, and keep it an advisory: for a
        # NEW shared file this is a genuine prompt to port it, and for a
        # standalone-only file it is a harmless note -- neither is a WARNING.
        key = (twin.get("repo", ""), twin.get("path", ""))
        if key not in exists_cache:
            exists_cache[key] = _twin_path_exists(gh, key[0], key[1])
        if exists_cache[key] is False:
            f["detail"] = ("`%s` does not exist in `%s`. Either this file is specific to "
                           "this shape of the module (the two copies are not file-for-file "
                           "identical), or it is new and still needs porting.\n\n"
                           % (key[1], key[0])) + f.get("detail", "")
            out.append(f)
            continue
        verdict = _twin_open_pr_touches(gh, twin.get("repo", ""), twin.get("path", ""), since=since)
        if verdict is True:
            # Twin is being updated in a matching PR — no warning needed.
            continue
        if verdict is False:
            f["level"] = "warning"
            f["title"] = "twin NOT updated — " + f.get("title", "")
            f["detail"] = ("No open PR in `%s` touches `%s`. Update the twin in lockstep "
                           "(dual-copy INVARIANT), or open the matching PR.\n\n"
                           % (twin.get("repo", "?"), twin.get("path", "?"))) + f.get("detail", "")
        # verdict is None → unreachable: keep the original advisory unchanged.
        out.append(f)
    return out


_FEATURE_DRIVE_MARKER_RE = re.compile(r"<!--\s*ab-feature-drive:\s*([^\s>]+)#(\d+)\s*-->")


def _automerge_decision(rec, changed_paths, config, pr_meta, state_flags=None, changed_files=None):
    """Pure function — the SINGLE choke point for whether the skeptical-review
    pipeline auto-approves + auto-merges a PR instead of waiting for a human.
    Returns (should_merge: bool, reason: str).

    THE INVARIANT THIS FUNCTION DELIBERATELY BREAKS: this module's own
    docstring (top of file) and routes.py's approve/merge routes say
    "AppBuilder never auto-approves"/"never auto-merges". This function is the
    deliberate exception.

    UPDATED 2026-08-29: containment used to be *who authored the PR* —
    pr_meta["is_feature_drive"] (the feature-drive marker) was what kept a
    human-authored PR structurally ineligible no matter how high its
    confidence. Now that more than one person works on this system, that's
    no longer sufficient on its own: pr_meta["base_ref"] must ALSO be in
    feature_automerge_target_branches (opt-in, defaults to empty — e.g.
    ["dev"]). A PR targeting main, or any branch not on that list, is
    blocked here regardless of author or confidence — combined with the
    feature_allowlist gate below (only small, additive diff shapes qualify
    unattended), this is what stands between "unattended merge" and "human
    required" for a human-authored PR now. is_feature_drive is still
    populated in pr_meta for logging/audit (which PRs were bot-authored vs
    human); it no longer gates this decision on its own.

    ALL conditions below are required; on ANY missing/ambiguous/unexpected
    input this returns (False, reason) — it must never accidentally return
    True. Every condition is independently gate-able (kill switches:
    feature_drive_enabled and feature_automerge_enabled; plus paused/
    blackout; plus a per-repo allowlist, a per-target-branch allowlist, and
    the feature_allowlist diff-shape gate).

    rec: state["pr_reviews"]["repo#num"] — panel_*/panel2_*/errors/warnings/
         merged/auto_merged.
    changed_paths: the PR's REAL changed-file list (pr.get_files() filenames)
                   — the boundary check is against the actual diff, never a
                   pre-build prediction.
    changed_files: normalised per-file records (see
                   feature_allowlist.files_from_pr_files) for the diff-shape
                   allowlist gate below; absent fails that gate closed.
    config: live config (feature_drive_enabled, feature_automerge_*,
            feature_boundaries).
    pr_meta: {"repo": str, "base_ref": str, "is_feature_drive": bool,
              "draft": bool, "state": "open"|"closed", "mergeable": bool|None}.
    state_flags: {"paused": bool, "blackout": bool}.
    """
    state_flags = state_flags or {}
    pr_meta = pr_meta or {}

    if not config.get("feature_drive_enabled", False):
        return False, "feature_drive_enabled is off"
    if not config.get("feature_automerge_enabled", False):
        return False, "feature_automerge_enabled is off"
    if pr_meta.get("repo") not in (config.get("feature_automerge_repos") or []):
        return False, "repo is not in feature_automerge_repos (opt-in, defaults to none)"
    # Structural refusal of the release branch, checked BEFORE the operator's
    # allowlist so configuration cannot override it. The allowlist is a free-text
    # field in Settings; both it and this function's docstring say "NEVER main",
    # but saying it is not enforcing it — typing "main" into that box was enough
    # to make an unattended merge onto the release branch eligible. Only the repo
    # owner merges into main, so that invariant lives in code, not in guidance.
    _base_ref = pr_meta.get("base_ref")
    _release_branches = {"main", "master", (config.get("default_branch") or "main")}
    if _base_ref in _release_branches:
        return False, (f"target branch {_base_ref!r} is a release branch — never eligible for "
                       "auto-merge at any confidence, even if listed in "
                       "feature_automerge_target_branches (merges into it are owner-only)")

    if _base_ref not in (config.get("feature_automerge_target_branches") or []):
        return False, ("target branch %r is not in feature_automerge_target_branches "
                        "(opt-in, defaults to none) — main (or any unlisted branch) is "
                        "never eligible, regardless of author or confidence" % _base_ref)

    if rec.get("merged") or rec.get("auto_merged"):
        return False, "already merged (idempotent no-op)"
    if pr_meta.get("draft"):
        return False, "PR is a draft"
    if (pr_meta.get("state") or "open") != "open":
        return False, "PR is not open"
    if pr_meta.get("mergeable") is not True:
        return False, "PR is not cleanly mergeable (conflicts / required checks / unknown)"
    if state_flags.get("paused"):
        return False, "AppBuilder is paused"
    if state_flags.get("blackout"):
        return False, "AppBuilder is in a blackout window"

    if rec.get("panel_status"):
        return False, "panel 1 (skeptical review) could not run"
    if rec.get("panel_verdict") != "Approve":
        return False, "panel 1 (skeptical review) did not Approve"
    if rec.get("panel2_status"):
        return False, "panel 2 (state-logic review) could not run"
    if rec.get("panel2_verdict") != "Approve":
        return False, "panel 2 (state-logic review) did not Approve"

    threshold = config.get("feature_automerge_min_confidence")
    try:
        threshold = float(threshold) if threshold is not None else 1.0
    except (TypeError, ValueError):
        threshold = 1.0
    threshold = max(0.0, min(1.0, threshold))

    conf1 = rec.get("panel_confidence")
    conf2 = rec.get("panel2_confidence")
    if conf1 is None or conf1 < threshold:
        return False, f"panel 1 confidence {conf1} is below the threshold {threshold:.2f}"
    if conf2 is None or conf2 < threshold:
        return False, f"panel 2 confidence {conf2} is below the threshold {threshold:.2f}"

    if config.get("feature_automerge_require_clean", True):
        if (rec.get("errors") or 0) > 0 or (rec.get("warnings") or 0) > 0:
            return False, "Tier-1 findings present (errors or warnings) — advisories alone are fine"

    hits = feature_boundary.boundary_hits(changed_paths or [], config.get("feature_boundaries") or [])
    if hits:
        ids = ", ".join(h.get("id", "?") for h in hits)
        return False, f"diff touches configured boundary path(s): {ids}"

    # DEFAULT-DENY allowlist (feature_allowlist) — the positive counterpart to
    # the boundary deny-list above. Even a fully-reviewed, boundary-clean, high-
    # confidence PR only auto-merges if its diff is one of the small, provably-
    # additive shapes the operator is willing to merge unattended (docs-only /
    # log-only / tooltip-only). Everything behaviour-changing routes to a human.
    # `changed_files` carries the per-file patch text this needs; when absent
    # (a caller that only has path strings) classify() fails closed. This gate
    # can only ever make auto-merge MORE restrictive.
    if config.get("feature_automerge_require_allowlist", True):
        verdict = feature_allowlist.classify(changed_files or [],
                                             config.get("feature_automerge_allowlist"))
        if not verdict.get("auto_approvable"):
            return False, "not on additive auto-approve allowlist: " + verdict.get("reason", "")

    score = min(conf1, conf2)
    return True, f"cleared: both panels Approve, min confidence {score:.2f} >= threshold {threshold:.2f}, no boundary touched"


def _maybe_auto_merge(gh, repo, pr, config):
    """Called immediately after record_pr_review inside _review_one, so it
    always sees a fresh record. Evaluates _automerge_decision and, if it
    clears, performs the SAME approve+merge actions a human clicking the
    buttons would — via pr_actions.approve_pr/merge_pr, the shared
    implementation, so the merge_pr's own "must be approved" guard is
    satisfied honestly rather than bypassed. Best-effort: any error here is
    logged and swallowed — a failed auto-merge attempt leaves the PR exactly
    where a normal reviewed-but-not-yet-approved PR would be, safe for a
    human to pick up."""
    try:
        key = "%s#%s" % (repo.full_name, pr.number)
        rec = (state.get("pr_reviews") or {}).get(key) or {}
        pr_files = list(pr.get_files())
        changed_paths = [f.filename for f in pr_files]
        changed_files = feature_allowlist.files_from_pr_files(pr_files)
        marker_match = _FEATURE_DRIVE_MARKER_RE.search(pr.body or "")
        pr_meta = {
            "repo": repo.full_name,
            "base_ref": getattr(getattr(pr, "base", None), "ref", None),
            "is_feature_drive": bool(marker_match),
            "draft": bool(getattr(pr, "draft", False)),
            "state": (pr.state or "open"),
            "mergeable": getattr(pr, "mergeable", None),
        }
        state_flags = {"paused": bool(state.get("paused")), "blackout": bool(state.get("blackout"))}
        should_merge, reason = _automerge_decision(rec, changed_paths, config, pr_meta, state_flags, changed_files)
        if not should_merge:
            logger.debug("pr_review: auto-merge skipped for %s (%s)", key, reason)
            return
        logger.info("pr_review: auto-merging %s — %s", key, reason)
        approve_pr(gh, repo.full_name, pr.number, actor="ab-auto")
        mark_pr_approved(repo.full_name, pr.number, True)
        update_pr_review(repo.full_name, pr.number, auto_merge_score=min(
            rec.get("panel_confidence") or 0.0, rec.get("panel2_confidence") or 0.0),
            auto_merge_reason=reason)
        status_code, result = merge_pr(gh, repo.full_name, pr.number)
        if status_code == 200 and result.get("status") == "success":
            update_pr_review(repo.full_name, pr.number, auto_merged=True)
            logger.info("pr_review: auto-merge succeeded for %s", key)
        else:
            logger.warning("pr_review: auto-merge's own merge_pr call did not succeed for %s: %s",
                           key, result)
    except Exception as e:
        logger.exception("pr_review: auto-merge attempt failed for %s#%s: %s", repo.full_name, pr.number, e)


def _review_one(gh, repo, pr, config, force=False):
    """force=True (the "Reprocess" button) bypasses the already-current cache
    check below and regenerates the comment + panel(s) even when the head SHA
    hasn't changed since the last scan — for when the underlying code (a repo
    twin, a route the panel couldn't see) changed without this PR's own head
    moving, or the operator just doesn't want to wait for the next poll cycle."""
    if getattr(pr, "draft", False):
        return  # skip WIP drafts
    # Skip AppBuilder's OWN AI-fix PRs — they were already vetted by the fix panel
    # when it opened them; pre-reviewing them is redundant and clutters the bot's
    # own fix backlog. Signals: title "AI Fix #N" and head branch "bug/..."
    # (fix_engine.create_pull, via branch_policy.auto_branch_name("bug", ...)).
    # AppBuilder commits under the operator's token, so author can't distinguish
    # it — use the title/branch signal.
    # DELIBERATELY does not match feature_build.py's PRs ("AI Feature #N" /
    # "ai-feature/...") — those have NOT been vetted by any panel yet (the
    # build agent has no reviewer), so they MUST fall through to a real
    # review here. Do not widen this prefix (e.g. to match both kinds) without
    # checking test_pr_review_own_pr_skip.py — a match here silently disables
    # review for the entire feature auto-drive pipeline, with no error anywhere.
    _title = pr.title or ""
    _head_ref = getattr(getattr(pr, "head", None), "ref", "") or ""
    if _title.startswith("AI Fix #") or _head_ref.startswith(AUTO_BRANCH_PREFIXES_BY_KIND["bug"]):
        logger.info("pr_review: skipping AppBuilder's own fix PR %s #%s", repo.full_name, pr.number)
        return
    head_sha = pr.head.sha
    # Compute findings every scan (cheap, deterministic, no LLM) so the UI
    # 'PRs Reviewed' list stays populated even after a restart. The COMMENT +
    # status are only (re)posted when the head SHA changed — dedup keeps us from
    # spamming, but we still RECORD the review below regardless.
    files = list(pr.get_files())
    changed = [f.filename for f in files]
    findings = check_parity(repo.full_name, changed)
    findings = _resolve_cross_repo_twins(gh, findings, since=getattr(pr, "created_at", None))
    findings += check_secrets(files)
    findings += find_missing_tooltips_in_files(files)
    findings += check_undefined_names(repo, files, head_sha)
    findings += check_unattended_mutation(files)
    existing = _find_marker_comment(pr)
    _prior_review = (state.get("pr_reviews") or {}).get("%s#%s" % (repo.full_name, pr.number)) or {}
    _prior_queued = is_queued_for_retry_stale(_prior_review, head_sha)
    already_current = ((not force) and not _prior_queued and bool(existing)
                       and ("<!-- head: %s -->" % head_sha) in (existing.body or ""))
    if _prior_queued and not force:
        logger.info("pr_review: %s PR #%s retrying — prior scan queued for retry (panel unavailable)",
                    repo.full_name, pr.number)
    if already_current:
        # Recover the previously-generated summary from the comment — no LLM call
        # on a cached re-scan / post-restart.
        summary = _extract_summary(existing.body if existing else "")
        action = "cached"
        # Recover the last persisted panel result(s) rather than leaving `review`/
        # `state_review` unset. NOTE: `review` used to be referenced UNCONDITIONALLY
        # below (in the record_pr_review call) but was only ever assigned in the
        # `else` branch — every cached re-scan (i.e. most polls, since a PR's head
        # rarely changes between cycles) raised NameError here, silently swallowed
        # by scan_open_prs' broad except. That meant record_pr_review never
        # actually ran on a cache hit despite the comment below claiming it always
        # persists — this recovery is the fix, not just a null-init, so a cache hit
        # re-persists the SAME panel verdict instead of erasing it to blank.
        _prior = (state.get("pr_reviews") or {}).get("%s#%s" % (repo.full_name, pr.number)) or {}
        review = ({"status": _prior["panel_status"]} if _prior.get("panel_status") else
                  {"verdict": _prior.get("panel_verdict"), "confidence": _prior.get("panel_confidence"),
                   "critique": _prior.get("panel_critique")}) if _prior.get("panel_verdict") or _prior.get("panel_status") else None
        # Same recovery for the second (state-logic) panel — without this, a
        # cached re-scan would call record_pr_review with review2=None and
        # silently wipe the panel2_* fields a real scan had just persisted.
        state_review = ({"status": _prior["panel2_status"]} if _prior.get("panel2_status") else
                        {"verdict": _prior.get("panel2_verdict"), "confidence": _prior.get("panel2_confidence"),
                         "critique": _prior.get("panel2_critique")}) if _prior.get("panel2_verdict") or _prior.get("panel2_status") else None
    else:
        # New/changed head: generate the plain-language change summary (LLM,
        # best-effort) and run the skeptical reviewer panel(s) (the unified
        # bug/feature confidence engine, advisory-only) — then (re)post the
        # comment + status. All per-head, not per-cycle, to bound LLM cost.
        #
        # Test-regression check goes here too (not in the unconditional block
        # above with the cheap static checks) — it actually clones + runs the
        # test suite, so it must only fire on a genuine head change, never on
        # every poll of an unchanged PR. OFF by default
        # (pr_test_regression_enabled); see check_test_regressions.py.
        token = config.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        findings += check_test_regressions(repo, pr, config, token)
        summary = _summarize_changes(pr, files, config, repo=repo)
        review = _skeptical_review(pr, files, config, repo=repo, head_sha=head_sha, gh=gh)
        state_review = _state_logic_review(pr, files, config, repo=repo, head_sha=head_sha)
        body = _render(findings, head_sha, summary, review=review, state_review=state_review,
                       base_ref=getattr(getattr(pr, "base", None), "ref", "") or "",
                       head_ref=_head_ref,
                       default_branch=getattr(repo, "default_branch", "") or "")
        if existing:
            existing.edit(body)
            action = "updated"
        else:
            pr.create_issue_comment(body)
            action = "created"
        # Informational, NON-blocking status. Always 'success' so it can never block
        # a merge (the human is the gate); the count rides the description.
        # Best-effort: needs statuses:write — if absent, the comment still posts.
        try:
            n = len(findings)
            desc = "no parity issues" if n == 0 else "%d parity finding(s) — see review comment" % n
            repo.get_commit(head_sha).create_status(
                state="success", context=STATUS_CONTEXT, description=desc[:140])
        except Exception as e:  # noqa: BLE001
            logger.info("pr_review: status check skipped (%s) — token likely lacks statuses:write", e)
    # Always persist for the UI 'PRs Reviewed' filter (survives restarts). The
    # panel result rides along so the advisory verdict/confidence shows in
    # AppBuilder's own PR list, not only in the GitHub comment. state_review (the
    # state-logic panel) is ALSO persisted now (panel2_* fields, app_state.py) —
    # feature auto-drive's auto-merge gate requires BOTH panels to clear, and
    # this fixes the pre-existing gap where that panel's verdict was invisible
    # in AppBuilder's own UI for every PR, not just feature-built ones.
    record_pr_review(repo.full_name, pr.number, pr.title, pr.html_url, findings, head_sha,
                     summary=summary, review=review, review2=state_review,
                     base_ref=getattr(getattr(pr, "base", None), "ref", "") or "",
                     head_ref=_head_ref)
    if action != "cached":
        logger.info("pr_review: %s PR #%s reviewed (%d findings, comment %s)",
                    repo.full_name, pr.number, len(findings), action)
    # Runs on EVERY scan (not just non-cached ones) — a PR whose panels only
    # just cleared the confidence bar via the LAST scan's record shouldn't
    # have to wait for its head to move again before being picked up. Inert
    # unless feature_automerge_enabled AND the repo is in
    # feature_automerge_repos AND the PR's target branch is in
    # feature_automerge_target_branches (see _automerge_decision's own
    # docstring for the full gate list) — a no-op read+return for every PR
    # targeting main or any other non-allowlisted branch.
    _maybe_auto_merge(gh, repo, pr, config)


def reprocess_one_pr(repo_full_name, number, config=None):
    """Entry point for the UI's per-PR "Reprocess" button (routes.py
    /api/pr-review/reprocess) — immediately re-runs the full pre-review for
    ONE PR, bypassing the head-SHA cache (see _review_one's force= param), so
    the operator doesn't have to wait for the next poll cycle. Raises on a
    genuine failure (bad token/repo/PR number) so the caller can surface it;
    a per-check internal error still degrades gracefully same as scan_open_prs
    (best-effort findings/panels), because it shares _review_one's own
    exception handling for those.

    Deliberately does NOT check pr_review_enabled — a manual reprocess is an
    explicit operator action, not the background poll, so it should work even
    if the operator hasn't (or doesn't want to) turn on the automatic scan.
    """
    config = config or load_config()
    token = config.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("No GitHub token configured")
    from github import Github
    gh = Github(token)
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(int(number))
    _review_one(gh, repo, pr, config, force=True)


def fix_one_pr(repo_full_name, number, config=None):
    """Entry point for the UI's per-PR "Fix" button (routes.py
    /api/pr-review/fix) — the ONLY way this ever runs; AppBuilder never applies a
    PR fix on its own. A human clicks Fix, and this:

      1. Recomputes this PR's Tier-1 findings (parity/secrets/lint) fresh, and
         folds in the persisted skeptical-panel critique, into a fix prompt.
      2. Clones the PR's OWN head branch (not a new branch) into a sandboxed
         temp checkout, mirroring process_single_issue's clone/token handling.
      3. Generates a fix via apply_ai_fix (targeted at exactly the PR's changed
         files — files_override, since a PR review has no "issue text" for the
         usual identifier-grep to anchor on) and parse_and_apply.
      4. Gates it through the SAME skeptical reviewer panel (review_fix) the
         bug/issue fix pipeline uses (builder_n=0: no builder to exclude, every
         configured provider reviews) — reject means no push, full stop.
      5. On approval (+ QA verify, if enabled), commits and pushes as a NEW
         commit onto the PR's EXISTING branch — never a new branch/PR — then
         triggers an immediate reprocess so the review comment/panel reflect
         the fix right away.

    Returns (True, message) on a pushed fix, (False, message) on any refusal/
    rejection (surfaced to the UI, not an error). Raises only on setup failure
    (bad token/repo/PR number), same contract as reprocess_one_pr.
    """
    import git
    import tempfile
    from github import Github
    from fix_engine import (
        _claim_issue, _release_issue, _authenticated_remote,
        apply_ai_fix, parse_and_apply, review_fix, verify_fix, prepare_environment,
    )

    config = config or load_config()
    token = config.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("No GitHub token configured")

    lock_id = "pr-fix:%s#%s" % (repo_full_name, number)
    if not _claim_issue(lock_id):
        return False, "A fix is already in progress for this PR."
    try:
        gh = Github(token)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(int(number))
        if pr.merged:
            return False, "PR #%s is already merged — nothing to fix." % number
        if (pr.state or "").lower() == "closed":
            return False, "PR #%s is closed — nothing to fix." % number

        head_sha = pr.head.sha
        branch = pr.head.ref
        files = list(pr.get_files())
        changed = [f.filename for f in files]

        findings = check_parity(repo_full_name, changed)
        findings = _resolve_cross_repo_twins(gh, findings, since=getattr(pr, "created_at", None))
        findings += check_secrets(files)
        findings += find_missing_tooltips_in_files(files)
        findings += check_undefined_names(repo, files, head_sha)

        rec = (state.get("pr_reviews") or {}).get("%s#%s" % (repo_full_name, number)) or {}
        panel_critique = (rec.get("panel_critique") or "").strip()

        # Self-improvement: also read the pre-review AB actually POSTED on this PR
        # and fold every below-target panel's dissent into the fix prompt. This
        # survives a restart (where in-memory `state` lost panel_critique) and
        # targets exactly the panels that lowered the score. See pr_review_score.
        score_feedback = ""
        try:
            import pr_review_score
            _report, _body = pr_review_score.read_pr_review(pr)
            score_feedback = pr_review_score.dissent_feedback(_report, _body).strip()
        except Exception as e:  # noqa: BLE001
            logger.debug("fix_one_pr: pre-review score read skipped: %s", e)

        if not findings and not panel_critique and not score_feedback:
            return False, "No findings to fix — this PR has a clean pre-review."

        lines = ["PR #%s: %s" % (pr.number, pr.title or "")]
        if pr.body:
            lines.append((pr.body or "").strip()[:2000])
        if findings:
            lines.append("\nAppBuilder pre-review findings to fix:")
            for f in findings:
                lines.append("- [%s] %s: %s" % (
                    (f.get("level") or "advisory").upper(), f.get("title") or "", f.get("detail") or ""))
        if panel_critique:
            lines.append("\nSkeptical reviewer critique (from the last panel pass):\n" + panel_critique)
        if score_feedback:
            lines.append("\n" + score_feedback)
        fix_body = "\n".join(lines)

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "repo")
            url = repo.clone_url.replace("https://", "https://%s@" % token)
            repo_git = git.Repo.clone_from(url, path)
            repo_git.remotes.origin.set_url(repo.clone_url)
            try:
                repo_git.git.checkout(branch)
            except Exception as e:
                return False, "Could not check out PR branch %s: %s" % (branch, e)

            try:
                fix_code = apply_ai_fix(path, fix_body, files_override=changed, task_id=lock_id)
            except Exception as e:
                return False, "Fix generation failed: %s" % e
            success_applied, fixes, confidence = parse_and_apply(fix_code, path)
            if not success_applied:
                return False, "AI generated invalid JSON format for the fix."

            review = review_fix(path, fix_body, fixes, task_id=lock_id, builder_n=0, repo=repo, head_sha=head_sha)
            if isinstance(review, dict) and review.get("status") == "queue_for_retry":
                _q_reason = review.get("reason") or "reviewers unavailable"
                return False, f"Reviewer panel could not run ({_q_reason}) — click Fix again shortly."
            review_conf = review.get("confidence", 0.0) if isinstance(review, dict) else 0.0
            review_verdict = review.get("verdict", "Reject") if isinstance(review, dict) else "Reject"
            critique = review.get("critique", "") if isinstance(review, dict) else ""
            if review_verdict != "Approve":
                try:
                    pr.create_issue_comment(
                        "\U0001F916 **AppBuilder — Fix attempt rejected**\n\nGenerated a fix for the "
                        "findings above, but the skeptical reviewer panel rejected it (not pushed):"
                        "\n\n%s" % (critique or "no critique given"))
                except Exception:  # noqa: BLE001
                    pass
                return False, "Reviewer panel rejected the generated fix: %s" % critique

            if config.get("qa_enabled", True):
                try:
                    prepare_environment(path)
                    verified, failure_msg = verify_fix(path, repo_full_name, config)
                except Exception as e:  # noqa: BLE001
                    verified, failure_msg = False, str(e)
                if not verified:
                    try:
                        pr.create_issue_comment(
                            "\U0001F916 **AppBuilder — Fix attempt failed verification**\n\nA fix was "
                            "generated and approved by the reviewer panel, but failed verification "
                            "(not pushed):\n\n%s" % (failure_msg or "unknown failure"))
                    except Exception:  # noqa: BLE001
                        pass
                    return False, "Fix failed verification: %s" % failure_msg

            final_confidence = (confidence + review_conf) / 2
            files_list = ", ".join(fixes.keys())
            commit_msg = "AppBuilder: fix PR #%s review findings" % pr.number
            repo_git.git.add(A=True)
            repo_git.index.commit(commit_msg)
            with _authenticated_remote(repo_git.remotes.origin, repo.clone_url, token):
                repo_git.remotes.origin.push("HEAD:%s" % branch)

            try:
                pr.create_issue_comment(
                    "\U0001F916 **AppBuilder — Fix applied**\n\nPushed a fix commit for the findings "
                    "above onto this PR's branch (avg confidence %.0f%%).\n\n**Files:** `%s`"
                    % (final_confidence * 100, files_list))
            except Exception:  # noqa: BLE001
                pass

            try:
                _review_one(gh, repo, repo.get_pull(int(number)), config, force=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("fix_one_pr: post-fix reprocess failed for %s#%s: %s", repo_full_name, number, e)

            return True, "Fix pushed to %s (%s)" % (branch, files_list)
    finally:
        _release_issue(lock_id)


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
    seen_open = set()
    try:
        for repo_name in repos:
            try:
                repo = gh.get_repo(repo_name)
                for pr in repo.get_pulls(state="open"):
                    try:
                        _review_one(gh, repo, pr, config)
                        seen_open.add("%s#%s" % (repo.full_name, pr.number))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("pr_review: PR #%s in %s failed: %s",
                                       getattr(pr, "number", "?"), repo_name, e)
            except Exception as e:  # noqa: BLE001
                logger.warning("pr_review: repo %s failed: %s", repo_name, e)
        # Backfill the target-branch badge on older records (see below), then
        # self-heal listed PRs that are no longer open (merged elsewhere or closed).
        _backfill_pr_review_refs(gh)
        _reconcile_closed_prs(gh, seen_open)
    finally:
        update_task_state("PRReview", action="end")


# Records written before base_ref/head_ref existed have neither, so the PR list
# renders no target-branch badge for them — in a dev -> qa -> main workflow that
# is the single most useful fact about a PR, and it was missing from the large
# majority of rows (67 of 72 on the production host). New reviews store the refs;
# this fills in the history so the column is uniformly populated.
#
# Deliberately bounded per scan cycle: a full backfill is one GET per affected
# record, and the scan cycle also has real review work to do. The work is
# idempotent and self-terminating — each cycle re-computes what is still missing
# and stops costing anything once nothing is (an in-memory dict scan).
_REFS_BACKFILL_PER_CYCLE = 25


def _backfill_pr_review_refs(gh, limit=_REFS_BACKFILL_PER_CYCLE):
    """Fill in base_ref/head_ref on reviewed-PR records that predate those
    fields. Never raises out — a backfill is cosmetic and must not break a scan.

    Returns the number of records updated (for tests/logging)."""
    try:
        reviews = dict(state.get("pr_reviews") or {})
    except Exception:  # noqa: BLE001
        return 0
    pending = [(k, r) for k, r in reviews.items()
               if r and not r.get("base_ref") and r.get("repo") and r.get("number")]
    if not pending:
        return 0
    filled = 0
    for _key, rec in pending[:max(0, int(limit or 0))]:
        repo_name, number = rec.get("repo"), rec.get("number")
        try:
            pr = gh.get_repo(repo_name).get_pull(int(number))
            base_ref = getattr(getattr(pr, "base", None), "ref", "") or ""
            head_ref = getattr(getattr(pr, "head", None), "ref", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("pr_review backfill: %s#%s fetch failed: %s", repo_name, number, e)
            continue
        if not base_ref:
            continue
        if update_pr_review(repo_name, number,
                            base_ref=str(base_ref)[:120], head_ref=str(head_ref)[:120]):
            filled += 1
    if filled:
        logger.info("pr_review: backfilled target branch on %d record(s); %d still missing",
                    filled, max(0, len(pending) - filled))
    return filled


def _reconcile_closed_prs(gh, seen_open):
    """Refresh the PRs-Reviewed list against GitHub so it never shows stale
    Approve/Merge buttons on a PR that is no longer open.

    Only non-terminal records that were NOT seen open in this scan are checked
    (so it costs one extra GET per genuinely-closed listed PR, not per open PR).
    A PR merged since we last saw it → MERGED; closed without a merge (e.g. we
    superseded it with another PR) → CLOSED. Reopened PRs get picked up as open
    again on the normal review pass, which rebuilds a fresh (non-terminal) record."""
    try:
        reviews = dict(state.get("pr_reviews") or {})
    except Exception:
        return
    for key, rec in reviews.items():
        if not rec or key in seen_open:
            continue
        if rec.get("merged") or rec.get("denied") or rec.get("closed"):
            continue  # already terminal in the UI
        repo_name, number = rec.get("repo"), rec.get("number")
        if not repo_name or not number:
            continue
        try:
            pr = gh.get_repo(repo_name).get_pull(int(number))
        except Exception as e:  # noqa: BLE001
            logger.debug("pr_review reconcile: %s#%s fetch failed: %s", repo_name, number, e)
            continue
        try:
            if pr.merged:
                update_pr_review(repo_name, number, merged=True)
                logger.info("pr_review reconcile: %s #%s is merged → MERGED", repo_name, number)
            elif (pr.state or "").lower() == "closed":
                update_pr_review(repo_name, number, closed=True)
                logger.info("pr_review reconcile: %s #%s is closed (unmerged) → CLOSED", repo_name, number)
        except Exception as e:  # noqa: BLE001
            logger.debug("pr_review reconcile: %s#%s update failed: %s", repo_name, number, e)
