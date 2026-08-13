#!/usr/bin/env python3
"""Self-test for the pr_summary call site's batch_kind=/batch_context= routing
(LLM Selection Redesign, Phase 5, call site #15 — pr_review.py's
_summarize_changes).

Run:  python3 test_pr_summary_batch_routing.py

llm_client.py and pr_review.py cannot be imported directly (both transitively
pull in main.py's circular import chain), so this extracts the specific
functions under test via ast and execs them with stubbed dependencies — the
established convention in this repo (see test_dedup_llm_adjudication.py).

Covers:
1. _call_llm_with_requirements (llm_client.py): when reqs.batch_ok, a
   batch_kind is given, no tools/explicit messages, batch_enabled is on, and
   the winning candidate's provider is a cloud batch-capable one -> routes
   through batch.enqueue() and returns "" WITHOUT touching the synchronous
   candidate chain at all.
2. Same, but each of the four gating conditions individually false (no
   batch_kind, batch_enabled off, tools given, explicit messages given) ->
   falls through to the normal synchronous chain-walk instead.
3. If batch.enqueue() itself raises, falls through to the synchronous chain
   rather than losing the call.
4. _apply_batched_pr_summary (pr_review.py): correctly injects the summary
   into the existing marker comment at the same position _render() would
   have used, and correctly discards a stale result (head_sha changed).
"""
import ast


def _load_llm_client_ns(enqueue_fn, batch_enabled=True):
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_call_llm_with_requirements")
    seg = ast.get_source_segment(src, node)

    calls = {"try_candidate": 0}

    class _FakeSelection:
        key = ("anthropic", "", "claude-x")
        alternatives = []

    class _FakeModelSelection:
        @staticmethod
        def select_model(reqs, candidates, perf, tuning):
            return _FakeSelection()

        @staticmethod
        def safety_floor(entries):
            return None

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    def _try_candidate(candidate, messages, tools, stream, task_id, config, **kwargs):
        calls["try_candidate"] += 1
        return "sync result", None

    class _FakeSem:
        def acquire(self):
            pass

        def release(self):
            pass

    ns = {
        "logger": _NoLog(),
        "model_selection": _FakeModelSelection(),
        "_enumerate_candidates": lambda config: [
            {"key": ("anthropic", "", "claude-x"), "provider": "anthropic",
             "model": "claude-x", "api_key": "k", "base_url": "", "rpm": 0},
        ],
        "get_llm_perf_snapshot": lambda: {},
        "_configured_entries": lambda config: [],
        "_model_key": lambda p, b, m: (p, b, m),
        "_try_candidate": _try_candidate,
        "_get_category_semaphore": lambda name: _FakeSem(),
        "batch": type("batch_mod", (), {"enqueue": staticmethod(enqueue_fn)})(),
    }
    exec(seg, ns)
    ns["__calls__"] = calls
    return ns


def _make_reqs(batch_ok=True):
    class _Reqs:
        pass
    r = _Reqs()
    r.batch_ok = batch_ok
    return r


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True

    # ---- 1. Fully eligible -> batches, returns "", never touches sync chain ----
    enqueued = []

    def _enqueue_ok(kind, context, provider, model, system, prompt):
        enqueued.append((kind, context, provider, model, system, prompt))
        return "req_abc"

    ns = _load_llm_client_ns(_enqueue_ok)
    # Patch the module-level "from batch import enqueue as _batch_enqueue" by
    # pre-seeding sys.modules so the function's inline import resolves to our fake.
    import sys
    import types
    fake_batch_mod = types.ModuleType("batch")
    fake_batch_mod.enqueue = _enqueue_ok
    sys.modules["batch"] = fake_batch_mod

    out = ns["_call_llm_with_requirements"](
        _make_reqs(batch_ok=True), "the prompt", "the system", None, None, None,
        "task1", {"batch_enabled": True}, batch_kind="pr_summary",
        batch_context={"repo": "o/r", "pr": 5, "head_sha": "abc123"})
    ok &= _check("eligible call routes through batch.enqueue and returns ''", out == "")
    ok &= _check("enqueue received the right kind/provider/model/system/prompt",
                 enqueued and enqueued[0][0] == "pr_summary" and enqueued[0][2] == "anthropic"
                 and enqueued[0][3] == "claude-x" and enqueued[0][4] == "the system"
                 and enqueued[0][5] == "the prompt")
    ok &= _check("synchronous candidate chain was never touched", ns["__calls__"]["try_candidate"] == 0)

    # ---- 2. Each gating condition off individually -> falls through to sync ----
    def _enqueue_should_not_be_called(*a, **k):
        raise AssertionError("enqueue should not have been called")

    fake_batch_mod.enqueue = _enqueue_should_not_be_called

    ns2 = _load_llm_client_ns(_enqueue_should_not_be_called)
    out2 = ns2["_call_llm_with_requirements"](
        _make_reqs(batch_ok=True), "p", "s", None, None, None, "t",
        {"batch_enabled": True}, batch_kind=None)  # no batch_kind
    ok &= _check("no batch_kind -> falls through to sync path",
                 out2 == "sync result" and ns2["__calls__"]["try_candidate"] == 1)

    ns3 = _load_llm_client_ns(_enqueue_should_not_be_called)
    out3 = ns3["_call_llm_with_requirements"](
        _make_reqs(batch_ok=True), "p", "s", None, None, None, "t",
        {"batch_enabled": False}, batch_kind="pr_summary")  # batch disabled
    ok &= _check("batch_enabled=False -> falls through to sync path",
                 out3 == "sync result" and ns3["__calls__"]["try_candidate"] == 1)

    ns4 = _load_llm_client_ns(_enqueue_should_not_be_called)
    out4 = ns4["_call_llm_with_requirements"](
        _make_reqs(batch_ok=True), "p", "s", None, ["some_tool"], None, "t",
        {"batch_enabled": True}, batch_kind="pr_summary")  # tools given
    ok &= _check("tools given -> falls through to sync path",
                 out4 == "sync result" and ns4["__calls__"]["try_candidate"] == 1)

    ns5 = _load_llm_client_ns(_enqueue_should_not_be_called)
    out5 = ns5["_call_llm_with_requirements"](
        _make_reqs(batch_ok=True), "p", "s", [{"role": "user", "content": "x"}], None, None, "t",
        {"batch_enabled": True}, batch_kind="pr_summary")  # explicit messages
    ok &= _check("explicit messages given -> falls through to sync path",
                 out5 == "sync result" and ns5["__calls__"]["try_candidate"] == 1)

    ns6 = _load_llm_client_ns(_enqueue_should_not_be_called)
    out6 = ns6["_call_llm_with_requirements"](
        _make_reqs(batch_ok=False), "p", "s", None, None, None, "t",
        {"batch_enabled": True}, batch_kind="pr_summary")  # batch_ok False
    ok &= _check("reqs.batch_ok=False -> falls through to sync path",
                 out6 == "sync result" and ns6["__calls__"]["try_candidate"] == 1)

    # ---- 3. enqueue() raising -> falls through to sync, call is not lost ----
    def _enqueue_raises(*a, **k):
        raise RuntimeError("disk full")

    fake_batch_mod.enqueue = _enqueue_raises
    ns7 = _load_llm_client_ns(_enqueue_raises)
    out7 = ns7["_call_llm_with_requirements"](
        _make_reqs(batch_ok=True), "p", "s", None, None, None, "t",
        {"batch_enabled": True}, batch_kind="pr_summary")
    ok &= _check("batch.enqueue raising falls back to sync instead of losing the call",
                 out7 == "sync result" and ns7["__calls__"]["try_candidate"] == 1)

    del sys.modules["batch"]

    # ---- 4. _apply_batched_pr_summary (pr_review.py) ----
    src2 = open("pr_review.py").read()
    tree2 = ast.parse(src2)
    want = {"_apply_batched_pr_summary", "_find_marker_comment"}
    segs = []
    for n in tree2.body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            segs.append(ast.get_source_segment(src2, n))
    # pull the two module-level constants the handler references
    header_node = next(n for n in tree2.body if isinstance(n, ast.Assign)
                       and any(getattr(t, "id", None) == "_SUMMARY_HEADER" for t in n.targets))
    marker_node = next(n for n in tree2.body if isinstance(n, ast.Assign)
                       and any(getattr(t, "id", None) == "PR_REVIEW_MARKER" for t in n.targets))
    segs.insert(0, ast.get_source_segment(src2, header_node))
    segs.insert(0, ast.get_source_segment(src2, marker_node))

    class _Comment:
        def __init__(self, body):
            self.body = body
            self.edited_to = None

        def edit(self, new_body):
            self.edited_to = new_body
            self.body = new_body

    class _PR:
        def __init__(self, head_sha, comments):
            self.head = type("H", (), {"sha": head_sha})()
            self._comments = comments

        def get_issue_comments(self):
            return list(self._comments)

    class _Repo2:
        def __init__(self, pr):
            self._pr = pr

        def get_pull(self, number):
            return self._pr

    class _Gh:
        def __init__(self, repo):
            self._repo = repo

        def get_repo(self, name):
            return self._repo

    body_before = (
        "<!-- bugfixer-pr-review -->\n<!-- head: abc123 -->\n## \U0001F916 BugFixer PR pre-review\n\n"
        "_Automated pre-review — **informational only**. A human is the sole approver; "
        "this bot never approves, denies, or edits the branch._\n\n"
        "### Tier-1 checks\n\n✅ **Passed**\n"
    )

    def _run_handler(head_sha_in_pr, marker_body):
        comment = _Comment(marker_body)
        pr = _PR(head_sha_in_pr, [comment])
        repo = _Repo2(pr)

        # The handler does an inline "from github import Github" (matching the
        # reconnect pattern used elsewhere in pr_review.py), which would
        # otherwise import the REAL PyGithub client and attempt a live network
        # call. Stub it out via sys.modules so the inline import resolves to
        # our fake instead.
        fake_github_mod = types.ModuleType("github")
        fake_github_mod.Github = lambda token: _Gh(repo)
        sys.modules["github"] = fake_github_mod
        try:
            ns_pr = {
                "logger": type("L", (), {"__getattr__": lambda s, _: (lambda *a, **k: None)})(),
                "os": __import__("os"),
                "load_config": lambda: {"GITHUB_TOKEN": "tok"},
            }
            # _find_marker_comment needs PR_REVIEW_MARKER in scope, already spliced in.
            exec("\n\n".join(segs), ns_pr)
            ns_pr["_apply_batched_pr_summary"](
                {"repo": "o/r", "pr": 7, "head_sha": "abc123"}, "- bullet one\n- bullet two")
        finally:
            del sys.modules["github"]
        return comment

    c1 = _run_handler("abc123", body_before)
    ok &= _check("summary injected right after the disclaimer paragraph",
                 c1.edited_to is not None
                 and c1.edited_to.index("bullet one") > c1.edited_to.index("never approves, denies")
                 and c1.edited_to.index("bullet one") < c1.edited_to.index("Tier-1 checks"))
    ok &= _check("_SUMMARY_HEADER present exactly once", c1.edited_to.count("What changed") == 1)

    c2 = _run_handler("DIFFERENT_SHA", body_before)
    ok &= _check("stale head_sha -> comment left untouched", c2.edited_to is None)

    body_already_has_summary = body_before.replace(
        "### Tier-1 checks", "### \U0001F4DD What changed\n\n- already here\n\n### Tier-1 checks")
    c3 = _run_handler("abc123", body_already_has_summary)
    ok &= _check("summary already present -> not duplicated", c3.edited_to is None)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running pr_summary batch routing self-test...")
    import sys
    sys.exit(main())
