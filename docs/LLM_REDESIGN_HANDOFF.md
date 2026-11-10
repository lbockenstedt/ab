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
5. `6a53ccb`/`a36bc1b`/`c8e7e02`/`b99bf91`/`4efca3c`/`b11aa87`/`9cbf515`/
   `8ff9a73`/`4fbb0e8`/`738e259`/`bf59f2b` — **Phase 5, ALL remaining call
   sites (#2-#12, 12 of 12)** — see the Phase 5 section below for full detail
   per site. Sites **#10** (reviewer panel) and **#12** (fix generation) were
   initially left blocked on a design question (no per-call specific-
   provider-pin field in `LlmRequirements`), then resolved: both convert to
   picker-driven diversity/escalation instead of a literal pin — see their
   entries below.

**Verification state**: the full `test_*.py` suite (now 30+ files, growing as
each site gains/extends a test) passes after every commit
(`for f in test_*.py; do python3 "$f"; done` — every one prints `ALL CASES
PASSED`). **All 12 Phase 5 call sites now have real `requirements=` callers.**
Phase 6 (deletion of the now-dead slot machinery) is unblocked.

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
  `ab-needs-human`, persist status) lives in `fix_engine.py` today and is
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
2. ✅ **DONE** (commit `6a53ccb`) — `github_ops.py:318` dedup adjudication
   (`_llm_confirms_same_issue`): `requirements=LlmRequirements(complexity=
   "trivial", needs_structured_output=True, min_context_tokens=...)`.
3. ✅ **DONE** (commit `a36bc1b`) — `llm_client.py`'s `analyze_logs` (takes
   `requirements` as a **caller-supplied param**, not a constant): `routes.py`'s
   `_run_log_analysis` passes `latency_sensitive=True` (human watching);
   `hub_agent.py`'s `_handle_analyze_logs` passes `latency_sensitive=False`
   (background executor thread).
4. ✅ **DONE** (commit `c8e7e02`) — `log_scan.py:283` log_review
   (`analyze_logs_for_errors`): `complexity="small", needs_structured_output=
   True`. Found (not fixed, out of scope) a pre-existing dead-code bug: the
   JSON-array regex can never match a bare `{...}` object, so the "wrap single
   object into a list" branch is unreachable.
5. ✅ **DONE** (commit `b99bf91`) — `fix_engine.py:308` triage (`analyze_issue`):
   `complexity="trivial", needs_structured_output=True`.
6. ✅ **DONE** (commit `4efca3c`) — `fix_engine.py:506` identify_files
   (`identify_files_to_fix`): `complexity="small", needs_structured_output=True`.
7. ✅ **DONE** (commit `b11aa87`) — `feature_drive.py:172` classifier
   (`classify()`): `complexity="small", needs_structured_output=True`, existing
   `json_schema=` passed through unchanged.
8. ✅ **DONE** (commit `9cbf515`) — `chat.py:733/790/807/813` (4 sites):
   `_run_chat_reply_simple` and `run_chat_reply`'s no-tools/tool-loop/fallback
   turns all converted (`complexity="small"|"medium"`, `needs_tools=True` on
   the tool-loop call, `latency_sensitive=True` on all 4 — a human is always
   waiting on chat). `_chat_force_provider` left defined but now dead — actual
   deletion is Phase 6.
9. ✅ **DONE** (commit `8ff9a73`) — `hub_agent.py`'s HELP_ASK proxy (inside
   `_handle_message`; the plan's line 1219 had drifted, real call site
   verified via grep): `complexity="medium", needs_tools=bool(tools),
   latency_sensitive=True`. This site had NO prior task_kind/force_provider —
   purely additive.
10. ✅ **DONE** (commit `738e259`) — `fix_engine.py:742/768/785` reviewer (3
    sites — the panel, `_run_reviewer_turn`). **Initially blocked, then
    resolved**: `review_fix` used to iterate a slot pool (`_REVIEW_SLOTS`/
    `_CODE_SLOTS`) to give each reviewer a SPECIFIC, DIFFERENT configured
    provider, pinned via `force_provider=provider_n`. Since `LlmRequirements`
    has no per-call specific-provider-pin field by design, the redesign
    instead makes diversity the PICKER's job: new `_select_review_panel(config,
    builder_n=None, builder_key=None, max_reviewers=4)` calls
    `model_selection.select_model()` repeatedly with a growing
    `exclude_models` set (seeded with the builder's model) to auto-pick up to
    4 distinct candidates. `_run_reviewer_turn` now takes the exact picked
    `reviewer_candidate` dict (not `provider_n`/`model`) and dispatches every
    branch (native claude_cli, tools-blind, tool-calling loop) directly
    against that ONE fixed candidate via `llm_client._try_candidate` — the
    picker path's own single-candidate executor, no re-picking mid-review.
    `review_fix` gained a `builder_key=` param (preferred over the legacy
    `builder_n=` slot number when given). New test:
    `test_fix_engine_reviewer_panel.py` (15 cases).
11. ✅ **DONE** (commit `4fbb0e8`) — `feature_build.py:129` build agent
    (`_run_build_agent`): `complexity="large", needs_mutating_agent=True,
    needs_structured_output=True`. `needs_mutating_agent=True` uniquely
    resolves to claude_cli in `model_registry.py`, replacing
    `_find_claude_cli_slot` per the plan's explicit note. `build_feature`'s
    early fast-fail check (before the clone) now calls `select_model()`
    directly against `_enumerate_candidates(config)` instead of
    `_find_claude_cli_slot`.
12. ✅ **DONE** (commit `bf59f2b`) — `fix_engine.py:1614` fix generation
    (`apply_ai_fix`). **Same root-cause conflict as site #10, resolved the
    same way, plus the plan's `next_attempt_requirements()` helper.**
    `process_single_issue`'s retry loop used to build a fixed
    `ladder = [(slot, model), ...]` across configured provider slots (+
    per-slot `escalation_models`, including ollama `"*"`-expansion to every
    installed local model) and walk it with `ladder[(attempt-1) %
    len(ladder)]` on low-confidence retry, threading the pinned slot into
    `review_fix`'s `builder_n` and an opt-in CPU/local-ensemble path. That
    whole ladder-construction block is deleted. New
    `next_attempt_requirements(prev_reqs, failure_kind, tried_key, config)`
    (per plan §3) is the entire redesign in one function: every failure kind
    (`invalid_json`, `review_rejected`, `low_confidence`, `qa_failed`,
    `error`) excludes the just-tried `ModelKey`; ONLY `low_confidence`
    additionally raises `complexity` one rank. `llm_preference`
    ("cloud"/"local"/"claude") now maps straight onto
    `LlmRequirements.restrict` instead of resolving a slot number up front.
    `apply_ai_fix` gained `requirements=`/`used_model_out=` params alongside
    its existing `force_cloud=`/`force_provider=`/`model_override=` (same
    dual-path convention as `call_llm` itself) — so `_run_cpu_ensemble`'s and
    `_crosscheck_review`'s still-slot-based calls are untouched; only the
    normal (non-ensemble) attempt loop converts. `llm_client.py` gained
    `used_model_out=` (mirrors the existing `usage_out=` convention — lets a
    caller learn WHICH model actually built a given result, without touching
    `call_llm`'s return-value contract) and `LlmHumanEscalationNeeded`
    (raised instead of silently falling to the safety floor when
    `reqs.must_escalate_to_human` is True and `select_model()` found nothing
    — only this one call site opts in; every other `requirements=` site is
    unaffected). Also fixed a small pre-existing gap: the "invalid JSON"
    parse failure branch never set `last_failure` at all, silently reusing
    stale failure info on retry — now its own `invalid_json` failure kind.
    New test: `test_fix_engine_retry_triggers.py` (17 cases); 3 new cases
    added to `test_llm_client_requirements_path.py` for `used_model_out=`/
    `must_escalate_to_human=`. The CPU ensemble (`local_ensemble` opt-in) is
    explicitly out of scope — the plan already marks it retiring entirely in
    Phase 6 (site #6).

**All 12 Phase 5 call sites are now converted.** Phase 6 (deletion) can
proceed — `fix_engine.py`'s ladder and `_REVIEW_SLOTS`/`_CODE_SLOTS` are gone;
the CPU ensemble (`_crosscheck_review`, `_run_cpu_ensemble`, still
slot-based) and 8 slot constants/`_slots_for_task`/etc. are the remaining
readers of `force_provider`/`task_kind` to clean up in Phase 6.

Each conversion: replace `task_kind=` (and any `force_provider`/`force_cloud`/
`model_override`) with a `requirements=LlmRequirements(...)` built per the
plan's §2 table, remove the old param usage at that call site only. Test each
site individually before moving to the next — don't batch multiple sites into
one commit given the risk profile the plan itself calls out (R9).

### Phase 6 — deletion (DONE, commits `2b18d62`, `6d4a4e2`)
- **CPU ensemble retired** (`2b18d62`): deleted `_crosscheck_review`,
  `_run_cpu_ensemble`, `_filter_ensemble_models`, `_model_param_b` and the
  `local_ensemble` caller block in `process_single_issue`. The standard
  requirements= retry loop + the `review_fix` multi-reviewer panel (Phase 5,
  site #10) already provide capability/cost-aware building and N-model review,
  so the duplicate orchestrator retires entirely. `apply_ai_fix` dropped
  `force_cloud`/`force_provider`/`model_override`/`task_kind` (builds a default
  `large`/structured `LlmRequirements` when none given); the Default Reviewer
  fallback in `_run_reviewer_turn` now uses `requirements=medium`; workers'
  `preload_ollama_models` uses `model_registry.local_models_for_preload`. The
  dead ensemble config surface (`local_ensemble`,
  `ensemble_skip_external_at_full`, `EXTERNAL_MIN_CONFIDENCE`,
  `CPU_CROSSCHECK_TARGET`, `ensemble_min_model_b`) was removed from the Settings
  UI, `save_settings`, and the status endpoint.
- **`call_llm` is requirements-only** (`6d4a4e2`): the entire legacy body (slot
  pool, Provider 1→4 failover, busy-wait loop) and the
  `force_cloud`/`force_provider`/`task_kind`/`model_override`/`url_override`
  params are gone; `call_llm` now requires a `requirements=LlmRequirements` and
  delegates to `_call_llm_with_requirements`. Deleted the now-dead
  `_slots_for_task`, `_pool_category_name`, `_get_escalation_models`,
  `_provider_rate_limit_wait` (+ `_PROVIDER_REQUEST_TIMES`), and `_SLOT_LOCKS`.
  Removed `test_llm_concurrency.py` (regression guard for the deleted slot-pool
  concurrency model); added `test_llm_client_call_llm_wrapper.py`.
- **Still present, intentionally deferred to the Phase 7 diagnostics rework**
  (they still have live readers): `model_router.py`, the 8 slot constants
  (`_CODE_SLOTS`/`_LOG_SLOTS`/`_REVIEW_SLOTS`/`_ALL_SLOTS`),
  `_get_provider_config`/`_get_provider_rpm`/`_find_claude_cli_slot`, the
  slot-keyed `_PROVIDER_CREDIT_CB`, and `_record_ollama_tps`/`llm_tps.json`.
  Their only remaining consumers are `llm_diag` and the `routes.py`/`workers.py`
  per-slot Diagnostics tables — retiring those consumers **is** the Phase 7c
  Settings-UI work below, so the constants come out with it rather than being
  stranded mid-rework.

### Phase 7 — migration + Settings UI
- **Security (DONE, `f408f81`)**: `GET /api/llm/config` and the Settings render
  path already redact `api_key` to a `has_key`/`configured` presence flag (a
  prior commit). Fixed the follow-on display bug where three template checks
  still tested `cred.get('api_key')` (always falsy post-redaction), so the
  "Saved"/"Connected" badges and Copilot sign-out button never rendered.
- **Migration (DONE, `8264790`)**: `llm_migrate.migrate(config)` — idempotent,
  guarded by `llm_config_version==2`, called once at startup. Adds `enabled`
  flags to `llm_entries` (True if slotted), drops `escalation_models`, deletes
  `llm_slots`, converts `chat_slot`→`chat_pin`, seeds `model_registry_auto`
  stubs for unmatched endpoints. `test_llm_migrate.py`.
- **Settings UI reshape (NOT DONE — deferred)**: the Model Registry editor +
  preview table, retiring the 3 slot sections into one Endpoints list, the
  Model Performance panel rekey (`provider|base_url|model`), and `llm_diag`
  becoming a dry-run picker. This is the large, template/JS-heavy piece the
  Python `test_*.py` suite can't cover; it is coupled to the Phase 6 deferrals
  above (deleting `model_router.py` + the slot constants). It should be done as
  its own focused change with manual UI verification, not folded into the
  deletion commits.

### Phase 8 — behavior-change wiring (DONE, `2b18d62`/`bf59f2b`/this branch)
All three intentional behavior changes are wired and covered:
- **`awaiting_human` re-trigger** on `must_escalate_to_human=True` +
  `select_model()` returning None — `LlmHumanEscalationNeeded` raised in
  `llm_client._call_llm_with_requirements`, caught in `process_single_issue`,
  which resets the tree, labels `ab-needs-human`, and marks the issue
  `awaiting_human` (no silent safety-floor fallback).
- **Invalid-JSON sets `error_context`** — the `invalid_json` failure kind feeds
  the reviewer/parse failure back into the next attempt's prompt.
- **QA-fail (and invalid-JSON) tree reset** — every failure branch in the retry
  loop (`review_rejected`/`low_confidence`/`error` already did;
  `qa_failed`/`invalid_json` were the two gaps now fixed) does
  `git reset --hard HEAD && git clean -fd` before the next attempt, so
  attempt N+1 builds against pristine source instead of inheriting attempt N's
  rejected edits. Guarded by `test_fix_engine_retry_triggers.py`.

## Release notes — intentional behavior changes (Phases 6–8)

Operator-facing changes to call out in the next release:

- **The CPU ensemble is removed.** The `local_ensemble` opt-in and its knobs
  (`ensemble_skip_external_at_full`, `EXTERNAL_MIN_CONFIDENCE`,
  `CPU_CROSSCHECK_TARGET`, `ensemble_min_model_b`) no longer exist. Fixes are
  now built and reviewed by the capability/cost-aware picker + multi-reviewer
  panel for every issue, not just when the ensemble was enabled. These settings
  are dropped on first save; no action needed.
- **Model routing is capability/cost-aware, not slot-ordered.** The old
  Provider 1→2→3→4 slot order and per-task slot pools (Code/Log/Review) are
  gone. Endpoints are ranked by capability and cost tier per call. On first
  boot the config is migrated to v2: entries that were assigned to a slot stay
  enabled; entries that were configured but never slotted are **disabled** and
  must be re-enabled in Settings if wanted (this matches their old behavior —
  an unslotted entry was never used).
- **Issues can now be held for human review at selection time.** If no
  configured model meets a fix's requirements, the issue is labeled
  `ab-needs-human` and marked `awaiting_human` instead of silently
  falling back — surfaced in the dashboard.
- **Retries always start from a clean working tree.** A fix attempt that fails
  QA (or returns invalid JSON) no longer leaves its rejected edits in the tree
  for the next attempt to build on top of.


## Before resuming

1. `git log --oneline origin/main..HEAD` to confirm branch state — expect the
   Phase 2-4 commits plus one commit per Phase 5 site landed so far (see
   "Done" list above for hashes).
2. `for f in test_*.py; do python3 "$f" 2>&1 | tail -1; done` — every file should
   print `ALL CASES PASSED` before any new work starts.
3. Re-read the plan file — line numbers in both the plan and this handoff will
   have drifted from any commits landed elsewhere on `main` in the meantime.
4. **Phase 5 is fully done (all 12 sites).** Phase 6 (deletion of now-dead
   slot machinery) can proceed: `model_router.py`, 8 slot constants +
   `_slots_for_task`, `_chat_force_provider`, `task_kind`/`force_provider`/
   `force_cloud`/`model_override` params on `call_llm` are all safe to
   delete. The CPU ensemble (`_crosscheck_review`, `_run_cpu_ensemble`,
   `local_ensemble` config opt-in) is explicitly still slot-based and
   untouched by Phase 5 — its retirement (site #6: "becomes a
   `select_panel`-style N-model call inside `review_fix`") is itself part of
   Phase 6's scope, not a Phase 5 leftover.
