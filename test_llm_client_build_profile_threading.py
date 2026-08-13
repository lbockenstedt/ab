#!/usr/bin/env python3
"""Self-test that `profile=` actually reaches claude_cli_native_tools.build_command
through the dispatch layer, not just that build_command itself accepts it.

Run:  python3 bugfixer/test_llm_client_build_profile_threading.py

llm_client.py imports `main` (app-init side effects — see
test_skills_loader.py's docstring for why direct import is unsafe here), so
this extracts _call_provider by source via ast and execs it with a stubbed
_request_claude_cli that just records what it was called with.

Regression guard: claude_cli_native_tools.build_command's "build" profile
(test_claude_cli_build_profile.py) is USELESS if nothing upstream ever passes
profile="build" to it — this is the plumbing check for that link. The plan
flagged "a mis-scoped call turns every reviewer call into a mutator" as the
single riskiest step in the whole feature-auto-drive project; this pins the
opposite failure mode too (profile SILENTLY DROPPED somewhere in the dispatch
chain, so feature_build.py THINKS it's requesting the mutating profile but
claude_cli actually runs read-only and every build silently no-ops)."""
import ast
import sys


def _load_ns():
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_call_provider":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "_call_provider not found in llm_client.py"

    calls = []

    def _request_claude_cli(model, messages, task_id, config, repo_checkout_path=None,
                            json_schema=None, enable_native_tools=False, search_model=None,
                            profile="readonly", extra_add_dirs=None):
        calls.append({"profile": profile, "enable_native_tools": enable_native_tools,
                      "repo_checkout_path": repo_checkout_path, "extra_add_dirs": extra_add_dirs})
        return "stub result"

    # Every OTHER provider branch _call_provider can reach — none of these
    # are under test here, just present so the dispatch doesn't NameError
    # if a test case accidentally routes somewhere unexpected.
    def _unexpected(*a, **k):
        raise AssertionError("_call_provider routed to a non-claude_cli provider unexpectedly")

    def _is_copilot(p):
        return p == "copilot"

    def _is_ollama(p):
        return p.startswith("ollama")

    def _is_lmstudio(p):
        return p == "lmstudio"

    def _normalize_lmstudio_url(u):
        return u

    ns = {
        "_request_claude_cli": _request_claude_cli,
        "_request_copilot": _unexpected, "_request_anthropic": _unexpected,
        "_request_google": _unexpected, "_request_ollama": _unexpected,
        "_request_openai": _unexpected,
        "_is_copilot": _is_copilot, "_is_ollama": _is_ollama, "_is_lmstudio": _is_lmstudio,
        "_normalize_lmstudio_url": _normalize_lmstudio_url,
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1", "OPENROUTER_HEADERS": {},
    }
    exec(seg, ns)
    ns["_calls"] = calls
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True
    ns = _load_ns()

    # ── default call (no profile kwarg passed) reaches claude_cli as "readonly" ─
    ns["_call_provider"]("claude_cli", "sonnet", None, None, [], None, False, None, {},
                         enable_native_tools=True, repo_checkout_path="/tmp/r")
    ok &= _check("a default call reaches _request_claude_cli with profile='readonly'",
                ns["_calls"][-1]["profile"] == "readonly")

    # ── explicit profile="build" reaches claude_cli, NOT silently dropped ───
    ns["_call_provider"]("claude_cli", "sonnet", None, None, [], None, False, None, {},
                         enable_native_tools=True, repo_checkout_path="/tmp/r", profile="build",
                         extra_add_dirs=["/tmp/skills"])
    ok &= _check("profile='build' reaches _request_claude_cli unchanged",
                ns["_calls"][-1]["profile"] == "build")
    ok &= _check("repo_checkout_path is still forwarded alongside profile",
                ns["_calls"][-1]["repo_checkout_path"] == "/tmp/r")
    ok &= _check("extra_add_dirs (the materialized skill directory) is forwarded too",
                ns["_calls"][-1]["extra_add_dirs"] == ["/tmp/skills"])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running llm_client build-profile threading self-test...")
    sys.exit(main())
