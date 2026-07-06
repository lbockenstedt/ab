#!/bin/bash
# BugFixer Installer
#
# Pipe directly (root shell):
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/bugfixer/main/install.sh | bash
# Or download then run:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/bugfixer/main/install.sh -o /tmp/install-bugfixer.sh && bash /tmp/install-bugfixer.sh
set -e

# NOTE: do NOT `exec </dev/null` here. When this script is run via
# `curl … | bash`, bash reads the SCRIPT ITSELF from stdin — repointing fd 0
# to /dev/null makes bash hit EOF on the next line and exit immediately
# (symptom: "install ends right away, nothing logged"). Interactive hangs are
# instead prevented per-command: apt runs DEBIAN_FRONTEND=noninteractive with
# -y, and the npm / NodeSource calls get their own `</dev/null` redirect below.

REPO_URL="https://github.com/lbockenstedt/bugfixer.git"
INSTALL_DIR="/opt/bugfixer"
CONFIG_DIR="/etc/bugfixer"
LOG_FILE="/var/log/bugfixer.log"

echo "=== BugFixer Installer ==="

# 1. System dependencies
echo ">> Installing system dependencies..."
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl git build-essential python3-pip python3-venv psmisc

# Node.js (needed for Claude Code CLI)
if ! command -v node &>/dev/null; then
    echo ">> Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 </dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs </dev/null
fi

# Claude Code CLI (optional — enables the claude_cli LLM provider)
if ! command -v claude &>/dev/null; then
    echo ">> Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code --silent --no-progress </dev/null 2>&1 | tail -3
    echo "   claude CLI installed. Run 'claude auth login' on this server to authenticate."
else
    echo ">> Claude Code CLI already installed ($(claude --version 2>/dev/null | head -1))"
fi

# 2. Clone or update repo
if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo ">> Cloning BugFixer to $INSTALL_DIR..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
else
    echo ">> Updating existing install in $INSTALL_DIR..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard origin/main
fi

# 3. Persistent config directory
echo ">> Setting up config in $CONFIG_DIR..."
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/config.json" ]; then
    if [ -f "$INSTALL_DIR/config.json" ]; then
        cp "$INSTALL_DIR/config.json" "$CONFIG_DIR/config.json"
        echo "   Copied existing config.json"
    elif [ -f "$INSTALL_DIR/config.json.example" ]; then
        cp "$INSTALL_DIR/config.json.example" "$CONFIG_DIR/config.json"
        echo "   Seeded config from template — finish setup via the WebUI"
    fi
fi

# Migrate legacy local state files if present
for f in processed_issues.json .env; do
    if [ -f "$INSTALL_DIR/$f" ] && [ ! -f "$CONFIG_DIR/$f" ]; then
        cp "$INSTALL_DIR/$f" "$CONFIG_DIR/$f"
        echo "   Migrated $f to $CONFIG_DIR"
    fi
done

# 4. Log directories
touch "$LOG_FILE" && chmod 644 "$LOG_FILE"
mkdir -p /var/log/lm

# 5. Python venv + dependencies
echo ">> Installing Python dependencies (this may take a minute)..."
cd "$INSTALL_DIR"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q
echo "   Python dependencies installed."

# 6. Systemd service for BugFixer
echo ">> Installing systemd services..."
cat > /etc/systemd/system/bugfixer.service << SERVICE
[Unit]
Description=BugFixer Autonomous GitHub Issue Bot
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 main.py
Restart=always
RestartSec=10
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
SERVICE

# 7. Watchdog service
cat > /etc/systemd/system/bugfixer-watchdog.service << WSERVICE
[Unit]
Description=BugFixer Watchdog (auto-update recovery)
After=bugfixer.service

[Service]
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python3 watchdog.py
Restart=always
RestartSec=15
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
WSERVICE

chmod +x "$INSTALL_DIR/update.sh" 2>/dev/null || true

systemctl daemon-reload
systemctl enable bugfixer bugfixer-watchdog
systemctl restart bugfixer bugfixer-watchdog

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "======================================="
echo " BugFixer installed successfully"
echo "======================================="
echo "  Dashboard : http://${IP:-<server-ip>}:8000"
echo "  Config    : $CONFIG_DIR/config.json"
echo "  Logs      : journalctl -u bugfixer -f"
echo "              tail -f $LOG_FILE"
echo "  Status    : systemctl status bugfixer"
echo "======================================="
echo ""
echo "Next: open the dashboard and go to Settings to add your"
echo "GitHub token and LLM provider API keys."
