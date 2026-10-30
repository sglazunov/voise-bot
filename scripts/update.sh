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

# SSH до сервера рвётся посреди сборки («client_loop: send disconnect:
# Connection reset», 28.10) — и скрипт умирал вместе с сессией, иногда между
# остановкой старого контейнера и запуском нового. Поэтому из терминала он
# перезапускает себя в отдельной сессии (обрыв SSH её не задевает), а на экран
# выводит журнал. Связь оборвалась — обновление идёт дальше; журнал остаётся в
# logs/update-*.log, досмотреть: tail -f logs/update-*.log
if [ -z "${VTX_UPDATE_DETACHED:-}" ] && [ -t 1 ]; then
  mkdir -p logs
  LOG="logs/update-$(date +%Y%m%d-%H%M%S).log"
  VTX_UPDATE_DETACHED=1 setsid -w nohup bash scripts/update.sh "$@" \
    >"$LOG" 2>&1 </dev/null &
  PID=$!
  echo "==> Обновление идёт в фоне и переживёт обрыв SSH. Журнал: $LOG"
  tail -n +1 --pid="$PID" -f "$LOG" || true
  rc=0; wait "$PID" || rc=$?
  exit "$rc"
fi

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# Пользователь и база — из .env, как у compose. Раньше брались из окружения
# оболочки (обычно пустого), и при своём POSTGRES_USER проверка занятости
# молча отвечала «0 задач» — обновление шло поверх работающего распознавания.
env_val() { { sed -n "s/^$1=//p" .env 2>/dev/null || true; } | tail -1 | tr -d '\r"'"'"; }
PGUSER_="${POSTGRES_USER:-$(env_val POSTGRES_USER)}"; PGUSER_="${PGUSER_:-voise}"
PGDB_="${POSTGRES_DB:-$(env_val POSTGRES_DB)}"; PGDB_="${PGDB_:-voise}"

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

refuse_if_busy() {
  [ "$FORCE" -eq 0 ] || return 0
  jobs_n="$(busy_jobs)"; jobs_n="${jobs_n:-0}"
  rec_n="$(busy_recorders)"; rec_n="${rec_n:-0}"
  if [ "$jobs_n" -gt 0 ] || [ "$rec_n" -gt 0 ]; then
    echo "СЕЙЧАС ОБНОВЛЯТЬ НЕЛЬЗЯ:"
    [ "$jobs_n" -gt 0 ] && echo "  • задач в работе: $jobs_n (распознавание или сборка протокола)"
    [ "$rec_n" -gt 0 ] && echo "  • идёт запись встречи (процессов ffmpeg: $rec_n)"
    echo
    echo "$1"
    echo "Дождитесь окончания либо запустите: bash scripts/update.sh --force"
    exit 1
  fi
}

refuse_if_busy "Пересборка прервёт их, и работа начнётся заново."
[ "$FORCE" -eq 0 ] && echo "==> Сервис свободен, обновляюсь."

# Изменённые отслеживаемые файлы попадут в образ как есть. Так однажды чуть не
# уехал compose с DATABASE_URL без хоста db (ревью 30.10, P0-1): контейнер
# просто перезапускался бы по кругу, не достучавшись до базы.
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "!!  В рабочей копии есть НЕЗАКОММИЧЕННЫЕ правки — они попадут в образ:"
  git status --short --untracked-files=no | sed 's/^/      /'
  echo "    Если это случайность: git checkout -- <файл> и запустите снова."
fi

echo "==> Забираю изменения (IPv4, с повторами)…"
PULLED=0
for i in 1 2 3; do
  if GIT_SSH_COMMAND="ssh -4 -o ConnectTimeout=10" git pull --ff-only; then
    PULLED=1
    break
  fi
  [ "$i" -lt 3 ] && { echo "    попытка $i не прошла, повтор через 20 с…"; sleep 20; }
done

if [ "$PULLED" -eq 0 ]; then
  # Недоступность репозитория НЕ должна отменять применение настроек: правки
  # .env (модель распознавания, число потоков) к git отношения не имеют, а
  # раньше скрипт выходил здесь с ошибкой и молча их не применял.
  echo "!!  Репозиторий недоступен — собираю ТЕКУЩИЙ код."
  echo "    Правки .env применятся, новых изменений из git не будет."
fi

echo "==> Собираю образ (несколько минут)…"
# Хэш коммита вшивается в образ: после сборки видно, из чего собран контейнер.
export VTX_BUILD="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
docker compose build app
# Сборка идёт минуты, и за это время могла начаться встреча — проверяем ещё
# раз прямо перед перезапуском: `up -d` убил бы и её.
refuse_if_busy "За время сборки началась работа. Образ собран — запустите обновление позже, пересборка будет быстрой (кэш)."
if ! docker compose config -q; then
  echo "!!  docker compose config не проходит — контейнер НЕ перезапускаю."
  exit 1
fi
docker compose up -d app
echo "==> Контейнер поднят. Убираю старые образы (бывает до пары минут, вывода нет)…"
# Каждая сборка оставляет прежний образ «висячим» (несколько ГБ) — за месяц
# обновлений они и забивают диск. Убираем только безымянные образы, работающий
# контейнер и его образ не трогаются.
docker image prune -f >/dev/null 2>&1 || true

echo "==> Готово. Проверка:"
docker compose ps app
docker compose exec -T app ps -o comm -p 1 | tail -1   # должен быть docker-init
IN_IMAGE="$(docker compose exec -T app printenv VTX_BUILD 2>/dev/null | tr -d '\r' || true)"
if [ "$IN_IMAGE" = "$VTX_BUILD" ]; then
  echo "==> В контейнере сборка $IN_IMAGE — совпадает с git."
else
  echo "!!  В контейнере сборка «${IN_IMAGE:-?}», в git — $VTX_BUILD: образ НЕ обновился."
  echo "    Смотрите вывод сборки выше (обычно падает npm/vite или pip)."
fi
