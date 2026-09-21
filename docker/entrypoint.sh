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

# Политика Chromium для бота: ссылки «открыть в приложении» (Телемост при входе
# пробует свою схему вида yandex-telemost://…) без неё вызывают системный
# диалог «Open xdg-open?», который перекрывает страницу и НЕ пропускает клики
# бота (проверено на стенде: клик через CDP при открытом диалоге в страницу не
# доходит). AutoLaunchProtocolsFromOrigins заставляет Chromium «запустить
# приложение» молча — xdg-open в контейнере ничего не открывает, страница
# остаётся целой. URLBlocklist для этого НЕ годится: он подменяет страницу
# ошибкой «заблокировано администратором». Схемы — из VTX_APP_SCHEMES
# (через запятую); настоящую схему бот пишет в лог карточки
# («Страница пыталась открыть приложение: …»).
POLICY_DIR=/etc/chromium/policies/managed
if mkdir -p "$POLICY_DIR" 2>/dev/null; then
    SCHEMES="${VTX_APP_SCHEMES:-telemost,yandex-telemost,yandextelemost,ya-telemost,yandexmessenger,yandex-messenger,ya-messenger,yamb,yandex360,ya360}"
    _entries=""
    IFS=',' read -ra _arr <<< "$SCHEMES"
    for _s in "${_arr[@]}"; do
        _s="$(echo "$_s" | tr -d '[:space:]' | tr 'A-Z' 'a-z')"
        [ -n "$_s" ] || continue
        _entries="${_entries}${_entries:+,}{\"protocol\":\"${_s}\",\"allowed_origins\":[\"*\"]}"
    done
    printf '{"AutoLaunchProtocolsFromOrigins":[%s]}\n' "$_entries" > "$POLICY_DIR/voise.json" \
        && echo "[entrypoint] политика Chromium: схемы приложений без диалога — $SCHEMES" \
        || echo "[entrypoint] ПРЕДУПРЕЖДЕНИЕ: не удалось записать политику Chromium"
fi

# Re-exec the rest as the unprivileged app user.
exec gosu app /app/docker/run.sh
