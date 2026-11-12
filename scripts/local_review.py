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
only when both panels Approve.
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
    """AB's seats for each panel on its latest review, e.g.
    {"panel1": ["copilot/claude-opus-5.5", ...], "panel2": [...]}.

    AB picks each panel's seats separately (each runs its own review_fix), so
    they are read from each panel's section of the comment. A review whose
    state-logic section names no seat (that panel was skipped) uses panel 1's
    seats for panel 2. A seat is the full provider/model label, so two seats running the same
    model through different providers stay two seats, as in AB. The latest
    review is the most recently UPDATED comment carrying AB's marker (AB edits
    its review comment in place) in the target repo or AB's own repo, which
    always carries reviews; the target's own last review may be older.

    Raises RuntimeError when GitHub cannot be read or no AB review names a
    model, so a caller never mistakes "could not find out" for "no reviewers"."""
    import pr_review
    newest = None
    for slug in dict.fromkeys((repo_slug, _slug(_HERE))):
        comments = _gh_json("api", "repos/%s/issues/comments?sort=updated&direction=desc"
                            "&per_page=100" % slug)
        if comments is None:
            raise RuntimeError("could not read %s's PR comments with gh" % slug)
        for c in comments:
            if pr_review.PR_REVIEW_MARKER not in (c.get("body") or ""):
                continue
            skeptical, _, state = (c.get("body") or "").partition(pr_review._STATE_PANEL_HEADER)
            seats = {"panel1": _seats_in(skeptical), "panel2": _seats_in(state)}
            if seats["panel1"]:
                seats["panel2"] = seats["panel2"] or seats["panel1"]
                if newest is None or c.get("updated_at", "") > newest[0]:
                    newest = (c.get("updated_at", ""), seats)
                break
    if newest is None:
        raise RuntimeError("no AB review comment naming its reviewer models was found")
    return newest[1]


def _seats_in(text):
    """The distinct seats named in one panel's section; a seat label without
    "/model" names no model and is skipped."""
    return list(dict.fromkeys(m for m in _REVIEWER_RE.findall(text) if "/" in m))


def _fresh_votes(folder):
    """Start a round with an empty votes folder: a reply left from an earlier
    round would otherwise be gated as a vote on this change."""
    votes = os.path.join(folder, "votes")
    shutil.rmtree(votes, ignore_errors=True)
    os.makedirs(votes)


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


def gate(folder):
    """AB's verdict for each panel from the saved replies; True when both Approve.

    ``folder`` is the --out folder: each panel's expected seats come from its
    reviewers.json, the replies from vote_file(). A seat with no reply file,
    or a reply with no verdict, is a failed reviewer, as in AB."""
    import fix_engine
    with open(os.path.join(folder, "reviewers.json")) as fh:
        seats = json.load(fh)
    result = {}
    for panel in ("panel1", "panel2"):
        votes, failed = [], []
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
        if not votes:
            result[panel] = {"failed": failed, "verdict": "no replies"}
            continue
        conf, verdict = fix_engine.panel_verdict(votes, len(failed))
        result[panel] = {"votes": len(votes), "failed": failed,
                         "confidence": round(conf, 2), "verdict": verdict}
    passed = all(r["verdict"] == "Approve" for r in result.values())
    print(json.dumps(dict(result, passed=passed)))
    return passed


def main():
    """Write both panels' prompts and the reviewer models for a local change."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--gate", metavar="DIR")
    ap.add_argument("--base", default="origin/dev")
    ap.add_argument("--out", default="/tmp/review")
    ap.add_argument("--title")
    ap.add_argument("--body-file")
    args = ap.parse_args()

    # Importing AB writes config.json and ab.log into the working directory;
    # run from a scratch folder so they never land in the repo under review.
    for name in ("repo", "gate", "out", "body_file"):
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
