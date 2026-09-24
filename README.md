# 🤖 AppBuilder — Autonomous Defect Remediation & Audit Spoke (Lab Manager Module)

**AppBuilder (`ab`)** is an autonomous GitHub issue and Pull Request auditor, defect remediation engine, and code quality orchestrator. It continuously monitors fleet repositories, performs multi-panel code reviews, generates automated fixes via local GPU and cloud LLMs, and verifies changes against comprehensive test suites before promotion.

See [`docs/ab.md`](docs/ab.md) for the complete architecture reference and [`docs/architecture-topology.md`](docs/architecture-topology.md) for fleet topology.

## Core Capabilities & Architecture

1. **Dual-Panel Review Engine (`reviewers.py`):**
   - **Skeptical Review Panel (`### 🧠 Skeptical review (panel)`):** Deep adversarial audit examining PR intent, scope creep, unintended regressions, missing tests, and contract violations.
   - **State-Logic & Control-Flow Panel (`### 🔀 State-logic / control-flow review (panel)`):** Formal verification of asynchronous state transitions, race conditions, error recovery, idempotency, and rollback handling.
   - **Composite Scoring:** Combines panel verdicts into a deterministic composite recommendation (`Recommendation: APPROVE` or `Recommendation: DENY`).
   - **Remediation Objective (`pr_remediate.should_remediate`):** AppBuilder's job on a PR is not to publish an opinion — it is to **drive the panel approval score as close to 100% as it can get it, unattended, and only then merge**. Any verdict below Approve, any panel score under `pr_remediate_target_score` (default 0.95), or *any* individual reviewer who dissented or rated the change low (`pr_remediate_address_all_concerns`, default on) triggers an automatic repair. Remediation always runs **before** the auto-merge decision, because the merge threshold sits below the remediation target.
2. **Automated PR Fix Loop (`pr_fix_loop.py`):**
   - Scans active PRs across all 16 fleet repositories.
   - Parses AppBuilder panel rejections and extracts complete defect specifications.
   - Penalizes failing models with rework penalties in the native model reliability registry (`reliability.db`).
   - Dispatches targeted remediation tasks to the best replacement workers.
3. **Model Router & Reliability Database (`model_router.py` / `router.py`):**
   - Dynamically selects workers across local GPU (Ollama Apple M4 Max), Anthropic Claude, OpenAI, and Google Gemini based on capability tiers, latency, and real-world reliability scores.
   - Implements an amplified rework penalty ($2\times$) and a 45-minute cooldown for repeated model failures.
4. **Twin Repository Synchronization (`twin_sync.py`):**
   - Maintains real-time parity across twin repository mirrors with automated bidirectional sync and branch policy enforcement.
5. **Deterministic Tier-1 Linters:**
   - **`check_tooltips.py`:** Zero-LLM deterministic UI tooltip verification ensuring all interactive HTML/JS controls carry descriptive `title=` attributes.
   - **`secrets_scan.py`:** Pre-review regex scan blocking accidental leaks of credentials, keys, or tokens.

## Agent & Spoke Commands Reference

AppBuilder operates as an autonomous worker and optionally as a connected hub agent (`module_type = "agent"`):

| Command | Direction | Payload | Description |
| :--- | :--- | :--- | :--- |
| `APPROVAL_REQUIRED` | Inbound from Hub | `{"request_id": "...", ...}` | Prompts operator approval for sensitive actions |
| `APPROVED` | Inbound from Hub | `{"request_id": "..."}` | Continues execution of an approved action |
| `DENIED` | Inbound from Hub | `{"request_id": "..."}` | Aborts denied action execution |
| `SET_LOG_LEVEL` | Inbound from Hub | `{"level": "DEBUG\|INFO"}` | Dynamically adjusts logging verbosity |
| `GET_VERSION` | Inbound from Hub | `{}` | Returns current AppBuilder engine version |
| `GET_LOGS` | Outbound to Hub | `{"spoke_id": "...", "lines": N}` | Requests aggregated spoke logs from the hub |
| `TRIGGER_ALL_UPDATES` | Outbound to Hub | `{}` | Broadcasts update signals across connected spokes |

<!-- INSTALLERS:START -->
## Installation

Every installer in this repo, with every flag and environment variable it accepts.
Installers are idempotent — re-running one updates code and preserves credentials.

### AppBuilder — `install.sh`

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/ab/main/install.sh | bash
```

Connect it to an LM hub at install time — first positional argument, or `HUB_WS_URL`:

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/ab/main/install.sh | bash -s -- wss://lm-hub.lrbtechnologies.com
curl -sSL https://raw.githubusercontent.com/lbockenstedt/ab/main/install.sh | HUB_WS_URL=wss://lm-hub.lrbtechnologies.com bash
```

| Argument | Purpose |
| :--- | :--- |
| `HUB_WS_URL` | Hub WebSocket URL. |
| `HUB_QUERY_URL` | Hub WebUI URL, used for approvals and log fallback. |
| `IP_FOR_CERT` | SAN for self-signed WebUI certificate (default `127.0.0.1`). |

Installs to `/opt/ab`, config in `/etc/ab`, log at `/var/log/ab.log`.
<!-- INSTALLERS:END -->

## 🔀 LLM Router Proxy (point Claude Code at AppBuilder)

AppBuilder exposes an **Anthropic Messages API-compatible** endpoint at `/v1/messages` on its normal listener. Any Anthropic client — notably **Claude Code** — can send requests to it, and AppBuilder routes each one to the **best available LLM for the job** using its capability/cost-aware model selection.

Point Claude Code at it with environment variables:

```bash
export ANTHROPIC_BASE_URL="https://<ab-host>:<port>"
export ANTHROPIC_API_KEY="<your-proxy-key>"
claude
```

Endpoints: `POST /v1/messages`, `POST /v1/messages/count_tokens`, `GET /v1/models`.
