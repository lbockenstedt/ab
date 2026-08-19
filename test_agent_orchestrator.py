#!/usr/bin/env python3
"""Self-test for agent_orchestrator.py — AppBuilder's multi-agent request
orchestrator (plan -> parallel fan-out over distinct endpoints -> merge).

Run:  python3 ab/test_agent_orchestrator.py

Standalone: imports only agent_orchestrator + model_selection (no app/main
init). All LLM/selection I/O is faked, so nothing hits the network.
"""
import json
import sys
import threading
import time

import agent_orchestrator as orch
from model_selection import Selection


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeLLM:
    """Routes call_llm by role (planner via json_schema, agent/merge via
    system_prompt). Records prompts and reports a per-call 'used' model taken
    from the pin_key so tests can assert endpoint spread."""

    def __init__(self, plan=None, fail_key=None, slow_key=None):
        self.plan = plan                # dict for planner, or None -> single task
        self.fail_key = fail_key        # pin_key that raises (simulate dead endpoint)
        self.slow_key = slow_key        # pin_key that sleeps (to prove parallelism)
        self.agent_prompts = []
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, prompt, system_prompt="", requirements=None, json_schema=None,
                 task_id=None, used_model_out=None, messages=None, tools=None, stream=None):
        with self._lock:
            self.calls += 1
        pin = getattr(requirements, "pin_key", None)
        # Planner turn.
        if json_schema is not None:
            return json.dumps(self.plan) if self.plan is not None else '{"tasks": []}'
        # Merge/eval turn.
        if "integrator" in (system_prompt or ""):
            return "MERGED:" + prompt.count("###").__str__()
        # Agent turn.
        if pin is not None and pin == self.fail_key:
            raise RuntimeError("simulated dead endpoint")
        if pin is not None and pin == self.slow_key:
            time.sleep(0.3)
        if used_model_out is not None:
            used_model_out["key"] = pin if pin is not None else ("default", "", "m")
            used_model_out["label"] = orch._key_label(used_model_out["key"])
        with self._lock:
            self.agent_prompts.append(prompt)
        return f"out::{prompt.splitlines()[1] if len(prompt.splitlines()) > 1 else prompt}"


def _make_candidates(n):
    return [{"key": (f"p{i}", "", f"m{i}"), "provider": f"p{i}", "model": f"m{i}",
             "base_url": "", "api_key": "", "rpm": 0, "available": True,
             "caps": {"cost_tier": "free", "max_complexity": "large", "context_window": 100000}}
            for i in range(n)]


def _fake_select(candidates):
    """Return a select_model stand-in that hands out the first candidate whose
    key isn't excluded — mirrors real distinct-endpoint leasing."""
    def _sel(reqs, cands, perf=None, tuning=None):
        excl = set(getattr(reqs, "exclude_models", ()) or ())
        pin = getattr(reqs, "pin_key", None)
        for c in cands:
            if c["key"] in excl:
                continue
            if pin is not None and c["key"] != pin:
                continue
            return Selection(key=c["key"], provider=c["provider"], model=c["model"],
                             api_key="", base_url="", rpm=0, tier="free", reason="fake")
        return None
    return _sel


def main():
    print("Running ab agent_orchestrator self-test...")
    ok = True

    # --- Planning ---------------------------------------------------------- #
    plan = {"tasks": [
        {"id": "a", "goal": "do A", "depends_on": [], "complexity": "small"},
        {"id": "b", "goal": "do B", "depends_on": ["a"], "complexity": "medium"},
    ]}
    tasks = orch.plan_tasks("req", {}, FakeLLM(plan=plan))
    ok &= _check("planner parses a valid 2-task DAG", len(tasks) == 2 and tasks[1].depends_on == ("a",))

    # Invalid planner output -> single-task fallback.
    def _boom(*a, **k):
        raise RuntimeError("planner exploded")
    tasks1 = orch.plan_tasks("req", {}, _boom)
    ok &= _check("planner failure degrades to a single task", len(tasks1) == 1 and tasks1[0].goal == "req")

    # Empty task list -> single-task fallback.
    tasks_empty = orch.plan_tasks("req", {}, FakeLLM(plan={"tasks": []}))
    ok &= _check("empty plan degrades to a single task", len(tasks_empty) == 1)

    # Planner turn is fast+smart: capture the LlmRequirements it builds.
    captured = {}
    def _capture_llm(prompt, system_prompt="", requirements=None, json_schema=None, **k):
        captured["reqs"] = requirements
        return json.dumps({"tasks": []})
    orch.plan_tasks("req", {}, _capture_llm)
    _r = captured.get("reqs")
    ok &= _check("planner uses cost-first ordering (prefer_capable NOT set), so free/GPU wins if capable",
                 getattr(_r, "prefer_capable", False) is False)
    ok &= _check("planner still requires medium capability so only capable models qualify",
                 getattr(_r, "complexity", None) == "medium")
    ok &= _check("planner requests a fast model via latency_sensitive",
                 getattr(_r, "latency_sensitive", False) is True)
    ok &= _check("planner has no pin under default config", getattr(_r, "pin_key", "x") is None)

    # An explicit ORCHESTRATOR_PLANNER_PIN overrides the automatic pick.
    captured.clear()
    orch.plan_tasks("req", {"ORCHESTRATOR_PLANNER_PIN": "ollama|http://gpu|q"}, _capture_llm)
    ok &= _check("ORCHESTRATOR_PLANNER_PIN is honored as the planner pin",
                 getattr(captured.get("reqs"), "pin_key", None) == "ollama|http://gpu|q")

    # chat_pin is the fallback planner pin when no explicit planner pin is set.
    captured.clear()
    orch.plan_tasks("req", {"chat_pin": "anthropic|https://api|claude"}, _capture_llm)
    ok &= _check("planner falls back to chat_pin when no ORCHESTRATOR_PLANNER_PIN",
                 getattr(captured.get("reqs"), "pin_key", None) == "anthropic|https://api|claude")

    # --- DAG utilities ----------------------------------------------------- #
    waves = orch._topological_waves(tasks)
    ok &= _check("topological_waves orders deps into 2 waves",
                 waves is not None and [t.id for t in waves[0]] == ["a"] and [t.id for t in waves[1]] == ["b"])

    cyclic = [orch.AgentTask(id="x", depends_on=("y",), goal="g"),
              orch.AgentTask(id="y", depends_on=("x",), goal="g")]
    ok &= _check("topological_waves detects a cycle", orch._topological_waves(cyclic) is None)

    # sanitize drops dangling deps + caps count.
    raw = [orch.AgentTask(id=str(i), goal="g", depends_on=("999",)) for i in range(8)]
    san = orch._sanitize_dag(raw, max_tasks=5)
    ok &= _check("sanitize_dag caps task count and drops dangling deps",
                 len(san) == 5 and all(t.depends_on == () for t in san))

    # sanitize breaks a cycle into independent tasks.
    san_cyc = orch._sanitize_dag([orch.AgentTask(id="x", depends_on=("y",), goal="g"),
                                  orch.AgentTask(id="y", depends_on=("x",), goal="g")], max_tasks=5)
    ok &= _check("sanitize_dag flattens a cyclic plan", all(t.depends_on == () for t in san_cyc))

    # --- Leasing distinct endpoints --------------------------------------- #
    cands3 = _make_candidates(3)
    ready = [orch.AgentTask(id=c, goal="g") for c in ("a", "b", "c")]
    leased = orch._lease_models(ready, {}, _fake_select(cands3), cands3, {})
    ok &= _check("lease assigns 3 DISTINCT endpoints to 3 parallel tasks",
                 len({v for v in leased.values()}) == 3 and all(v is not None for v in leased.values()))

    # Fewer endpoints than tasks -> the extras get None (run unpinned/shared).
    cands1 = _make_candidates(1)
    leased2 = orch._lease_models(ready, {}, _fake_select(cands1), cands1, {})
    ok &= _check("lease hands the extra tasks None when endpoints run out",
                 sum(1 for v in leased2.values() if v is None) == 2)

    # --- End-to-end: single task (no merge) -------------------------------- #
    llm = FakeLLM(plan=None)
    res = orch.orchestrate("just answer", {}, call_llm_fn=llm,
                           select_model_fn=_fake_select(cands3),
                           enumerate_fn=lambda cfg: cands3, perf_fn=lambda: {})
    ok &= _check("single-task run returns the agent output without a merge step",
                 not res.planned and not res.evaluated and res.final_text.startswith("out::"))

    # --- End-to-end: parallel fan-out spreads across DISTINCT endpoints ---- #
    par_plan = {"tasks": [{"id": "a", "goal": "A", "depends_on": []},
                          {"id": "b", "goal": "B", "depends_on": []},
                          {"id": "c", "goal": "C", "depends_on": []}]}
    llm2 = FakeLLM(plan=par_plan)
    res2 = orch.orchestrate("big req", {"ORCHESTRATOR_MAX_PARALLEL": 3}, call_llm_fn=llm2,
                            select_model_fn=_fake_select(cands3),
                            enumerate_fn=lambda cfg: cands3, perf_fn=lambda: {})
    used_keys = {r.model_key for r in res2.results}
    ok &= _check("parallel fan-out ran 3 agents on 3 distinct endpoints",
                 res2.planned and len(res2.results) == 3 and len(used_keys) == 3)
    ok &= _check("parallel run produced a merged/evaluated answer",
                 res2.evaluated and res2.final_text.startswith("MERGED:"))

    # --- Parallelism actually overlaps (wall-clock < serial sum) ----------- #
    slow_key = cands3[0]["key"]
    llm_slow = FakeLLM(plan=par_plan, slow_key=slow_key)
    t0 = time.time()
    orch.orchestrate("big req", {"ORCHESTRATOR_MAX_PARALLEL": 3}, call_llm_fn=llm_slow,
                     select_model_fn=_fake_select(cands3),
                     enumerate_fn=lambda cfg: cands3, perf_fn=lambda: {})
    elapsed = time.time() - t0
    ok &= _check("3 agents run concurrently (one 0.3s sleep, wall-clock < 0.6s)", elapsed < 0.6)

    # --- Dependency outputs feed the dependent agent's prompt -------------- #
    dep_plan = {"tasks": [{"id": "a", "goal": "AAA", "depends_on": []},
                          {"id": "b", "goal": "BBB", "depends_on": ["a"]}]}
    llm3 = FakeLLM(plan=dep_plan)
    orch.orchestrate("req", {}, call_llm_fn=llm3, select_model_fn=_fake_select(cands3),
                     enumerate_fn=lambda cfg: cands3, perf_fn=lambda: {})
    b_prompt = next((p for p in llm3.agent_prompts if "BBB" in p), "")
    ok &= _check("dependent agent's prompt includes its dependency's result",
                 "[Result of a]" in b_prompt)

    # --- Pinned failure retried unpinned ----------------------------------- #
    single_plan = {"tasks": [{"id": "a", "goal": "A", "depends_on": []}]}
    llm4 = FakeLLM(plan=single_plan, fail_key=cands3[0]["key"])
    res4 = orch.orchestrate("req", {}, call_llm_fn=llm4, select_model_fn=_fake_select(cands3),
                            enumerate_fn=lambda cfg: cands3, perf_fn=lambda: {})
    # First (pinned) agent call raises; retry unpinned succeeds with default key.
    ok &= _check("a pinned endpoint failure is retried unpinned and still succeeds",
                 res4.results and res4.results[0].ok and res4.results[0].model_key == ("default", "", "m"))

    # --- All agents fail -> failure surfaced ------------------------------- #
    class AllFail(FakeLLM):
        def __call__(self, prompt, system_prompt="", requirements=None, json_schema=None, **k):
            if json_schema is not None:
                return json.dumps(par_plan)
            if "integrator" in (system_prompt or ""):
                return ""
            raise RuntimeError("dead")
    res5 = orch.orchestrate("req", {}, call_llm_fn=AllFail(),
                            select_model_fn=_fake_select(cands3),
                            enumerate_fn=lambda cfg: cands3, perf_fn=lambda: {})
    ok &= _check("all-agents-fail run reports failure instead of crashing",
                 all(not r.ok for r in res5.results) and "failed" in res5.final_text.lower())

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
