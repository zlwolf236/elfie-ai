#!/usr/bin/env bash
# Quick foreground dev run: dashboard + agent.
#   Dashboard — http://localhost:8765
#   Agent     — connects to LiveKit and waits for you to join a room
# For the always-on setup (auto-start + wake word), use deploy/install_services.sh.
# Ctrl-C stops all of it.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
cd "$SCRIPT_DIR"

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet -r requirements.txt
fi

PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup EXIT

"$VENV/bin/python" -m elfie.dashboard &
PIDS+=($!)

bash "$SCRIPT_DIR/deploy/fetch_models.sh"
"$VENV/bin/python" elfie_agent.py dev
