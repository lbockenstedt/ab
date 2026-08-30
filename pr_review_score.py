#!/usr/bin/env python3
"""Read, score, and act on AppBuilder's own PR pre-review comment.

AppBuilder posts an automated skeptical-panel pre-review on every PR (see
pr_review.py, marker ``<!-- ab-pr-review -->``). Each advisory panel — the
general "Skeptical review" and the narrow "State-logic / control-flow review" —
reports a ``panel confidence`` and per-reviewer verdicts. The composite score a
human reads is only as good as the WEAKEST panel, and the actionable signal is
the set of reviewers that did NOT approve.

This module is the repeatable driver for the "improve the review score" loop. It
is used two ways:

1. **Programmatically, so AppBuilder can improve ITSELF.** `read_pr_review(pr)`
   parses the pre-review AB posted on its own PR, and `dissent_feedback(...)`
   turns every below-target panel and dissenting reviewer into concise, actionable
   retry guidance. `pr_review.fix_one_pr` folds that back into the next fix
   attempt's prompt, so a low score on AB's own PR drives AB's own next revision.

2. **From the CLI, for a human running the loop by hand** — see `main()` /
   ``python3 pr_review_score.py 152``: report every panel's confidence and every
   dissenting concern so the next change targets exactly what lowered the score.

CLI usage:
    python3 pr_review_score.py 152                 # report latest review on PR 152
    python3 pr_review_score.py 152 --json          # machine-readable
    python3 pr_review_score.py 152 --watch         # wait for a review of the CURRENT head, then report
    python3 pr_review_score.py 152 --min 90        # exit non-zero if any panel < 90%

CLI exit code is 0 when every panel is >= --min (default 90) AND no reviewer
dissents; 1 otherwise (2 on operational error) — so it slots into a CI gate or a
scripted iterate-until-green loop. The CLI depends only on the `gh` CLI (already
required by this repo); the programmatic API takes a PyGithub PR and needs no gh.
"""
import argparse
import json
import logging
import re
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

DEFAULT_REPO = "lbockenstedt/ab"
# Mirrors pr_review.PR_REVIEW_MARKER — resolved lazily via _marker() to stay in
# lockstep without a top-level import cycle (pr_review imports this module).
_MARKER_FALLBACK = "<!-- ab-pr-review -->"

# Anchors kept in sync with pr_review.py's renderer (_render_* / _PANEL_HEADER).
_HEAD_RE = re.compile(r"<!--\s*head:\s*([0-9a-f]{7,40})\s*-->")
_PANEL_HEADER_RE = re.compile(r"^###\s+.*review \(panel\)\s*$", re.MULTILINE)
_RECO_RE = re.compile(
    r"\*\*Recommendation:\s*(?P<verdict>[A-Z]+)\*\*\s*·\s*panel confidence\s*\*\*(?P<pct>\d+)%\*\*")
_REVIEWER_RE = re.compile(
    r"\*\*(?P<name>[^*]+?)\*\*\s*—\s*\S+\s*(?P<verdict>Approve|Reject|Deny)\s*·\s*confidence\s*(?P<pct>\d+)%",
    re.IGNORECASE)


def _marker():
    """The pre-review marker, sourced from pr_review when importable."""
    try:
        from pr_review import PR_REVIEW_MARKER
        return PR_REVIEW_MARKER
    except Exception:  # noqa: BLE001 — CLI/standalone use without the full app
        return _MARKER_FALLBACK


def _gh_json(repo, pr, fields):
    """Return `gh pr view` JSON for the given fields, or raise RuntimeError."""
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo, "--json", fields],
            capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise RuntimeError("gh CLI not found on PATH")
    except subprocess.CalledProcessError as e:
        raise RuntimeError((e.stderr or e.stdout or "gh pr view failed").strip())
    return json.loads(out)


def _latest_review_comment(comments):
    """The most recent AppBuilder pre-review comment body, or None.

    `comments` is an iterable of objects/dicts exposing a comment body — either
    `gh` JSON dicts ({"body": ...}) or PyGithub IssueComment objects (.body).
    """
    marker = _marker()
    hits = []
    for c in comments:
        body = c.get("body") if isinstance(c, dict) else getattr(c, "body", None)
        if body and marker in body:
            hits.append(body)
    # oldest-first from both gh and PyGithub; the last marker comment is current.
    return hits[-1] if hits else None


def _section_bounds(body):
    """Yield (title_line, start, end) for each panel section in the comment."""
    starts = [m.start() for m in _PANEL_HEADER_RE.finditer(body)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        title = body[s:body.find("\n", s) if body.find("\n", s) != -1 else len(body)].strip("# ").strip()
        yield title, s, end


def parse_review(body):
    """Parse a pre-review comment body into a structured score report."""
    head = None
    m = _HEAD_RE.search(body)
    if m:
        head = m.group(1)

    panels = []
    for title, s, e in _section_bounds(body):
        seg = body[s:e]
        reco = _RECO_RE.search(seg)
        reviewers = []
        for rm in _REVIEWER_RE.finditer(seg):
            verdict = rm.group("verdict").title()
            reviewers.append({
                "name": rm.group("name").strip(),
                "verdict": "Reject" if verdict in ("Reject", "Deny") else "Approve",
                "confidence": int(rm.group("pct")),
            })
        panels.append({
            "panel": title,
            "verdict": (reco.group("verdict") if reco else None),
            "confidence": (int(reco.group("pct")) if reco else None),
            "reviewers": reviewers,
            "dissenting": [r for r in reviewers if r["verdict"] != "Approve"],
        })

    scored = [p["confidence"] for p in panels if p["confidence"] is not None]
    return {
        "head": head,
        "panels": panels,
        "composite": (min(scored) if scored else None),
        "any_dissent": any(p["dissenting"] for p in panels),
    }


def _concern_snippets(body, panel_title):
    """Pull the dissenting-reviewer critique bullets for a panel (for humans)."""
    for title, s, e in _section_bounds(body):
        if title != panel_title:
            continue
        seg = body[s:e]
        # Everything under a "Reviewer (…) — 🔴 Reject" block, first ~600 chars.
        out = []
        for rm in _REVIEWER_RE.finditer(seg):
            if rm.group("verdict").title() in ("Reject", "Deny"):
                tail = seg[rm.end():]
                nxt = _REVIEWER_RE.search(tail)
                block = tail[:nxt.start()] if nxt else tail
                block = block.strip().strip("-").strip()
                if block:
                    out.append(block[:600].rstrip())
        return out
    return []


# ── programmatic API: let AppBuilder read + act on its own review ──────────────

def read_pr_review(pr):
    """Parse the pre-review AppBuilder posted on a PyGithub PR.

    Returns (report, body) where report is the parse_review() dict and body is
    the raw comment markdown (needed by dissent_feedback for the critique text),
    or (None, None) when AB has not posted a pre-review on this PR yet.
    """
    try:
        comments = list(pr.get_issue_comments())
    except Exception as e:  # noqa: BLE001
        logger.debug("read_pr_review: could not list comments: %s", e)
        return None, None
    body = _latest_review_comment(comments)
    if not body:
        return None, None
    return parse_review(body), body


def dissent_feedback(report, body, min_pct=90):
    """Turn a parsed review into actionable retry guidance for the fix engine.

    Emits one bullet per panel that fell below `min_pct` OR carried a dissenting
    reviewer, quoting that reviewer's concern. This is what closes AppBuilder's
    self-improvement loop: fed back into the next fix attempt's prompt (see
    pr_review.fix_one_pr), it points the builder at exactly what lowered the
    score. Returns "" when every panel is at/above target with no dissent.
    """
    if not report:
        return ""
    lines = []
    for p in report.get("panels", []):
        conf = p.get("confidence")
        below = conf is not None and conf < min_pct
        if not below and not p.get("dissenting"):
            continue
        head = "%s — panel confidence %s%s" % (
            p.get("panel") or "panel",
            ("%d%%" % conf) if conf is not None else "n/a",
            " (below %d%% target)" % min_pct if below else "")
        lines.append("- " + head)
        for snip in (_concern_snippets(body, p.get("panel")) if body else []):
            first = " ".join(snip.split())
            lines.append("    • " + first[:500])
    if not lines:
        return ""
    return ("The AppBuilder pre-review scored below target. Address these panel "
            "concerns so the next revision raises the score:\n" + "\n".join(lines))


def render_human(report, body, min_pct):
    lines = []
    comp = report["composite"]
    comp_s = f"{comp}%" if comp is not None else "n/a"
    head_s = (report["head"] or "?")[:12]
    lines.append(f"Composite (weakest panel): {comp_s}   head={head_s}")
    lines.append("")
    for p in report["panels"]:
        conf = f"{p['confidence']}%" if p["confidence"] is not None else "n/a"
        flag = ""
        if p["confidence"] is not None and p["confidence"] < min_pct:
            flag = "  ⬇ below threshold"
        elif p["dissenting"]:
            flag = "  ⚠ dissent"
        lines.append(f"• {p['panel']}: {conf} ({p['verdict'] or '?'}){flag}")
        for r in p["reviewers"]:
            mark = "✓" if r["verdict"] == "Approve" else "✗"
            lines.append(f"    {mark} {r['name']}: {r['verdict']} {r['confidence']}%")
        for snip in _concern_snippets(body, p["panel"]):
            first = snip.splitlines()[0]
            lines.append(f"      ↳ {first[:200]}")
    return "\n".join(lines)


def fetch_report(repo, pr, want_head=None, watch=False, timeout=600, interval=20):
    """Fetch (optionally waiting for a review of `want_head`) and parse the review."""
    deadline = time.time() + timeout
    while True:
        data = _gh_json(repo, pr, "comments,headRefOid")
        head_oid = data.get("headRefOid")
        target = want_head or head_oid
        body = _latest_review_comment(data.get("comments") or [])
        if body:
            report = parse_review(body)
            fresh = (not watch) or (report.get("head") and target
                                    and report["head"].startswith(target[:len(report["head"])]) or
                                    (target and report.get("head") and target.startswith(report["head"])))
            if fresh:
                return report, body, head_oid
        if not watch or time.time() >= deadline:
            if body:
                return parse_review(body), body, head_oid
            return None, None, head_oid
        print(f"… waiting for AppBuilder review of {(target or '')[:12]} "
              f"({int(deadline - time.time())}s left)", file=sys.stderr)
        time.sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pr", help="PR number")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    ap.add_argument("--min", type=int, default=90, help="min per-panel confidence to pass (default 90)")
    ap.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    ap.add_argument("--watch", action="store_true",
                    help="wait for a review of the PR's CURRENT head, then report")
    ap.add_argument("--timeout", type=int, default=600, help="--watch timeout seconds (default 600)")
    args = ap.parse_args(argv)

    try:
        report, body, head_oid = fetch_report(
            args.repo, args.pr, watch=args.watch, timeout=args.timeout)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if report is None:
        print(f"error: no AppBuilder pre-review comment found on {args.repo}#{args.pr}",
              file=sys.stderr)
        return 2

    stale = bool(head_oid and report.get("head")
                 and not (head_oid.startswith(report["head"]) or report["head"].startswith(head_oid)))

    if args.as_json:
        print(json.dumps({**report, "repo": args.repo, "pr": args.pr,
                          "current_head": head_oid, "stale": stale, "min": args.min}, indent=2))
    else:
        print(render_human(report, body, args.min))
        if stale:
            print(f"\n⚠ review is for {report['head'][:12]}, PR head is now "
                  f"{(head_oid or '')[:12]} — push landed, re-review pending.")

    comp = report["composite"]
    passed = (comp is not None and comp >= args.min
              and all((p["confidence"] or 0) >= args.min for p in report["panels"])
              and not report["any_dissent"])
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
