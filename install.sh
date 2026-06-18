#!/bin/bash
# BugFixer Installer
# Usage: curl -sSL https://raw.githubusercontent.com/lbockenstedt/bugfixer/main/install.sh | sudo bash
set -e

REPO_URL="https://github.com/lbockenstedt/bugfixer.git"
INSTALL_DIR="/opt/bugfixer"
CONFIG_DIR="/etc/bugfixer"
LOG_FILE="/var/log/bugfixer.log"

echo "=== BugFixer Installer ==="

# 1. System dependencies
echo ">> Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq curl git build-essential python3-pip python3-venv psmisc

# Install Node + Claude Code CLI (for claude_cli provider)
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - &>/dev/null
    apt-get install -y -qq nodejs
fi
if ! command -v claude &>/dev/null; then
    echo ">> Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code --silent
fi

# 2. Clone or update repo
if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo ">> Cloning BugFixer to $INSTALL_DIR..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
else
    echo ">> Updating existing install..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard origin/main
fi

# 3. Persistent config directory
echo ">> Setting up config directory $CONFIG_DIR..."
mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/config.json" ]; then
    if [ -f "$INSTALL_DIR/config.json" ]; then
        cp "$INSTALL_DIR/config.json" "$CONFIG_DIR/config.json"
    elif [ -f "$INSTALL_DIR/config.json.example" ]; then
        cp "$INSTALL_DIR/config.json.example" "$CONFIG_DIR/config.json"
        echo "   Seeded config from template — configure via the WebUI at :8000/settings"
    fi
fi

# Migrate legacy local files if present
for f in processed_issues.json .env; do
    if [ -f "$INSTALL_DIR/$f" ] && [ ! -f "$CONFIG_DIR/$f" ]; then
        cp "$INSTALL_DIR/$f" "$CONFIG_DIR/$f"
    fi
done

# 4. Log file
touch "$LOG_FILE"
chmod 644 "$LOG_FILE"
mkdir -p /var/log/lm

# 5. Python virtualenv + deps
echo ">> Installing Python dependencies..."
cd "$INSTALL_DIR"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# 6. Systemd service
echo ">> Installing systemd service..."
cat > /etc/systemd/system/bugfixer.service << SERVICE
[Unit]
Description=BugFixer Autonomous GitHub Issue Bot
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 main.py
Restart=always
RestartSec=10
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

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
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 watchdog.py
Restart=always
RestartSec=15
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
WSERVICE

chmod +x "$INSTALL_DIR/update.sh" 2>/dev/null || true

systemctl daemon-reload
systemctl enable bugfixer bugfixer-watchdog
systemctl restart bugfixer bugfixer-watchdog

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== BugFixer installed successfully ==="
echo "   Dashboard : http://$IP:8000"
echo "   Config    : $CONFIG_DIR/config.json"
echo "   Logs      : $LOG_FILE"
echo "   Status    : sudo systemctl status bugfixer"
echo ""
echo "Open the dashboard and go to Settings to configure your GitHub token and LLM providers."
