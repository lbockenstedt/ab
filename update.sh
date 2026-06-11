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
echo "♻️ Restarting ai-fixer service..."
systemctl restart ai-fixer

echo "✅ BugFixer updated successfully and service restarted!"
EOF
