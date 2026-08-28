#!/bin/bash
# AppBuilder Installer
#
# Pipe directly (root shell):
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/ab/main/install.sh | bash
# Connect to an LM hub at install time (first arg via `bash -s --`, or env):
#   curl -sSL .../install.sh | bash -s -- wss://lm-hub.example.com
#   curl -sSL .../install.sh | HUB_WS_URL=wss://lm-hub.example.com bash
# Or download then run:
#   curl -sSL .../install.sh -o /tmp/install-ab.sh && bash /tmp/install-ab.sh wss://lm-hub.example.com
set -e

# NOTE: do NOT `exec </dev/null` here. When this script is run via
# `curl … | bash`, bash reads the SCRIPT ITSELF from stdin — repointing fd 0
# to /dev/null makes bash hit EOF on the next line and exit immediately
# (symptom: "install ends right away, nothing logged"). Interactive hangs are
# instead prevented per-command: apt runs DEBIAN_FRONTEND=noninteractive with
# -y, and the npm / NodeSource calls get their own `</dev/null` redirect below.

REPO_URL="https://github.com/lbockenstedt/ab.git"
INSTALL_DIR="/opt/ab"
CONFIG_DIR="/etc/ab"
LOG_FILE="/var/log/ab.log"

# Optional LM-hub connection, baked into config.json below. First positional arg
# (via `bash -s -- <url>`) OR the HUB_WS_URL env var. A bare host is fine — the
# agent normalizes it to wss://<host>:443/ws/spoke. HUB_QUERY_URL (hub WebUI URL,
# for approvals + log fallback) is derived as https://<host> unless set.
HUB_WS_URL="${1:-${HUB_WS_URL:-}}"
HUB_QUERY_URL="${HUB_QUERY_URL:-}"

echo "=== AppBuilder Installer ==="

# 1. System dependencies
echo ">> Installing system dependencies..."
DEBIAN_FRONTEND=noninteractive apt-get update -qq
# NOTE: `sudo` is REQUIRED — a minimal Debian LXC (Proxmox template) ships
# without it, so /etc/sudoers.d/ wouldn't exist and the sudoers drop-in below
# would fail with "no such file or directory". svc_bg also invokes its root
# helpers via passwordless sudo, so the package is genuinely needed at runtime.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl git build-essential python3-pip python3-venv psmisc openssl zstd sudo

# Dedicated service user (mirrors lm/install_all.sh svc_lm). ab + its
# watchdog run as svc_bg; the two genuinely root-only capabilities (the Docker
# sandbox for untrusted repo code, and ollama service management) stay behind
# narrow root helpers invoked via passwordless sudo (see the sudoers drop-in
# below). /opt/ab is the home dir so claude auth login / git creds land
# in ~svc_bg, not ~root.
SVC_USER="svc_bg"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    echo ">> Creating service user $SVC_USER..."
    # No -m: /opt/ab is created by the git clone below (with -m, useradd
    # would pre-create it + skel litter and the clone would refuse a non-empty
    # target). chown after the clone makes svc_bg own its home.
    useradd -r -d /opt/ab -s /usr/sbin/nologin "$SVC_USER"
fi

# Node.js + npm (needed only for the OPTIONAL Claude Code CLI). Trigger the
# install when EITHER is missing — a box with a node from elsewhere (Debian's
# split nodejs package) can have node but no npm, which used to skip this block
# and then fail at `npm install` with "npm: command not found".
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo ">> Installing Node.js + npm..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 </dev/null || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs </dev/null || true
fi

# Claude Code CLI (OPTIONAL — enables the claude_cli LLM provider). NEVER fatal:
# a missing/failed npm must not abort the whole install — the bot runs fine with
# a cloud LLM configured in the WebUI instead.
if command -v claude >/dev/null 2>&1; then
    echo ">> Claude Code CLI already installed ($(claude --version 2>/dev/null | head -1))"
elif command -v npm >/dev/null 2>&1; then
    echo ">> Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code --silent --no-progress </dev/null 2>&1 | tail -3 || true
    echo "   claude CLI installed. Run 'claude auth login' on this server to authenticate."
else
    echo "   ⚠️  npm unavailable — skipping the optional Claude Code CLI (configure a cloud LLM in the WebUI instead)."
fi

# 2. Clone or update repo
# Git's dubious-ownership guard fires when a user runs git on a repo owned by
# ANOTHER user — here root re-running the installer over an already svc_bg-owned
# tree (the fetch/reset below), or svc_bg's update.sh over a root-owned file.
# Whitelist the tree SYSTEM-WIDE (/etc/gitconfig) so every user can operate on it.
git config --system --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo ">> Cloning AppBuilder to $INSTALL_DIR..."
    # AB_BRANCH lets a dev/qa host be provisioned straight onto its branch.
    git clone --depth 1 --branch "${AB_BRANCH:-main}" "$REPO_URL" "$INSTALL_DIR"
else
    echo ">> Updating existing install in $INSTALL_DIR..."
    # Follow the branch the install is on so re-running the installer on a
    # dev/qa host does not silently move it back to main.
    BR=$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    case "$BR" in ""|HEAD) BR=main ;; esac
    git -C "$INSTALL_DIR" fetch origin "$BR"
    git -C "$INSTALL_DIR" reset --hard "origin/$BR"
fi
# svc_bg owns the whole tree (git clone/pull/push, the venv, claude creds in
# ~svc_bg). Migrates an existing root-owned install on re-run.
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

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

# Bake the hub connection into config.json when a URL was supplied (arg/env).
# Updates the two keys in place — everything else in config.json is preserved,
# so it's safe on a re-run. HUB_QUERY_URL is derived from the ws host when unset.
if [ -n "$HUB_WS_URL" ]; then
    echo ">> Configuring LM hub connection → $HUB_WS_URL"
    HUB_WS_URL="$HUB_WS_URL" HUB_QUERY_URL="$HUB_QUERY_URL" \
    CONFIG_FILE="$CONFIG_DIR/config.json" python3 - <<'PY'
import json, os
from urllib.parse import urlsplit
cf = os.environ["CONFIG_FILE"]
ws = os.environ["HUB_WS_URL"].strip()
query = os.environ.get("HUB_QUERY_URL", "").strip()
try:
    cfg = json.load(open(cf))
except Exception:
    cfg = {}
cfg["HUB_WS_URL"] = ws
if not query:
    u = ws if "://" in ws else "wss://" + ws
    parts = urlsplit(u)
    host, port = parts.hostname or "", parts.port
    if host:
        query = "https://" + host + ("" if port in (None, 443) else f":{port}")
if query:
    cfg["HUB_QUERY_URL"] = query
with open(cf, "w") as fh:
    json.dump(cfg, fh, indent=2)
print(f"   HUB_WS_URL={cfg.get('HUB_WS_URL')}")
print(f"   HUB_QUERY_URL={cfg.get('HUB_QUERY_URL', '(none)')}")
PY
fi

# Migrate legacy local state files if present
for f in processed_issues.json .env; do
    if [ -f "$INSTALL_DIR/$f" ] && [ ! -f "$CONFIG_DIR/$f" ]; then
        cp "$INSTALL_DIR/$f" "$CONFIG_DIR/$f"
        echo "   Migrated $f to $CONFIG_DIR"
    fi
done

# 4. Log directories — owned by svc_bg so the unit's StandardOutput=append
# (and the watchdog's) can write them. Watchdog uses a separate log file.
WATCHDOG_LOG="/var/log/ab_watchdog.log"
touch "$LOG_FILE" "$WATCHDOG_LOG"
chmod 644 "$LOG_FILE" "$WATCHDOG_LOG"
chown "$SVC_USER:$SVC_USER" "$LOG_FILE" "$WATCHDOG_LOG"
mkdir -p /var/log/lm

# Circular logging: cap /var/log/lm/*.log so it can't fill the disk (copytruncate
# keeps the inode → the running spoke's O_APPEND FileHandler + systemd stderr
# keep appending). Belt-and-suspenders alongside logging_setup's RotatingFileHandler.
cat > /etc/logrotate.d/lm <<'LOGROTATE'
/var/log/lm/*.log /var/log/client-sim-*.log {
    su root root
    size 50M
    rotate 5
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
LOGROTATE

# 5. Python venv + dependencies
echo ">> Installing Python dependencies (this may take a minute)..."
cd "$INSTALL_DIR"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q
# Re-chown the venv: the tree-wide chown above runs BEFORE this step, and the
# venv is created and populated HERE as root, so every site-packages file would
# otherwise be left root-owned. The service runs as $SVC_USER, and CPython writes
# bytecode next to the source on first import -- so the first import after a
# restart fails with
#   [Errno 13] Permission denied: .../site-packages/__pycache__/<mod>.pyc.<pid>
# which surfaces as "Update check failed" rather than anything mentioning
# permissions or the venv. Cheap, idempotent, and repairs an existing install on
# re-run.
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR/venv"
echo "   Python dependencies installed."

# 5b. Self-signed TLS cert (unified-443: the UI serves HTTPS on :443)
CERT_FILE="$CONFIG_DIR/cert.pem"
KEY_FILE="$CONFIG_DIR/key.pem"
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo ">> Generating self-signed TLS certificate in $CONFIG_DIR..."
    IP_FOR_CERT=$(hostname -I 2>/dev/null | awk '{print $1}')
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -subj "/CN=${IP_FOR_CERT:-ab}" \
        -addext "subjectAltName=IP:${IP_FOR_CERT:-127.0.0.1},IP:127.0.0.1,DNS:localhost" \
        >/dev/null 2>&1 \
      && echo "   Certificate created (self-signed, 10y)." \
      || warn_cert=1
    chmod 600 "$KEY_FILE" 2>/dev/null || true
    chmod 644 "$CERT_FILE" 2>/dev/null || true
    [ "${warn_cert:-0}" = 1 ] && echo "   ⚠️  cert generation failed — UI will fall back to plain HTTP on :443"
else
    echo ">> Reusing existing TLS certificate in $CONFIG_DIR"
fi
# svc_bg owns the config dir so the running service can read config.json, the
# .env (token/keys), and the TLS cert/key (key.pem stays chmod 600 — owned by
# svc_bg, readable only by it). Migrates an existing root-owned dir on re-run.
chown -R "$SVC_USER:$SVC_USER" "$CONFIG_DIR"

# 6. Systemd service for AppBuilder
echo ">> Installing systemd services..."
cat > /etc/systemd/system/ab.service << SERVICE
[Unit]
Description=AppBuilder Autonomous GitHub Issue Bot
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
User=${SVC_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=AB_HOST=0.0.0.0
Environment=AB_PORT=443
Environment=AB_SSL_CERT=${CERT_FILE}
Environment=AB_SSL_KEY=${KEY_FILE}
# svc_bg binds the privileged 443 without being root (mirrors lm.service's
# AmbientCapabilities=CAP_NET_BIND_SERVICE). CapabilityBoundingSet drops
# everything else, so the unit has no other ambient root powers.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
ExecStart=${INSTALL_DIR}/venv/bin/python3 main.py
Restart=always
RestartSec=10
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
SERVICE

# 7. Watchdog service
cat > /etc/systemd/system/ab-watchdog.service << WSERVICE
[Unit]
Description=AppBuilder Watchdog (auto-update recovery)
After=ab.service

[Service]
User=${SVC_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=AB_PORT=443
Environment=AB_SSL_CERT=${CERT_FILE}
Environment=AB_SSL_KEY=${KEY_FILE}
ExecStart=${INSTALL_DIR}/venv/bin/python3 watchdog.py
Restart=always
RestartSec=15
StandardOutput=append:${WATCHDOG_LOG}
StandardError=append:${WATCHDOG_LOG}

[Install]
WantedBy=multi-user.target
WSERVICE

# 7b. Root helpers + sudoers drop-in. ab runs as svc_bg (no ambient root
# powers), but two capabilities genuinely need root: the Docker sandbox that
# runs UNTRUSTED repo code (fix_engine.run_sandboxed_command), and ollama
# service management (install + /etc override + restart). Plus its own
# self-restart, which must run from OUTSIDE ab.service's cgroup to avoid
# the ~min-strand race a bare `systemctl restart ab` hits from inside
# the unit (same bug lm hit — see lm/install_all.sh lm-self-restart). Each
# helper is a narrow, root-owned path; the sudoers drop-in grants svc_bg ONLY
# these three exact paths (no direct systemctl, no docker, no apt).
echo ">> Installing root helpers + sudoers for $SVC_USER..."

# Self-restart via a transient systemd unit owned by PID 1, so the restart
# command survives ab.service being stopped (mirrors lm-self-restart).
cat > /usr/local/bin/ab-self-restart <<'HELPER'
#!/bin/bash
# Schedules a ab.service restart from a transient unit outside
# ab's cgroup. Invoked by ab as `sudo -n /usr/local/bin/ab-self-restart`.
set -euo pipefail
_unit="ab-self-restart-$$-$RANDOM"
exec systemd-run --no-block --quiet --collect \
    --unit="$_unit" --service-type=oneshot \
    /bin/bash -c 'sleep 3; exec systemctl restart ab'
HELPER

# Docker sandbox for untrusted repo code. Takes <image> <cwd> <command>; the
# command is passed to docker as a single argv element (sh -c inside the
# container) — no host-side shell parsing, so svc_bg can't inject host args.
# cwd MUST be under /opt/ab so a compromised svc_bg can't bind-mount
# arbitrary host dirs into a root container.
cat > /usr/local/bin/ab-sandbox <<'HELPER'
#!/bin/bash
# Runs untrusted repo code in a Docker container as root. Invoked by fix_engine
# as `sudo -n /usr/local/bin/ab-sandbox <image> <cwd> <command>`.
set -euo pipefail
image="${1:?usage: ab-sandbox <image> <cwd> <command>}"
cwd="${2:?missing cwd}"
cmd="${3:?missing command}"
case "$cwd" in
    /opt/ab/*) : ;;
    *) echo "cwd must be under /opt/ab (got $cwd)" >&2; exit 2 ;;
esac
exec docker run --rm -v "$cwd:/app" -w /app "$image" sh -c "$cmd"
HELPER

# Ollama privileged setup: install ollama if absent, write the CPU-tuning
# systemd override, daemon-reload + restart. The HTTP-API stages (pull model,
# create derived model, verify reachable) stay in ab running as svc_bg.
# Args: <num_thread>. Prints progress lines to stdout for ab to relay.
cat > /usr/local/bin/ab-ollama-setup <<'HELPER'
#!/bin/bash
# Privileged stages of ab's Local LLM Setup. Invoked by main.py as
# `sudo -n /usr/local/bin/ab-ollama-setup <num_thread>`.
set -uo pipefail
num_thread="${1:-1}"
max_loaded="${2:-3}"    # OLLAMA_MAX_LOADED_MODELS — how many models stay resident at once
# OLLAMA_HOST — the address ollama BINDS to. Upstream defaults to 127.0.0.1, which
# makes the API reachable only from the box itself; other hosts (a second
# ab, the hub, a workstation) get connection-refused. Bind all interfaces
# so the model server is usable across the lab.
#
# NOTE: ollama has NO authentication. Anything that can reach this port can run
# inference AND manage models (pull/delete). Keep it on a trusted segment, or
# pass a specific address (e.g. 172.16.1.100:11434) as the 3rd argument to bind
# one interface instead of all of them.
bind_host="${3:-0.0.0.0:11434}"

say() { echo "$1"; }

# zstd is required by the ollama installer's tarball extraction.
if ! command -v zstd >/dev/null 2>&1; then
    say ">> installing zstd..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zstd >/dev/null 2>&1 || { say "ERROR: zstd install failed"; exit 1; }
fi

# Install ollama if the binary is absent AND the HTTP API isn't already up.
ollama_up() { curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }
if ! ollama_up && [ ! -x /usr/local/bin/ollama ] && [ ! -x /usr/bin/ollama ]; then
    say ">> installing ollama..."
    if bash -c 'curl -fsSL https://ollama.com/install.sh | sh' >/dev/null 2>&1; then
        say "ok ollama installed"
    else
        say "ERROR: ollama installer failed"; exit 1
    fi
else
    say "ok ollama already installed"
fi

# Ensure the service is up.
if [ "$(systemctl is-active ollama 2>/dev/null)" != "active" ]; then
    say ">> starting ollama..."
    systemctl start ollama || { say "ERROR: systemctl start ollama failed"; exit 1; }
fi

# CPU-tuning override.
OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"
OVERRIDE="$OVERRIDE_DIR/override.conf"
wanted=$(printf '[Service]\nEnvironment="OLLAMA_NUM_PARALLEL=1"\nEnvironment="OLLAMA_KEEP_ALIVE=-1"\nEnvironment="OLLAMA_NUM_THREAD=%s"\nEnvironment="OLLAMA_MAX_LOADED_MODELS=%s"\nEnvironment="OLLAMA_HOST=%s"\n' "$num_thread" "$max_loaded" "$bind_host")
current=""
[ -f "$OVERRIDE" ] && current=$(cat "$OVERRIDE" 2>/dev/null || true)
if [ "$current" != "$wanted" ]; then
    say ">> writing ollama systemd override..."
    mkdir -p "$OVERRIDE_DIR"
    printf '%s' "$wanted" > "$OVERRIDE" || { say "ERROR: override write failed"; exit 1; }
    systemctl daemon-reload || { say "ERROR: daemon-reload failed"; exit 1; }
    if ! systemctl restart ollama; then
        say "ERROR: systemctl restart ollama failed"; exit 1
    fi
    say "ok override applied + ollama restarted (listening on $bind_host)"
else
    say "ok override already current"
fi
say "done"
HELPER

cat > /usr/local/bin/ab-claude-install <<'HELPER'
#!/bin/bash
# Root helper: install the Claude Code CLI FOR THE AB SERVICE USER.
#
# Invoked as `sudo -n /usr/local/bin/ab-claude-install <svc_user>` by
# ab-watchdog on behalf of the cap-locked main service (which cannot
# escalate). Also run directly at install time.
#
# Installed as the SERVICE USER, not root, on purpose: `claude` authenticates
# with a per-user session, so a root-owned install leaves the service able to
# find the binary but not to use it ("needs_auth" forever). Installing under the
# service user's HOME means "Start Login Flow" authenticates the account that
# will actually run it.
set -uo pipefail
SVC="${1:-svc_bg}"
HOME_DIR="$(getent passwd "$SVC" | cut -d: -f6)"
if [ -z "$HOME_DIR" ]; then echo "no home dir for user '$SVC'"; exit 2; fi
mkdir -p "$HOME_DIR/.local/bin"
chown -R "$SVC" "$HOME_DIR/.local" 2>/dev/null || true
if [ -x "$HOME_DIR/.local/bin/claude" ]; then
    echo "claude already installed at $HOME_DIR/.local/bin/claude"
    sudo -u "$SVC" "$HOME_DIR/.local/bin/claude" --version 2>&1 || true
    exit 0
fi
echo ">> Installing Claude Code for $SVC (HOME=$HOME_DIR)..."
# Official installer, run AS the service user so it lands in their HOME.
if ! sudo -u "$SVC" HOME="$HOME_DIR" bash -c 'curl -fsSL https://claude.ai/install.sh | bash' 2>&1; then
    echo "installer failed (no network, or the install URL changed)"; exit 1
fi
if [ -x "$HOME_DIR/.local/bin/claude" ]; then
    # Symlink so the binary resolves even on systemd's minimal PATH.
    ln -sf "$HOME_DIR/.local/bin/claude" /usr/local/bin/claude
    echo "installed: $HOME_DIR/.local/bin/claude (symlinked into /usr/local/bin)"
    exit 0
fi
echo "installer completed but no binary at $HOME_DIR/.local/bin/claude"; exit 1
HELPER

chown root:root /usr/local/bin/ab-self-restart /usr/local/bin/ab-sandbox /usr/local/bin/ab-ollama-setup /usr/local/bin/ab-claude-install
chmod 0755 /usr/local/bin/ab-self-restart /usr/local/bin/ab-sandbox /usr/local/bin/ab-ollama-setup /usr/local/bin/ab-claude-install

# Sudoers: svc_bg may invoke ONLY these four exact paths, passwordless.
# No direct systemctl, no docker, no apt — least privilege.
mkdir -p /etc/sudoers.d   # belt-and-suspenders: exists once `sudo` is installed
cat > /etc/sudoers.d/ab <<SUDOERS
# Grants the ab service user (svc_bg) passwordless access to its four
# narrow root helpers ONLY. Mirrors lm/install_all.sh's /etc/sudoers.d/lm.
${SVC_USER} ALL=(root) NOPASSWD: /usr/local/bin/ab-self-restart
${SVC_USER} ALL=(root) NOPASSWD: /usr/local/bin/ab-sandbox *
${SVC_USER} ALL=(root) NOPASSWD: /usr/local/bin/ab-ollama-setup *
${SVC_USER} ALL=(root) NOPASSWD: /usr/local/bin/ab-claude-install *
SUDOERS
chmod 440 /etc/sudoers.d/ab
# Validate the sudoers syntax before leaving it in place (visudo -c would
# refuse a broken drop-in on next sudo load, stranding ab's root ops).
if command -v visudo >/dev/null 2>&1; then
    visudo -cf /etc/sudoers.d/ab >/dev/null 2>&1 || echo "   ⚠️  sudoers syntax check failed — review /etc/sudoers.d/ab"
fi

# 7c. Claude Code CLI for the service user (best-effort; the claude_cli provider
# slot needs the binary AND a session owned by the user that runs it). Never
# fatal — ab works fine on the API-key providers without it.
echo ">> Installing Claude Code CLI for $SVC_USER (optional provider)..."
/usr/local/bin/ab-claude-install "$SVC_USER" || \
    echo "   ⚠️  Claude Code CLI not installed — the claude_cli slot stays unavailable until it is."

chmod +x "$INSTALL_DIR/update.sh" 2>/dev/null || true

systemctl daemon-reload
systemctl enable ab ab-watchdog
systemctl restart ab ab-watchdog

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "======================================="
echo " AppBuilder installed successfully"
echo "======================================="
echo "  Dashboard : https://${IP:-<server-ip>}/   (self-signed cert — accept the browser warning)"
echo "  Config    : $CONFIG_DIR/config.json"
if [ -n "$HUB_WS_URL" ]; then
echo "  Hub       : $HUB_WS_URL  → APPROVE 'ab' in the LM Hub WebUI (Setup → Spokes & Agents)"
fi
echo "  Logs      : journalctl -u ab -f"
echo "              tail -f $LOG_FILE"
echo "  Status    : systemctl status ab"
echo "======================================="
echo ""
echo "Next: open the dashboard and go to Settings to add your"
echo "GitHub token and LLM provider API keys."
if [ -n "$HUB_WS_URL" ]; then
echo "Then approve the pending 'ab' agent in the LM Hub WebUI so it can connect."
fi
