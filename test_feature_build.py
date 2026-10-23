#!/usr/bin/env python3
"""Self-test for feature_build.py — Phase 2's mutating build->PR orchestrator.

Run:  python3 ab/test_feature_build.py

feature_build.py imports `main`/`fix_engine`/`llm_client`/`github_ops` (real
app-context modules whose import fully boots the live app as a side effect in
this checkout — see test_skills_loader.py's docstring), so this extracts its
functions by source via ast and execs them with a fully stubbed git/GitHub/
LLM layer — no real clone, push, or LLM call ever happens.

Regression guards this pins:
  - a "build" verdict with no resolvable skill or no claude_cli slot refuses
    and flags rather than attempting an unscoped/recipe-less build
  - an agent run that makes no file changes fails cleanly (feature_failed),
    never reaches the push/PR step
  - the docs-completeness gate: no docs/*.md touched -> ONE corrective turn
    -> still none -> flags (not built), NO PR opened
  - when docs ARE present, the full commit->push->PR path runs, and the PR
    is named/branched EXACTLY "AI Feature #N" / "ai-feature/N-<slug>" (the
    load-bearing detail that makes pr_review._review_one's "skip AppBuilder's
    own AI Fix PR" check NOT match — see test_pr_review_own_pr_skip.py)
  - ground truth for "what changed" is the REAL git diff, never the agent's
    self-reported (and possibly malformed) JSON summary
"""
import ast
import contextlib
import sys

from github import GithubException

import branch_policy as real_branch_policy
import feature_boundary as real_feature_boundary


def _load_ns():
    src = open("feature_build.py").read()
    tree = ast.parse(src)

    segs = []
    want_funcs = {
        "_build_prompt", "_materialize_skill", "_changed_files", "_docs_touched",
        "_non_doc_files", "_run_build_agent", "_mark_failed", "_mark_built",
        "_flag_incomplete", "build_feature", "_handoff_to_fix_pipeline",
        "_build_mode", "_syntax_errors", "_run_guardrails",
    }
    want_assigns = {"_FEATURE_DRIVE_MARKER", "_BUILD_JSON_SCHEMA", "_BUILD_SYSTEM", "_DOCS_NUDGE_TMPL",
                    "_BUILD_SYSTEM_TOOLS", "_GUARDRAIL_FEEDBACK_TMPL", "_GATE_LABELS"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in want_assigns:
                    segs.append(ast.get_source_segment(src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    _store = {}
    _handoffs = []

    def load_processed():
        return dict(_store)

    def save_processed(d):
        _store.clear()
        _store.update(d)

    def recompute_issue_counters(processed):
        pass

    def _ensure_label(gh_repo, name):
        gh_repo.ensured_labels.append(name)
        return True

    _candidates_holder = {"candidates": [{
        "key": ("claude_cli", "", "claude"), "provider": "claude_cli", "model": "claude",
        "base_url": "", "api_key": "", "rpm": 0, "available": True, "unavailable_reason": None,
        "caps": {"supports_mutating_agent": True, "native_agentic_tools": True,
                "supports_structured_output": True, "cost_tier": "cheap", "max_complexity": "large",
                "context_window": 200000},
    }]}

    class _FakeLlmClient:
        def _enumerate_candidates(self, config):
            return list(_candidates_holder["candidates"])
        def get_llm_perf_snapshot(self):
            return {}

    import model_selection as real_model_selection

    _pr_holder = {"existing": None}

    def find_existing_pull_request(repo_obj, target_branch, base_branch):
        return _pr_holder["existing"]

    @contextlib.contextmanager
    def _authenticated_remote(remote, plain_url, token):
        remote.set_url(f"https://TOKEN@{plain_url.split('://', 1)[1]}")
        try:
            yield remote
        finally:
            remote.set_url(plain_url)

    class _FakeSkillsLoader:
        def __init__(self):
            self._files = {}
            self._instructions = {}
        def skill_files(self, name):
            return dict(self._files.get(name, {}))
        def skill_instructions(self, name):
            return self._instructions.get(name, "")

    _llm_holder = {"responses": []}  # list of return values, consumed in order; last repeats

    def call_llm(*a, **k):
        _llm_holder.setdefault("calls", []).append({"args": a, "kwargs": k})
        resp = _llm_holder["responses"]
        if not resp:
            return "{}"
        return resp.pop(0) if len(resp) > 1 else resp[0]

    import git as real_git

    # Pre-PR guardrail stubs. Each holder is a list consumed in order (last
    # value repeats); the defaults pass every gate.
    _guard = {"review": [{"verdict": "Approve", "confidence": 0.9, "critique": ""}],
              "verify": [(True, None)], "prepare": [None], "syntax": [{"ok": True}],
              "review_calls": [], "verify_calls": []}

    def _next(key):
        vals = _guard[key]
        return vals.pop(0) if len(vals) > 1 else vals[0]

    def review_fix(repo_path, issue_body, proposed_fixes, **kw):
        _guard["review_calls"].append({"issue_body": issue_body, **kw})
        return _next("review")

    def verify_fix(repo_path, repo_name, config):
        _guard["verify_calls"].append(repo_name)
        return _next("verify")

    class _FakeRepoTools:
        @staticmethod
        def check_file_syntax(path):
            return _next("syntax")

    ns = {
        "logger": _NoLog(),
        "load_processed": load_processed, "save_processed": save_processed,
        "recompute_issue_counters": recompute_issue_counters,
        "_ensure_label": _ensure_label,
        "llm_client": _FakeLlmClient(),
        "LlmRequirements": real_model_selection.LlmRequirements,
        "select_model": real_model_selection.select_model,
        "find_existing_pull_request": find_existing_pull_request,
        "_authenticated_remote": _authenticated_remote,
        "skills_loader": _FakeSkillsLoader(),
        "feature_boundary": real_feature_boundary,  # pure/standalone — used for real
        # branch_policy's helpers are pure functions of their arguments, so the
        # real ones are used rather than stubs — same rationale as
        # feature_boundary above. Their ABSENCE was silently defeating this
        # file: build_feature calls auto_branch_name() before the build agent
        # runs, so every case that got that far died with
        # NameError: name 'auto_branch_name' is not defined, which
        # build_feature's broad `except Exception` recorded as a generic
        # "Unexpected error" -> feature_failed. The docs-gate cases below then
        # asserted against a run that never reached the docs gate at all.
        "auto_branch_name": real_branch_policy.auto_branch_name,
        "integration_branch": real_branch_policy.integration_branch,
        "may_force_push": real_branch_policy.may_force_push,
        "call_llm": call_llm,
        "process_single_issue": lambda repo, num: (_handoffs.append((repo, num)) or (True, "fix pipeline ran")),
        "GithubException": GithubException,
        "git": real_git,  # patched per-test-case via _patch_clone(); real module otherwise
        "os": __import__("os"), "tempfile": __import__("tempfile"),
        "datetime": __import__("datetime").datetime,
        "_BUILD_LOCK": __import__("threading").Lock(),
        "review_fix": review_fix, "verify_fix": verify_fix,
        "prepare_environment": lambda path: _next("prepare"),
        "is_verification_infra_failure": lambda m: bool(m) and str(m).startswith("[infra]"),
        "_worktree_state": lambda path: {},
        "discard_verification_artifacts": lambda path, before: None,
        "repo_tools": _FakeRepoTools(),
    }

    # _robust_json_loads: the real one (fix_engine.py) needs `json` + a regex
    # constant in scope — reuse the exact extraction test_feature_drive_
    # classify.py already does, rather than reimplementing it a third time.
    fe_src = open("fix_engine.py").read()
    fe_tree = ast.parse(fe_src)
    for node in fe_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ("_robust_json_loads", "_loads_llm_json_object"):
            segs.insert(0, ast.get_source_segment(fe_src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in ("_JSON_BAD_ESCAPE_RE", "_JSON_FENCE_RE"):
                    segs.insert(0, ast.get_source_segment(fe_src, node))
    ns["json"] = __import__("json")
    ns["re"] = __import__("re")

    exec("\n\n".join(segs), ns)
    ns["_store"] = _store
    ns["_handoffs"] = _handoffs
    ns["_candidates_holder"] = _candidates_holder
    ns["_pr_holder"] = _pr_holder
    ns["_llm_holder"] = _llm_holder
    ns["_guard"] = _guard
    return ns


# ── fake git.Repo layer ──────────────────────────────────────────────────

class _FakeGitCmd:
    def __init__(self, repo):
        self._repo = repo
    def add(self, A=True):
        pass  # staging is implicit in this fake — _changed_files reads _repo._changed
    def diff(self, *args):
        if "--cached" in args and "--name-only" in args:
            return "\n".join(self._repo._changed)
        return ""
    def checkout(self, branch):
        raise Exception("no such branch")  # forces callers onto create_head, matching real usage


class _FakeIndex:
    def __init__(self, repo):
        self._repo = repo
    def commit(self, msg):
        self._repo.commits.append(msg)


class _FakeRemote:
    def __init__(self, repo):
        self._repo = repo
        self.pushed = []
        self.urls = []
    def set_url(self, url):
        self.urls.append(url)
    def push(self, branch, force=False):
        self.pushed.append((branch, force))


class _FakeRemotes:
    def __init__(self, repo):
        self.origin = _FakeRemote(repo)


class _FakeHead:
    def __init__(self, repo, name):
        self._repo = repo
        self.name = name
    def checkout(self):
        self._repo.current_branch = self.name


class _FakeRepoGit:
    def __init__(self, changed=None):
        self.remotes = _FakeRemotes(self)
        self.git = _FakeGitCmd(self)
        self.index = _FakeIndex(self)
        self._changed = changed if changed is not None else ["some/file.py"]
        self.commits = []
        self.current_branch = None
    def create_head(self, name):
        return _FakeHead(self, name)


class _FakeGhIssue:
    def __init__(self, number=42, title="Add a clear-dongles button", body="Adds a button."):
        self.number = number
        self.title = title
        self.body = body
        self.added_labels = []
        self.comments_posted = []
    def add_to_labels(self, name):
        self.added_labels.append(name)
    def create_comment(self, body):
        self.comments_posted.append(body)


class _FakePR:
    def __init__(self, number=99):
        self.number = number
        self.html_url = f"https://github.com/owner/repo/pull/{number}"
        self.added_labels = []
    def add_to_labels(self, name):
        self.added_labels.append(name)


class _FakeGhRepo:
    def __init__(self):
        self.full_name = "owner/repo"
        self.clone_url = "https://github.com/owner/repo.git"
        self.ensured_labels = []
        self.created_prs = []
    def create_pull(self, title, body, head, base):
        pr = _FakePR()
        self.created_prs.append({"title": title, "body": body, "head": head, "base": base})
        return pr


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True
    ns = _load_ns()

    def _patch_clone(monkey_repo_git):
        import git as real_git
        def _clone(url, path):
            # Materialize the "changed" files so the syntax gate has real paths to check.
            import os as _os
            for rel in monkey_repo_git._changed:
                full = _os.path.join(path, rel)
                _os.makedirs(_os.path.dirname(full), exist_ok=True)
                open(full, "w").close()
            return monkey_repo_git
        real_git.Repo.clone_from = staticmethod(_clone)

    config = {"GITHUB_TOKEN": "tok", "feature_require_docs": True, "default_branch": "main"}
    classify_build = {"verdict": "build", "skill": "add-webui-control", "reason": "safe bolt-on", "confidence": 0.9}

    # ── no skill resolved -> refuses, flags, never clones ───────────────────
    ns["_store"].clear()
    issue = _FakeGhIssue()
    repo = _FakeGhRepo()
    ok_result, msg = ns["build_feature"](None, repo, issue, {**classify_build, "skill": None}, config)
    ok &= _check("no skill resolved -> refuses to build", ok_result is False)
    ok &= _check("no skill resolved -> flags (needs-human label applied)",
                "ab-needs-human" in issue.added_labels)
    ok &= _check("no skill resolved -> status recorded as feature_flagged",
                ns["_store"]["owner/repo:42"]["status"] == "feature_flagged")

    # ── no claude_cli slot -> hands off to the standard fix pipeline ───────
    ns["_store"].clear()
    issue = _FakeGhIssue()
    repo = _FakeGhRepo()
    ns["_candidates_holder"]["candidates"] = []
    ok_result, msg = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("no claude_cli slot -> hands off to process_single_issue",
                ns["_handoffs"] == [("owner/repo", 42)] and ok_result is True)
    rec = ns["_store"]["owner/repo:42"]
    ok &= _check("hand-off record names the pipeline and why",
                rec.get("pipeline") == "fix_engine" and "claude_cli" in rec.get("handoff_reason", ""))
    ok &= _check("hand-off does not flag for a human", "ab-needs-human" not in issue.added_labels)

    # ── ...unless the fallback is turned off -> refuses, flags ─────────────
    ns["_store"].clear()
    issue = _FakeGhIssue()
    ok_result, msg = ns["build_feature"](None, repo, issue, classify_build,
                                         {**config, "feature_build_fix_pipeline_fallback": False})
    ok &= _check("fallback off + no claude_cli slot -> refuses to build", ok_result is False)
    ok &= _check("fallback off + no claude_cli slot -> flags",
                ns["_store"]["owner/repo:42"]["status"] == "feature_flagged")
    ns["_candidates_holder"]["candidates"] = [{
        "key": ("claude_cli", "", "claude"), "provider": "claude_cli", "model": "claude",
        "base_url": "", "api_key": "", "rpm": 0, "available": True, "unavailable_reason": None,
        "caps": {"supports_mutating_agent": True, "native_agentic_tools": True,
                "supports_structured_output": True, "cost_tier": "cheap", "max_complexity": "large",
                "context_window": 200000},
    }]

    # ── skill has a name but no loaded instructions -> refuses, flags ───────
    ns["_store"].clear()
    issue = _FakeGhIssue()
    repo = _FakeGhRepo()
    ok_result, msg = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("skill instructions unavailable -> refuses to build", ok_result is False)
    ok &= _check("skill instructions unavailable -> flags",
                ns["_store"]["owner/repo:42"]["status"] == "feature_flagged")

    # From here on, give the skill real instructions so build actually proceeds.
    ns["skills_loader"]._instructions["add-webui-control"] = "# Add a webui control\n1. Do the thing."

    # ── agent makes no changes -> feature_failed, no PR ─────────────────────
    ns["_store"].clear()
    issue = _FakeGhIssue()
    repo = _FakeGhRepo()
    fake_repo_git = _FakeRepoGit(changed=[])
    _patch_clone(fake_repo_git)
    ns["_llm_holder"]["responses"] = ['{"pr_body": "did nothing"}']
    ok_result, msg = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("agent makes no changes -> build fails cleanly", ok_result is False)
    ok &= _check("agent makes no changes -> status is feature_failed",
                ns["_store"]["owner/repo:42"]["status"] == "feature_failed")
    ok &= _check("agent makes no changes -> no PR created", repo.created_prs == [])
    ok &= _check("agent makes no changes -> no commit made", fake_repo_git.commits == [])

    # ── docs gate: no docs touched even after one corrective turn -> flags ──
    ns["_store"].clear()
    issue = _FakeGhIssue()
    repo = _FakeGhRepo()
    fake_repo_git = _FakeRepoGit(changed=["templates/index.html", "routes.py"])  # no docs/*.md
    _patch_clone(fake_repo_git)
    ns["_llm_holder"]["calls"] = []
    ns["_llm_holder"]["responses"] = ['{"pr_body": "built it"}']  # same response both turns
    ok_result, msg = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("no docs touched (even after corrective turn) -> build refuses", ok_result is False)
    ok &= _check("no docs touched -> flags, does NOT report feature_failed",
                ns["_store"]["owner/repo:42"]["status"] == "feature_flagged")
    ok &= _check("no docs touched -> NO PR was opened", repo.created_prs == [])
    ok &= _check("no docs touched -> the agent WAS given a second (corrective) turn",
                len(ns["_llm_holder"]["calls"]) == 2)
    ok &= _check("the corrective turn's prompt names the missing docs gap",
                "docs/*.md" in ns["_llm_holder"]["calls"][1]["args"][0])

    # ── docs touched from the start -> full commit/push/PR path ─────────────
    ns["_store"].clear()
    issue = _FakeGhIssue(number=7, title="Add a clear-dongles button")
    repo = _FakeGhRepo()
    fake_repo_git = _FakeRepoGit(changed=["templates/index.html", "docs/pxmx.md"])
    _patch_clone(fake_repo_git)
    ns["_llm_holder"]["responses"] = ['{"pr_body": "Added the button and docs.", '
                                      '"touchpoints_done": ["route", "button"], "touchpoints_skipped": []}']
    ns["_llm_holder"]["calls"] = []
    ns["_pr_holder"]["existing"] = None
    ok_result, pr_url = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("docs present from the start -> build succeeds", ok_result is True)
    ok &= _check("build succeeds -> exactly one commit made", len(fake_repo_git.commits) == 1)
    _first_call_kwargs = ns["_llm_holder"]["calls"][0]["kwargs"]
    ok &= _check("build agent call uses requirements= (not force_provider=)",
                "requirements" in _first_call_kwargs and "force_provider" not in _first_call_kwargs)
    _build_reqs = _first_call_kwargs.get("requirements")
    ok &= _check("build agent requirements: complexity='large', needs_mutating_agent=True",
                _build_reqs is not None and _build_reqs.complexity == "large"
                and _build_reqs.needs_mutating_agent is True)
    ok &= _check("commit message references the right issue number",
                "AI Feature #7" in fake_repo_git.commits[0])
    ok &= _check("branch created is ai-feature/<N>-<slug> (load-bearing for pr_review's own-PR skip)",
                fake_repo_git.current_branch == "ai-feature/7-add-a-clear-dongles-button")
    ok &= _check("push happened on that branch", fake_repo_git.remotes.origin.pushed == [("ai-feature/7-add-a-clear-dongles-button", True)])
    ok &= _check("token was stripped back out after push (last set_url is the plain clone_url)",
                fake_repo_git.remotes.origin.urls[-1] == repo.clone_url)
    ok &= _check("exactly one PR was created", len(repo.created_prs) == 1)
    ok &= _check("PR title is 'AI Feature #N: ...' (NOT 'AI Fix #N' — pr_review's own-PR skip must not match)",
                repo.created_prs[0]["title"].startswith("AI Feature #7"))
    ok &= _check("PR body carries the feature-drive marker",
                "<!-- ab-feature-drive: owner/repo#7 -->" in repo.created_prs[0]["body"])
    ok &= _check("PR body's file list comes from the REAL git diff, not agent self-report",
                "docs/pxmx.md" in repo.created_prs[0]["body"] and "templates/index.html" in repo.created_prs[0]["body"])
    ok &= _check("PR gets the feature-drive label", "ab-feature-drive" in repo.ensured_labels)
    ok &= _check("processed status is feature_built with the PR url",
                ns["_store"]["owner/repo:7"]["status"] == "feature_built"
                and ns["_store"]["owner/repo:7"]["pr_url"] == pr_url)

    # ── an existing open PR is reused, not duplicated ───────────────────────
    ns["_store"].clear()
    issue = _FakeGhIssue(number=8)
    repo = _FakeGhRepo()
    fake_repo_git = _FakeRepoGit(changed=["docs/x.md", "y.py"])
    _patch_clone(fake_repo_git)
    ns["_llm_holder"]["responses"] = ['{"pr_body": "x"}']
    existing = _FakePR(number=55)
    ns["_pr_holder"]["existing"] = existing
    ok_result, pr_url = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("an existing open PR is reused rather than creating a duplicate",
                repo.created_prs == [] and pr_url == existing.html_url)
    ns["_pr_holder"]["existing"] = None

    # ── malformed agent JSON never crashes the build (ground-truth diff still used) ─
    ns["_store"].clear()
    issue = _FakeGhIssue(number=9)
    repo = _FakeGhRepo()
    fake_repo_git = _FakeRepoGit(changed=["docs/x.md", "y.py"])
    _patch_clone(fake_repo_git)
    ns["_llm_holder"]["responses"] = ["not valid json at all, just prose"]
    ok_result, pr_url = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("malformed agent JSON doesn't crash the build", ok_result is True)
    ok &= _check("malformed agent JSON still produces a PR using the real diff",
                len(repo.created_prs) == 1 and "docs/x.md" in repo.created_prs[0]["body"])

    # ── Copilot-only deployment (no claude_cli): tool-calling build agent ───
    _copilot = {
        "key": ("copilot", "https://api.githubcopilot.com", "gpt-6-astra"), "provider": "copilot",
        "model": "gpt-6-astra", "base_url": "https://api.githubcopilot.com", "api_key": "k", "rpm": 0,
        "available": True, "unavailable_reason": None,
        "caps": {"supports_tools": True, "supports_structured_output": False, "cost_tier": "cheap",
                 "max_complexity": "large", "context_window": 64000, "capability_rank": 98},
    }
    saved_candidates = ns["_candidates_holder"]["candidates"]
    ns["_candidates_holder"]["candidates"] = [_copilot]
    g = ns["_guard"]

    def _reset_guard(review=None, verify=None, prepare=None, syntax=None):
        g["review"] = review or [{"verdict": "Approve", "confidence": 0.9, "critique": ""}]
        g["verify"] = verify or [(True, None)]
        g["prepare"] = prepare or [None]
        g["syntax"] = syntax or [{"ok": True}]
        g["review_calls"].clear()
        g["verify_calls"].clear()

    def _run(number, changed=("webui/sim-views.js", "docs/cs.md"), cfg=None):
        ns["_store"].clear()
        ns["_handoffs"].clear()
        issue = _FakeGhIssue(number=number)
        repo = _FakeGhRepo()
        frg = _FakeRepoGit(changed=list(changed))
        _patch_clone(frg)
        ns["_llm_holder"]["calls"] = []
        ns["_llm_holder"]["responses"] = ['{"pr_body": "built"}']
        res = ns["build_feature"](None, repo, issue, classify_build, cfg or config)
        return res, issue, repo, frg, ns["_store"].get(f"owner/repo:{number}") or {}

    _reset_guard()
    (ok_result, pr_url), issue, repo, frg, rec = _run(20)
    ok &= _check("copilot-only: builds instead of handing off to the fix pipeline",
                ok_result is True and ns["_handoffs"] == [] and len(repo.created_prs) == 1)
    kw = ns["_llm_holder"]["calls"][0]["kwargs"]
    reqs = kw.get("requirements")
    ok &= _check("copilot-only: requirements need tools, NOT a mutating (claude_cli) agent",
                reqs.needs_tools is True and reqs.needs_mutating_agent is False)
    ok &= _check("copilot-only: profile='build' on the checkout (llm_client runs the mutating tool loop)",
                kw.get("profile") == "build" and kw.get("enable_native_tools") is True
                and bool(kw.get("repo_checkout_path")))
    ok &= _check("copilot-only: tool-agent system prompt is used",
                kw.get("system_prompt") == ns["_BUILD_SYSTEM_TOOLS"])
    ok &= _check("guardrails ran before the PR: reviewer panel + repository tests",
                len(g["review_calls"]) == 1 and g["verify_calls"] == ["owner/repo"])
    ok &= _check("record carries build mode and guardrail trail",
                rec.get("status") == "feature_built" and rec.get("build_mode") == "tool_agent"
                and [r["gate"] for r in rec.get("guardrail_rounds")] == ["passed"])
    ok &= _check("PR body states the pre-PR checks that passed",
                "skeptical reviewer panel approved" in repo.created_prs[0]["body"])

    # reviewer rejects once -> critique fed back to the agent -> approved -> PR
    _reset_guard(review=[{"verdict": "Reject", "confidence": 0.3, "critique": "select pasted into csUOField"},
                         {"verdict": "Approve", "confidence": 0.85, "critique": ""}])
    (ok_result, _), issue, repo, frg, rec = _run(21)
    calls = ns["_llm_holder"]["calls"]
    ok &= _check("review reject -> agent gets a correction turn with the critique",
                len(calls) == 2 and "select pasted into csUOField" in calls[1]["args"][0]
                and "skeptical reviewer panel" in calls[1]["args"][0])
    ok &= _check("review reject then approve -> PR opened, two guardrail rounds recorded",
                ok_result is True and len(repo.created_prs) == 1
                and [r["gate"] for r in rec["guardrail_rounds"]] == ["review_rejected", "passed"])

    # reviewer keeps rejecting -> flagged guardrails_failed, NO PR
    _reset_guard(review=[{"verdict": "Reject", "confidence": 0.2, "critique": "still wrong"}])
    (ok_result, _), issue, repo, frg, rec = _run(22, cfg={**config, "feature_build_correction_rounds": 2})
    ok &= _check("persistent rejection -> no PR, flagged guardrails_failed",
                ok_result is False and repo.created_prs == [] and rec.get("status") == "feature_flagged"
                and rec.get("flag_kind") == "guardrails_failed" and rec.get("failure_gate") == "review_rejected")
    ok &= _check("persistent rejection -> exactly N correction turns (1 build + 2 corrections)",
                len(ns["_llm_holder"]["calls"]) == 3 and len(rec.get("guardrail_rounds")) == 3)
    ok &= _check("persistent rejection -> reason quotes the last critique",
                "still wrong" in rec.get("reason", ""))
    ok &= _check("persistent rejection -> nothing committed or pushed",
                frg.commits == [] and frg.remotes.origin.pushed == [])

    # tests fail -> test output fed back
    _reset_guard(verify=[(False, "FAILED tests/test_x.py::test_y - AssertionError"), (True, None)])
    (ok_result, _), issue, repo, frg, rec = _run(23)
    ok &= _check("test failure -> agent gets the failing test output",
                ok_result is True and "FAILED tests/test_x.py::test_y" in ns["_llm_holder"]["calls"][1]["args"][0])

    # syntax error -> fed back BEFORE the panel sees it
    _reset_guard(syntax=[{"ok": False, "checker": "node --check", "output": "SyntaxError: Unexpected token"},
                         {"ok": True}])
    (ok_result, _), issue, repo, frg, rec = _run(24)
    ok &= _check("syntax error -> corrected before review (panel only sees the fixed build)",
                ok_result is True and "Unexpected token" in ns["_llm_holder"]["calls"][1]["args"][0]
                and rec["guardrail_rounds"][0]["gate"] == "syntax")

    # reviewers unavailable -> flagged (not the code's fault), no correction turns
    _reset_guard(review=[{"status": "queue_for_retry", "reason": "all_reviewers_failed: x"}])
    (ok_result, _), issue, repo, frg, rec = _run(25)
    ok &= _check("reviewers unavailable -> flagged review_unavailable, no PR, no correction turn",
                ok_result is False and repo.created_prs == [] and rec.get("flag_kind") == "review_unavailable"
                and len(ns["_llm_holder"]["calls"]) == 1)

    # tool agent disabled by config -> back to the fix-pipeline hand-off
    _reset_guard()
    (ok_result, _), issue, repo, frg, rec = _run(26, cfg={**config, "feature_build_tool_agent": False})
    ok &= _check("feature_build_tool_agent=False -> hands off to the fix pipeline",
                ns["_handoffs"] == [("owner/repo", 26)])
    ns["_candidates_holder"]["candidates"] = saved_candidates
    _reset_guard()

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature_build orchestration self-test...")
    sys.exit(main())
