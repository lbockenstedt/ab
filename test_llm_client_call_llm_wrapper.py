#!/usr/bin/env python3
"""Self-test for call_llm's post-redesign contract (Phase 6): it is now a thin
wrapper that REQUIRES a requirements=LlmRequirements and delegates entirely to
_call_llm_with_requirements — the legacy slot-pool/task_kind/force_provider
routing was deleted.

Run:  python3 ab/test_llm_client_call_llm_wrapper.py

llm_client.py imports `main` (app-init side effects), so this extracts the
source of call_llm via ast and execs it with stubbed leaf dependencies,
following the established pattern in test_llm_client_requirements_path.py.
"""
import ast


def _load_call_llm(delegate, load_config=lambda: {}):
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "call_llm":
            seg = ast.get_source_segment(src, node)
            break
    assert seg is not None, "call_llm not found in llm_client.py"
    ns = {
        "load_config": load_config,
        "_call_llm_with_requirements": delegate,
    }
    exec(compile(seg, "llm_client.py", "exec"), ns)
    return ns["call_llm"]


def _check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    return cond


def main():
    print("Running call_llm wrapper contract self-test...")
    ok = True

    # 1. requirements=None must raise (legacy routing retired).
    call_llm = _load_call_llm(delegate=lambda *a, **k: "SHOULD_NOT_RUN")
    raised = False
    try:
        call_llm("hi")
    except ValueError:
        raised = True
    ok &= _check("call_llm() with no requirements raises ValueError", raised)

    # 2. requirements given → delegates to _call_llm_with_requirements, passing
    #    the requirements object and prompt through, and returns its result.
    seen = {}

    def _delegate(requirements, prompt, system_prompt, messages, tools, stream,
                  task_id, config, **kwargs):
        seen["requirements"] = requirements
        seen["prompt"] = prompt
        seen["used_model_out"] = kwargs.get("used_model_out")
        return "DELEGATED"

    sentinel_reqs = object()
    umo = {}
    call_llm = _load_call_llm(delegate=_delegate)
    result = call_llm("do the thing", requirements=sentinel_reqs, used_model_out=umo)
    ok &= _check("delegates to _call_llm_with_requirements and returns its result",
                 result == "DELEGATED")
    ok &= _check("forwards the requirements object unchanged",
                 seen.get("requirements") is sentinel_reqs)
    ok &= _check("forwards the prompt", seen.get("prompt") == "do the thing")
    ok &= _check("forwards used_model_out", seen.get("used_model_out") is umo)

    # 3. the retired legacy kwargs are gone from the signature.
    import inspect
    params = set(inspect.signature(call_llm).parameters)
    retired = {"force_cloud", "force_provider", "task_kind", "model_override", "url_override"}
    ok &= _check("no retired routing params remain on call_llm",
                 not (params & retired))

    print("\nALL CASES PASSED" if ok else "\nONE OR MORE CASES FAILED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
