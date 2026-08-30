"""Selftest for pr_review._backfill_pr_review_refs.

WHY: the PR list renders a target-branch badge only when a record carries
base_ref. Records written before that field existed have neither base_ref nor
head_ref, so the badge was missing from the large majority of rows (67 of 72 on
the production host) — in a dev -> qa -> main workflow the target branch is the
most useful fact about a PR. New reviews store it; this backfills the history.

pr_review imports the app (circular at import time), so the function is
extracted with ast and exec'd against fakes — the same harness pattern the
test_llm_client_* selftests use.
"""
import ast
import re


class _FakeRef:
    def __init__(self, ref):
        self.ref = ref


class _FakePR:
    def __init__(self, base, head):
        self.base = _FakeRef(base)
        self.head = _FakeRef(head)


class _FakeRepo:
    def __init__(self, gh, name):
        self._gh, self._name = gh, name

    def get_pull(self, number):
        self._gh.fetches.append((self._name, number))
        pr = self._gh.prs.get((self._name, number))
        if pr is None:
            raise RuntimeError("404 not found")
        return pr


class _FakeGH:
    def __init__(self, prs):
        self.prs = prs
        self.fetches = []

    def get_repo(self, name):
        return _FakeRepo(self, name)


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _load(state, updates):
    """Extract the backfill function + its constant against injected fakes."""
    tree = ast.parse(open("pr_review.py").read())
    ns = {"re": re, "state": state, "logger": _Logger()}

    def _update_pr_review(repo, number, **fields):
        key = "%s#%s" % (repo, number)
        rec = state["pr_reviews"].get(key)
        if not rec:
            return False
        rec.update(fields)
        updates.append((key, dict(fields)))
        return True

    ns["update_pr_review"] = _update_pr_review
    mod = ast.Module(body=[], type_ignores=[])
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_backfill_pr_review_refs":
            mod.body.append(node)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_REFS_BACKFILL_PER_CYCLE" for t in node.targets):
            mod.body.append(node)
    exec(compile(mod, "pr_review_extract", "exec"), ns)
    for name in ("_backfill_pr_review_refs", "_REFS_BACKFILL_PER_CYCLE"):
        if name not in ns:
            raise AssertionError("extraction incomplete, missing: %s" % name)
    return ns


def _check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    return bool(cond)


def _rec(repo, number, **extra):
    r = {"repo": repo, "number": number}
    r.update(extra)
    return r


def main():
    ok = True
    print("pr_review target-branch backfill:")

    # ── fills a record that predates base_ref, leaves a populated one alone ──
    state = {"pr_reviews": {
        "o/r#1": _rec("o/r", 1),                              # missing -> fill
        "o/r#2": _rec("o/r", 2, base_ref="qa", head_ref="x"),  # already set
    }}
    updates = []
    ns = _load(state, updates)
    backfill = ns["_backfill_pr_review_refs"]
    gh = _FakeGH({("o/r", 1): _FakePR("dev", "fix/thing"),
                  ("o/r", 2): _FakePR("main", "other")})
    n = backfill(gh)
    ok &= _check("returns the number filled", n == 1)
    ok &= _check("missing record gets base_ref", state["pr_reviews"]["o/r#1"]["base_ref"] == "dev")
    ok &= _check("missing record gets head_ref",
                 state["pr_reviews"]["o/r#1"]["head_ref"] == "fix/thing")
    ok &= _check("already-populated record untouched",
                 state["pr_reviews"]["o/r#2"]["base_ref"] == "qa")
    ok &= _check("no wasted API call for a populated record", gh.fetches == [("o/r", 1)])

    # ── idempotent: a second pass does nothing and costs no API calls ───────
    gh2 = _FakeGH({("o/r", 1): _FakePR("dev", "fix/thing")})
    ok &= _check("second pass is a no-op", backfill(gh2) == 0)
    ok &= _check("second pass makes no API calls", gh2.fetches == [])

    # ── bounded per cycle ──────────────────────────────────────────────────
    many = {"o/r#%d" % i: _rec("o/r", i) for i in range(1, 11)}
    state2 = {"pr_reviews": many}
    ns2 = _load(state2, [])
    gh3 = _FakeGH({("o/r", i): _FakePR("dev", "h%d" % i) for i in range(1, 11)})
    n2 = ns2["_backfill_pr_review_refs"](gh3, limit=4)
    ok &= _check("respects the per-cycle limit", n2 == 4 and len(gh3.fetches) == 4)
    remaining = [k for k, v in state2["pr_reviews"].items() if not v.get("base_ref")]
    ok &= _check("the rest are left for the next cycle", len(remaining) == 6)
    ok &= _check("default per-cycle bound is sane",
                 isinstance(ns2["_REFS_BACKFILL_PER_CYCLE"], int)
                 and 0 < ns2["_REFS_BACKFILL_PER_CYCLE"] <= 100)

    # ── failure isolation ──────────────────────────────────────────────────
    state3 = {"pr_reviews": {"o/r#7": _rec("o/r", 7), "o/r#8": _rec("o/r", 8)}}
    ns3 = _load(state3, [])
    gh4 = _FakeGH({("o/r", 8): _FakePR("main", "h8")})  # #7 will raise 404
    try:
        n3 = ns3["_backfill_pr_review_refs"](gh4)
        raised = False
    except Exception:
        n3, raised = -1, True
    ok &= _check("a failed fetch does not raise", not raised)
    ok &= _check("a failed fetch does not block the others", n3 == 1)
    ok &= _check("unfetchable record left unset", not state3["pr_reviews"]["o/r#7"].get("base_ref"))

    # ── an empty base_ref is never written (would render a blank badge) ─────
    state4 = {"pr_reviews": {"o/r#9": _rec("o/r", 9)}}
    ns4 = _load(state4, [])
    gh5 = _FakeGH({("o/r", 9): _FakePR("", "")})
    ok &= _check("empty base_ref is not written", ns4["_backfill_pr_review_refs"](gh5) == 0)
    ok &= _check("record stays unset rather than blank",
                 not state4["pr_reviews"]["o/r#9"].get("base_ref"))

    # ── records without repo/number are skipped, not crashed on ────────────
    state5 = {"pr_reviews": {"bad": {"findings": 1}, "o/r#3": _rec("o/r", 3)}}
    ns5 = _load(state5, [])
    gh6 = _FakeGH({("o/r", 3): _FakePR("dev", "h3")})
    ok &= _check("malformed record skipped, valid one still filled",
                 ns5["_backfill_pr_review_refs"](gh6) == 1)

    # ── over-long ref names are truncated (matches record_pr_review) ────────
    state6 = {"pr_reviews": {"o/r#4": _rec("o/r", 4)}}
    ns6 = _load(state6, [])
    gh7 = _FakeGH({("o/r", 4): _FakePR("b" * 300, "h" * 300)})
    ns6["_backfill_pr_review_refs"](gh7)
    ok &= _check("base_ref truncated to 120",
                 len(state6["pr_reviews"]["o/r#4"]["base_ref"]) == 120)
    ok &= _check("head_ref truncated to 120",
                 len(state6["pr_reviews"]["o/r#4"]["head_ref"]) == 120)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
