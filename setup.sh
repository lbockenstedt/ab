#!/bin/bash
set -e
echo "🚀 Installing AppBuilder..."

# 1. System dependencies
apt-get update && apt-get install -y curl git gnupg build-essential python3-pip python3-venv psmisc
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs
npm install -g @anthropic-ai/claude-code

# 2. Setup environment
mkdir -p /opt/ab
mkdir -p /var/log/ab
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. .env creation
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

# 4. Systemd Service
cat << 'SERVICE' > /etc/systemd/system/ab.service
[Unit]
Description=GitHub AppBuilder Hybrid LLM
After=network.target
[Service]
User=root
WorkingDirectory=/opt/ab
ExecStart=/opt/ab/venv/bin/python3 main.py
Restart=always
[Install]
WantedBy=multi-user.target
SERVICE

chmod +x update.sh

systemctl daemon-reload && systemctl enable ab && systemctl restart ab
echo "✅ Installation complete. Dashboard at http://$(hostname -I | awk '{print $1}'):8000"
