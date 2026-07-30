# 🤖 BugFixer

An automated GitHub issue fixer that polls repositories for the `automated-fix` label, generates code fixes using local or cloud LLMs, and synchronizes changes with an infrastructure API.

<!-- INSTALLERS:START -->
## Installation

Every installer in this repo, with every flag and environment variable it accepts.
Installers are idempotent — re-running one updates code and preserves credentials.

### BugFixer — `install.sh`

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/bugfixer/main/install.sh | bash
```

Connect it to an LM hub at install time — first positional argument, or `HUB_WS_URL`:

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/bugfixer/main/install.sh | bash -s -- wss://lm-hub.lrbtechnologies.com
curl -sSL https://raw.githubusercontent.com/lbockenstedt/bugfixer/main/install.sh | HUB_WS_URL=wss://lm-hub.lrbtechnologies.com bash
```

| Argument | Purpose |
| :--- | :--- |
| *(positional 1)* | Hub WebSocket URL. A bare host is fine — the agent normalizes it to `wss://<host>:443/ws/spoke`. |

**Environment overrides:**

| Variable | Purpose |
| :--- | :--- |
| `HUB_WS_URL` | Same as the positional argument. |
| `HUB_QUERY_URL` | Hub WebUI URL, used for approvals and as the log fallback. Derived as `https://<host>` when unset. |
| `IP_FOR_CERT` | SAN baked into the self-signed WebUI certificate. Default `127.0.0.1`. |

Installs to `/opt/bugfixer`, config in `/etc/bugfixer`, log at `/var/log/bugfixer.log`.

**The WebUI requires a login.** On first visit you are sent to `/setup-admin` to
create the initial account; every account is a full admin. Locked out:

```bash
python3 -c "import sys; sys.path.insert(0,'/opt/bugfixer'); import auth; auth.set_password('user','newpass')"
```
<!-- INSTALLERS:END -->

## 🌟 Features
- **Hybrid LLM**: Local-first (MacBook/Proxmox) with automatic failover to Cloud Ollama.
- **WebUI Dashboard**: Real-time status, Heartbeat monitoring, and Settings management.
- **Git Workflow**: Automated cloning, fixing, and pushing.
- **Direct Commit**: Support for "Trusted Repositories" that allow direct pushes to the main branch.
- **Infra Sync**: Triggers an external API update after every fix.
- **Auto-Update**: Automatically pulls the latest version of the bot from GitHub every hour.

## 🛠️ Setup & Configuration
1. Run the installer above.
2. Visit `http://<LXC-IP>:8000` to access the dashboard.
3. Navigate to the **Settings** page to configure your API tokens, LLM endpoints, and repository lists. No manual CLI editing of config files is required.

## ⚙️ How it Works
1. **Scan**: Poller finds open issues with the `automated-fix` label.
2. **Fix**: LLM generates a fix based on the issue body.
3. **Verify**: The bot runs internal tests or an external QA suite.
4. **Iterate**: If verification fails, the error is fed back to the LLM (up to 3 attempts).
5. **Deploy**: Once verified, the bot pushes to a branch (PR) or directly to main (Trusted).
6. **Sync**: An external infrastructure API is notified of the change.
