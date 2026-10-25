#!/usr/bin/env bash
# PyNAS uninstaller for Linux.
#
#   ./uninstall.sh             remove the per-user install
#   ./uninstall.sh --system    remove the system-wide install (uses sudo if needed)
#   ./uninstall.sh --purge     also delete settings and data (see below)
#   ./uninstall.sh --yes       do not ask questions
#
# Without --purge your settings (~/.pynas.json) and your files (~/PyNAS)
# are left untouched, so a reinstall picks up where you stopped.
set -euo pipefail

MODE=""
PURGE=0
ASSUME_YES=0

usage() {
    cat <<USAGE
Usage: $0 [--user | --system] [--purge] [--yes] [--help]
USAGE
}

for arg in "$@"; do
    case "$arg" in
        --user)   MODE="user" ;;
        --system) MODE="system" ;;
        --purge)  PURGE=1 ;;
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

if [ -z "$MODE" ]; then
    if [ "$(id -u)" -eq 0 ]; then MODE="system"; else MODE="user"; fi
fi

if [ "$MODE" = "system" ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        echo "Removing a system-wide install needs root, re-running with sudo..."
        # SUDO_USER is used below to find the right home folder for --purge
        args=(--system)
        [ "$PURGE" -eq 1 ] && args+=(--purge)
        [ "$ASSUME_YES" -eq 1 ] && args+=(--yes)
        exec sudo "$0" "${args[@]}"
    fi
    echo "Error: --system needs root and sudo was not found." >&2
    exit 1
fi

if [ "$MODE" = "system" ]; then
    APP_DIR="/opt/pynas"
    BIN_DIR="/usr/local/bin"
    SHARE_DIR="/usr/share"
else
    DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
    APP_DIR="$DATA_HOME/pynas"
    BIN_DIR="$HOME/.local/bin"
    SHARE_DIR="$DATA_HOME"
fi
LAUNCHER="$BIN_DIR/pynas"
DESKTOP_FILE="$SHARE_DIR/applications/pynas.desktop"
ICON_FILE="$SHARE_DIR/icons/hicolor/scalable/apps/pynas.svg"

# Home folder of the person who owns the settings (matters when run via sudo)
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    OWNER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
    OWNER_HOME="$HOME"
fi

if [ ! -d "$APP_DIR" ] && [ ! -e "$LAUNCHER" ] && [ ! -e "$DESKTOP_FILE" ]; then
    echo "PyNAS ($MODE install) was not found in $APP_DIR, nothing to remove."
    [ "$MODE" = "user" ] && echo "If you installed it system-wide, run: $0 --system"
    exit 0
fi

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    read -r -p "$1 [y/N] " ans || return 1
    case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# Stop a running copy (quitting PyNAS also stops the sharing service)
if pgrep -f "$APP_DIR/PyNAS.py" >/dev/null 2>&1; then
    echo "Stopping running PyNAS..."
    pkill -f "$APP_DIR/PyNAS.py" || true
    sleep 1
fi

echo "Removing PyNAS ($MODE install)..."
rm -rf "$APP_DIR"
rm -f "$LAUNCHER" "$DESKTOP_FILE" "$ICON_FILE"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$SHARE_DIR/icons/hicolor" >/dev/null 2>&1 || true

CONFIG="$OWNER_HOME/.pynas.json"
DATA="$OWNER_HOME/PyNAS"

if [ "$PURGE" -eq 1 ]; then
    if [ -e "$CONFIG" ] || [ -d "$DATA" ]; then
        echo
        echo "--purge will permanently delete:"
        [ -e "$CONFIG" ] && echo "  $CONFIG   (users, shares, port)"
        [ -d "$DATA" ]   && echo "  $DATA   (datasets: your shared files!)"
        if confirm "Delete these?"; then
            rm -f "$CONFIG"
            rm -rf "$DATA"
            echo "Settings and data deleted."
        else
            echo "Kept settings and data."
        fi
    fi
else
    echo
    echo "Kept your settings and files:"
    [ -e "$CONFIG" ] && echo "  $CONFIG"
    [ -d "$DATA" ]   && echo "  $DATA"
    echo "Use --purge to delete them as well."
fi

echo
echo "PyNAS has been removed."
