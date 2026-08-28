#!/bin/bash
set -e

echo "🔄 Updating AppBuilder from GitHub..."

# Navigate to the script's directory to ensure git commands run in the correct place
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# 1. Fetch + hard-reset to the deployed branch. /opt/ab is a deployment mirror, so a
# plain `git pull` aborts on "local changes would be overwritten" if any tracked
# file was dirtied at runtime. Reset is robust to that (matches the in-process
# self-update). Any local changes are intentionally discarded.
if [ -d ".git" ]; then
    # Track the branch this deployment is checked out on rather than a hardcoded
    # "main", so a dev/qa instance is not reset back onto main. Detached HEAD
    # (or any failure) falls back to main.
    BR=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    case "$BR" in ""|HEAD) BR=main ;; esac
    git fetch origin "$BR"
    if ! git diff --quiet HEAD; then
        echo "⚠️  Local changes present — discarding (deployment mirror):"
        git status --porcelain
    fi
    git reset --hard "origin/$BR"
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
echo "♻️ Restarting ab service..."
if [ "$(id -u)" != "0" ]; then
    # Non-root (svc_bg): ab.service is cap-locked (CAP_NET_BIND_SERVICE
    # only), so `sudo -n ab-self-restart` fails ("unable to change to root
    # gid"). Delegate to ab-watchdog (the unrestricted privileged arm)
    # via the same restart_request file the in-process _spawn_restart uses; the
    # watchdog performs the restart within ~10s. If the watchdog isn't active,
    # fall back to the root helper so a standalone `./update.sh` still works.
    if systemctl is-active --quiet ab-watchdog; then
        printf '{"requested_at": %s}\n' "$(date +%s)" > /etc/ab/restart_request
        echo "  delegated restart to ab-watchdog."
    else
        echo "  ab-watchdog not active — using root helper."
        exec sudo -n /usr/local/bin/ab-self-restart
    fi
else
    systemctl restart ab
fi

echo "✅ AppBuilder updated successfully and service restarted!"
