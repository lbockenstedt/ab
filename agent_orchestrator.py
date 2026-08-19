"""
agent_orchestrator.py — multi-agent request orchestration for AppBuilder.

Turns ONE request into a small DAG of sub-tasks ("agents"), runs the
independent ones CONCURRENTLY — each end-to-end on the LLM best suited AND
available for its job — and then evaluates/merges the parts into one answer.

Why this exists: the picker (model_selection.select_model) already routes a
single call to the cheapest capable model, and the fix pipeline already runs
its stages sequentially. What was missing is INTRA-request parallelism: split
a request, fan it out across DISTINCT available endpoints (your CPU box, GPU
box, and cloud all working at once instead of queuing on the one serialized
Ollama), then aggregate. That is what this module adds.

Pipeline:
    plan  -> an LLM planner decomposes the request into a sub-task DAG
             (id, goal, depends_on, complexity). Any failure degrades to a
             single-task DAG, so behaviour never regresses.
    lease -> per wave of dependency-ready tasks, select_model is called with a
             growing exclude set so each concurrent agent lands on a DISTINCT
             endpoint when enough are available (pinned via pin_key). When
             agents outnumber endpoints the extras run unpinned and the
             per-model lock in llm_client serialises them.
    run   -> each agent runs its job via call_llm, fed its dependencies'
             outputs as context; a pinned agent that errors is retried once
             UNPINNED so a single dead endpoint can't strand a sub-task.
    merge -> an evaluator call synthesises the parts into the final answer
             (skipped for a single-task DAG, whose output is already final).

Purity/testability: all I/O is injectable (call_llm_fn, select_model_fn,
enumerate_fn, perf_fn). The default wiring lazily imports llm_client /
model_selection so importing this module stays cheap and side-effect free.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

logger = logging.getLogger("ab.orchestrator")

# Config-key defaults (operator-tunable in Settings). Kept conservative: the
# whole subsystem is gated on ORCHESTRATOR_ENABLED, default off.
DEFAULT_MAX_PARALLEL = 3
DEFAULT_MAX_TASKS = 5
_VALID_COMPLEXITY = ("trivial", "small", "medium", "large")

_PLANNER_SYSTEM_PROMPT = (
    "You are a planning module. Decompose the user's request into the SMALLEST "
    "number of sub-tasks that can each be worked end-to-end by one worker. "
    "Prefer INDEPENDENT sub-tasks that can run in parallel; only add a "
    "dependency when a sub-task genuinely needs another's output. If the "
    "request is simple, return a SINGLE task. Never exceed the task limit. "
    "Return ONLY a JSON object of the form "
    '{"tasks":[{"id":"t1","goal":"...","depends_on":[],"complexity":"small"}]}. '
    "complexity is one of trivial|small|medium|large (how hard that sub-task is). "
    "ids are short and unique; depends_on lists ids of tasks that must finish first."
)

_PLANNER_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "goal": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "complexity": {"type": "string"},
                },
                "required": ["id", "goal"],
            },
        }
    },
    "required": ["tasks"],
}


@dataclass
class AgentTask:
    """One node in the plan: a job for a single best-suited LLM."""
    id: str
    goal: str
    depends_on: tuple = ()
    complexity: str = "small"
    needs_tools: bool = False


@dataclass
class AgentResult:
    """The outcome of running one AgentTask."""
    task_id: str
    output: str = ""
    model_key: tuple | None = None
    model_label: str = ""
    ok: bool = True
    error: str | None = None


@dataclass
class OrchestrationResult:
    """The whole run: the final synthesised answer plus per-agent detail."""
    final_text: str = ""
    tasks: list = field(default_factory=list)      # list[AgentTask]
    results: list = field(default_factory=list)    # list[AgentResult]
    planned: bool = False                            # True if the planner produced >1 task
    evaluated: bool = False                          # True if the merge/eval step ran
    error: str | None = None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _coerce_task(raw, seen_ids):
    """Best-effort turn one planner dict into an AgentTask; None if unusable."""
    if not isinstance(raw, dict):
        return None
    tid = str(raw.get("id") or "").strip()
    goal = str(raw.get("goal") or "").strip()
    if not tid or not goal or tid in seen_ids:
        return None
    complexity = str(raw.get("complexity") or "small").strip().lower()
    if complexity not in _VALID_COMPLEXITY:
        complexity = "small"
    deps_raw = raw.get("depends_on") or []
    deps = tuple(str(d).strip() for d in deps_raw if str(d).strip()) if isinstance(deps_raw, list) else ()
    return AgentTask(id=tid, goal=goal, depends_on=deps, complexity=complexity)


def _sanitize_dag(tasks, max_tasks):
    """Drop dangling deps, break cycles, and cap the task count so a bad plan
    can never deadlock or explode the scheduler. Returns a safe task list."""
    tasks = tasks[:max_tasks]
    valid_ids = {t.id for t in tasks}
    # Drop deps that point outside the (possibly truncated) id set.
    for t in tasks:
        t.depends_on = tuple(d for d in t.depends_on if d in valid_ids and d != t.id)
    # Cycle guard: if the DAG can't be fully ordered, flatten deps to none.
    if _topological_waves(tasks) is None:
        logger.warning("orchestrator: planner returned a cyclic DAG; flattening to independent tasks")
        for t in tasks:
            t.depends_on = ()
    return tasks


def plan_tasks(request, config, call_llm_fn, max_tasks=DEFAULT_MAX_TASKS):
    """Ask the planner LLM to decompose *request* into an AgentTask DAG.

    Always returns at least one task: any error or empty/invalid plan degrades
    to a single task carrying the original request, so the caller can treat a
    single-node DAG as "just run it normally"."""
    single = [AgentTask(id="main", goal=request, depends_on=(), complexity="medium")]
    try:
        from model_selection import LlmRequirements
        # The planner/router turn decides how the whole request is decomposed
        # and routed, so it must be handled by a genuinely capable model — but
        # NOT necessarily a paid one. We keep the DEFAULT cost-first ordering
        # (free/GPU tier first, then cheap, then frontier): the capability
        # filter (complexity="medium") already guarantees only capable models
        # are in the pool, so the cheapest capable one wins and a paid frontier
        # model is used only when nothing free/local qualifies. Paid tiers are
        # never excluded — just deferred. (This deliberately does NOT set
        # prefer_capable, which would invert to frontier-first.) latency_sensitive
        # + the picker's throughput ranking keep it fast; an explicit
        # ORCHESTRATOR_PLANNER_PIN (else chat_pin) hard-overrides the pick.
        planner_pin = (config or {}).get("ORCHESTRATOR_PLANNER_PIN") or (config or {}).get("chat_pin") or None
        reqs = LlmRequirements(
            complexity="medium",
            needs_structured_output=True,
            latency_sensitive=True,
            pin_key=planner_pin,
        )
        prompt = (
            f"Task limit: {max_tasks}. Decompose this request into a sub-task DAG "
            f"as specified.\n\nREQUEST:\n{request}"
        )
        raw = call_llm_fn(
            prompt,
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            requirements=reqs,
            json_schema=_PLANNER_SCHEMA,
        )
        parsed = _extract_json_obj(raw)
        items = (parsed or {}).get("tasks") if isinstance(parsed, dict) else None
        if not isinstance(items, list) or not items:
            return single
        tasks, seen = [], set()
        for it in items:
            t = _coerce_task(it, seen)
            if t is not None:
                tasks.append(t)
                seen.add(t.id)
        if not tasks:
            return single
        return _sanitize_dag(tasks, max_tasks)
    except Exception as e:  # noqa: BLE001 — planning must never hard-fail the request
        logger.warning(f"orchestrator: planning failed ({e}); running the request as one task")
        return single


def _extract_json_obj(text):
    """Parse a JSON object from an LLM response, tolerating prose/code fences."""
    if isinstance(text, (dict, list)):
        return text
    if not text or not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def _topological_waves(tasks):
    """Kahn layering: return a list of waves (each a list of AgentTask whose
    deps are all satisfied by earlier waves), or None if the DAG has a cycle."""
    by_id = {t.id: t for t in tasks}
    remaining = {t.id: set(d for d in t.depends_on if d in by_id) for t in tasks}
    waves = []
    while remaining:
        ready = [tid for tid, deps in remaining.items() if not deps]
        if not ready:
            return None  # cycle
        waves.append([by_id[tid] for tid in ready])
        for tid in ready:
            del remaining[tid]
        for deps in remaining.values():
            deps.difference_update(ready)
    return waves


def _lease_models(ready_tasks, config, select_model_fn, candidates, perf):
    """Assign each ready task a DISTINCT available endpoint when possible.

    Returns {task_id: model_key|None}. None means "no distinct endpoint left —
    run unpinned and let call_llm pick / the per-model lock serialise it"."""
    from model_selection import LlmRequirements
    leased = {}
    assigned = set()
    for t in ready_tasks:
        reqs = LlmRequirements(
            complexity=t.complexity,
            needs_tools=t.needs_tools,
            exclude_models=tuple(assigned),
        )
        try:
            sel = select_model_fn(reqs, candidates, perf)
        except Exception:  # noqa: BLE001 — leasing is an optimisation, never fatal
            sel = None
        if sel is not None and getattr(sel, "key", None) is not None and sel.key not in assigned:
            leased[t.id] = sel.key
            assigned.add(sel.key)
        else:
            leased[t.id] = None
    return leased


# --------------------------------------------------------------------------- #
# Agent execution
# --------------------------------------------------------------------------- #
def _build_agent_prompt(task, dep_results):
    """Compose one agent's prompt: its goal plus any dependency outputs."""
    parts = [f"Your job:\n{task.goal}"]
    done = [r for r in dep_results if r.output and r.output.strip()]
    if done:
        ctx = "\n\n".join(f"[Result of {r.task_id}]\n{r.output}" for r in done)
        parts.append(
            "You may build on these completed sub-task results:\n\n" + ctx
        )
    parts.append("Complete YOUR job only. Return just your result.")
    return "\n\n".join(parts)


def _run_agent(task, dep_results, leased_key, config, call_llm_fn, task_id=None):
    """Run one agent end-to-end on its best-suited model. A pinned agent whose
    endpoint errors is retried once UNPINNED so one dead box can't strand it."""
    from model_selection import LlmRequirements
    prompt = _build_agent_prompt(task, dep_results)

    def _once(pin):
        used = {}
        reqs = LlmRequirements(
            complexity=task.complexity,
            needs_tools=task.needs_tools,
            pin_key=pin,
        )
        out = call_llm_fn(
            prompt,
            system_prompt="You are a focused worker completing one assigned sub-task.",
            requirements=reqs,
            task_id=task_id,
            used_model_out=used,
        )
        return out, used

    try:
        out, used = _once(leased_key)
    except Exception as e:  # noqa: BLE001
        if leased_key is not None:
            logger.warning(f"orchestrator: agent {task.id} pinned endpoint failed ({e}); retrying unpinned")
            try:
                out, used = _once(None)
            except Exception as e2:  # noqa: BLE001
                return AgentResult(task_id=task.id, ok=False, error=str(e2))
        else:
            return AgentResult(task_id=task.id, ok=False, error=str(e))

    return AgentResult(
        task_id=task.id,
        output=out or "",
        model_key=used.get("key"),
        model_label=used.get("label") or _key_label(used.get("key")),
        ok=bool(out and out.strip()),
        error=None if (out and out.strip()) else "empty response",
    )


def _key_label(key):
    """Human label for a ModelKey tuple (provider, base_url, model)."""
    if isinstance(key, (tuple, list)) and len(key) >= 3:
        provider, _base, model = key[0], key[1], key[2]
        return f"{provider}/{model}" if model else str(provider)
    return str(key) if key else "?"


# --------------------------------------------------------------------------- #
# Evaluation / merge
# --------------------------------------------------------------------------- #
def _evaluate_and_merge(request, tasks, results, config, call_llm_fn):
    """Synthesise the per-agent outputs into one final answer and lightly
    evaluate completeness. Returns (final_text, evaluated: bool). On any error
    falls back to a plain concatenation so a run always yields something."""
    ok_results = [r for r in results if r.ok and r.output.strip()]
    if not ok_results:
        failed = "; ".join(f"{r.task_id}: {r.error}" for r in results) or "no output"
        return (f"All sub-tasks failed ({failed}).", False)

    joined = "\n\n".join(f"### {r.task_id}\n{r.output}" for r in ok_results)
    try:
        from model_selection import LlmRequirements
        reqs = LlmRequirements(complexity="medium")
        prompt = (
            "Combine the sub-task results below into ONE coherent, complete "
            "answer to the original request. Resolve overlaps, keep everything "
            "correct, and note anything still missing.\n\n"
            f"ORIGINAL REQUEST:\n{request}\n\nSUB-TASK RESULTS:\n{joined}"
        )
        merged = call_llm_fn(
            prompt,
            system_prompt="You are an integrator assembling worker outputs into a final answer.",
            requirements=reqs,
        )
        if merged and merged.strip():
            return (merged, True)
    except Exception as e:  # noqa: BLE001 — merge is best-effort
        logger.warning(f"orchestrator: merge/eval failed ({e}); returning concatenated parts")
    return (joined, False)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def orchestrate(request, config=None, *, call_llm_fn=None, select_model_fn=None,
                enumerate_fn=None, perf_fn=None, max_parallel=None, max_tasks=None,
                progress_cb=None, task_id=None):
    """Run *request* as a multi-agent DAG and return an OrchestrationResult.

    All I/O is injectable for testing; the defaults wire to llm_client /
    model_selection. `progress_cb(event: dict)`, if given, is called with
    {"event": ..., ...} at plan/lease/agent-done/merge milestones so a caller
    (e.g. the chat stream) can surface which LLM handled each part."""
    config = config or {}
    if call_llm_fn is None:
        import llm_client
        call_llm_fn = llm_client.call_llm
    if select_model_fn is None:
        import model_selection
        select_model_fn = model_selection.select_model
    if enumerate_fn is None:
        import llm_client
        enumerate_fn = llm_client._enumerate_candidates
    if perf_fn is None:
        import llm_client
        perf_fn = llm_client.get_llm_perf_snapshot

    max_parallel = int(max_parallel or config.get("ORCHESTRATOR_MAX_PARALLEL", DEFAULT_MAX_PARALLEL) or DEFAULT_MAX_PARALLEL)
    max_parallel = max(1, max_parallel)
    max_tasks = int(max_tasks or config.get("ORCHESTRATOR_MAX_TASKS", DEFAULT_MAX_TASKS) or DEFAULT_MAX_TASKS)
    max_tasks = max(1, max_tasks)

    def _emit(event, **kw):
        if progress_cb:
            try:
                progress_cb({"event": event, **kw})
            except Exception:  # noqa: BLE001 — a progress hook must never break the run
                pass

    # 1) Plan.
    tasks = plan_tasks(request, config, call_llm_fn, max_tasks=max_tasks)
    planned = len(tasks) > 1
    _emit("planned", task_count=len(tasks), tasks=[{"id": t.id, "goal": t.goal, "depends_on": list(t.depends_on)} for t in tasks])

    # Single-task DAG: no fan-out, no merge — just run it (keeps the simple
    # path cheap and behaviourally identical to a normal call).
    waves = _topological_waves(tasks) or [tasks]
    results_by_id = {}

    for wave in waves:
        ready = [t for t in wave if t.id not in results_by_id]
        if not ready:
            continue
        # Lease distinct endpoints for this wave (best-effort optimisation).
        try:
            candidates = enumerate_fn(config)
            perf = perf_fn()
        except Exception:  # noqa: BLE001
            candidates, perf = [], {}
        leased = _lease_models(ready, config, select_model_fn, candidates, perf) if candidates else {t.id: None for t in ready}

        def _work(t):
            deps = [results_by_id[d] for d in t.depends_on if d in results_by_id]
            res = _run_agent(t, deps, leased.get(t.id), config, call_llm_fn, task_id=task_id)
            _emit("agent_done", task_id=t.id, model=res.model_label, ok=res.ok)
            return res

        if len(ready) == 1:
            r = _work(ready[0])
            results_by_id[r.task_id] = r
        else:
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(ready))) as ex:
                for r in ex.map(_work, ready):
                    results_by_id[r.task_id] = r

    results = [results_by_id[t.id] for t in tasks if t.id in results_by_id]

    # 2) Merge/evaluate — only when the request actually fanned out.
    if planned:
        final_text, evaluated = _evaluate_and_merge(request, tasks, results, config, call_llm_fn)
    else:
        only = results[0] if results else AgentResult(task_id="main", ok=False, error="no result")
        final_text, evaluated = (only.output, False)
    _emit("merged", evaluated=evaluated)

    return OrchestrationResult(
        final_text=final_text,
        tasks=tasks,
        results=results,
        planned=planned,
        evaluated=evaluated,
        error=None if final_text.strip() else "empty result",
    )
