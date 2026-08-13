# LLM Selection Redesign — Handoff / Remaining Work

Branch: `feature/llm-capability-cost-picker` (based on `origin/main`).
Full plan: `/Users/lbockenstedt/.claude/plans/agile-snacking-lark.md` (still the
source of truth for design intent — this file just tracks build status against it).

## Done (commits on this branch, in order)

1. `b11bb97` — **Phase 2**: pure modules. `model_registry.py` (capability/cost-tier
   resolution), `model_selection.py` (`select_model()` — filtering, cost-tier
   ranking, cold-start-safe scoring, relative exhaustion, `safety_floor()`),
   `llm_perf.py` (sample store: window/age-out/median). 104 test checks across
   `test_model_registry.py` / `test_model_selection.py` / `test_llm_perf_store.py`.
2. `913cdc5` — **Phase 3**: instrumentation. All 6 `_request_*` functions in
   `llm_client.py` gained a `usage_out=` out-parameter (deliberately NOT the
   plan's literal "envelope return" — an out-param doesn't touch the return-value
   contract 18 call sites depend on, and survives `asyncio.to_thread` unlike a
   contextvar). `_call_provider_timed` wraps `_try_provider`'s dispatch with wall-
   clock latency + a `llm_perf.record()` call. Fixed a real deadlock (reentrant
   non-reentrant-lock acquire) caught by the new test.
3. `7dd4089` — **Phase 4**: `call_llm(..., requirements=<LlmRequirements>)` — a
   fully separate routing path, coexisting with (not replacing) the existing
   `task_kind`/slot machinery. `_enumerate_candidates` / `_iter_configured_endpoints`
   / `_configured_entries` / `_try_candidate` / `_call_llm_with_requirements` are
   all new, in `llm_client.py`. Identity-keyed circuit breakers
   (`_ENDPOINT_CREDIT_CB` by `(provider,base_url)`, `_MODEL_RATE_CB` by full
   ModelKey) are new and separate from the slot-keyed `_PROVIDER_CREDIT_CB`.
4. `4409fde` — **Phase 5, call site #1**: `pr_review.py`'s pr_summary
   (`_summarize_changes`) converted to `requirements=LlmRequirements(...)` and
   is now the first (and still only) real caller of `requirements=`. Also gave
   `call_llm()`/`_call_llm_with_requirements()` new `batch_kind=`/`batch_context=`
   params, wiring `batch.py`'s previously-dead `enqueue()`/`register_handler()`
   fire-and-forget path into production for the first time (see Phase 5 section
   below for details). New test: `test_pr_summary_batch_routing.py`.

**Verification state**: all 30 `test_*.py` files in the repo pass
(`for f in test_*.py; do python3 "$f"; done` — every one prints `ALL CASES
PASSED`). This includes every pre-existing test, confirming the 18 live call
sites are behaviorally unchanged — `requirements=` currently has **zero real
callers**, it's only exercised by its own test files.

## Known gaps / deliberate scope cuts worth revisiting

- **Streaming calls get no tok/s** (latency only). Adding
  `stream_options={"include_usage":true}` risks a 400 on strict OpenAI-compatible
  servers (LM Studio) — flagged as plan risk R6, deferred rather than risked.
  Would need a per-provider allowlist or catch-400-and-retry-without.
- **The legacy `llm_tps.json` panel still runs, unchanged, in parallel** with the
  new `llm_perf.json` store. Left alive on purpose so Settings' "Model
  Performance" panel doesn't go blank mid-migration — retire it only when Phase 7
  actually ships the new panel.
- **`_try_candidate` is a conservative subset of `_try_provider`'s error
  handling** — covers credit exhaustion, rate-limit (incl. claude_cli session
  limits), nothing else. Missing on purpose: ollama 404/403 detail-message
  rewriting, tool-calling-400 retry-without-tools, routed-404-model retry. Port
  each in as Phase 5 converts a call site that actually needs it — don't
  pre-build the whole set speculatively.
- **Concurrency for the `requirements=` path reuses the existing per-category
  semaphore** under a new `"PICKER"` category key, rather than building the
  plan's full 3-layer (global / per-endpoint / per-ModelKey) design. The
  per-ModelKey lock (layer 3) IS implemented (`_model_lock`). Layers 1/2 are
  simplified until Phase 5 gives this path real concurrent traffic to validate
  against.
- **`must_escalate_to_human` is not wired to anything yet** — `LlmRequirements`
  has the field, but `_call_llm_with_requirements` just raises a generic
  exception when every candidate + the safety floor fail. The actual
  `awaiting_human` state-setting behavior (reset tree, comment, label
  `bugfixer-needs-human`, persist status) lives in `fix_engine.py` today and is
  explicitly Phase 8 scope per the plan — don't build it early.

## Remaining phases (unstarted)

Tracked as Task IDs #11–#14 in this session's task list; re-create if starting
fresh.

### Phase 5 — convert all 18 call sites to `requirements=`
The plan's own ordering (cheapest/lowest-risk first), with file:line from the
plan's requirement table (§2) — **re-verify line numbers before editing**, they
will have drifted:
1. ✅ **DONE** (commit `4409fde`) — `pr_review.py:296` pr_summary. Also wired
   the batch route: `llm_client.py`'s `call_llm()`/`_call_llm_with_requirements()`
   now accept `batch_kind=`/`batch_context=`; when `reqs.batch_ok` and eligible
   (no tools, no explicit `messages`, `config['batch_enabled']`, cloud
   candidate), the call goes through `batch.enqueue()` (previously dead code
   per the plan's retirement map, R5) and returns `""` immediately instead of
   blocking. `pr_review.py` gained `_apply_batched_pr_summary(context, text)`,
   registered via `register_handler("pr_summary", ...)` — reconnects to the PR
   via a fresh `Github(token)`, discards if `head_sha` is stale, else injects
   the summary into the existing marker comment. New test:
   `test_pr_summary_batch_routing.py` (13 cases, all passing). Full existing
   suite re-run clean (30+ files).
2. `github_ops.py:318` dedup adjudication  ← **next up**
3. `llm_client.py:2355` analyze_logs (takes `requirements` as a **caller-supplied
   param**, not a constant — `routes.py:1575` and `hub_agent.py:443` need
   different profiles)
4. `log_scan.py:283` log_review
5. `fix_engine.py:308` triage
6. `fix_engine.py:506` identify_files
7. `feature_drive.py:172` classifier
8. `chat.py:733/790/807/813` (4 sites)
9. `hub_agent.py:1219` HELP_ASK proxy
10. `fix_engine.py:742/768/785` reviewer (3 sites — the panel)
11. `feature_build.py:129` build agent (`needs_mutating_agent=True`)
12. `fix_engine.py:1614` fix generation — **last**, riskiest/most expensive,
    also the one that sets `must_escalate_to_human=True`

Each conversion: replace `task_kind=` (and any `force_provider`/`force_cloud`/
`model_override`) with a `requirements=LlmRequirements(...)` built per the
plan's §2 table, remove the old param usage at that call site only. Test each
site individually before moving to the next — don't batch multiple sites into
one commit given the risk profile the plan itself calls out (R9).

### Phase 6 — deletion only
Only after Phase 5 removes every reader: `model_router.py` (whole file), the
ladder in `fix_engine.py` (~lines 2162-2195, 2292-2424 per the plan — re-verify),
the CPU ensemble (`_crosscheck_review`, `_run_cpu_ensemble`), 8 slot constants +
`_slots_for_task` + `_get_provider_config`/`_get_provider_rpm`/
`_get_escalation_models`/`_find_claude_cli_slot`, `_chat_force_provider`,
`task_kind`/`force_provider`/`force_cloud`/`model_override` params on
`call_llm`. Also delete the now-superseded slot-keyed `_PROVIDER_CREDIT_CB` /
`_SLOT_LOCKS` in favor of the identity-keyed ones from Phase 4. Also: delete
`_record_ollama_tps` + the `llm_tps.json` path, now that Phase 5 will have
proven `llm_perf.json` in real traffic.

### Phase 7 — migration + Settings UI
`llm_migrate.py` (new, not yet written — see plan §7): idempotent one-shot
`llm_slots`/`escalation_models` cleanup, `chat_slot`→`chat_pin`. Settings UI:
Model Registry editor (JSON textarea + preview table, reuse the
`feature_boundary.py` pattern), retire the 3 slot sections into one Endpoints
list, reshape the Model Performance panel onto `llm_perf.json`
(provider|base_url|model keying — rekey the JS/header-chip consumers too, per
plan §8), `llm_diag` becomes a dry-run picker.

### Phase 8 — behavior-change wiring
`awaiting_human` re-trigger on `must_escalate_to_human` + `select_model() is
None`; the two `fix_engine.py` quirk fixes called out in the plan's retirement
map (QA-fail tree reset, invalid-JSON `error_context`) — both are intentional
behavior changes, call them out in release notes per the plan.

## Before resuming

1. `git log --oneline origin/main..HEAD` to confirm branch state matches this
   file (4 commits expected: `b11bb97`, `913cdc5`, `7dd4089`, plus the earlier
   independent API-key fix already shipped via PR #824).
2. `for f in test_*.py; do python3 "$f" 2>&1 | tail -1; done` — every file should
   print `ALL CASES PASSED` before any new work starts.
3. Re-read the plan file — line numbers in both the plan and this handoff will
   have drifted from any commits landed elsewhere on `main` in the meantime.
