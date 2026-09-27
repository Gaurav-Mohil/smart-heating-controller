#!/bin/bash
set -e
cd "$(dirname "$0")"

printf '\n========================================\n SMART HEATING CONTROLLER\n========================================\n\n'

# Keep an existing calibration/account database. If this is a fresh extracted
# folder, recover the newest database from an older Smart Heating project.
mkdir -p data
if [ ! -f data/heating.db ]; then
  OLD_DB=$(find "$HOME/Downloads" "$HOME/Documents" -maxdepth 4 -type f -path '*/smart-heating-controller*/data/heating.db' ! -path "$PWD/data/heating.db" -print 2>/dev/null | while read -r f; do stat -f '%m %N' "$f" 2>/dev/null; done | sort -nr | head -1 | cut -d' ' -f2-)
  if [ -n "$OLD_DB" ] && [ -f "$OLD_DB" ]; then
    echo "Recovering your saved calibration/settings from an older project..."
    cp "$OLD_DB" data/heating.db
    OLD_DIR=$(dirname "$OLD_DB")
    [ -f "$OLD_DIR/password.txt" ] && cp "$OLD_DIR/password.txt" data/password.txt
    [ -f "$OLD_DIR/device_token.txt" ] && cp "$OLD_DIR/device_token.txt" data/device_token.txt
    [ -f "$OLD_DIR/.secret_key" ] && cp "$OLD_DIR/.secret_key" data/.secret_key
  fi
fi

if [ ! -d .venv ]; then
  echo "First start: creating Python environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install -q -r requirements.txt
python -m pip install -q -r bridge/requirements.txt

echo "Starting website..."
python -m server.app > data/server.log 2>&1 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait briefly for the local server.
for i in {1..20}; do
  if curl -s http://localhost:8000/ >/dev/null 2>&1; then break; fi
  sleep 0.5
done

open http://localhost:8000 >/dev/null 2>&1 || true

echo "Starting Arduino USB connection..."
echo "Keep this window open while Smart Heating is running."
echo "Press Control+C to stop everything."
echo
python bridge/serial_bridge.py
