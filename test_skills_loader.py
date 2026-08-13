#!/usr/bin/env python3
"""Self-test for skills_loader.py's skill_files()/skill_instructions() —
added so feature_build.py's agentic builder can inject a chosen skill's full
recipe into its prompt (skills_context() only exposes names+descriptions,
enough to PICK a skill but not enough to follow it).

Run:  python3 bugfixer/test_skills_loader.py

NOT a direct import: skills_loader.py's `from main import logger` is wrapped
in try/except, but in this checkout main.py actually IMPORTS CLEANLY (no
ImportError — it's the module-level app-init SIDE EFFECTS that are the real
problem, matching every other module in this repo that needs main). A first
version of this test imported skills_loader directly and it silently booted
the whole app in-process (worker threads, scheduler, a self-update check) as
a side effect just to test two pure functions — exactly the trap the rest of
this repo's tests use ast-extraction to avoid. This extracts skill_files/
skill_instructions by source instead, execs them into a stub namespace, and
never imports skills_loader (or main) at all.
"""
import ast
import sys


def _load_ns():
    src = open("skills_loader.py").read()
    tree = ast.parse(src)
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ("skill_files", "skill_instructions"):
            segs.append(ast.get_source_segment(src, node))
    ns = {"_CACHE": {"skills": {}}}
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


def main():
    print("Running bugfixer skills_loader self-test...")
    ok = True
    ns = _load_ns()

    ns["_CACHE"]["skills"] = {
        "add-simulation": {
            "name": "add-simulation",
            "description": "Use when adding a new client traffic-simulation.",
            "instructions": "# Add a Simulation\n\nThe shape (in order)...",
            "reference": "# reference\n\n1 — sim files\n...",
        },
        "docs-only": {
            "name": "docs-only",
            "description": "A skill with only a SKILL.md, no reference.md.",
            "instructions": "# Docs Only\n\nJust this file.",
            "reference": "",
        },
    }

    # --- skill_files ---------------------------------------------------

    files = ns["skill_files"]("add-simulation")
    ok &= _check("skill_files returns both SKILL.md and reference.md when both present",
                set(files.keys()) == {"SKILL.md", "reference.md"})
    ok &= _check("SKILL.md content matches the cached instructions",
                files["SKILL.md"] == "# Add a Simulation\n\nThe shape (in order)...")
    ok &= _check("reference.md content matches the cached reference",
                files["reference.md"] == "# reference\n\n1 — sim files\n...")

    docs_only_files = ns["skill_files"]("docs-only")
    ok &= _check("skill_files omits reference.md when the skill has none",
                set(docs_only_files.keys()) == {"SKILL.md"})

    ok &= _check("skill_files on an unloaded skill returns {}",
                ns["skill_files"]("nonexistent") == {})

    # --- skill_instructions ----------------------------------------------

    combined = ns["skill_instructions"]("add-simulation")
    ok &= _check("skill_instructions includes the SKILL.md content",
                "# Add a Simulation" in combined)
    ok &= _check("skill_instructions includes the reference.md content, labeled",
                "--- reference.md ---" in combined and "1 — sim files" in combined)
    ok &= _check("SKILL.md content comes before reference.md content",
                combined.index("# Add a Simulation") < combined.index("--- reference.md ---"))

    docs_only_combined = ns["skill_instructions"]("docs-only")
    ok &= _check("skill_instructions with no reference.md has no reference marker",
                "--- reference.md ---" not in docs_only_combined
                and "# Docs Only" in docs_only_combined)

    ok &= _check("skill_instructions on an unloaded skill returns empty string",
                ns["skill_instructions"]("nonexistent") == "")

    ns["_CACHE"]["skills"]["long"] = {
        "name": "long", "description": "", "instructions": "x" * 100, "reference": "",
    }
    ok &= _check("skill_instructions respects max_chars",
                len(ns["skill_instructions"]("long", max_chars=10)) == 10)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
