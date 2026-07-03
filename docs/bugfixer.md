# bugfixer — Autonomous GitHub Issue Fixer

Autonomous bot, and an optional hub **agent** (not a spoke). Repo: `bugfixer`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

A standalone autonomous bot that polls GitHub repos for issues labeled `automated-fix`, generates code fixes via local/cloud LLMs, verifies (internal tests or external QA), iterates up to 3×, pushes to a branch (PR) or directly to main (trusted repos), and notifies an external infra API. It is **not a spoke** — it is a standalone FastAPI app (dashboard :8000) that **optionally connects to the LM hub as a WebSocket agent** (`module_type = "agent"`) via `hub_agent.py`, so the hub can broadcast `SET_LOG_LEVEL` to it and it can call the hub (signed `HUB_REQUEST`) for aggregated logs and to trigger spoke self-updates.

## Entrypoints

- **Main:** `python3 main.py` (FastAPI app, poller, LLM orchestration, git workflow), systemd `bugfixer.service`, `User=root`, `Restart=always`. Installer `install.sh` (clones to `/opt/bugfixer`, apt + Node.js 20 + `@anthropic-ai/claude-code` CLI, `bugfixer.service` + `bugfixer-watchdog.service`, config to `/etc/bugfixer/config.json`). Alternates: `setup.sh` (local), `install_github.sh` (legacy spoke-style installer), `update.sh` (hourly self-update).
- **Watchdog:** `python3 watchdog.py`, systemd `bugfixer-watchdog.service` — polls `http://localhost:8000/api/health` every 5s and rolls back a failed auto-update.

## Ports

FastAPI dashboard on **8000** (HTTP). No WS listener — it is a WS **client** to the hub (when `HUB_WS_URL` configured); `hub_agent.py` connects with `max_size=16 MiB` to accept large `GET_LOGS` responses.

## Environment variables

- `.env`: `GITHUB_TOKEN`, `LOCAL_OLLAMA_MODEL`, `CLOUD_OLLAMA_MODEL`, `LOCAL_OLLAMA_URL`, `CLOUD_OLLAMA_URL`, `POLL_INTERVAL_SECONDS`, `UPDATE_API_URL`, `LOG_FILE_PATH` (`/var/log/bugfixer.log`).
- `config.json`: `monitored_repos`, `trusted_repos`, `default_branch`, `self_diagnosis_repo`, `enabled_models`, `direct_push_enabled`, `dev_branch`, `repo_tests`, `GITHUB_TOKEN`, `monitored_labels` (default `["automated-fix"]`), `HUB_WS_URL`, `HUB_AGENT_ID` (default `bugfixer`), `HUB_AGENT_SECRET`, `HUB_SECRET`, `refresh_status_seconds`, `refresh_logs_seconds`, `bug_report_enabled`, `bug_report_repo`, `TRIAGE_STRICTNESS`, `heartbeat_exclude`. LLM provider slots: `LLM_PROVIDER_N`, `LLM_API_KEY_N`, `LLM_MODEL_N`, `LLM_BASE_URL_N`, `LLM_RPM_N` (1-based; vault-based `llm_credentials`/`llm_entries`/`llm_slots` supported). Providers: openai, anthropic, google, groq, ollama (local+cloud), lmstudio, claude_cli.

## Install flags

`install.sh`: none (curl|bash; stdin to `/dev/null`). `install_github.sh`: `--spoke-url`, `--id`, `--secret`, `--hub-secret`, `--clone-only` (legacy).

## Key commands / handlers

- **Inbound from hub** (`hub_agent.py::_handle_message`): `APPROVAL_REQUIRED`, `APPROVED`, `SPOKE_UPDATE_SESSION_KEY` (provisions session secret), `SPOKE_SET_HUB_SECRET`, `HUB_RESPONSE` (correlated reply to a `HUB_REQUEST`), `DENIED`, `get_version`/`GET_VERSION` (signed `COMMAND_RESULT` with `data.version`), `SET_LOG_LEVEL`/`SPOKE_SET_LOG_LEVEL` (WebUI "Enable Debug" broadcast).
- **Outbound to hub** (`client.request_sync`): `GET_LOGS` (aggregated spoke logs, 20s timeout), `TRIGGER_ALL_UPDATES` (kick all spoke self-updates, 60s timeout). These replace the old static-token HTTP calls (`LM_ADMIN_TOKEN`/`X-Admin-Token`) the hub never honored.
- **FastAPI routes (own dashboard):** `/api/health`, settings, status/logs endpoints, `hub_agent_status`/`hub_agent_reregister`, scan/poll/fix/verify/iterate/deploy/sync workflow. The hub agent singleton starts at app startup via `_start_hub_agent()` → `hub_agent.start_agent_from_config(...)`.

## Key files

`main.py` (FastAPI app — poller, LLM orchestration, git workflow, triage), `hub_agent.py` (self-contained `HubAgentClient` + `MessageSigner` reimplementing lm-core's HMAC-SHA256 scheme; daemon-thread asyncio loop), `dedup.py` (pure stdlib duplicate-issue detection, `test_dedup.py`), `watchdog.py` (health-gate + auto-update rollback), `install.sh`/`setup.sh`/`update.sh`/`install_github.sh`, `templates/index.html`, `config.json.example`, `.env.example`, `requirements.txt`, `Dockerfile`, `VERSION`, `.github/workflows/version-bump.yml`.

## Notable behaviors & gotchas

- **Hub agent, not spoke** — registered in `active_connections` as `module_type="agent"`; does not register a spoke module or handle `CS_*`/`PXMX_*`/`LE_*` commands.
- **Reimplements lm-core signing** — `hub_agent.py::MessageSigner` mirrors `lm/core/src/security/signer.py` (HMAC-SHA256 canonical JSON) so it can talk to the hub without depending on lm-core.
- **Watchdog rollback** — polls `/api/health` every 5s; rolls back via `update_state.json`/`update_pending` in `/etc/bugfixer/` only on a failed auto-update.
- **`SET_LOG_LEVEL`** — bugfixer is in the hub's broadcast set, so the WebUI "Enable Debug" flips its log level too.

## Related pages

[architecture-topology.md](architecture-topology.md), [lm-hub.md](lm-hub.md), [generic-agent.md](generic-agent.md), [install-flags.md](install-flags.md).