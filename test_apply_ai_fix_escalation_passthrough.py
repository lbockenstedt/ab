#!/usr/bin/env python3
"""Regression test for apply_ai_fix's exception handling.

fix_engine.py imports `main` (heavy app-init side effects — see
test_llm_client_requirements_path.py's docstring), so this extracts just
apply_ai_fix by source via ast and execs it with stubbed dependencies,
following the established extraction pattern used across the AB test suite.

The bug: apply_ai_fix's tail wrapped EVERY exception from the picker in a
bare `Exception(f"Fix generation failed: {e}")`. That erased the type of
llm_client.LlmHumanEscalationNeeded — the typed signal the fix loop raises
when NO configured model meets a fix's requirements — so the loop's dedicated
`except LlmHumanEscalationNeeded` handler (post a clean "held for human
review" note and stop) never fired. The escalation was mis-handled as a
generic per-attempt provider error, retried up to max_attempts, and finally
dumped onto the issue as a raw, truncated
"No candidate satisfies requirements (reqs=LlmRequirements(complexity='large'…"
repr. This test pins the fix: the typed control exceptions propagate
unchanged, while ordinary errors are still wrapped.

Run:  python3 ab/test_apply_ai_fix_escalation_passthrough.py
"""
import ast
import os
import sys
import tempfile


def _check(label, cond):
    print(("PASS" if cond else "FAIL") + f": {label}")
    return bool(cond)


def _load_apply_ai_fix(call_llm_stub, llm_client_stub):
    src = open("fix_engine.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "apply_ai_fix":
            seg = ast.get_source_segment(src, node)
            break
    if seg is None:
        raise AssertionError("apply_ai_fix not found in fix_engine.py")

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {
        "load_config": lambda: {},
        "CHAT_CONFIG_DEFAULTS": {
            "FIX_MAX_FILES": 10,
            "FIX_MAX_FILE_CHARS": 10000,
            "FIX_MAX_CONTEXT_CHARS": 50000,
        },
        "identify_files_to_fix": lambda *a, **k: [],
        "_issue_identifiers": lambda *a, **k: [],
        "os": os,
        "logger": _NoLog(),
        "call_llm": call_llm_stub,
        "llm_client": llm_client_stub,
        "_FIX_GENERATION_JSON_SCHEMA": {},
    }
    exec(compile(seg, "fix_engine.py:apply_ai_fix", "exec"), ns)
    return ns["apply_ai_fix"]


class _LlmClientStub:
    class LlmHumanEscalationNeeded(Exception):
        pass

    class LLMCreditExhausted(Exception):
        pass


def main():
    ok = True
    tmp = tempfile.mkdtemp()

    # A typed human-escalation signal must propagate UNCHANGED (not re-wrapped).
    def _raise_escalation(*a, **k):
        raise _LlmClientStub.LlmHumanEscalationNeeded(
            "No candidate satisfies requirements (reqs=LlmRequirements(...))")

    fn = _load_apply_ai_fix(_raise_escalation, _LlmClientStub)
    try:
        fn(tmp, "some issue", files_override=["nope.py"])
        ok &= _check("LlmHumanEscalationNeeded propagates (raised something)", False)
    except _LlmClientStub.LlmHumanEscalationNeeded:
        ok &= _check("LlmHumanEscalationNeeded propagates with type intact", True)
    except Exception as e:  # noqa: BLE001
        ok &= _check(f"LlmHumanEscalationNeeded NOT re-wrapped (got {type(e).__name__}: {e})", False)

    # A credit-exhausted signal must likewise propagate unchanged.
    def _raise_credit(*a, **k):
        raise _LlmClientStub.LLMCreditExhausted("out of credit")

    fn = _load_apply_ai_fix(_raise_credit, _LlmClientStub)
    try:
        fn(tmp, "some issue", files_override=["nope.py"])
        ok &= _check("LLMCreditExhausted propagates (raised something)", False)
    except _LlmClientStub.LLMCreditExhausted:
        ok &= _check("LLMCreditExhausted propagates with type intact", True)
    except Exception as e:  # noqa: BLE001
        ok &= _check(f"LLMCreditExhausted NOT re-wrapped (got {type(e).__name__})", False)

    # An ORDINARY error is still wrapped as "Fix generation failed: …".
    def _raise_value(*a, **k):
        raise ValueError("boom")

    fn = _load_apply_ai_fix(_raise_value, _LlmClientStub)
    try:
        fn(tmp, "some issue", files_override=["nope.py"])
        ok &= _check("ordinary error wrapped (raised something)", False)
    except _LlmClientStub.LlmHumanEscalationNeeded:
        ok &= _check("ordinary error NOT mistaken for escalation", False)
    except Exception as e:  # noqa: BLE001
        ok &= _check("ordinary error wrapped as 'Fix generation failed: …'",
                     str(e).startswith("Fix generation failed:") and "boom" in str(e))

    print("\nALL PASS" if ok else "\nSOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
