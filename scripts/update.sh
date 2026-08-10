#!/usr/bin/env bash
# Обновление боевого сервиса БЕЗ потери идущей работы.
#
# Пересоздание контейнера убивает распознавание и запись встречи на любом
# проценте: задача помечается «Прервано (сервис был перезапущен)» и начинается
# заново. На часовой встрече это час работы процессора впустую, а если в этот
# момент шла запись — встреча теряется совсем.
#
# Поэтому скрипт сначала спрашивает у самого сервиса, занят ли он, и только
# потом собирает. Обойти проверку: bash scripts/update.sh --force
#
# Заодно решает две местные болячки:
#  - ssh до SourceCraft периодически уходит в IPv6 («Network is unreachable»)
#    и отваливается по таймауту — тянем через IPv4 с повторами;
#  - сборка идёт несколько минут, и запускать её вслепую по расписанию нельзя.
set -euo pipefail
cd "$(dirname "$0")/.."

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

PGUSER_="${POSTGRES_USER:-voise}"
PGDB_="${POSTGRES_DB:-voise}"

busy_jobs() {
  docker compose exec -T db psql -U "$PGUSER_" -d "$PGDB_" -tA -c \
    "select count(*) from jobs where status in ('queued','running','analyzing');" \
    2>/dev/null | tr -dc '0-9' || true
}

busy_recorders() {
  # ffmpeg в контейнере = идёт запись встречи.
  docker compose exec -T app sh -c 'pgrep -c ffmpeg || true' 2>/dev/null \
    | tr -dc '0-9' || true
}

if [ "$FORCE" -eq 0 ]; then
  jobs_n="$(busy_jobs)"; jobs_n="${jobs_n:-0}"
  rec_n="$(busy_recorders)"; rec_n="${rec_n:-0}"
  if [ "$jobs_n" -gt 0 ] || [ "$rec_n" -gt 0 ]; then
    echo "СЕЙЧАС ОБНОВЛЯТЬ НЕЛЬЗЯ:"
    [ "$jobs_n" -gt 0 ] && echo "  • задач в работе: $jobs_n (распознавание или сборка протокола)"
    [ "$rec_n" -gt 0 ] && echo "  • идёт запись встречи (процессов ffmpeg: $rec_n)"
    echo
    echo "Пересборка прервёт их, и работа начнётся заново."
    echo "Дождитесь окончания либо запустите: bash scripts/update.sh --force"
    exit 1
  fi
  echo "==> Сервис свободен, обновляюсь."
fi

echo "==> Забираю изменения (IPv4, с повторами)…"
for i in 1 2 3 4 5; do
  if GIT_SSH_COMMAND="ssh -4 -o ConnectTimeout=10" git pull --ff-only; then
    break
  fi
  [ "$i" -eq 5 ] && { echo "Не удалось связаться с репозиторием."; exit 1; }
  echo "    попытка $i не прошла, повтор через 20 с…"
  sleep 20
done

echo "==> Собираю образ (несколько минут)…"
docker compose up -d --build app

echo "==> Готово. Проверка:"
docker compose ps app
docker compose exec -T app ps -o comm -p 1 | tail -1   # должен быть docker-init
