#!/usr/bin/env python3
"""Self-test for claude_cli_native_tools.build_command / extract_text.

Run:  python3 bugfixer/test_claude_cli_native_tools.py

Standalone: imports only claude_cli_native_tools (no app/main init — see that
module's docstring for why llm_client.py itself can't be imported outside the
running app).
"""
import json
import sys

import claude_cli_native_tools as cct


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


def main():
    print("Running bugfixer claude_cli_native_tools self-test...")
    ok = True

    # --- build_command --------------------------------------------------

    # (1) Plain legacy call (no native tools, no schema): just the binary +
    # output-format + model, none of the tool-access flags.
    cmd = cct.build_command("claude", model="sonnet")
    ok &= _check("plain call has no --tools flag", "--tools" not in cmd)
    ok &= _check("plain call has no --permission-mode flag",
                "--permission-mode" not in cmd)
    ok &= _check("plain call includes --model sonnet",
                "--model" in cmd and cmd[cmd.index("--model") + 1] == "sonnet")

    # (2) enable_native_tools=True wires the full read-only tool-access
    # surface: categories, allow/deny lists, bypassPermissions, the search
    # subagent definition.
    cmd = cct.build_command("claude", enable_native_tools=True)
    ok &= _check("native-tools call sets --tools to the category string",
                "--tools" in cmd and cmd[cmd.index("--tools") + 1] == cct.TOOL_CATEGORIES)
    ok &= _check("native-tools call sets bypassPermissions",
                "--permission-mode" in cmd
                and cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions")
    ok &= _check("native-tools call includes --agents with the search agent",
                "--agents" in cmd
                and cct.SEARCH_AGENT_NAME in cmd[cmd.index("--agents") + 1])
    ok &= _check("native-tools call has no --add-dir without a repo_checkout_path",
                "--add-dir" not in cmd)

    # (3) repo_checkout_path only takes effect when native tools are enabled
    # (a plain call has no real use for filesystem scoping).
    cmd = cct.build_command("claude", repo_checkout_path="/tmp/repo")
    ok &= _check("repo_checkout_path alone (no native tools) adds no --add-dir",
                "--add-dir" not in cmd)
    cmd = cct.build_command("claude", enable_native_tools=True, repo_checkout_path="/tmp/repo")
    ok &= _check("repo_checkout_path + native tools adds --add-dir <path>",
                "--add-dir" in cmd and cmd[cmd.index("--add-dir") + 1] == "/tmp/repo")

    # (4) json_schema accepts both a dict (serialized) and a pre-serialized
    # string (passed through untouched), independent of native tools.
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    cmd = cct.build_command("claude", json_schema=schema)
    ok &= _check("dict json_schema is serialized into --json-schema",
                "--json-schema" in cmd
                and json.loads(cmd[cmd.index("--json-schema") + 1]) == schema)
    raw = json.dumps(schema)
    cmd = cct.build_command("claude", json_schema=raw)
    ok &= _check("pre-serialized string json_schema passed through untouched",
                cmd[cmd.index("--json-schema") + 1] == raw)

    # (5) search_model overrides the default (haiku) inside the --agents def.
    cmd = cct.build_command("claude", enable_native_tools=True, search_model="opus")
    agents_json = cmd[cmd.index("--agents") + 1]
    ok &= _check("search_model override lands in the --agents definition",
                json.loads(agents_json)[cct.SEARCH_AGENT_NAME]["model"] == "opus")
    cmd = cct.build_command("claude", enable_native_tools=True)
    agents_json = cmd[cmd.index("--agents") + 1]
    ok &= _check("no search_model falls back to DEFAULT_SEARCH_MODEL",
                json.loads(agents_json)[cct.SEARCH_AGENT_NAME]["model"] == cct.DEFAULT_SEARCH_MODEL)

    # (6) The allow/deny lists are the real safety boundary — confirm the
    # mutating operations are explicitly denied and the narrow git-history
    # reads are explicitly allowed.
    ok &= _check("Edit is disallowed", "Edit" in cct.DISALLOWED_TOOLS)
    ok &= _check("Write is disallowed", "Write" in cct.DISALLOWED_TOOLS)
    ok &= _check("git commit is disallowed", "Bash(git commit:*)" in cct.DISALLOWED_TOOLS)
    ok &= _check("git log is allowed", "Bash(git log:*)" in cct.ALLOWED_TOOLS)
    ok &= _check("git diff is allowed", "Bash(git diff:*)" in cct.ALLOWED_TOOLS)

    # --- extract_text ------------------------------------------------------

    # (7) With a schema, structured_output wins over a (possibly messy)
    # freeform result field.
    ok &= _check(
        "schema call prefers structured_output over result",
        cct.extract_text({"structured_output": {"a": 1}, "result": "ignored"},
                         json_schema=schema) == json.dumps({"a": 1}))

    # (8) No schema: falls back to result, then text, in that order.
    ok &= _check("no schema falls back to result",
                cct.extract_text({"result": "hello"}) == "hello")
    ok &= _check("no schema falls back to text when result is absent",
                cct.extract_text({"text": "hi"}) == "hi")
    ok &= _check("result wins over text when both present",
                cct.extract_text({"result": "r", "text": "t"}) == "r")

    # (9) Schema requested but structured_output missing (e.g. the CLI
    # ignored --json-schema) — falls back to freeform fields, not empty.
    ok &= _check(
        "schema requested but structured_output missing falls back to result",
        cct.extract_text({"result": "fallback"}, json_schema=schema) == "fallback")

    # (10) Nothing usable at all — empty string, not an exception.
    ok &= _check("empty dict yields empty string", cct.extract_text({}) == "")
    ok &= _check("non-dict input yields empty string", cct.extract_text(None) == "")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
