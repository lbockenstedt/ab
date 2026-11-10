#!/usr/bin/env python3
"""Self-test for claude_cli_native_tools' mutating "build" profile.

Run:  python3 ab/test_claude_cli_build_profile.py

Standalone: imports only claude_cli_native_tools (no app/main init).

Regression guard: feature_build.py's agentic feature builder needs Edit/
Write/Skill so it can actually construct a bolt-on feature, but the module's
original profile is documented as "always a REVIEWER/investigator, never a
mutator" and is still relied on by every PR-review / fix-generation call
site. This pins that the new build profile is ADDITIVE — a completely
separate constant set and command shape — and that the pre-existing readonly
profile is byte-for-byte unchanged by its presence (test_claude_cli_native_
tools.py already covers the readonly profile's own behavior end to end; this
file only adds the NEW surface + the separation guarantee)."""
import sys

import claude_cli_native_tools as cct


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


def main():
    print("Running ab claude_cli build-profile self-test...")
    ok = True

    # --- separation: the two constant sets never alias or overlap ----------
    ok &= _check("BUILD_ALLOWED_TOOLS is a distinct object from ALLOWED_TOOLS",
                cct.BUILD_ALLOWED_TOOLS is not cct.ALLOWED_TOOLS)
    ok &= _check("BUILD_DISALLOWED_TOOLS is a distinct object from DISALLOWED_TOOLS",
                cct.BUILD_DISALLOWED_TOOLS is not cct.DISALLOWED_TOOLS)
    ok &= _check("BUILD_TOOL_CATEGORIES is a distinct object from TOOL_CATEGORIES",
                cct.BUILD_TOOL_CATEGORIES is not cct.TOOL_CATEGORIES)

    # --- the readonly deny list is untouched by the build profile existing -
    ok &= _check("readonly profile still denies Edit", "Edit" in cct.DISALLOWED_TOOLS)
    ok &= _check("readonly profile still denies Write", "Write" in cct.DISALLOWED_TOOLS)
    ok &= _check("readonly profile has no Skill category",
                "Skill" not in cct.TOOL_CATEGORIES.split(","))

    # --- the build profile actually grants what the builder needs ----------
    ok &= _check("build categories include Edit", "Edit" in cct.BUILD_TOOL_CATEGORIES.split(","))
    ok &= _check("build categories include Write", "Write" in cct.BUILD_TOOL_CATEGORIES.split(","))
    ok &= _check("build categories include Skill", "Skill" in cct.BUILD_TOOL_CATEGORIES.split(","))

    # --- but the build profile STILL denies history-rewriting/publishing ---
    for op in ("Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)",
              "Bash(git reset:*)", "Bash(git clean:*)"):
        ok &= _check(f"build profile still denies {op}", op in cct.BUILD_DISALLOWED_TOOLS)
    for op in ("Bash(rm:*)", "Bash(sudo:*)", "Bash(curl:*)", "Bash(pip install:*)",
              "WebFetch", "WebSearch"):
        ok &= _check(f"build profile denies {op}", op in cct.BUILD_DISALLOWED_TOOLS)

    # --- build_command: profile plumbing ------------------------------------

    # (1) default profile (no profile= passed) behaves exactly as before —
    # readonly categories, readonly allow/deny lists.
    cmd = cct.build_command("claude", enable_native_tools=True, repo_checkout_path="/tmp/r")
    ok &= _check("default profile uses TOOL_CATEGORIES",
                cmd[cmd.index("--tools") + 1] == cct.TOOL_CATEGORIES)
    ok &= _check("default profile's --allowedTools matches ALLOWED_TOOLS",
                cmd[cmd.index("--allowedTools"):cmd.index("--disallowedTools")][1:] == cct.ALLOWED_TOOLS)

    # (2) explicit profile="readonly" is identical to the default.
    cmd_explicit = cct.build_command("claude", enable_native_tools=True, repo_checkout_path="/tmp/r",
                                     profile="readonly")
    ok &= _check("profile='readonly' produces the same command as the default",
                cmd_explicit == cmd)

    # (3) profile="build" swaps in the build categories/allow/deny lists.
    cmd = cct.build_command("claude", enable_native_tools=True, repo_checkout_path="/tmp/r",
                            profile="build")
    ok &= _check("profile='build' uses BUILD_TOOL_CATEGORIES",
                cmd[cmd.index("--tools") + 1] == cct.BUILD_TOOL_CATEGORIES)
    ok &= _check("profile='build' still adds --add-dir",
                "--add-dir" in cmd and cmd[cmd.index("--add-dir") + 1] == "/tmp/r")
    ok &= _check("profile='build' still sets bypassPermissions",
                cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions")

    # (4) profile="build" WITHOUT a repo_checkout_path raises — a mutating
    # agent with no scoped directory is a bug, not a degraded mode.
    try:
        cct.build_command("claude", enable_native_tools=True, profile="build")
        ok &= _check("profile='build' with no repo_checkout_path raises ValueError", False)
    except ValueError:
        ok &= _check("profile='build' with no repo_checkout_path raises ValueError", True)

    # (5) an unknown profile string raises rather than silently falling back
    # to readonly (which would mask a typo as "it worked, just read-only").
    try:
        cct.build_command("claude", enable_native_tools=True, repo_checkout_path="/tmp/r",
                          profile="bogus")
        ok &= _check("unknown profile raises ValueError", False)
    except ValueError:
        ok &= _check("unknown profile raises ValueError", True)

    # (5b) extra_add_dirs (feature_build.py's materialized skill directory)
    # appends additional --add-dir entries after the primary checkout.
    cmd = cct.build_command("claude", enable_native_tools=True, repo_checkout_path="/tmp/r",
                            profile="build", extra_add_dirs=["/tmp/skills"])
    add_dir_positions = [i for i, x in enumerate(cmd) if x == "--add-dir"]
    ok &= _check("extra_add_dirs adds a SECOND --add-dir flag",
                len(add_dir_positions) == 2)
    ok &= _check("the primary checkout is still the FIRST --add-dir",
                cmd[add_dir_positions[0] + 1] == "/tmp/r")
    ok &= _check("the extra dir is the SECOND --add-dir",
                cmd[add_dir_positions[1] + 1] == "/tmp/skills")
    cmd_no_extra = cct.build_command("claude", enable_native_tools=True, repo_checkout_path="/tmp/r",
                                     profile="build")
    ok &= _check("omitting extra_add_dirs adds no extra --add-dir (backward compatible)",
                cmd_no_extra.count("--add-dir") == 1)

    # (6) enable_native_tools=False means profile is never consulted for the
    # tools block — a plain call stays plain regardless of profile= — EXCEPT
    # the build-profile-needs-a-checkout guard, which is a caller-config
    # error independent of whether native tools ended up wired this call.
    cmd = cct.build_command("claude", enable_native_tools=False, profile="build",
                            repo_checkout_path="/tmp/r")
    ok &= _check("enable_native_tools=False emits no --tools block even with profile='build'",
                "--tools" not in cmd)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
