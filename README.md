# 🤖 BugFixer

An automated GitHub issue fixer that polls repositories for the `automated-fix` label, generates code fixes using local or cloud LLMs, and synchronizes changes with an infrastructure API.

## 🌟 Features
- **Hybrid LLM**: Local-first (MacBook/Proxmox) with automatic failover to Cloud Ollama.
- **WebUI Dashboard**: Real-time status, Heartbeat monitoring, and Settings management.
- **Git Workflow**: Automated cloning, fixing, and pushing (Direct Commit or PR).
- **Infra Sync**: Triggers an external API update after every fix.
- **Force Cloud**: Manual override via UI to bypass local compute.
- **Auto-Update**: Automatically pulls the latest version of the bot from GitHub every hour.

## 🚀 Quick Start
1. Clone this repo to your Debian LXC.
2. Run `./setup.sh`.
3. Edit `.env` with your tokens.
4. Visit `http://<LXC-IP>:8000`.
