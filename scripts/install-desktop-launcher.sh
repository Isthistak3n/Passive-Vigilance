#!/usr/bin/env bash
#
# Install the one-touch "Passive Vigilance" desktop launcher for a mobile node.
#
# Creates a desktop icon (and an app-menu entry) that starts the node in mobile
# mode and opens the dashboard. Run it once, as the desktop user (NOT with sudo —
# it installs into your own home/Desktop):
#
#     ./scripts/install-desktop-launcher.sh
#
# It handles the three things that usually make a Pi desktop icon fail to launch:
#   1. the launcher's path must be absolute (substituted in here),
#   2. the .desktop file and the script must be executable,
#   3. the file must be marked "trusted" or the desktop refuses to run it.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
TEMPLATE="$REPO_DIR/deploy/passive-vigilance-gui.desktop"
LAUNCHER="$REPO_DIR/scripts/launch-mobile-gui.sh"
NAME="passive-vigilance-gui.desktop"

if [ "$(id -u)" = "0" ]; then
    echo "Run this as your normal desktop user, not with sudo — it installs into your home." >&2
    exit 1
fi
[ -f "$TEMPLATE" ] || { echo "Missing template: $TEMPLATE" >&2; exit 1; }

APPS_DIR="$HOME/.local/share/applications"
DESK_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -n "$DESK_DIR" ] || DESK_DIR="$HOME/Desktop"
mkdir -p "$APPS_DIR" "$DESK_DIR"

# Make the launcher itself executable.
chmod +x "$LAUNCHER"

# Substitute the absolute repo path into the entry and install it.
sed "s|__REPO_DIR__|$REPO_DIR|g" "$TEMPLATE" > "$APPS_DIR/$NAME"
chmod +x "$APPS_DIR/$NAME"
install -m 0755 "$APPS_DIR/$NAME" "$DESK_DIR/$NAME"

# Mark the desktop copy "trusted" so it launches without an untrusted-file prompt.
# GNOME/Nautilus and recent file managers read this gio metadata flag; older
# PCManFM (classic Pi OS) honours the executable bit set above. Either way is fine.
gio set "$DESK_DIR/$NAME" metadata::trusted true 2>/dev/null || true

# Refresh the app-menu database if the tool is available.
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo "Installed."
echo "  Desktop icon : $DESK_DIR/$NAME"
echo "  App menu     : $APPS_DIR/$NAME  (search 'Passive Vigilance')"
echo
echo "Double-click the desktop icon to start the mobile node and open the dashboard."
echo "If the desktop still shows it as untrusted, right-click it once and choose"
echo "'Allow Launching' (GNOME) — that only needs to be done once."
