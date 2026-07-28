#!/usr/bin/env bash
# Runs as root: make the mounted data dir writable by the app user, then drop
# privileges and continue. PulseAudio refuses to run as root, hence the app user.
set -e

DATA_DIR="${VTX_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
chown -R app:app "$DATA_DIR" 2>/dev/null || true

# Служебные каталоги PulseAudio — заранее и во владении app.
#
# Иначе достаточно ОДНОЙ команды от root внутри контейнера (`docker compose exec
# app pactl …` — обычная диагностика), чтобы клиент создал /tmp/pulse с правами
# root:root 0700. После этого приложение, работающее под app, теряет доступ к
# звуковому серверу: «Failed to create secure directory» → «Connection refused»,
# и запись встречи уходит без звука. Ровно это и случилось на боевом сервере:
# запись в 11:30 прошла со звуком, а в 13:30 — уже нет.
#
# Создаём и отдаём app, пока ещё есть права root: тогда чужая папка не появится,
# а если осталась от прошлого запуска — владелец чинится здесь же.
for _d in "${XDG_RUNTIME_DIR:-/tmp/xdg}" "${PULSE_RUNTIME_PATH:-/tmp/pulse}"; do
    mkdir -p "$_d"
    chown app:app "$_d" 2>/dev/null || true
    chmod 700 "$_d" 2>/dev/null || true
done

# Re-exec the rest as the unprivileged app user.
exec gosu app /app/docker/run.sh
