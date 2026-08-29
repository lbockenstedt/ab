# AGENTS.md — `ab`

**AppBuilder** — an automated GitHub issue fixer. Polls repos for the `automated-fix` label, generates fixes with a local or cloud LLM, and syncs changes with the infrastructure API.

- **Repo:** `github.com/lbockenstedt/ab`
- **Module type:** `module_type = "agent"`
- **Canonical docs:** [`lm/docs/appbuilder.md`](../lm/docs/appbuilder.md) *(in the `lm` repo — the master registry)*
- **Fleet map:** [`../AGENTS.md`](../AGENTS.md) *(only present in a side-by-side checkout)*

## Context

This repo is **one of 16** that make up **Lab Manager (LM)** — a hub-and-spoke
"single pane of glass" orchestrator for lab/datacenter infrastructure. One hub (the `lm`
repo) runs the control plane, REST API and WebUI. Every other repo is a **spoke** wrapping
exactly one external system and dialling the hub over a WebSocket on port 443.

Read [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) — a verbatim
copy also lives in this repo's `docs/` — before making structural changes.

## Not a spoke

`ab` connects to the hub as an **agent-type WS client** (`module_type="agent"`), **not** a
spoke — it registers no module. It consumes hub logs and can trigger spoke self-updates.

## Layout

Flat and large (~100 top-level modules). Orientation points: `agent_orchestrator.py` (the
driver), `fix_engine.py`, `chat.py`, `batch.py`, `auth.py`, `app_state.py`, `config_store.py`,
`claude_cli_native_tools.py`, the `feature_*.py` family (allowlist/boundary/build/drive), and
the `check_*.py` guards (test regressions, tooltips, unattended mutation).
`LLM_REDESIGN_HANDOFF.md` carries in-flight redesign context.

## ab-specific gotchas

- **This thing writes code and opens PRs against the other 15 repos.** Anything you change here can act on the whole fleet. The `check_unattended_mutation.py` / `feature_allowlist.py` / `feature_boundary.py` guards exist for that reason — **do not weaken them**.
- **The only repo with a `dev` branch already pushed**, so the full `dev -> qa -> main` flow is live here.
- It is LLM-backed (local or cloud). Never commit model credentials; use `.env.example` / `config.json.example`.
- Connect it to a hub at install time via the first positional arg or `HUB_WS_URL`.
- It closes the loop with `qa`: QA files findings into AppBuilder, AppBuilder attempts fixes.

## Fleet conventions (identical in every LM repo)

- **Python 3.11**, FastAPI + `websockets` + `asyncio`. WebUI is dependency-free vanilla JS — **no npm build step exists anywhere in this project**.
- **`VERSION` is `MAJOR.NN` and branch-owned.** A bot bumps the last segment. **Never bump it by hand.** Promotion carries code only.
- **Branching: `dev -> qa -> main`.** `qa` and `main` need a PR; `ci.yml` is the required check. Direct pushes to `dev` are allowed.
- **CI runs one pytest process per component.** Components share top-level module names (`control_plane.py` exists in most repos) and collide in a single process.
- **Installers are idempotent** — re-running updates code and preserves credentials. Common flags: `--hub` (bare hostname is normalised to `wss://...:443`), `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs`.
- **Transport:** WebSocket on 443, mailbox pattern, **push-ack-retry — no fire-and-forget**. Heartbeat 30s; yellow at >=120s, red at >=300s. Hub queues 24h for offline spokes.
- **TLS:** encrypted but **verify-OFF by default** (self-signed hub cert). Verification is opt-in at install time via `--tls-verify` / `--tls-ca-cert` — never by hand-editing `.env`.
- **Heavy lifting belongs in the spoke, not the hub.** The hub is transport, state, policy and UI. See `lm/docs/architecture-spoke-heavy-lifting.md`.
- **API-first:** every operation exposes an API; the WebUI only ever calls that API.
- **Atomic transactions:** a mid-chain failure rolls back every preceding step and reports a before/after diff. No zombie resources.
- **Multitenancy is not optional:** isolation rides on Proxmox labels + NetBox tenant IDs. New resources carry tenant context.

## Rules

1. **One repo per change.** Cross-repo work is separate PRs, and the wire contract must stay backward-compatible because the two sides deploy independently.
2. **Read the canonical doc first** (linked above) — it is usually more current than this repo's README.
3. **Never hand-edit `VERSION`.**
4. **Check you are editing the live path,** not a preserved legacy one.
5. Match surrounding style. Comment only what needs clarifying.
