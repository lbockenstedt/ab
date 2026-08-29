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

import feature_boundary as real_feature_boundary


def _load_ns():
    src = open("feature_build.py").read()
    tree = ast.parse(src)

    segs = []
    want_funcs = {
        "_build_prompt", "_materialize_skill", "_changed_files", "_docs_touched",
        "_non_doc_files", "_run_build_agent", "_mark_failed", "_mark_built",
        "_flag_incomplete", "build_feature",
    }
    want_assigns = {"_FEATURE_DRIVE_MARKER", "_BUILD_JSON_SCHEMA", "_BUILD_SYSTEM", "_DOCS_NUDGE_TMPL"}
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
        "call_llm": call_llm,
        "GithubException": GithubException,
        "git": real_git,  # patched per-test-case via _patch_clone(); real module otherwise
        "os": __import__("os"), "tempfile": __import__("tempfile"),
        "datetime": __import__("datetime").datetime,
        "_BUILD_LOCK": __import__("threading").Lock(),
    }

    # _robust_json_loads: the real one (fix_engine.py) needs `json` + a regex
    # constant in scope — reuse the exact extraction test_feature_drive_
    # classify.py already does, rather than reimplementing it a third time.
    fe_src = open("fix_engine.py").read()
    fe_tree = ast.parse(fe_src)
    for node in fe_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_robust_json_loads":
            segs.insert(0, ast.get_source_segment(fe_src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_JSON_BAD_ESCAPE_RE":
                    segs.insert(0, ast.get_source_segment(fe_src, node))
    ns["json"] = __import__("json")
    ns["re"] = __import__("re")

    exec("\n\n".join(segs), ns)
    ns["_store"] = _store
    ns["_candidates_holder"] = _candidates_holder
    ns["_pr_holder"] = _pr_holder
    ns["_llm_holder"] = _llm_holder
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
        real_git.Repo.clone_from = staticmethod(lambda url, path: monkey_repo_git)

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

    # ── no claude_cli slot configured -> refuses, flags ─────────────────────
    ns["_store"].clear()
    issue = _FakeGhIssue()
    repo = _FakeGhRepo()
    ns["_candidates_holder"]["candidates"] = []
    ok_result, msg = ns["build_feature"](None, repo, issue, classify_build, config)
    ok &= _check("no claude_cli slot -> refuses to build", ok_result is False)
    ok &= _check("no claude_cli slot -> flags", ns["_store"]["owner/repo:42"]["status"] == "feature_flagged")
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

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running feature_build orchestration self-test...")
    sys.exit(main())
