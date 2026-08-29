---
summary: "Autonomous bot, and an optional hub agent (not a spoke). Repo: ab. See architecture-topology.md."
keywords: [ab, authentication, behaviors, commands, dashboard, get_logs, hub_agent, install_github, module_type, set_log_level]
---

# ab — Autonomous GitHub Issue Fixer

Autonomous bot, and an optional hub **agent** (not a spoke). Repo: `ab`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

A standalone autonomous bot that polls GitHub repos for issues labeled `automated-fix`, generates code fixes via local/cloud LLMs, verifies (internal tests or external QA), iterates up to 3×, pushes to a branch (PR) or directly to main (trusted repos), and notifies an external infra API. It is **not a spoke** — it is a standalone FastAPI app (dashboard :443, HTTPS) that **optionally connects to the LM hub as a WebSocket agent** (`module_type = "agent"`) via `hub_agent.py`, so the hub can broadcast `SET_LOG_LEVEL` to it and it can call the hub (signed `HUB_REQUEST`) for aggregated logs and to trigger spoke self-updates.

## Entrypoints

- **Main:** `python3 main.py` (FastAPI app, poller, LLM orchestration, git workflow), systemd `ab.service`, `User=root`, `Restart=always`. Installer `install.sh` (clones to `/opt/ab`, apt + Node.js 20 + `@anthropic-ai/claude-code` CLI, `ab.service` + `ab-watchdog.service`, config to `/etc/ab/config.json`). Alternates: `setup.sh` (local), `install_github.sh` (legacy spoke-style installer), `update.sh` (hourly self-update).
- **Watchdog:** `python3 watchdog.py`, systemd `ab-watchdog.service` — polls `https://127.0.0.1:443/api/health` every 5s (scheme/port derived from `AB_PORT` and the presence of `AB_SSL_CERT`, so it stays in lockstep with `main.py`'s bind; override with `AB_HEALTH_URL`) and rolls back a failed auto-update.

## Ports

FastAPI dashboard on **443** (HTTPS, `AB_PORT`/`AB_SSL_CERT`; falls back to HTTP only when no cert is present). No WS listener — it is a WS **client** to the hub (when `HUB_WS_URL` configured); `hub_agent.py` connects with `max_size=16 MiB` to accept large `GET_LOGS` responses.

## Environment variables

- `.env`: `GITHUB_TOKEN`, `LOCAL_OLLAMA_MODEL`, `CLOUD_OLLAMA_MODEL`, `LOCAL_OLLAMA_URL`, `CLOUD_OLLAMA_URL`, `POLL_INTERVAL_SECONDS`, `UPDATE_API_URL`, `LOG_FILE_PATH` (`/var/log/ab.log`).
- `config.json`: `monitored_repos`, `trusted_repos`, `default_branch`, `self_diagnosis_repo`, `enabled_models`, `direct_push_enabled`, `dev_branch`, `delete_merged_branches`, `protected_branches`, `auto_branch_prefixes`, `repo_tests`, `GITHUB_TOKEN`, `monitored_labels` (default `["automated-fix"]`), `HUB_WS_URL`, `HUB_AGENT_ID` (default `ab`), `HUB_AGENT_SECRET`, `HUB_SECRET`, `refresh_status_seconds`, `refresh_logs_seconds`, `bug_report_enabled`, `bug_report_repo`, `TRIAGE_STRICTNESS`, `heartbeat_exclude`. LLM provider slots: `LLM_PROVIDER_N`, `LLM_API_KEY_N`, `LLM_MODEL_N`, `LLM_BASE_URL_N`, `LLM_RPM_N` (1-based; vault-based `llm_credentials`/`llm_entries`/`llm_slots` supported). Providers: openai, anthropic, google, groq, openrouter, ollama (local+cloud), lmstudio, claude_cli, copilot.

## Install flags

`install.sh`: none (curl|bash; stdin to `/dev/null`). `install_github.sh`: `--spoke-url`, `--id`, `--secret`, `--hub-secret`, `--clone-only` (legacy).

## Key commands / handlers

- **Inbound from hub** (`hub_agent.py::_handle_message`): `APPROVAL_REQUIRED`, `APPROVED`, `SPOKE_UPDATE_SESSION_KEY` (provisions session secret), `SPOKE_SET_HUB_SECRET`, `HUB_RESPONSE` (correlated reply to a `HUB_REQUEST`), `DENIED`, `get_version`/`GET_VERSION` (signed `COMMAND_RESULT` with `data.version`), `SET_LOG_LEVEL`/`SPOKE_SET_LOG_LEVEL` (WebUI "Enable Debug" broadcast).
- **Outbound to hub** (`client.request_sync`): `GET_LOGS` (aggregated spoke logs, 20s timeout), `TRIGGER_ALL_UPDATES` (kick all spoke self-updates, 60s timeout). These replace the old static-token HTTP calls (`LM_ADMIN_TOKEN`/`X-Admin-Token`) the hub never honored.
- **FastAPI routes (own dashboard):** `/api/health`, settings, status/logs endpoints, `hub_agent_status`/`hub_agent_reregister`, scan/poll/fix/verify/iterate/deploy/sync workflow. The hub agent singleton starts at app startup via `_start_hub_agent()` → `hub_agent.start_agent_from_config(...)`.
- **Promotion (`routes.py`):** `GET /api/promote/repos` (repos the Release Management buttons can target — `monitored_repos` plus the self-diagnosis repo, which is the default selection) and `POST /api/promote` (`{repo, source, target}`) which dispatches that repo's `.github/workflows/promote.yml`. See *Branch promotion buttons* below.

## Key files

`main.py` (FastAPI app — poller, LLM orchestration, git workflow, triage), `hub_agent.py` (self-contained `HubAgentClient` + `MessageSigner` reimplementing lm-core's HMAC-SHA256 scheme; daemon-thread asyncio loop), `dedup.py` (pure stdlib duplicate-issue detection, `test_dedup.py`), `watchdog.py` (health-gate + auto-update rollback), `install.sh`/`setup.sh`/`update.sh`/`install_github.sh`, `templates/index.html`, `config.json.example`, `.env.example`, `requirements.txt`, `Dockerfile`, `VERSION` (static display string, pinned by hand; change detection keys off the git commit hash, not this file).

## Authentication & multi-user

- **Local accounts** (`auth.py`, stdlib-only) — every account is a full admin. `users.json` (0600) in `/etc/ab`; scrypt/pbkdf2 hashes; signed `ab_session` cookie. First run funnels to `/setup-admin` to create the initial account, which then closes.
- **Azure Entra ID (OIDC) SSO** (`oidc.py`, routes in `routes.py`) — Authorization Code + PKCE against Entra. Configure it in the WebUI at **Settings → Sign-in (SSO)**, which reads/writes the same values via `GET`/`POST /setup/oidc-config` (both require a login session); the stored form is `config.json` under `oidc`. Needs `tenant_id`, `client_id`, `redirect_uri`, and **one** credential: a `client_secret` OR a certificate (`cert_path`/`key_path`, sent as an RS256 `client_assertion` — mirrors the LM hub). `AB_OIDC_*` env vars override stored config.
  - **`allowed_group` is the only access control AppBuilder has.** It takes comma/space-separated Entra group **object IDs** (a group *name* silently matches nothing). Leaving it blank lets **every** user in the tenant sign in — and since AppBuilder has no roles, each of them is a full admin. Setting it also requires the Entra app to emit a `groups` claim, or every sign-in fails with a "read 0 groups" error.
  - The Entra app also needs admin consent for `openid profile email offline_access`; without it, non-admin users are stopped by a consent prompt only an admin can approve, which looks like "only admins can log in".
  - Endpoints: `GET /auth/oidc/enabled` (login page probe), `GET /auth/oidc/login` (→ Entra), `GET /auth/oidc/callback` (verifies id-token signature/issuer/audience/nonce, provisions the user, issues a session). The id-token is verified against the Entra JWKS; MFA is enforced by Entra Conditional Access, not in-token.
  - Entra users are provisioned into `users.json` as passwordless `auth_type:"entra"` accounts (keyed by email/UPN, else `oid`), so they can only ever SSO in and can never password-log-in. A local password account is never silently converted.
  - Redirect URI to register in Entra: `https://<host>/auth/oidc/callback`.
- **LLM router API key** (`llm_proxy.py`) — the `/v1/*` endpoints are exempt from the session middleware (`main._AUTH_EXEMPT_PREFIX`) and do their own key check, so they are the one surface the WebUI login does **not** protect. They **fail closed**: with no key configured every request is refused with `401`. Set it in **Settings → Automation → LLM Router API Key** (a *Generate* button mints a 32-byte base64url key client-side), or via `AB_PROXY_KEY` / `llm_proxy_api_key`; the env var wins. Clients send `x-api-key` or `Authorization: Bearer`.
  - The stored key is redacted out of the template context in `settings_page` (the whole config is otherwise merged in via `{**settings, **config}`), so it is never embedded in the served HTML. The input therefore renders blank, and because Settings is ONE form that submits every field from any tab, a blank value means **keep the stored key** — clearing is explicit, via the `__CLEAR__` sentinel the *Clear* button sets.

## Notable behaviors & gotchas

- **Hub agent, not spoke** — registered in `active_connections` as `module_type="agent"`; does not register a spoke module or handle `CS_*`/`PXMX_*`/`LE_*` commands.
- **Reimplements lm-core signing** — `hub_agent.py::MessageSigner` mirrors `lm/core/src/security/signer.py` (HMAC-SHA256 canonical JSON) so it can talk to the hub without depending on lm-core.
- **Watchdog rollback** — polls `/api/health` every 5s; rolls back via `update_state.json`/`update_pending` in `/etc/ab/` only on a failed auto-update.
- **`SET_LOG_LEVEL`** — ab is in the hub's broadcast set, so the WebUI "Enable Debug" flips its log level too.
- **Branch naming (`branch_policy.auto_branch_name`)** — AppBuilder's own automation-driven branches are named `bug/<desc>` (fix_engine) or `ai-feature/<desc>` (feature_build), with the GitHub issue number prefixed onto the description when one exists (`bug/123-null-pointer-in-parser`), so they're identifiable as bot-created at a glance and traceable back to their issue. Deliberately NOT plain `feature/` — that's the existing human branch-naming convention in this repo, so reusing it would make automation branches indistinguishable from human ones and would make every human `feature/*` branch match the auto-branch allowlist below.
- **Branch cleanup is an allowlist (`branch_policy.py`)** — post-merge deletion and force-push are both gated. A branch is touchable only if it *positively* matches an `auto_branch_prefixes` entry (default `bug/`, `ai-feature/`) and is not in `protected_branches` (`main`/`master`/`dev`/`qa`/`staging`/`release`/`next` plus the configured default and dev branches are always protected). Anything unrecognised — `feature/*`, `fix/*`, `promote/*` — is kept. Configured under Settings → GitHub & Repository Core; refusals are logged with a reason. This exists because the previous code guarded only the repo's default branch, so a low-confidence fix (which uses `dev` itself as the PR head) force-pushed and then **deleted `dev` and `qa`** on merge.
- **Low-confidence fixes still target the default branch** — `fix_engine` opens those PRs `dev` → `main`, bypassing `qa`. Safe now (no force-push), but it sidesteps the dev→qa→main promotion model.
- **Branch promotion buttons (WebUI → Release Management)** — the sidebar's **Release Management** entry (`GET /release`, view `release`) holds a repo selector (populated from `GET /api/promote/repos`; defaults to AppBuilder's own self-diagnosis repo) and three promotion rows. They previously sat in the footer action bar; promoting a branch is a deliberate release decision rather than a global one-click op belonging beside *Restart*/*Blackout*, and the override needs room to state what it skips. Placement is pinned by `test_promote_routes.py` (ids appear exactly once, live inside the `release` view block, and are absent from the footer):
  - **Dev → QA** and **QA → Main** — the normal promotion path.
  - **Dev → Main** — an **override that skips qa**, styled red and guarded by a typed `PROMOTE` confirmation.

  **All three only OPEN a pull request.** They dispatch `promote.yml`, which builds a `promote/<src>-to-<tgt>` branch (code only — VERSION is branch-owned and pinned to the target) and opens a PR. **Nothing in this path merges, and nothing pushes to a branch** — the repo owner still reviews and merges. Auto-merge is a separate, opt-in mechanism gated on `feature_automerge_target_branches` (see below), which this feature does not touch.

  The permitted routes are allowlisted in **three** places, all of which must agree: `routes.PROMOTE_ROUTES`, the route `case` in `promote.yml`, and the `main` allowlist in `branch-flow.yml`. This redundancy is deliberate — AppBuilder's token is the repo owner's PAT, so it **bypasses every GitHub ruleset**; these code-level checks are the enforcement, not branch protection.

  **Sibling-repo caveat:** the normal Dev → QA / QA → Main buttons send only the `source` input, exactly as `gh workflow run promote.yml -f source=dev` always has, so they work against every repo that has `promote.yml`. The **Dev → Main** override additionally sends `target`, which only this repo's `promote.yml` currently declares — GitHub rejects a dispatch carrying an input the workflow does not declare. Until the other repos' `promote.yml` and `branch-flow.yml` are updated the same way, the override button will fail against them with an explicit message saying so (it will not silently do the wrong thing).
- **Back-merge keeps qa and dev from stranding (`.github/workflows/backmerge.yml`)** — the promotion flow is one-way, so anything reaching `main` outside it leaves the lower branches permanently behind. Two things do that routinely: the owner committing directly to `main`, and the **merge commit each promotion PR creates on `main`** (plus its VERSION bump), which `qa` never sees. This is not hypothetical — `ab`'s `main` was 4 commits ahead of `qa` from promotions alone before this existed, and the gap compounds until promotion diffs fill with changes that are "already on main".

  On every push to `main`, the workflow opens **`backmerge/main-to-qa`** and **`backmerge/main-to-dev`** pull requests. It **does not merge and does not push to qa/dev** — a conflicting back-merge surfaces for a human instead of being force-resolved by a bot. It is a **no-op when the target is already current**, so the constant runs do not produce empty PRs. **No loop:** the only trigger is a push to `main` and nothing in it writes to `main`; merging a back-merge PR pushes to qa/dev, which triggers nothing.

  It reuses `.github/scripts/promote.sh` (shared via a `LABEL` env var) rather than copying it — the VERSION pinning and conflict handling are the fiddly part and a second copy would drift. So `qa`/`dev` keep their own version lineage and main's VERSION never leaks backwards. `branch-flow.yml` allows `backmerge/main-to-qa` into `qa`; that cannot skip a step because it is the **reverse** direction. Nothing is ever back-merged *into* `main`, and `backmerge/main-to-dev` needs no entry because PRs into `dev` are ungated. Pinned by `test_backmerge.py`, which **executes** the script against throwaway repos rather than only parsing YAML — a previous workflow change shipped a bash syntax error that `yaml.safe_load` accepted happily.

- **`promote/dev-to-main` is an override, not a bypass** — `branch-flow.yml` (the required `check-direction` status check on qa/main) allows exactly `qa`, `promote/qa-to-main` and `promote/dev-to-main` into `main`. Plain `dev` and any feature branch are still rejected, and the allowlist matches **exact names, never globs** (`promote/*` would let any hand-made branch into main). The only way to produce a passing `promote/dev-to-main` head is to run `promote.yml` with `target: main`, which logs a warning identifying the run as an override. Pinned by `test_promote_routes.py`.
- **Direct pushes can never reach production (`branch_policy.may_direct_push`)** — `fix_engine`'s trusted-repo direct-push path is checked against this guard before it pushes; a refusal is logged and falls through to opening a PR. It permits the **integration branch** (`dev`, or whatever `dev_branch` is set to) and AppBuilder's own topic branches, and refuses `main`/`master`, the repo's default branch, any configured `default_branch`/`protected_branches`, and the promotion gates `qa`/`staging`/`release`/`next`. Deliberately narrower than `is_protected`, which also covers `dev` — refusing `dev` would break the ordinary trusted-repo flow. **Committing straight to `main` therefore remains possible only from a CLI session using the owner's own token by hand; neither the WebUI nor AppBuilder's automation can do it.** Pinned by `test_direct_push_guard.py`.

## Related pages

[architecture-topology.md](architecture-topology.md), [lm-hub.md](lm-hub.md), [generic-agent.md](generic-agent.md), [install-flags.md](install-flags.md).