#!/bin/bash
set -e
REPO_URL="https://github.com/lbockenstedt/bugfixer.git"
INSTALL_DIR="/opt/bugfixer"

echo "🚀 Installing BugFixer..."

# 1. System dependencies
apt-get update && apt-get install -y curl git gnupg build-essential python3-pip python3-venv psmisc
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs
npm install -g @anthropic-ai/claude-code

# 2. Clone or Update repository
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Cloning repository to $INSTALL_DIR..."
    git clone $REPO_URL $INSTALL_DIR
else
    echo "Repository directory already exists. Updating to latest version..."
    cd $INSTALL_DIR
    git fetch origin
    git reset --hard origin/main
    cd - > /dev/null
fi

# 2b. Persistent Config Setup
echo "📁 Ensuring persistent configuration directory..."
mkdir -p /etc/bugfixer

# Migrate local configs to persistent storage if not already there
if [ -f "$INSTALL_DIR/config.json" ] && [ ! -f "/etc/bugfixer/config.json" ]; then
    echo "Migrating config.json to /etc/bugfixer..."
    cp "$INSTALL_DIR/config.json" /etc/bugfixer/config.json
fi
if [ -f "$INSTALL_DIR/.env" ] && [ ! -f "/etc/bugfixer/.env" ]; then
    echo "Migrating .env to /etc/bugfixer..."
    cp "$INSTALL_DIR/.env" /etc/bugfixer/.env
fi
if [ -f "$INSTALL_DIR/processed_issues.json" ] && [ ! -f "/etc/bugfixer/processed_issues.json" ]; then
    echo "Migrating processed_issues.json to /etc/bugfixer..."
    cp "$INSTALL_DIR/processed_issues.json" /etc/bugfixer/processed_issues.json
fi

cd $INSTALL_DIR

# 3. Setup environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. .env creation
if [ ! -f .env ]; then
    echo "📝 Creating initial .env file..."
    cat << 'SOTP' > .env
GITHUB_TOKEN=
LOCAL_OLLAMA_MODEL=gemma4:31b-coding-mtp-bf16
CLOUD_OLLAMA_MODEL=gemma4:31b-cloud
LOCAL_OLLAMA_URL=http://172.16.1.100:11434
CLOUD_OLLAMA_URL=
POLL_INTERVAL_SECONDS=3600
UPDATE_API_URL=
SOTP
    echo "✅ Initial .env created. Please configure your keys via the WebUI."
fi

# 5. Systemd Service
cat << 'SERVICE' > /etc/systemd/system/bugfixer.service
[Unit]
Description=GitHub BugFixer Hybrid LLM
After=network.target
[Service]
User=root
WorkingDirectory=/opt/bugfixer
ExecStart=/opt/bugfixer/venv/bin/python3 main.py
Restart=always
[Install]
WantedBy=multi-user.target
SERVICE

chmod +x update.sh

systemctl daemon-reload && systemctl enable bugfixer && systemctl restart bugfixer
echo "✅ Installation complete. Dashboard at http://$(hostname -I | awk '{print $1}'):8000"
