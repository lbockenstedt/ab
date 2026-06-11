#!/bin/bash
set -e
echo "🚀 Installing BugFixer..."

# 1. System dependencies
apt update && apt install -y python3-pip python3-venv git psmisc

# 2. Setup environment
mkdir -p /opt/bugfixer
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. Robust .env creation
if [ ! -f .env ]; then
    echo "📝 .env file not found. Creating from template..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "✅ .env created from .env.example. PLEASE EDIT IT WITH YOUR TOKENS."
    else
        echo "❌ Error: .env.example not found. Creating a blank .env file."
        cat << 'SOTP' > .env
GITHUB_TOKEN=
LOCAL_OLLAMA_MODEL=gemma4:31b-coding-mtp-bf16
CLOUD_OLLAMA_MODEL=gemma4:31b-cloud
LOCAL_OLLAMA_URL=http://172.16.1.100:11434
CLOUD_OLLAMA_URL=
POLL_INTERVAL_SECONDS=3600
UPDATE_API_URL=
SOTP
        echo "✅ Blank .env created. PLEASE EDIT IT."
    fi
fi

# 4. Systemd Service
cat << 'SERVICE' > /etc/systemd/system/ai-fixer.service
[Unit]
Description=GitHub AI-Fixer Hybrid LLM
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

systemctl daemon-reload && systemctl enable ai-fixer && systemctl restart ai-fixer
echo "✅ Installation complete. Dashboard at http://$(hostname -I | awk '{print $1}'):8000"
