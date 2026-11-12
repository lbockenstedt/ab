"""Build AB's PR-review prompts for a local change, so a review before
`git push` asks exactly what AB asks after it.

Every piece comes from AB's own code, so changing AB's review changes this
too: the panel briefs (pr_review.skeptical_issue_body and
state_logic_issue_body), the reviewer prompt with the fleet rules
(fix_engine.reviewer_prompt), the diff budget (pr_review._pr_diff_text), the
specialist agents (skills_loader) and the reviewer models (read from AB's
latest review comment). Used by the ab-local-review skill.

Usage:
    python scripts/local_review.py <repo path> [--base origin/dev] [--out DIR]
        [--title TEXT] [--body-file FILE]

Reviews everything on the branch since its merge-base with --base, plus
uncommitted and untracked files. Writes panel1.txt, panel2.txt and
reviewers.json to --out and prints a JSON summary. Exits non-zero, writing
nothing, when it cannot tell which models AB reviews with.

Gate:
    python scripts/local_review.py --gate DIR

DIR is the --out folder. Applies AB's panel verdict (fix_engine.panel_verdict)
to the replies saved as DIR/votes/panel1-<model>.txt and panel2-<model>.txt,
one per model in DIR/reviewers.json. Prints each panel's verdict and exits 0
only when both panels Approve.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from types import SimpleNamespace

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


def _reviewer_models(repo_slug):
    """Models AB's panel used on its latest review comment, e.g. ["claude-opus-5.5"].

    Raises RuntimeError when GitHub cannot be read or no AB review names a
    model, so a caller never mistakes "could not find out" for "no reviewers".
    AB edits its review comment in place on each new head, so the newest
    review is the most recently UPDATED comment carrying AB's marker."""
    import pr_review
    # AB's own repo always carries its reviews; use it when the target has none.
    for slug in (repo_slug, _slug(_HERE)):
        comments = _gh_json("api", "repos/%s/issues/comments?sort=updated&direction=desc"
                            "&per_page=100" % slug)
        if comments is None:
            raise RuntimeError("could not read %s's PR comments with gh" % slug)
        for c in comments:
            if pr_review.PR_REVIEW_MARKER not in (c.get("body") or ""):
                continue
            # A seat label without "/model" names no model; skip it.
            found = [m.split("/", 1)[1] for m in _REVIEWER_RE.findall(c.get("body") or "")
                     if "/" in m]
            if found:
                return list(dict.fromkeys(found))
    raise RuntimeError("no AB review comment naming its reviewer models was found")


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

    ``folder`` is the --out folder: the expected seats come from its
    reviewers.json, the replies from votes/<panel>-<model>.txt. A seat with no
    reply file, or a reply with no verdict, is a failed reviewer, as in AB."""
    import fix_engine
    with open(os.path.join(folder, "reviewers.json")) as fh:
        models = json.load(fh)
    result = {}
    for panel in ("panel1", "panel2"):
        votes, failed = [], []
        for model in models:
            path = os.path.join(folder, "votes", "%s-%s.txt" % (panel, model))
            vote = None
            if os.path.exists(path):
                with open(path) as fh:
                    vote = fix_engine._extract_reviewer_verdict(fh.read())
            if vote is None:
                failed.append(model)
                continue
            vote["confidence"] = fix_engine._norm_confidence(vote.get("confidence"))
            votes.append(vote)
        if not votes:
            result[panel] = {"failed": failed, "verdict": "no replies"}
            continue
        conf, verdict = fix_engine.panel_verdict(votes, len(failed))
        result[panel] = {"votes": len(votes), "failed": failed,
                         "confidence": round(conf, 2), "verdict": verdict}
    passed = bool(models) and all(r["verdict"] == "Approve" for r in result.values())
    print(json.dumps(dict(result, passed=passed)))
    return passed


def main():
    """Write both panels' prompts and the reviewer models for a local change."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--gate", metavar="DIR")
    ap.add_argument("--base", default="origin/dev")
    ap.add_argument("--out", default="/tmp/ab-local-review")
    ap.add_argument("--title")
    ap.add_argument("--body-file")
    args = ap.parse_args()

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
        sys.exit("no change since %s" % args.base)
    title = args.title or _git(repo, "log", "-1", "--format=%s").strip()
    body = (open(args.body_file).read() if args.body_file
            else _git(repo, "log", "--format=%B", "%s..HEAD" % base).strip())
    pr = SimpleNamespace(title=title, body=body, get_commits=lambda: [])

    try:
        models = _reviewer_models(_slug(repo))
    except RuntimeError as e:
        sys.exit("cannot run AB's panels: %s" % e)

    diff = fix_engine.diff_details(pr_review._pr_diff_text(files))
    agents, agents_error = _agents_context("%s\n%s\n%s" % (title, body, "\n".join(f.filename for f in files)))
    panels = {
        "panel1.txt": fix_engine.reviewer_prompt(
            pr_review.skeptical_issue_body(pr) + ("\n\n" + agents if agents else ""), diff),
        "panel2.txt": fix_engine.reviewer_prompt(pr_review.state_logic_issue_body(pr), diff),
    }
    os.makedirs(args.out, exist_ok=True)
    for name, text in panels.items():
        with open(os.path.join(args.out, name), "w") as fh:
            fh.write(text)
    with open(os.path.join(args.out, "reviewers.json"), "w") as fh:
        json.dump(models, fh)
    print(json.dumps({"repo": repo, "base": base[:8], "files": len(files),
                      "diff_chars": len(diff), "reviewers": models,
                      "agents_error": agents_error,
                      "prompts": [os.path.join(args.out, n) for n in panels]}))


if __name__ == "__main__":
    main()
