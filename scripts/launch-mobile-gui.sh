#!/usr/bin/env bash
#
# Passive Vigilance — one-touch mobile launcher.
#
# Starts the node in MOBILE mode with the web dashboard enabled, waits for the
# dashboard to come up, and opens it in the local browser. Designed to be run
# from the desktop launcher (deploy/passive-vigilance-gui.desktop) with a single
# click, but it is a normal script you can also run from a terminal.
#
# Leave the window open while using the node; press Ctrl+C (or close it) to stop
# the node cleanly. If the node is already running, this just opens the browser.
#
set -euo pipefail

# Resolve the repo root from this script's real location (works via symlink).
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$REPO_DIR"

# Prefer the project virtualenv; fall back to the system interpreter.
PY="/opt/passive-vigilance/venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "No Python interpreter found (looked for the project venv and python3)." >&2
    exit 1
fi

# Dashboard port: honour GUI_PORT from .env if set, else main.py's default 8080.
PORT="$(sed -n 's/^[[:space:]]*GUI_PORT[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | tail -1 | tr -d '[:space:]')"
PORT="${PORT:-8080}"
URL="http://localhost:${PORT}"

open_when_ready() {
    # Poll for up to ~30s for the dashboard, then open it once in the browser.
    local i
    for i in $(seq 1 60); do
        if curl -fsS -o /dev/null "$URL" 2>/dev/null; then
            xdg-open "$URL" >/dev/null 2>&1 || true
            return 0
        fi
        sleep 0.5
    done
    echo "Dashboard did not respond on ${URL} within 30s — see the log above." >&2
}

# Already running? Just open the dashboard and exit (a second click is harmless).
if curl -fsS -o /dev/null "$URL" 2>/dev/null; then
    echo "Passive Vigilance is already running — opening the dashboard at ${URL}"
    xdg-open "$URL" >/dev/null 2>&1 || true
    exit 0
fi

echo "Starting Passive Vigilance (mobile) — dashboard at ${URL}"
echo "Keep this window open while you use it; press Ctrl+C here to stop the node."

# Open the browser in the background once the server is listening.
open_when_ready &

# Force mobile + dashboard regardless of .env: python-dotenv's load_dotenv() does
# not override variables already set in the environment, so these win. Run in the
# foreground (exec) so Ctrl+C / closing the window delivers SIGINT for a clean stop.
exec env NODE_MODE=mobile GUI_ENABLED=true GUI_PORT="$PORT" "$PY" main.py --mode mobile
