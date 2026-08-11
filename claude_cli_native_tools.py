"""claude_cli_native_tools.py — standalone (no app/main import) command-
building and response-parsing helpers for llm_client._request_claude_cli's
claude_cli native-tool-access mode.

Split out so it's unit-testable in isolation: llm_client.py imports `main`
at module level (a circular-import chain — main -> app_state -> main — that
only resolves inside the running app, never a standalone script), the same
constraint pr_review_retry.py / check_unattended_mutation.py /
attr_definition_lookup.py were built standalone to work around.

Read-only exploration + narrow git-history inspection ONLY. Deliberately
excludes Edit/Write and every mutating Bash command (git push/commit/
checkout/reset/clean, rm, ...) — a claude_cli call using this mode is always
a REVIEWER/investigator, never a mutator, so the allow/deny lists are
defense in depth even under --permission-mode bypassPermissions (which only
means "don't prompt for the tools that ARE enabled", not "enable
everything").
"""
import json

SEARCH_AGENT_NAME = "searcher"
DEFAULT_SEARCH_MODEL = "haiku"  # cheapest tier — mechanical grep/file-lookup only
TOOL_CATEGORIES = "Read,Grep,Glob,Bash,Task"
ALLOWED_TOOLS = ["Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git blame:*)"]
DISALLOWED_TOOLS = ["Edit", "Write", "Bash(rm:*)", "Bash(git push:*)", "Bash(git commit:*)",
                    "Bash(git checkout:*)", "Bash(git reset:*)", "Bash(git clean:*)",
                    "WebFetch", "WebSearch"]


def search_agent_json(search_model=None):
    """The --agents definition for the cheap-model search subagent: the
    top-level (usually pricier) session delegates mechanical grep/file-
    hunting to this agent instead of spending its own tokens on it — the
    same cost-tiering pattern used elsewhere for research subagents."""
    return json.dumps({
        SEARCH_AGENT_NAME: {
            "description": "Fast file/grep/glob search specialist. Delegate any "
                           "\"find X\"/\"where is Y defined\"/\"which files reference Z\" "
                           "task here instead of searching yourself.",
            "prompt": "You locate code via Read/Grep/Glob and report findings "
                     "concisely (file:line + a short excerpt) — you do not "
                     "judge, review, or explain WHY something is a problem, "
                     "only WHERE it is.",
            "tools": "Read,Grep,Glob",
            "model": search_model or DEFAULT_SEARCH_MODEL,
        }
    })


def build_command(claude_bin, model=None, repo_checkout_path=None, json_schema=None,
                  enable_native_tools=False, search_model=None):
    """Pure command-list builder for the `claude` CLI invocation — no
    subprocess execution, no filesystem access. See module docstring for the
    tool-scope rationale."""
    cmd = [claude_bin, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if enable_native_tools:
        cmd += ["--tools", TOOL_CATEGORIES]
        cmd += ["--allowedTools"] + ALLOWED_TOOLS
        cmd += ["--disallowedTools"] + DISALLOWED_TOOLS
        cmd += ["--permission-mode", "bypassPermissions"]
        cmd += ["--agents", search_agent_json(search_model)]
        if repo_checkout_path:
            cmd += ["--add-dir", repo_checkout_path]
    if json_schema:
        cmd += ["--json-schema",
               json_schema if isinstance(json_schema, str) else json.dumps(json_schema)]
    return cmd


def extract_text(data, json_schema=None):
    """From a parsed claude_cli JSON response ``data``, return the text the
    caller should treat as the call's result.

    A schema-validated call returns ``structured_output`` pre-parsed —
    re-serialize IT (guaranteed clean JSON) rather than trusting the
    freeform ``result``/``text`` field, which is where claude_cli's "JSON
    parse failed (Extra data: ...)" errors came from (stray prose/markdown
    around the JSON in a freeform response)."""
    data = data if isinstance(data, dict) else {}
    if json_schema and "structured_output" in data:
        return json.dumps(data["structured_output"])
    return data.get("result") or data.get("text") or ""
