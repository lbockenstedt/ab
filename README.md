# 🤖 BugFixer

An automated GitHub issue fixer that polls repositories for the `automated-fix` label, generates code fixes using local or cloud LLMs, and synchronizes changes with an infrastructure API.

## 🚀 Quick Installation

For a fresh Debian installation, use this one-liner:

```bash
git clone <your-repo-url> /opt/bugfixer && cd /opt/bugfixer && sudo ./setup.sh
```

*Replace `<your-repo-url>` with the actual repository URL.*

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
