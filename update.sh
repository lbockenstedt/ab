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
    # Non-root (svc_bg): bugfixer.service is cap-locked (CAP_NET_BIND_SERVICE
    # only), so `sudo -n bugfixer-self-restart` fails ("unable to change to root
    # gid"). Delegate to bugfixer-watchdog (the unrestricted privileged arm)
    # via the same restart_request file the in-process _spawn_restart uses; the
    # watchdog performs the restart within ~10s. If the watchdog isn't active,
    # fall back to the root helper so a standalone `./update.sh` still works.
    if systemctl is-active --quiet bugfixer-watchdog; then
        printf '{"requested_at": %s}\n' "$(date +%s)" > /etc/bugfixer/restart_request
        echo "  delegated restart to bugfixer-watchdog."
    else
        echo "  bugfixer-watchdog not active — using root helper."
        exec sudo -n /usr/local/bin/bugfixer-self-restart
    fi
else
    systemctl restart bugfixer
fi

echo "✅ BugFixer updated successfully and service restarted!"
