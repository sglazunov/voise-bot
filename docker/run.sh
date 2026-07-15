#!/usr/bin/env bash
# Runs as the unprivileged `app` user. When the recorder bot is enabled, brings
# up ONE virtual display (Xvfb) + ONE PulseAudio null-sink PER PARALLEL SLOT, so
# up to VTX_MAX_CONCURRENT_RECORDINGS meetings record in isolation at once.
set -e

export HOME=/home/app
SCREEN_RES="${VTX_SCREEN_RES:-1920x1080x24}"
DISPLAY_BASE="${VTX_DISPLAY_BASE:-99}"
SLOTS="${VTX_MAX_CONCURRENT_RECORDINGS:-4}"
export DISPLAY=":${DISPLAY_BASE}"   # default for tools; each recording overrides it

start_display_and_audio() {
  echo "[run] starting PulseAudio…"
  export XDG_RUNTIME_DIR=/tmp/xdg
  export PULSE_RUNTIME_PATH=/tmp/pulse
  mkdir -p "$XDG_RUNTIME_DIR" "$PULSE_RUNTIME_PATH"

  # Start PulseAudio robustly: a stale pid/socket from a previous (crashed)
  # start makes `pulseaudio -D` fail with a bare "Daemon startup failed", which
  # leaves the recorder with no audio source (meetN.monitor missing). Kill any
  # leftover, clear the runtime dir, then start and VERIFY it's actually up.
  for attempt in 1 2 3; do
    pulseaudio -k >/dev/null 2>&1 || true
    rm -f "$PULSE_RUNTIME_PATH/pid" "$PULSE_RUNTIME_PATH/native" 2>/dev/null || true
    pulseaudio -D --exit-idle-time=-1 --disable-shm=true >/tmp/pulse.log 2>&1 || true
    for _ in $(seq 1 30); do
      pactl info >/dev/null 2>&1 && break
      sleep 0.2
    done
    if pactl info >/dev/null 2>&1; then
      break
    fi
    echo "[run] PulseAudio not up (attempt $attempt) — retrying…"
    sleep 0.5
  done
  pactl info >/dev/null 2>&1 \
    && echo "[run] PulseAudio ready." \
    || echo "[run] WARNING: PulseAudio failed to start — recordings will have NO audio."

  # One Xvfb display + one null-sink per slot (indices 0..SLOTS-1).
  i=0
  while [ "$i" -lt "$SLOTS" ]; do
    disp=":$((DISPLAY_BASE + i))"
    echo "[run] slot $i: Xvfb $disp + sink meet$i…"
    Xvfb "$disp" -screen 0 "$SCREEN_RES" -nolisten tcp -ac >"/tmp/xvfb$i.log" 2>&1 &
    for _ in $(seq 1 40); do
      xdpyinfo -display "$disp" >/dev/null 2>&1 && break
      sleep 0.2
    done
    pactl load-module module-null-sink \
          "sink_name=meet$i" "sink_properties=device.description=meet$i" \
          >/dev/null 2>&1 || true
    i=$((i + 1))
  done
  echo "[run] pulse sinks:"; pactl list short sinks 2>/dev/null || true
}

if [ "${VTX_RECORDER_ENABLED:-0}" = "1" ]; then
  start_display_and_audio
else
  echo "[run] recorder bot disabled (VTX_RECORDER_ENABLED=0) — core mode."
fi

echo "[run] starting web app on :8000…"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
