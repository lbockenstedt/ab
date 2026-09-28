#!/usr/bin/env python3
"""Every promotion hop must have a back-merge edge returning to its source.

Run:  python3 ab/test_backmerge_covers_promotions.py
      (also collected by pytest via the test_* functions below)

This pins the coupling that `pr_auto_remediate_skip_promotion` **off** silently
depends on, and which nothing else defends.

With that flag off -- the documented setting for running dev -> qa -> main
unattended (docs/ab.md, "Running the chain unattended") -- AppBuilder repairs
promotion PRs in place. A repair committed onto `promote/dev-to-qa` lands on
`qa` having never existed on `dev`, so on its own `dev` would re-promote the
unfixed code every cycle, forever, while the two branches drift in exactly the
files AppBuilder keeps repairing. docs/ab.md calls that configuration
"supported rather than a leak" for ONE reason: backmerge.yml carries the repair
back to the source branch.

That reason is a load-bearing invariant expressed in two files that know
nothing about each other -- PROMOTE_ROUTES in routes.py and the job matrix in
.github/workflows/backmerge.yml. Drop an edge from the matrix, or add a
promotion route without its mirror, and the flag-off configuration quietly
becomes a permanent-drift generator. Nothing failed; the next promotion just
starts conflicting in a workflow file and never stops.

That is not hypothetical: tsa jammed exactly this way. `e8cc55a "AppBuilder:
fix PR #15 review findings"` reached `main` through `promote/qa-to-main`, and
every later qa -> main promotion died on "CONFLICT (content): Merge conflict in
promote.yml" while the same merge succeeded by hand.

So the invariant is asserted directly: for every (src -> tgt) promotion, the
back-merge matrix must contain an edge that returns tgt's content to src.
"""
import os
import re
import sys

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROUTES = os.path.join(_HERE, "routes.py")
_BACKMERGE = os.path.join(_HERE, ".github", "workflows", "backmerge.yml")


def promote_routes():
    """The (source, target) promotion edges declared in routes.py.

    Parsed from source, not imported: routes.py pulls in main.py, whose
    app-init side effects boot a second AppBuilder. Same approach as
    test_promote_routes.py."""
    with open(_ROUTES, encoding="utf-8") as fh:
        src = fh.read()
    seg = re.search(r"^PROMOTE_ROUTES\s*=\s*\{.*?^\}", src, re.S | re.M)
    assert seg, "PROMOTE_ROUTES literal not found in routes.py"
    edges = re.findall(r'\(\s*"([a-z]+)"\s*,\s*"([a-z]+)"\s*\)', seg.group(0))
    assert edges, "no promotion edges parsed out of PROMOTE_ROUTES"
    return [tuple(e) for e in edges]


def backmerge_edges():
    """The (source, target) edges in backmerge.yml's job matrix."""
    with open(_BACKMERGE, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    found = []
    for job in (wf.get("jobs") or {}).values():
        include = (((job.get("strategy") or {}).get("matrix") or {}).get("include")) or []
        for entry in include:
            if entry.get("source") and entry.get("target"):
                found.append((entry["source"], entry["target"]))
    assert found, "no back-merge edges found in %s" % _BACKMERGE
    return found


def test_every_promotion_has_a_return_edge():
    """The invariant itself.

    A repair on promote/<src>-to-<tgt> lands on tgt. Without tgt -> src the
    content is stranded there permanently and src re-promotes the unfixed code
    every cycle."""
    back = set(backmerge_edges())
    missing = [(s, t) for (s, t) in promote_routes() if (t, s) not in back]
    assert not missing, (
        "promotion route(s) with no back-merge edge returning to the source: %s.\n"
        "With pr_auto_remediate_skip_promotion off, a repair committed onto "
        "promote/<src>-to-<tgt> would land on the target and never return to the "
        "source -- the source would then re-promote the unfixed code forever, and "
        "the two branches drift in exactly the files being repaired. Either add "
        "the mirror edge to .github/workflows/backmerge.yml or drop the promotion "
        "route." % ["%s -> %s (needs %s -> %s)" % (s, t, t, s) for (s, t) in missing])


def test_back_merge_edges_are_not_self_loops():
    """A source==target entry would make the job a no-op that still reports
    success, hiding a missing edge behind a green run."""
    bad = [(s, t) for (s, t) in backmerge_edges() if s == t]
    assert not bad, "back-merge edges merging a branch into itself: %s" % bad


def test_promotion_routes_are_not_self_loops():
    bad = [(s, t) for (s, t) in promote_routes() if s == t]
    assert not bad, "promotion routes with source == target: %s" % bad


def test_the_docs_still_explain_why_flag_off_is_safe():
    """The flag-off configuration is only defensible alongside this reasoning.
    If the explanation is deleted, the next reader has no way to know the back-
    merge matrix is load-bearing rather than a convenience."""
    with open(os.path.join(_HERE, "docs", "ab.md"), encoding="utf-8") as fh:
        docs = fh.read()
    assert "pr_auto_remediate_skip_promotion" in docs
    assert "backmerge" in docs.lower(), (
        "docs/ab.md no longer ties pr_auto_remediate_skip_promotion to the "
        "back-merge that makes turning it off safe")


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
    print("\n%d/%d passed" % (len(fns) - failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
