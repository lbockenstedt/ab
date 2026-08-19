#!/usr/bin/env python3
"""Self-test for llm_proxy.py's agentic mode (the LLM router reusing AppBuilder's
own agent loop as a third fix intake).

Run:  python3 test_llm_proxy_agentic.py

llm_proxy.py can't be imported directly (it transitively pulls in main.py's
circular import chain), so — per this repo's convention (see
test_chat_requirements.py) — the module-level helpers under test are extracted
via ast and exec'd with stubbed dependencies.

Covers:
1. _wants_agentic: model id containing "ab-agent" opts in; a plain model
   does not; llm_proxy_agentic_default forces it on; an operator-configured
   llm_proxy_agent_model_ids entry opts in.
2. _proxy_fix_proposal: with autofix DISABLED (default) it never triggers a fix
   and says so; with autofix ENABLED it launches process_single_issue exactly
   once with the proposal's repo/number/preference.
3. _proxy_fix_proposal: the pre-build boundary PRE-FLIGHT note appears when the
   issue text matches a configured core-systems boundary.
"""
import ast
import sys
import threading
import time
import types


def _load_ns(names):
    src = open("llm_proxy.py").read()
    tree = ast.parse(src)
    segs = []
    wanted = set(names)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            segs.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in wanted:
                    segs.append(ast.get_source_segment(src, node))
    ns = {
        "os": __import__("os"),
        "threading": threading,
        "logger": type("L", (), {"__getattr__": lambda s, n: (lambda *a, **k: None)})(),
        "Dict": dict, "Any": object, "List": list, "Tuple": tuple,
    }
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def main():
    print("Running llm_proxy agentic-mode self-test...")
    ok = True
    ns = _load_ns({"_PROXY_AGENT_MODEL_ID", "_wants_agentic", "_proxy_fix_proposal"})
    wants = ns["_wants_agentic"]
    propose = ns["_proxy_fix_proposal"]

    # ---- 1. _wants_agentic ----
    ok &= _check("model 'ab-agent' opts into agentic mode",
                 wants({"model": "ab-agent"}, {}) is True)
    ok &= _check("plain model does NOT opt in",
                 wants({"model": "claude-3-5-sonnet"}, {}) is False)
    ok &= _check("empty model does NOT opt in",
                 wants({}, {}) is False)
    ok &= _check("llm_proxy_agentic_default forces agentic on",
                 wants({"model": "anything"}, {"llm_proxy_agentic_default": True}) is True)
    ok &= _check("operator-configured agent model id opts in",
                 wants({"model": "my-agent"}, {"llm_proxy_agent_model_ids": ["my-agent"]}) is True)

    # ---- 2. _proxy_fix_proposal: autofix disabled (default) ----
    triggered = {"calls": []}
    ev = threading.Event()

    def _fake_psi(repo, number, llm_preference=None):
        triggered["calls"].append((repo, number, llm_preference))
        ev.set()
        return True, "ok"

    fake_fix_engine = types.ModuleType("fix_engine")
    fake_fix_engine.process_single_issue = _fake_psi
    sys.modules["fix_engine"] = fake_fix_engine

    desc = {"kind": "confirm_fix", "repo": "o/r", "number": 42,
            "title": "some ordinary bug", "llm_preference": "cloud"}
    cfg_off = {"feature_boundaries": []}  # autofix not enabled
    out = propose(desc, "here is my analysis", cfg_off, None)
    ok &= _check("autofix DISABLED -> does not trigger process_single_issue",
                 len(triggered["calls"]) == 0)
    ok &= _check("autofix DISABLED -> message says fixing is disabled",
                 "DISABLED" in out and "o/r#42" in out)

    # ---- 2b. autofix enabled -> triggers exactly once with the right args ----
    cfg_on = {"feature_boundaries": [], "llm_proxy_autofix_enabled": True}
    out2 = propose(desc, "analysis", cfg_on, None)
    ev.wait(timeout=3)
    time.sleep(0.05)
    ok &= _check("autofix ENABLED -> process_single_issue called exactly once",
                 triggered["calls"] == [("o/r", 42, "cloud")])
    ok &= _check("autofix ENABLED -> message confirms the pipeline was triggered",
                 "Triggered" in out2 and "review panel" in out2)

    # ---- 3. pre-flight boundary note ----
    # feature_boundary.prefilter matches keyword tokens in the issue text; an
    # 'mtls'/'signing scheme' mention should trip the transport-scheme rule.
    boundary_cfg = {
        "feature_boundaries": [{
            "id": "transport-scheme", "label": "transport",
            "rule": "no transport changes", "paths": ["**/hub_agent.py"],
            "keywords": ["mtls", "signing scheme"], "hard": True, "enabled": True,
        }],
        # autofix off so we only exercise the pre-flight text path
    }
    desc_core = {"kind": "confirm_fix", "repo": "o/r", "number": 7,
                 "title": "change the mtls signing scheme", "llm_preference": None}
    out3 = propose(desc_core, "", boundary_cfg, None)
    ok &= _check("pre-flight flags a core-systems boundary as PR-only",
                 "Pre-flight" in out3 and "transport-scheme" in out3)

    print("\n" + ("ALL CASES PASSED" if ok else "ONE OR MORE CASES FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
