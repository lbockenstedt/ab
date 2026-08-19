#!/bin/bash
# AppBuilder Uninstaller
#
# Pipe directly (root shell):
#   curl -fsSL https://raw.githubusercontent.com/lbockenstedt/ab/main/uninstall.sh | bash
#
#   --purge-deps   also remove what the installer added FOR ab: ollama
#                  (incl. downloaded models), the Claude Code CLI, and Node.js.
#   --keep-config  leave /etc/ab (credentials, tokens, PR history) in place.
#   --yes, -y      no confirmation prompt.
#
# WHAT THIS DELIBERATELY DOES NOT REMOVE
# --------------------------------------
# The installer apt-installs: curl git build-essential python3-pip python3-venv
# psmisc openssl zstd sudo. Those are BASE SYSTEM packages that other things —
# including your remote access — depend on. Removing `sudo` or `curl` on a
# headless box is how you lock yourself out of it, and apt would happily take
# half the system with them. They are left alone even under --purge-deps; if you
# genuinely want them gone, remove them by hand where you can see what apt plans
# to take with them.
#
# /etc/logrotate.d/lm is also left alone: despite living next to ab's
# install it rotates /var/log/lm/*.log and /var/log/client-sim-*.log — it belongs
# to lm and client-sim, and deleting it would silently stop THEIR log rotation.
set -uo pipefail

INSTALL_DIR="/opt/ab"
CONFIG_DIR="/etc/ab"
LOG_FILE="/var/log/ab.log"
SVC_USER="svc_bg"

PURGE_DEPS=0
KEEP_CONFIG=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --purge-deps)  PURGE_DEPS=1 ;;
        --keep-config) KEEP_CONFIG=1 ;;
        --yes|-y)      ASSUME_YES=1 ;;
        -h|--help)     sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "WARNING: ignoring unknown argument '$arg'" >&2 ;;
    esac
done
# Non-interactive (curl | bash) can't prompt — stdin is the script itself.
[ -t 0 ] || ASSUME_YES=1

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: run as root (e.g. sudo bash $0)" >&2
    exit 1
fi

say() { echo "$*"; }
ok()  { echo "   ok   $*"; }
skip(){ echo "   --   $*"; }

say "AppBuilder uninstaller"
say ""
say "Will remove:"
say "  services   ab.service, ab-watchdog.service"
say "  files      $INSTALL_DIR, $LOG_FILE"
[ "$KEEP_CONFIG" -eq 1 ] && say "  config     KEPT ($CONFIG_DIR)" || say "  config     $CONFIG_DIR  (credentials, tokens, PR history)"
say "  helpers    /usr/local/bin/ab-*, /etc/sudoers.d/ab"
say "  user       $SVC_USER"
if [ "$PURGE_DEPS" -eq 1 ]; then
    say "  deps       ollama (+ models), Claude Code CLI, Node.js"
else
    say "  deps       KEPT (pass --purge-deps to remove ollama / claude / node)"
fi
say ""
say "NOT removed: curl git build-essential python3-* psmisc openssl zstd sudo"
say "             (base packages — removing them can break the system)"
say "             /etc/logrotate.d/lm (belongs to lm + client-sim)"
say ""

if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Proceed? [y/N]: " reply </dev/tty || reply=""
    case "$reply" in [Yy]*) ;; *) echo "Aborted."; exit 0 ;; esac
fi

# ── 1. Services ─────────────────────────────────────────────────────────────
say ">> Stopping services..."
for unit in ab.service ab-watchdog.service; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${unit}"; then
        systemctl stop "$unit" >/dev/null 2>&1
        systemctl disable "$unit" >/dev/null 2>&1
        rm -f "/etc/systemd/system/$unit"
        ok "removed $unit"
    else
        skip "$unit not installed"
    fi
done
# Any transient restart unit the self-restart helper may have scheduled.
systemctl stop 'ab-restart-*' >/dev/null 2>&1 || true
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl reset-failed >/dev/null 2>&1 || true

# Kill anything still running out of the install dir. The watchdog restarts the
# service, so this runs only AFTER both units are stopped and disabled —
# otherwise it would be restarted from under us mid-uninstall.
if pgrep -f "$INSTALL_DIR" >/dev/null 2>&1; then
    pkill -f "$INSTALL_DIR" >/dev/null 2>&1
    sleep 1
    pkill -9 -f "$INSTALL_DIR" >/dev/null 2>&1
    ok "stopped leftover processes"
fi

# ── 2. Root helpers + sudoers ───────────────────────────────────────────────
say ">> Removing root helpers..."
# sudoers FIRST: leaving a NOPASSWD rule pointing at a path that no longer
# exists is a footgun — anyone who can create that path gets root.
if [ -f /etc/sudoers.d/ab ]; then
    rm -f /etc/sudoers.d/ab
    ok "removed /etc/sudoers.d/ab"
else
    skip "no sudoers drop-in"
fi
for h in ab-self-restart ab-sandbox ab-ollama-setup ab-claude-install; do
    if [ -e "/usr/local/bin/$h" ]; then rm -f "/usr/local/bin/$h"; ok "removed $h"; fi
done

# ── 3. Files ────────────────────────────────────────────────────────────────
say ">> Removing files..."
[ -d "$INSTALL_DIR" ] && { rm -rf "$INSTALL_DIR"; ok "removed $INSTALL_DIR"; } || skip "$INSTALL_DIR absent"
if [ "$KEEP_CONFIG" -eq 1 ]; then
    skip "kept $CONFIG_DIR (--keep-config)"
elif [ -d "$CONFIG_DIR" ]; then
    rm -rf "$CONFIG_DIR"; ok "removed $CONFIG_DIR"
else
    skip "$CONFIG_DIR absent"
fi
rm -f "$LOG_FILE" "${LOG_FILE}".* 2>/dev/null && ok "removed $LOG_FILE*" || skip "no log files"

# ── 4. Service user ─────────────────────────────────────────────────────────
say ">> Removing user..."
if id "$SVC_USER" >/dev/null 2>&1; then
    # userdel -r would also take the home dir; it is $INSTALL_DIR (already gone)
    # for a default install, but a --keep-config or hand-edited box may point it
    # elsewhere, so remove the home explicitly only when it is NOT a path we
    # would refuse to touch.
    home_dir="$(getent passwd "$SVC_USER" | cut -d: -f6)"
    userdel "$SVC_USER" >/dev/null 2>&1 && ok "removed user $SVC_USER" \
        || say "   WARNING: userdel failed (processes still running as $SVC_USER?)"
    case "$home_dir" in
        /|/root|/home|/usr|/etc|/var|"") skip "refusing to remove home '$home_dir'" ;;
        *) [ -d "$home_dir" ] && { rm -rf "$home_dir"; ok "removed home $home_dir"; } ;;
    esac
else
    skip "user $SVC_USER does not exist"
fi

# ── 5. Optional: dependencies ab installed for itself ─────────────────
if [ "$PURGE_DEPS" -eq 1 ]; then
    say ">> Removing ab-installed dependencies..."

    # ollama: service, binary, models, the systemd override ab wrote, and
    # its own service user. Models are the reason this is opt-in — they are tens
    # of GB and re-downloading them is slow.
    if systemctl list-unit-files 2>/dev/null | grep -q "^ollama.service"; then
        systemctl stop ollama >/dev/null 2>&1
        systemctl disable ollama >/dev/null 2>&1
        ok "stopped ollama"
    fi
    rm -rf /etc/systemd/system/ollama.service.d 2>/dev/null && ok "removed ollama systemd override"
    rm -f /etc/systemd/system/ollama.service 2>/dev/null
    for p in /usr/local/bin/ollama /usr/bin/ollama; do
        [ -e "$p" ] && { rm -f "$p"; ok "removed $p"; }
    done
    for d in /usr/share/ollama /var/lib/ollama /root/.ollama; do
        [ -d "$d" ] && { rm -rf "$d"; ok "removed $d (models)"; }
    done
    id ollama >/dev/null 2>&1 && { userdel ollama >/dev/null 2>&1 && ok "removed user ollama"; }
    systemctl daemon-reload >/dev/null 2>&1 || true

    # Claude Code CLI — installed globally via npm and/or per-user by the helper.
    if command -v npm >/dev/null 2>&1; then
        npm uninstall -g @anthropic-ai/claude-code --silent >/dev/null 2>&1 \
            && ok "removed @anthropic-ai/claude-code" || skip "claude-code not installed via npm"
    fi

    # Node.js — only what the installer added via NodeSource. Left with apt-get
    # remove (not purge/autoremove) so a node another app depends on is not
    # silently dragged out with it.
    if dpkg -l nodejs 2>/dev/null | grep -q "^ii"; then
        DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq nodejs >/dev/null 2>&1 \
            && ok "removed nodejs" || skip "could not remove nodejs"
        rm -f /etc/apt/sources.list.d/nodesource.list \
              /etc/apt/keyrings/nodesource.gpg 2>/dev/null && ok "removed NodeSource apt source"
    fi
else
    skip "dependencies kept (pass --purge-deps for ollama / claude / node)"
fi

say ""
say "Done. AppBuilder removed."
[ "$KEEP_CONFIG" -eq 1 ] && say "Config kept at $CONFIG_DIR."
if [ "$PURGE_DEPS" -ne 1 ]; then
    say "ollama, the Claude CLI and Node.js were kept — re-run with --purge-deps to remove them."
fi
say "Base packages (curl, git, sudo, python3, build-essential, ...) were left installed on purpose."
