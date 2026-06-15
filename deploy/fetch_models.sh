#!/usr/bin/env bash
# Download the wake-word model on first setup (kept out of git to stay lean).
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -d "$DIR/models/vosk-small" ]; then
    echo "Downloading Vosk wake-word model (~40 MB, one time)…"
    mkdir -p "$DIR/models" && cd "$DIR/models"
    curl -sL -o vosk.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
    unzip -q vosk.zip && mv vosk-model-small-en-us-0.15 vosk-small && rm vosk.zip
    echo "Wake-word model ready."
fi
