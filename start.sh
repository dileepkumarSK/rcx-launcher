#!/usr/bin/env bash
# Start the RCX Env Launcher server (if not already running) and open the UI
# in a clean Chrome app window.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${RCX_PORT:-8766}"
URL="http://127.0.0.1:${PORT}"
CACHE="$HOME/.cache/rcx-launcher"
mkdir -p "$CACHE"

CHROME="$(command -v google-chrome || command -v google-chrome-stable || true)"
if [[ -z "$CHROME" ]]; then
  echo "google-chrome not found" >&2; exit 1
fi

# NOTE: launching never fetches from git. Links come from the existing
# config.json; use the "Fetch latest" button in the UI to pull fresh links.
if [[ ! -f "$DIR/config.json" ]]; then
  echo "No config.json yet — UI will be empty until you press 'Fetch latest'." >&2
fi

# Start the server if the port isn't already answering.
if ! curl -sf -o /dev/null "${URL}/api/config" 2>/dev/null; then
  echo "Starting server on ${URL} …"
  nohup python3 "$DIR/server.py" --port "$PORT" >"$CACHE/server.log" 2>&1 &
  for _ in $(seq 1 40); do
    curl -sf -o /dev/null "${URL}/api/config" 2>/dev/null && break
    sleep 0.25
  done
fi

exec "$CHROME" --app="$URL" --class=RCX-Launcher --window-size=1200,820 \
  --no-first-run --no-default-browser-check \
  --user-data-dir="$CACHE/chrome-profile" >/dev/null 2>&1
