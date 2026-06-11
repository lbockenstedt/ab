#!/bin/bash
set -e
echo "🚀 Installing BugFixer..."
apt update && apt install -y python3-pip python3-venv git psmisc
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "📝 Please edit .env with your tokens."
fi

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
