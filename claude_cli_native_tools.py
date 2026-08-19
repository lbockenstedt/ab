"""claude_cli_native_tools.py — standalone (no app/main import) command-
building and response-parsing helpers for llm_client._request_claude_cli's
claude_cli native-tool-access mode.

Split out so it's unit-testable in isolation: llm_client.py imports `main`
at module level (a circular-import chain — main -> app_state -> main — that
only resolves inside the running app, never a standalone script), the same
constraint pr_review_retry.py / check_unattended_mutation.py /
attr_definition_lookup.py were built standalone to work around.

Two PROFILES, selected by the caller, never mixed:

- ``profile="readonly"`` (the default, unchanged since this module's original
  ship): read-only exploration + narrow git-history inspection ONLY.
  Deliberately excludes Edit/Write and every mutating Bash command (git push/
  commit/checkout/reset/clean, rm, ...) — a claude_cli call using this
  profile is always a REVIEWER/investigator, never a mutator, so the
  allow/deny lists are defense in depth even under --permission-mode
  bypassPermissions (which only means "don't prompt for the tools that ARE
  enabled", not "enable everything"). ``TOOL_CATEGORIES``/``ALLOWED_TOOLS``/
  ``DISALLOWED_TOOLS`` back this profile and MUST NOT be edited to add
  mutating capability — that would silently turn every existing reviewer
  call (PR pre-review, fix-generation exploration) into a mutator. Add new
  capability to the BUILD constants below instead.

- ``profile="build"`` (feature auto-drive's mutating agent ONLY): adds
  Edit/Write/Skill on top of the readonly categories, for one specific,
  narrowly-scoped caller — ab/feature_build.py's agentic feature
  builder, which runs in a throwaway temp checkout (never a live working
  tree, never /opt/ab). Even in this profile, git commit/push/reset/
  clean/checkout stay DENIED: the agent only edits the working tree;
  AppBuilder's own code does add/commit/push via GitPython afterwards, so
  branch naming, commit message, and "what actually changed" stay under
  AppBuilder's control rather than the agent's self-report. ``BUILD_*``
  constants back this profile and are a SEPARATE object from the readonly
  ones on purpose (see test_claude_cli_build_profile.py's identity checks).
"""
import json

SEARCH_AGENT_NAME = "searcher"
DEFAULT_SEARCH_MODEL = "haiku"  # cheapest tier — mechanical grep/file-lookup only

# --- readonly profile (default, unchanged) ---------------------------------
TOOL_CATEGORIES = "Read,Grep,Glob,Bash,Task"
ALLOWED_TOOLS = ["Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git blame:*)"]
DISALLOWED_TOOLS = ["Edit", "Write", "Bash(rm:*)", "Bash(git push:*)", "Bash(git commit:*)",
                    "Bash(git checkout:*)", "Bash(git reset:*)", "Bash(git clean:*)",
                    "WebFetch", "WebSearch"]

# --- build profile (feature_build.py's mutating agent ONLY) ----------------
# Adds Edit/Write/Skill; still denies every history-rewriting/publishing git
# op (AppBuilder commits+pushes itself, never the agent) plus rm/sudo/curl/pip
# install/WebFetch/WebSearch (no network egress, no privilege escalation, no
# destructive shell from an agent operating on a temp checkout).
BUILD_TOOL_CATEGORIES = "Read,Grep,Glob,Bash,Task,Edit,Write,Skill"
BUILD_ALLOWED_TOOLS = ["Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
                       "Bash(git blame:*)", "Bash(git status:*)",
                       "Bash(python3 -m py_compile:*)", "Bash(bash -n:*)"]
BUILD_DISALLOWED_TOOLS = ["Bash(rm:*)", "Bash(git push:*)", "Bash(git commit:*)",
                          "Bash(git checkout:*)", "Bash(git reset:*)", "Bash(git clean:*)",
                          "Bash(sudo:*)", "Bash(curl:*)", "Bash(pip install:*)",
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
                  enable_native_tools=False, search_model=None, profile="readonly",
                  extra_add_dirs=None):
    """Pure command-list builder for the `claude` CLI invocation — no
    subprocess execution, no filesystem access. See module docstring for the
    tool-scope rationale.

    ``profile`` selects which allow/deny lists back the tool-access surface
    and is only consulted when ``enable_native_tools=True`` (a plain call has
    no tools block regardless of profile — same as before this parameter
    existed). Deliberately a NEW, separate parameter rather than overloading
    ``enable_native_tools``'s truthiness — that flag's meaning ("read-only
    native tools are on") must stay stable for every existing reviewer call
    site; profile is the only thing that can widen it to mutating.

    ``profile="build"`` REQUIRES ``repo_checkout_path`` — a mutating agent
    with no directory scoped via --add-dir is a configuration bug, not a
    degraded-but-safe mode, so this raises rather than silently running
    unscoped.

    ``extra_add_dirs`` (build profile only) adds further --add-dir entries
    AFTER the checkout — feature_build.py uses exactly one, a sibling temp
    directory holding the chosen skill's materialized SKILL.md/reference.md,
    so the agent can Read the full recipe on demand without it all burning
    prompt budget up front. Ignored when repo_checkout_path itself is falsy
    (no checkout means no tool-access surface to extend)."""
    if profile not in ("readonly", "build"):
        raise ValueError(f"unknown claude_cli profile: {profile!r}")
    if profile == "build" and not repo_checkout_path:
        raise ValueError("profile='build' requires an explicit repo_checkout_path")

    cmd = [claude_bin, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if enable_native_tools:
        if profile == "build":
            categories, allowed, disallowed = BUILD_TOOL_CATEGORIES, BUILD_ALLOWED_TOOLS, BUILD_DISALLOWED_TOOLS
        else:
            categories, allowed, disallowed = TOOL_CATEGORIES, ALLOWED_TOOLS, DISALLOWED_TOOLS
        cmd += ["--tools", categories]
        cmd += ["--allowedTools"] + allowed
        cmd += ["--disallowedTools"] + disallowed
        cmd += ["--permission-mode", "bypassPermissions"]
        cmd += ["--agents", search_agent_json(search_model)]
        if repo_checkout_path:
            cmd += ["--add-dir", repo_checkout_path]
            for extra_dir in (extra_add_dirs or []):
                cmd += ["--add-dir", extra_dir]
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
