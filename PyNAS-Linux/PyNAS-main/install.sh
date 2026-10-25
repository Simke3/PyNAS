#!/usr/bin/env bash
# PyNAS installer for Linux.
#
#   ./install.sh             install for the current user (no root needed)
#   sudo ./install.sh        install system-wide (/opt/pynas)
#   ./install.sh --system    same as above, will ask for sudo if needed
#   ./install.sh --help
#
# The app runs from its own virtual environment, so nothing is installed
# into the system Python.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="PyNAS.py"
MODE=""

usage() {
    cat <<USAGE
Usage: $0 [--user | --system] [--help]

  --user     install for the current user only (default when not root)
  --system   install for all users into /opt/pynas (default when root)
USAGE
}

for arg in "$@"; do
    case "$arg" in
        --user)   MODE="user" ;;
        --system) MODE="system" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

if [ -z "$MODE" ]; then
    if [ "$(id -u)" -eq 0 ]; then MODE="system"; else MODE="user"; fi
fi

# Re-run through sudo when --system was requested without root
if [ "$MODE" = "system" ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        echo "System-wide install needs root, re-running with sudo..."
        exec sudo "$0" --system
    fi
    echo "Error: --system needs root and sudo was not found." >&2
    exit 1
fi
if [ "$MODE" = "user" ] && [ "$(id -u)" -eq 0 ]; then
    echo "Warning: installing for the root user. Use --system for an all-users install."
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

# ---------------------------------------------------------------- checks
if [ ! -f "$SRC_DIR/$APP_FILE" ] || [ ! -f "$SRC_DIR/requirements.txt" ]; then
    echo "Error: $APP_FILE and requirements.txt must be next to install.sh." >&2
    exit 1
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Error: python3 was not found. Install Python 3.9 or newer first." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Error: Python 3.9 or newer is required (found $("$PYTHON" -V 2>&1))." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    echo "Error: the Python venv module is missing." >&2
    echo "  Debian/Ubuntu: sudo apt install python3-venv python3-pip" >&2
    echo "  Fedora:        sudo dnf install python3-pip" >&2
    echo "  Arch:          sudo pacman -S python-pip" >&2
    exit 1
fi

echo "Installing PyNAS ($MODE) into $APP_DIR"

# ------------------------------------------------------------------ files
mkdir -p "$APP_DIR" "$BIN_DIR" "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"

install -m 0644 "$SRC_DIR/$APP_FILE" "$APP_DIR/$APP_FILE"
install -m 0644 "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
for f in LICENSE README.md; do
    [ -f "$SRC_DIR/$f" ] && install -m 0644 "$SRC_DIR/$f" "$APP_DIR/$f"
done
# Icon files, if the project ships them (the app looks for them next to PyNAS.py)
for f in pynas.ico pynas_icon_256.png; do
    [ -f "$SRC_DIR/$f" ] && install -m 0644 "$SRC_DIR/$f" "$APP_DIR/$f"
done

# ------------------------------------------------------------ virtualenv
echo "Creating virtual environment and installing dependencies (PySide6 is large, this can take a few minutes)..."
"$PYTHON" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

# --------------------------------------------------------------- launcher
cat > "$LAUNCHER" <<LAUNCH
#!/bin/sh
exec "$APP_DIR/venv/bin/python" "$APP_DIR/$APP_FILE" "\$@"
LAUNCH
chmod 0755 "$LAUNCHER"

# -------------------------------------------------------- icon + menu entry
cat > "$ICON_FILE" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <rect width="256" height="256" rx="48" fill="#1b1b1b"/>
  <rect x="40" y="64" width="176" height="52" rx="10" fill="#0095d5"/>
  <rect x="40" y="140" width="176" height="52" rx="10" fill="#252525" stroke="#0095d5" stroke-width="6"/>
  <circle cx="184" cy="90" r="8" fill="#fff"/>
  <circle cx="184" cy="166" r="8" fill="#e8892b"/>
</svg>
SVG

cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PyNAS
Comment=Share folders over your network with a live dashboard
Exec=$LAUNCHER
Icon=pynas
Terminal=false
Categories=Network;FileTransfer;System;
StartupNotify=true
DESKTOP
chmod 0644 "$DESKTOP_FILE" "$ICON_FILE"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$SHARE_DIR/icons/hicolor" >/dev/null 2>&1 || true

# ------------------------------------------------------------------- done
echo
echo "PyNAS is installed."
echo "  Run:        pynas   (or find \"PyNAS\" in your application menu)"
echo "  Uninstall:  ./uninstall.sh $([ "$MODE" = system ] && echo --system)"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "  Note: $BIN_DIR is not in your PATH; add it or start PyNAS from the menu." ;;
esac
echo
echo "If the window does not open on Debian/Ubuntu, Qt may need extra libraries:"
echo "  sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1"
