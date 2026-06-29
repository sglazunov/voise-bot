#!/usr/bin/env bash
# One-shot setup for the transcription service on the Ubuntu laptop-server.
# Run on the server:  bash deploy.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-python3}"

echo "==> Installing system deps (ffmpeg)…"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y ffmpeg python3-venv python3-pip
fi

echo "==> Creating virtualenv…"
"$PY" -m venv "$APP_DIR/.venv"
# shellcheck disable=SC1091
source "$APP_DIR/.venv/bin/activate"
pip install --upgrade pip wheel
pip install -r "$APP_DIR/requirements.txt"

echo "==> Pre-downloading the Whisper model (so first request isn't slow)…"
VTX_MODEL="${VTX_MODEL:-small}" python - <<'PY'
import os
from faster_whisper import WhisperModel
m = os.environ.get("VTX_MODEL", "small")
print(f"Fetching model: {m}")
WhisperModel(m, device="cpu", compute_type="int8")
print("Model cached.")
PY

cat <<EOF

==> Done.
Start the service manually:
    cd "$APP_DIR"
    source .venv/bin/activate
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Then open from another device on the LAN:  http://192.168.0.63:8000/

To run it as a background service that starts on boot, install the systemd
unit (see README.md, section "Автозапуск").
EOF
