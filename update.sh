#!/bin/bash
set -e

echo "🔄 Updating BugFixer from GitHub..."

# Navigate to the script's directory to ensure git commands run in the correct place
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# 1. Pull latest changes
if [ -d ".git" ]; then
    git pull origin main
else
    echo "❌ Error: This directory is not a git repository. Please run setup.sh first."
    exit 1
fi

# 2. Update dependencies
if [ -d "venv" ]; then
    echo "📦 Updating Python dependencies..."
    ./venv/bin/pip install -r requirements.txt
else
    echo "❌ Error: Virtual environment not found. Please run setup.sh."
    exit 1
fi

# 3. Restart service
echo "♻️ Restarting bugfixer service..."
if [ "$(id -u)" != "0" ]; then
    # Non-root (svc_bg via sudoers): use the race-free root helper that
    # re-execs into a transient systemd unit so the restart survives this
    # shell exiting. Mirrors main.py _spawn_restart / watchdog.spawn_restart.
    exec sudo -n /usr/local/bin/bugfixer-self-restart
else
    systemctl restart bugfixer
fi

echo "✅ BugFixer updated successfully and service restarted!"
