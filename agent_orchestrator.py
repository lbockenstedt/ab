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
# Postpone evaluation of type hints so "tuple | None" style annotations work on older Pythons.
from __future__ import annotations

# json parses the planner's JSON reply; logging reports warnings; re finds a JSON object
# embedded in prose.
import json
import logging
import re
# Thread pool used to run independent agents of one wave at the same time.
from concurrent.futures import ThreadPoolExecutor
# dataclass/field build the simple record classes below without hand-written __init__ code.
from dataclasses import dataclass, field

# One named logger so orchestrator messages can be filtered together.
logger = logging.getLogger("ab.orchestrator")

# How many agents may run at once, and the most sub-tasks a plan may contain.
# Config-key defaults (operator-tunable in Settings). Kept conservative: the
# whole subsystem is gated on ORCHESTRATOR_ENABLED, default off.
DEFAULT_MAX_PARALLEL = 3
DEFAULT_MAX_TASKS = 5
# The only complexity labels the model picker understands; anything else becomes "small".
_VALID_COMPLEXITY = ("trivial", "small", "medium", "large")

# System prompt for the planner LLM: tells it to split the request into as few sub-tasks as
# possible and reply with strict JSON in a fixed shape.
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

# JSON schema handed to the LLM call so models that support structured output return exactly
# {"tasks": [...]} with an id and goal on every task.
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
    # Short unique id the planner gave this task (used for dependency links).
    id: str
    # Plain-English description of the work this agent must do.
    goal: str
    # Ids of tasks whose output must exist before this one can start.
    depends_on: tuple = ()
    # Difficulty hint that the model picker uses to choose a capable enough LLM.
    complexity: str = "small"
    # Whether the task needs tool-calling support from the model.
    needs_tools: bool = False


@dataclass
class AgentResult:
    """The outcome of running one AgentTask."""
    # Which AgentTask this result belongs to.
    task_id: str
    # Text the agent produced.
    output: str = ""
    # (provider, base_url, model) tuple of the model that answered, when known.
    model_key: tuple | None = None
    # Human-readable name of that model for progress messages.
    model_label: str = ""
    # False when the agent errored or returned nothing.
    ok: bool = True
    # Error message explaining a failure, if any.
    error: str | None = None


@dataclass
class OrchestrationResult:
    """The whole run: the final synthesised answer plus per-agent detail."""
    # The single answer returned to the caller.
    final_text: str = ""
    tasks: list = field(default_factory=list)      # list[AgentTask]
    results: list = field(default_factory=list)    # list[AgentResult]
    planned: bool = False                            # True if the planner produced >1 task
    evaluated: bool = False                          # True if the merge/eval step ran
    # Set only when the whole run produced an empty answer.
    error: str | None = None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _coerce_task(raw, seen_ids):
    """Best-effort turn one planner dict into an AgentTask; None if unusable."""
    # Planner output that is not a JSON object cannot be a task.
    if not isinstance(raw, dict):
        return None
    # Pull out the id and goal as clean strings, treating missing values as empty.
    tid = str(raw.get("id") or "").strip()
    goal = str(raw.get("goal") or "").strip()
    # A task needs both an id and a goal, and ids must be unique, otherwise skip it.
    if not tid or not goal or tid in seen_ids:
        return None
    # Normalise the complexity label and fall back to "small" for unknown values.
    complexity = str(raw.get("complexity") or "small").strip().lower()
    if complexity not in _VALID_COMPLEXITY:
        complexity = "small"
    # Accept depends_on only when it is a list; keep non-empty ids as a tuple.
    deps_raw = raw.get("depends_on") or []
    deps = tuple(str(d).strip() for d in deps_raw if str(d).strip()) if isinstance(deps_raw, list) else ()
    # Build the validated task record.
    return AgentTask(id=tid, goal=goal, depends_on=deps, complexity=complexity)


def _sanitize_dag(tasks, max_tasks):
    """Drop dangling deps, break cycles, and cap the task count so a bad plan
    can never deadlock or explode the scheduler. Returns a safe task list."""
    # Keep at most max_tasks tasks and remember which ids survived the cut.
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
    # Fallback plan: one task that is simply the whole original request.
    single = [AgentTask(id="main", goal=request, depends_on=(), complexity="medium")]
    try:
        # Imported lazily so loading this module does not pull in the model picker.
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
        # Requirements for the planner call: capable, JSON output, fast, optionally pinned.
        reqs = LlmRequirements(
            complexity="medium",
            needs_structured_output=True,
            latency_sensitive=True,
            pin_key=planner_pin,
        )
        # The user-facing prompt: the task limit plus the original request text.
        prompt = (
            f"Task limit: {max_tasks}. Decompose this request into a sub-task DAG "
            f"as specified.\n\nREQUEST:\n{request}"
        )
        # Ask the planner LLM for the DAG, requesting JSON that matches _PLANNER_SCHEMA.
        raw = call_llm_fn(
            prompt,
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            requirements=reqs,
            json_schema=_PLANNER_SCHEMA,
        )
        # Parse the reply and pull out the "tasks" list (None if the reply was not usable).
        parsed = _extract_json_obj(raw)
        items = (parsed or {}).get("tasks") if isinstance(parsed, dict) else None
        # No usable task list means we fall back to the single-task plan.
        if not isinstance(items, list) or not items:
            return single
        # Convert each raw item into an AgentTask, skipping bad or duplicate entries.
        tasks, seen = [], set()
        for it in items:
            t = _coerce_task(it, seen)
            if t is not None:
                tasks.append(t)
                seen.add(t.id)
        # If every item was rejected, fall back to the single-task plan.
        if not tasks:
            return single
        # Clean up dependencies/cycles before handing the plan to the scheduler.
        return _sanitize_dag(tasks, max_tasks)
    # Any unexpected error in planning is logged and the request runs as one task.
    except Exception as e:  # noqa: BLE001 — planning must never hard-fail the request
        logger.warning(f"orchestrator: planning failed ({e}); running the request as one task")
        return single


def _extract_json_obj(text):
    """Parse a JSON object from an LLM response, tolerating prose/code fences."""
    # Some LLM helpers already return parsed JSON; pass it through unchanged.
    if isinstance(text, (dict, list)):
        return text
    # Nothing (or a non-string) to parse.
    if not text or not isinstance(text, str):
        return None
    # First try: the whole reply is valid JSON.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Second try: grab the outermost {...} span, which skips prose or ``` fences around it.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    # No JSON object could be found.
    return None


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def _topological_waves(tasks):
    """Kahn layering: return a list of waves (each a list of AgentTask whose
    deps are all satisfied by earlier waves), or None if the DAG has a cycle."""
    # Map ids to tasks, and track each task's unmet dependencies (ignoring unknown ids).
    by_id = {t.id: t for t in tasks}
    remaining = {t.id: set(d for d in t.depends_on if d in by_id) for t in tasks}
    # Build waves until every task has been placed.
    waves = []
    while remaining:
        # Tasks with no unmet dependencies can run in this wave.
        ready = [tid for tid, deps in remaining.items() if not deps]
        # Nothing is ready but tasks remain, so the dependencies loop back on themselves.
        if not ready:
            return None  # cycle
        # Record this wave and remove its tasks from the pending set.
        waves.append([by_id[tid] for tid in ready])
        for tid in ready:
            del remaining[tid]
        # Those tasks are now done, so strike them from everyone else's dependency list.
        for deps in remaining.values():
            deps.difference_update(ready)
    # Ordered list of waves; each wave can run in parallel.
    return waves


def _lease_models(ready_tasks, config, select_model_fn, candidates, perf):
    """Assign each ready task a DISTINCT available endpoint when possible.

    Returns {task_id: model_key|None}. None means "no distinct endpoint left —
    run unpinned and let call_llm pick / the per-model lock serialise it"."""
    # Imported lazily to keep this module cheap to import.
    from model_selection import LlmRequirements
    # leased maps task id -> chosen model; assigned holds models already handed out this wave.
    leased = {}
    assigned = set()
    # Pick a model for each ready task in turn.
    for t in ready_tasks:
        # Ask for a model fit for this task's difficulty, excluding models already taken.
        reqs = LlmRequirements(
            complexity=t.complexity,
            needs_tools=t.needs_tools,
            exclude_models=tuple(assigned),
        )
        # Run the picker; a picker failure just means this task runs unpinned.
        try:
            sel = select_model_fn(reqs, candidates, perf)
        except Exception:  # noqa: BLE001 — leasing is an optimisation, never fatal
            sel = None
        # Got a fresh model: give it to this task and mark it taken.
        if sel is not None and getattr(sel, "key", None) is not None and sel.key not in assigned:
            leased[t.id] = sel.key
            assigned.add(sel.key)
        # No distinct model available: run unpinned and let the default routing decide.
        else:
            leased[t.id] = None
    return leased


# --------------------------------------------------------------------------- #
# Agent execution
# --------------------------------------------------------------------------- #
def _build_agent_prompt(task, dep_results):
    """Compose one agent's prompt: its goal plus any dependency outputs."""
    # Start with the agent's own goal.
    parts = [f"Your job:\n{task.goal}"]
    # Collect dependency results that actually produced text.
    done = [r for r in dep_results if r.output and r.output.strip()]
    # If there are any, append them as labelled context the agent can build on.
    if done:
        ctx = "\n\n".join(f"[Result of {r.task_id}]\n{r.output}" for r in done)
        parts.append(
            "You may build on these completed sub-task results:\n\n" + ctx
        )
    # Remind the agent to do only its own part.
    parts.append("Complete YOUR job only. Return just your result.")
    # Join the sections with blank lines into one prompt.
    return "\n\n".join(parts)


def _run_agent(task, dep_results, leased_key, config, call_llm_fn, task_id=None):
    """Run one agent end-to-end on its best-suited model. A pinned agent whose
    endpoint errors is retried once UNPINNED so one dead box can't strand it."""
    # Imported lazily to keep this module cheap to import.
    from model_selection import LlmRequirements
    # Build the prompt once; both the pinned attempt and the retry use it.
    prompt = _build_agent_prompt(task, dep_results)

    def _once(pin):
        """Make one LLM call for this task, optionally pinned to a model, and report which model ran."""
        # call_llm fills this dict with the key/label of the model it actually used.
        used = {}
        # Requirements for this agent: its difficulty, tool needs, and optional pin.
        reqs = LlmRequirements(
            complexity=task.complexity,
            needs_tools=task.needs_tools,
            pin_key=pin,
        )
        # Run the agent's job on the chosen (or auto-picked) model.
        out = call_llm_fn(
            prompt,
            system_prompt="You are a focused worker completing one assigned sub-task.",
            requirements=reqs,
            task_id=task_id,
            used_model_out=used,
        )
        # Return the text plus the "which model answered" details.
        return out, used

    # First attempt on the leased endpoint (or unpinned when none was leased).
    try:
        out, used = _once(leased_key)
    # The call failed outright.
    except Exception as e:  # noqa: BLE001
        # When pinned, the endpoint itself may be dead, so retry once letting the picker choose.
        if leased_key is not None:
            logger.warning(f"orchestrator: agent {task.id} pinned endpoint failed ({e}); retrying unpinned")
            try:
                out, used = _once(None)
            # The unpinned retry failed too; report the task as failed.
            except Exception as e2:  # noqa: BLE001
                return AgentResult(task_id=task.id, ok=False, error=str(e2))
        # Already unpinned, so there is nothing else to try; report the failure.
        else:
            return AgentResult(task_id=task.id, ok=False, error=str(e))

    # Package the outcome; an empty or whitespace-only reply counts as a failure.
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
    # A proper ModelKey is (provider, base_url, model); show "provider/model".
    if isinstance(key, (tuple, list)) and len(key) >= 3:
        provider, _base, model = key[0], key[1], key[2]
        return f"{provider}/{model}" if model else str(provider)
    # Anything else: show it as-is, or "?" when there is no key at all.
    return str(key) if key else "?"


# --------------------------------------------------------------------------- #
# Evaluation / merge
# --------------------------------------------------------------------------- #
def _evaluate_and_merge(request, tasks, results, config, call_llm_fn):
    """Synthesise the per-agent outputs into one final answer and lightly
    evaluate completeness. Returns (final_text, evaluated: bool). On any error
    falls back to a plain concatenation so a run always yields something."""
    # Keep only the agents that succeeded with real text.
    ok_results = [r for r in results if r.ok and r.output.strip()]
    # If every agent failed, return a summary of the errors instead of an answer.
    if not ok_results:
        failed = "; ".join(f"{r.task_id}: {r.error}" for r in results) or "no output"
        return (f"All sub-tasks failed ({failed}).", False)

    # Concatenate the good parts under "### task_id" headings; also the fallback answer.
    joined = "\n\n".join(f"### {r.task_id}\n{r.output}" for r in ok_results)
    # Ask an LLM to merge the parts into one coherent answer.
    try:
        # Imported lazily; a medium-capability model is enough for merging.
        from model_selection import LlmRequirements
        reqs = LlmRequirements(complexity="medium")
        # Prompt carrying the original request and all sub-task results.
        prompt = (
            "Combine the sub-task results below into ONE coherent, complete "
            "answer to the original request. Resolve overlaps, keep everything "
            "correct, and note anything still missing.\n\n"
            f"ORIGINAL REQUEST:\n{request}\n\nSUB-TASK RESULTS:\n{joined}"
        )
        # Run the merge call.
        merged = call_llm_fn(
            prompt,
            system_prompt="You are an integrator assembling worker outputs into a final answer.",
            requirements=reqs,
        )
        # Use the merged answer only if it is non-empty.
        if merged and merged.strip():
            return (merged, True)
    # A failed merge is logged and we fall through to the plain concatenation.
    except Exception as e:  # noqa: BLE001 — merge is best-effort
        logger.warning(f"orchestrator: merge/eval failed ({e}); returning concatenated parts")
    # Fallback: the joined parts, flagged as not evaluated.
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
    # Treat a missing config as empty settings.
    config = config or {}
    # Wire up the real LLM call, model picker, candidate list and perf stats when the caller
    # did not inject test doubles.
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

    # Read the parallelism and task-count limits (argument, then config, then default); never
    # allow less than 1.
    max_parallel = int(max_parallel or config.get("ORCHESTRATOR_MAX_PARALLEL", DEFAULT_MAX_PARALLEL) or DEFAULT_MAX_PARALLEL)
    max_parallel = max(1, max_parallel)
    max_tasks = int(max_tasks or config.get("ORCHESTRATOR_MAX_TASKS", DEFAULT_MAX_TASKS) or DEFAULT_MAX_TASKS)
    max_tasks = max(1, max_tasks)

    def _emit(event, **kw):
        """Send a progress event to the caller's callback, ignoring any error it raises."""
        # Only report progress if the caller asked for it.
        if progress_cb:
            try:
                progress_cb({"event": event, **kw})
            except Exception:  # noqa: BLE001 — a progress hook must never break the run
                pass

    # 1) Plan.
    tasks = plan_tasks(request, config, call_llm_fn, max_tasks=max_tasks)
    # More than one task means the request really fanned out and will need merging.
    planned = len(tasks) > 1
    # Tell the caller what the plan looks like.
    _emit("planned", task_count=len(tasks), tasks=[{"id": t.id, "goal": t.goal, "depends_on": list(t.depends_on)} for t in tasks])

    # Single-task DAG: no fan-out, no merge — just run it (keeps the simple
    # path cheap and behaviourally identical to a normal call).
    waves = _topological_waves(tasks) or [tasks]
    # Finished AgentResults keyed by task id; later waves read their dependencies from here.
    results_by_id = {}

    # Run each dependency wave in order.
    for wave in waves:
        # Skip tasks that already have a result (defensive; waves should not repeat tasks).
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
            """Run one task with its dependency outputs and report when it finishes."""
            # Gather the results of the tasks this one depends on.
            deps = [results_by_id[d] for d in t.depends_on if d in results_by_id]
            # Run the agent on its leased model (or unpinned).
            res = _run_agent(t, deps, leased.get(t.id), config, call_llm_fn, task_id=task_id)
            # Report which model handled this part.
            _emit("agent_done", task_id=t.id, model=res.model_label, ok=res.ok)
            return res

        # One task: just run it in this thread, no pool needed.
        if len(ready) == 1:
            r = _work(ready[0])
            results_by_id[r.task_id] = r
        # Several tasks: run them concurrently, capped at max_parallel threads.
        else:
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(ready))) as ex:
                for r in ex.map(_work, ready):
                    results_by_id[r.task_id] = r

    # Results in the planner's original task order.
    results = [results_by_id[t.id] for t in tasks if t.id in results_by_id]

    # 2) Merge/evaluate — only when the request actually fanned out.
    if planned:
        final_text, evaluated = _evaluate_and_merge(request, tasks, results, config, call_llm_fn)
    # Single task: its output already is the final answer, no merge needed.
    else:
        only = results[0] if results else AgentResult(task_id="main", ok=False, error="no result")
        final_text, evaluated = (only.output, False)
    # Tell the caller the run is finished.
    _emit("merged", evaluated=evaluated)

    # Return the answer plus per-task detail; an empty answer is flagged as an error.
    return OrchestrationResult(
        final_text=final_text,
        tasks=tasks,
        results=results,
        planned=planned,
        evaluated=evaluated,
        error=None if final_text.strip() else "empty result",
    )
