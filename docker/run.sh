#!/usr/bin/env bash
# Runs as the unprivileged `app` user. When the recorder bot is enabled, brings
# up a virtual display (Xvfb) and PulseAudio with a null-sink for meeting audio,
# then starts the web app. With the bot disabled it just starts the app.
set -e

export HOME=/home/app
export DISPLAY="${DISPLAY:-:99}"
SCREEN_RES="${VTX_SCREEN_RES:-1920x1080x24}"

start_display_and_audio() {
  echo "[run] starting Xvfb on $DISPLAY ($SCREEN_RES)…"
  Xvfb "$DISPLAY" -screen 0 "$SCREEN_RES" -nolisten tcp -ac >/tmp/xvfb.log 2>&1 &
  for _ in $(seq 1 40); do
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
    sleep 0.25
  done

  echo "[run] starting PulseAudio…"
  export XDG_RUNTIME_DIR=/tmp/xdg
  export PULSE_RUNTIME_PATH=/tmp/pulse
  mkdir -p "$XDG_RUNTIME_DIR" "$PULSE_RUNTIME_PATH"
  pulseaudio -D --exit-idle-time=-1 --disable-shm=true >/tmp/pulse.log 2>&1 || true
  sleep 1

  # Null-sink that the browser plays the call into; its monitor is what we record.
  pactl load-module module-null-sink \
        sink_name=meet sink_properties=device.description=meet >/dev/null 2>&1 || true
  pactl set-default-sink meet >/dev/null 2>&1 || true
  echo "[run] pulse sources:"; pactl list short sources 2>/dev/null || true
}

if [ "${VTX_RECORDER_ENABLED:-0}" = "1" ]; then
  start_display_and_audio
else
  echo "[run] recorder bot disabled (VTX_RECORDER_ENABLED=0) — core mode."
fi

echo "[run] starting web app on :8000…"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
