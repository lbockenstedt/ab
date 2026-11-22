"""Build AB's PR-review prompts for a local change, so a review before
`git push` asks exactly what AB asks after it.

Every piece comes from AB's own code, so changing AB's review changes this
too: the panel briefs (pr_review.skeptical_issue_body and
state_logic_issue_body), the reviewer prompt with the fleet rules
(fix_engine.reviewer_prompt), the diff budget (pr_review._pr_diff_text), the
specialist agents (skills_loader) and the reviewer models (read from AB's
latest review comment). Used by the fleet `review` skill
(lm/.claude/skills/review).

Usage:
    python scripts/local_review.py <repo path> [--base origin/dev] [--out DIR]
        [--title TEXT] [--body-file FILE]

Reviews everything on the branch since its merge-base with --base, plus
uncommitted and untracked files. Writes panel1.txt, panel2.txt and
reviewers.json (each panel's seats) to --out, creates an empty --out/votes (dropping the last round's replies), and prints
a JSON summary whose `reviewers` list one entry per panel seat: its model,
the prompt to give it and the file its reply goes in. Exit
codes: 0 done; 3 no change since --base (nothing to review); 4 cannot tell
which models AB reviews with (writes nothing); anything else is a broken
environment or bad arguments (see stderr).

Gate:
    python scripts/local_review.py --gate DIR

DIR is the --out folder. Applies AB's panel verdict (fix_engine.panel_verdict)
to the replies saved for each panel's seats in DIR/reviewers.json
(DIR/votes/<panel>-<seat>.txt). Prints each panel's verdict and exits 0
only when both panels Approve. On a clean tree with both Approving, it also writes
DIR/attestation.md: a marker AB reads from the PR body to skip its paid re-review of that exact head
and description (DIR/pr-title.txt and DIR/pr-body.md are the PR to open).

Attest (the review skill's reviewer ladder):
    python scripts/local_review.py --attest DIR --frontier MODEL

The ladder decides each panel on one frontier reviewer, whose reply is saved
in DIR/<panel>-<MODEL>.out. Writes the same attestation when both panels'
replies Approve at ATTEST_CONFIDENCE or more on a clean tree, and exits 0;
otherwise removes any old one and exits 1.
"""
import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import quote

# Importing AB's main starts its workers (the updater hard-resets the checkout).
os.environ.setdefault("AB_DISABLE_WORKERS", "1")
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the ab checkout
sys.path.insert(0, _HERE)

_REVIEWER_RE = re.compile(r"\*\*Reviewer \(([^)\s]+)\)\*\*")

# Owner-approved migration: AB's old endpoint is disabled, but historical review
# comments still name it. This is not an unavailable-model fallback.
_REVIEWER_SEAT_UPGRADES = {"copilot/claude-opus-5": "copilot/claude-opus-5.5"}


def _git(repo, *args):
    """Run git in ``repo`` and return its stdout."""
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout


def _slug(repo):
    """owner/name of a checkout's origin remote (ssh or https form)."""
    url = re.sub(r"(\.git)?\s*$", "", _git(repo, "remote", "get-url", "origin"))
    return "/".join(url.replace(":", "/").split("/")[-2:])


def _changed_files(repo, base):
    """The change since ``base`` as PR-style files: [{filename, patch}]."""
    files = []
    # NUL-separated so a path with a space stays one path.
    for name in _git(repo, "diff", "-z", "--name-only", base).split("\0"):
        if not name:
            continue
        files.append(SimpleNamespace(filename=name, patch=_git(repo, "diff", base, "--", name)))
    for name in _git(repo, "ls-files", "-z", "--others", "--exclude-standard").split("\0"):
        if not name:
            continue
        new = subprocess.run(["git", "-C", repo, "diff", "--no-index", "/dev/null", name],
                             capture_output=True, text=True).stdout
        files.append(SimpleNamespace(filename=name, patch=new))
    return files


def _gh_json(*args):
    """Run `gh` and parse its JSON output; None on any failure."""
    try:
        out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return None


def _reviewer_seats(repo_slug):
    """AB's seats for each panel on its latest review of that panel, e.g.
    {"panel1": ["copilot/claude-opus-5.5", ...], "panel2": [...]}.

    AB picks each panel's seats separately (each runs its own review_fix), so
    each panel's seats come from its own section of AB's review comments: the
    most recently UPDATED comment (AB edits its review in place) that names
    seats for that panel, in the target repo or AB's own repo, which always
    carries reviews. Panel 2 is skipped until panel 1 Approves, so its newest
    seats are often on an older comment than panel 1's. Only when no comment
    names panel 2 seats does panel 2 use panel 1's: AB seats both panels with
    the same review_fix call and arguments (pr_review._skeptical_review and
    _state_logic_review), so they get the same seats. A seat is the full provider/model label, so two seats running
    the same model through different providers stay two seats, as in AB.

    Only comments by authors with write access count (is_trusted_comment).

    Raises RuntimeError when GitHub cannot be read or no AB review names a
    panel 1 model, so a caller never mistakes "could not find out" for "no
    reviewers"."""
    import pr_review
    from pr_review_score import is_trusted_comment
    newest = {}  # panel -> (updated_at, seats)
    for slug in dict.fromkeys((repo_slug, _slug(_HERE))):
        comments = _gh_json("api", "repos/%s/issues/comments?sort=updated&direction=desc"
                            "&per_page=100" % slug)
        if comments is None:
            raise RuntimeError("could not read %s's PR comments with gh" % slug)
        for c in comments:
            # Anyone can comment on a public repo; a stranger's copied marker
            # must not pick the local review's (weaker) reviewer models.
            if (pr_review.PR_REVIEW_MARKER not in (c.get("body") or "")
                    or not is_trusted_comment(c)):
                continue
            skeptical, _, state = (c.get("body") or "").partition(pr_review._STATE_PANEL_HEADER)
            for panel, text in (("panel1", skeptical), ("panel2", state)):
                seats = _seats_in(text)
                when = c.get("updated_at", "")
                if seats and (panel not in newest or when > newest[panel][0]):
                    newest[panel] = (when, seats)
    if "panel1" not in newest:
        raise RuntimeError("no AB review comment naming its reviewer models was found")
    return {"panel1": newest["panel1"][1], "panel2": newest.get("panel2", newest["panel1"])[1]}


def _seats_in(text):
    """The distinct seats named in one panel's section; a seat label without
    "/model" names no model and is skipped.

    Matches are mapped through _REVIEWER_SEAT_UPGRADES before deduplication,
    so an old Opus 5 seat becomes Opus 5.5 (owner-approved migration)."""
    return list(
        dict.fromkeys(
            _REVIEWER_SEAT_UPGRADES.get(m, m) for m in _REVIEWER_RE.findall(text) if "/" in m
        )
    )


def _fresh_votes(folder):
    """Start a round with an empty votes folder: a reply left from an earlier
    round would otherwise be gated as a vote on this change."""
    votes = os.path.join(folder, "votes")
    shutil.rmtree(votes, ignore_errors=True)
    os.makedirs(votes)
    # Frontier replies for --attest are per round too: an old Approve must never
    # attest a new head when the folder is reused.
    for name in os.listdir(folder):
        if name.startswith(("panel1-", "panel2-")) and name.endswith(".out"):
            os.remove(os.path.join(folder, name))


def vote_file(folder, panel, seat):
    """Where the reply of ``seat`` on ``panel`` is saved: one flat file per
    seat. Percent-encoding keeps a slash out of the name and never gives two
    seats the same file."""
    return os.path.join(folder, "votes", "%s-%s.txt" % (panel, quote(seat, safe="")))


def _commits(repo, base):
    """The branch's commits since ``base`` with their parents, as AB reads them
    from GitHub (the same base..head range), so AB's merge-only context applies
    to a local merge too. Uncommitted edits are reviewed but are in no commit,
    so they count as one authored commit: a merge with hand edits on top is not
    merge-only."""
    commits = [SimpleNamespace(sha=line.split()[0], parents=line.split()[1:])
               for line in _git(repo, "rev-list", "--parents", "%s..HEAD" % base).splitlines()]
    if _git(repo, "status", "--porcelain"):
        commits.append(SimpleNamespace(sha="working-tree", parents=["HEAD"]))
    return commits


def _agents_context(text):
    """AB's specialist-agent checklists for this change (same loader, same source).

    Returns (context, error): error is None when the checklists loaded, even if
    none matched, and the reason otherwise."""
    try:
        from github import Github
        import skills_loader
        token = subprocess.run(["gh", "auth", "token"], check=True,
                               capture_output=True, text=True).stdout.strip()
        loaded = skills_loader.load_skills(Github(token), {})
        # The loader swallows a failed agents listing, so check agents arrived.
        if not any(s.get("kind") == "agent" for s in loaded.values()):
            return "", "no specialist agents loaded from lm"
        return skills_loader.agents_context(text), None
    except Exception as e:  # noqa: BLE001
        return "", str(e) or type(e).__name__


def _read_json(path):
    """The JSON object in ``path``, or {} when the file is absent."""
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


# A frontier reviewer's Approve passes a panel only at AB's own bar (fleet rule 10.3).
ATTEST_CONFIDENCE = 0.90


def _write_attestation(folder, panels):
    """Write the attestation for ``panels`` ({panel: (confidence, seats)} for
    both panels, or None when they did not both Approve) on a clean tree, so AB
    can take this review for the PR instead of re-running its panels
    (pr_review._local_review).

    DIR/attestation.md holds the marker alone; DIR/pr-title.txt and
    DIR/pr-body.md hold the reviewed title and body with the marker appended,
    for ``gh pr create --title "$(cat pr-title.txt)" --body-file pr-body.md``.
    The marker binds the head SHA, the target branch and merge base, and the
    title/body digest, so the PR must be opened with exactly those. A dirty tree, a failed gate or a missing
    meta.json removes any files left by an earlier round."""
    paths = [os.path.join(folder, n) for n in ("attestation.md", "pr-title.txt", "pr-body.md")]
    meta = _read_json(os.path.join(folder, "meta.json"))
    bound = all(meta.get(k) for k in ("head", "desc", "base", "merge_base"))
    if not (panels and bound and meta.get("dirty") is False):
        for path in paths:
            if os.path.exists(path):
                os.remove(path)
        return
    att = {k: meta[k] for k in ("head", "base", "merge_base", "desc")}
    for panel, (confidence, seats) in panels.items():
        att[panel] = {"verdict": "Approve", "confidence": confidence, "seats": seats}
    marker = "<!-- ab-local-review: %s -->" % json.dumps(att, separators=(",", ":"))
    for path, text in zip(paths, (marker, meta["title"], meta["body"].rstrip() + "\n\n" + marker)):
        with open(path, "w") as fh:
            fh.write(text + "\n")
    print("attestation: %s (open the PR with pr-title.txt and pr-body.md)" % paths[0], file=sys.stderr)


def gate(folder):
    """AB's verdict for each panel from the saved replies; True when both Approve.

    ``folder`` is the --out folder: each panel's expected seats come from its
    reviewers.json, the replies from vote_file(). A seat with no reply file,
    or a reply with no verdict, is a failed reviewer, as in AB. Panel 2 is
    "skipped" until panel 1 Approves, as in AB, then "pending" until any of
    its reply files exist: run its reviewers then and gate again. When both
    Approve on a clean tree, the result is also attested (_write_attestation)."""
    import fix_engine
    with open(os.path.join(folder, "reviewers.json")) as fh:
        seats = json.load(fh)
    result, replied = {}, {}
    for panel in ("panel1", "panel2"):
        if panel == "panel2" and result["panel1"]["verdict"] != "Approve":
            # As in AB (pr_review._state_panel_after): panel 2 waits for panel 1.
            result[panel] = {"verdict": "skipped", "reason": "runs once panel 1 Approves"}
            continue
        if panel == "panel2" and not any(os.path.exists(vote_file(folder, panel, s)) for s in seats[panel]):
            result[panel] = {"verdict": "pending", "reason": "panel 1 Approves: run panel 2's reviewers"}
            continue
        votes, failed = [], []
        replied[panel] = []
        for seat in seats[panel]:
            path = vote_file(folder, panel, seat)
            vote = None
            if os.path.exists(path):
                with open(path) as fh:
                    vote = fix_engine._extract_reviewer_verdict(fh.read())
            if vote is None:
                failed.append(seat)
                continue
            vote["confidence"] = fix_engine._norm_confidence(vote.get("confidence"))
            votes.append(vote)
            replied[panel].append(seat.split("/", 1)[-1])
        if not votes:
            result[panel] = {"failed": failed, "verdict": "no replies"}
            continue
        conf, verdict = fix_engine.panel_verdict(votes, len(failed))
        result[panel] = {"votes": len(votes), "failed": failed,
                         "confidence": round(conf, 2), "verdict": verdict}
    passed = all(r["verdict"] == "Approve" for r in result.values())
    print(json.dumps(dict(result, passed=passed)))
    _write_attestation(folder, passed and {p: (result[p]["confidence"], replied[p]) for p in result})
    return passed


def attest(folder, model):
    """Attest a review run on the skill's ladder: True, with the attestation
    written, when the frontier ``model``'s reply on each panel
    (DIR/<panel>-<model>.out) is an Approve at ATTEST_CONFIDENCE or more."""
    import fix_engine
    panels = {}
    for panel in ("panel1", "panel2"):
        path = os.path.join(folder, "%s-%s.out" % (panel, model))
        vote = None
        if os.path.exists(path):
            with open(path) as fh:
                vote = fix_engine._extract_reviewer_verdict(fh.read())
        conf = fix_engine._norm_confidence((vote or {}).get("confidence"))
        ok = bool(vote) and vote.get("verdict") == "Approve" and conf >= ATTEST_CONFIDENCE
        print("%s: %s" % (panel, "Approve %.2f" % conf if ok else "not a passing Approve (%s)" % path))
        if ok:
            panels[panel] = (round(conf, 2), [model])
    passed = len(panels) == 2
    _write_attestation(folder, panels if passed else None)
    return passed


def main():
    """Write both panels' prompts and the reviewer models for a local change."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--gate", metavar="DIR")
    ap.add_argument("--attest", metavar="DIR")
    ap.add_argument("--frontier", metavar="MODEL")
    ap.add_argument("--base", default="origin/dev")
    ap.add_argument("--out", default="/tmp/review")
    ap.add_argument("--title")
    ap.add_argument("--body-file")
    args = ap.parse_args()

    # Importing AB writes config.json and ab.log into the working directory;
    # run from a scratch folder so they never land in the repo under review.
    for name in ("repo", "gate", "attest", "out", "body_file"):
        if getattr(args, name):
            setattr(args, name, os.path.abspath(getattr(args, name)))
    scratch = tempfile.mkdtemp(prefix="review-")
    atexit.register(shutil.rmtree, scratch, True)
    os.chdir(scratch)

    import main as _ab_main  # noqa: F401  (pr_review imports main; import it first)
    import fix_engine
    import pr_review

    if args.gate:
        sys.exit(0 if gate(args.gate) else 1)
    if args.attest:
        if not args.frontier:
            ap.error("--attest needs --frontier MODEL")
        sys.exit(0 if attest(args.attest, args.frontier) else 1)
    if not args.repo:
        ap.error("a repo path is required")

    repo = os.path.abspath(args.repo)
    base = _git(repo, "merge-base", args.base, "HEAD").strip()
    files = _changed_files(repo, base)
    if not files:
        print("no change since %s" % args.base, file=sys.stderr)
        sys.exit(3)
    title = args.title or _git(repo, "log", "-1", "--format=%s").strip()
    body = (open(args.body_file).read() if args.body_file
            else _git(repo, "log", "--format=%B", "%s..HEAD" % base).strip())
    pr = SimpleNamespace(title=title, body=body, get_commits=lambda: _commits(repo, base))

    try:
        seats = _reviewer_seats(_slug(repo))
    except RuntimeError as e:
        print("cannot run AB's panels: %s" % e, file=sys.stderr)
        sys.exit(4)

    diff = fix_engine.diff_details(pr_review._pr_diff_text(files))
    agents, agents_error = _agents_context("%s\n%s\n%s" % (title, body, "\n".join(f.filename for f in files)))
    panels = {
        "panel1.txt": fix_engine.reviewer_prompt(
            pr_review.skeptical_issue_body(pr) + ("\n\n" + agents if agents else ""), diff),
        "panel2.txt": fix_engine.reviewer_prompt(pr_review.state_logic_issue_body(pr), diff),
    }
    _fresh_votes(args.out)
    for name, text in panels.items():
        with open(os.path.join(args.out, name), "w") as fh:
            fh.write(text)
    with open(os.path.join(args.out, "reviewers.json"), "w") as fh:
        json.dump(seats, fh)
    # The attestation is only for a committed head: uncommitted edits were reviewed too.
    # The target branch name (origin/dev -> dev) and merge base bind the
    # attestation to the diff reviewed, so retargeting the PR voids it.
    remote, _, branch = args.base.partition("/")
    target = branch if branch and remote in _git(repo, "remote").split() else args.base
    meta = {"head": _git(repo, "rev-parse", "HEAD").strip(), "base": target, "merge_base": base,
            "dirty": bool(_git(repo, "status", "--porcelain").strip()),
            "title": title, "body": body, "desc": pr_review.local_review_desc(title, body)}
    with open(os.path.join(args.out, "meta.json"), "w") as fh:
        json.dump(meta, fh)
    print(json.dumps({"repo": repo, "base": base[:8], "files": len(files),
                      "diff_chars": len(diff),
                      "reviewers": [{"panel": p, "seat": s, "model": s.split("/", 1)[1],
                                     "prompt": os.path.join(args.out, p + ".txt"),
                                     "reply": vote_file(args.out, p, s)}
                                    for p in ("panel1", "panel2") for s in seats[p]],
                      "agents_error": agents_error,
                      "prompts": [os.path.join(args.out, n) for n in panels]}))


if __name__ == "__main__":
    main()
