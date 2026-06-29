#!/usr/bin/env bash
# Runs as root: make the mounted data dir writable by the app user, then drop
# privileges and continue. PulseAudio refuses to run as root, hence the app user.
set -e

DATA_DIR="${VTX_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
chown -R app:app "$DATA_DIR" 2>/dev/null || true

# Re-exec the rest as the unprivileged app user.
exec gosu app /app/docker/run.sh
