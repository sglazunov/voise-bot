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
# Собрать с локальными правками отслеживаемых файлов: --allow-dirty
#
# Заодно решает две местные болячки:
#  - ssh до SourceCraft периодически уходит в IPv6 («Network is unreachable»)
#    и отваливается по таймауту — тянем через IPv4 с повторами;
#  - сборка идёт несколько минут, и запускать её вслепую по расписанию нельзя.
#
# Тесты: tests/test_update_sh.py (шимы docker и git во временной папке).
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
ALLOW_DIRTY=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    *) echo "Неизвестный параметр: $arg (есть --force и --allow-dirty)"; exit 2 ;;
  esac
done
# Пауза между попытками git fetch (тесты ставят 0).
RETRY_SEC="${VTX_UPDATE_RETRY_SEC:-20}"

# SQL из stdin → psql в контейнере db. Пользователь и база — из окружения
# САМОГО контейнера: с ними он и был создан. Брать их из оболочки или из
# .env нельзя — оба могут разойтись с работающей базой.
db_sql() {
  docker compose exec -T db sh -c \
    'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA'
}

# Число задач в работе. Ошибка (контейнер db не запущен, psql упал, в ответе
# не число) — код возврата 1, а НЕ «0 задач»: раньше `|| true` превращал
# упавшую базу в «свободно», и обновление шло поверх распознавания.
busy_jobs() {
  local out
  out="$(echo "select count(*) from jobs where status in ('queued','running','analyzing');" \
    | db_sql 2>/dev/null)" || return 1
  out="$(printf '%s' "$out" | tr -d '[:space:]')"
  case "$out" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s' "$out"
}

busy_recorders() {
  # ffmpeg в контейнере = идёт запись встречи. Контейнер app не запущен —
  # значит, и записи нет.
  docker compose exec -T app sh -c 'pgrep -c ffmpeg || true' 2>/dev/null \
    | tr -dc '0-9' || true
}

refuse_if_busy() {
  [ "$FORCE" -eq 0 ] || return 0
  if ! jobs_n="$(busy_jobs)"; then
    echo "НЕ МОГУ ПРОВЕРИТЬ ЗАНЯТОСТЬ: база не ответила (контейнер db не запущен"
    echo "или psql завершился с ошибкой). Обновляться вслепую — значит рисковать"
    echo "идущим распознаванием, поэтому останавливаюсь."
    echo "  Проверьте: docker compose ps db; docker compose logs --tail 50 db"
    echo "  Если база лежит и обновление как раз должно её поднять:"
    echo "  bash scripts/update.sh --force"
    exit 1
  fi
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

# Пробный вход в базу с НОВЫМ конфигом (после git pull и правок .env) — из
# работающего контейнера app, он в сети compose. Конфиг идёт через stdin, в
# командную строку пароль не попадает. Postgres берёт POSTGRES_PASSWORD только
# при создании тома, и удалённая из .env строка DATABASE_URL=…voise:voise…
# (на старом томе рабочий пароль — voise) значит: после перезапуска
# приложение в базу не войдёт. Коды: 0 — вошёл, 3 — отказ по паролю,
# 4 — прочее (база не отвечает, нет psycopg).
PROBE_PY='
import json, os, sys
env = json.load(sys.stdin).get("services", {}).get("app", {}).get("environment") or {}
url = env.get("DATABASE_URL") or ""
if not url:
    sys.exit(0)
for k in [k for k in os.environ if k.startswith("PG")]:
    del os.environ[k]
for k, v in env.items():
    if k.startswith("PG") and v is not None:
        os.environ[k] = str(v)
try:
    import psycopg
except ImportError:
    sys.exit(4)
try:
    psycopg.connect(url, connect_timeout=10).close()
except Exception as e:
    text = " ".join(str(e).split()) or type(e).__name__
    print(text, file=sys.stderr)
    sys.exit(3 if "password" in text or "authentication failed" in text else 4)
'

check_db_login() {
  local rc=0
  docker compose config --format json \
    | docker compose exec -T app python -c "$PROBE_PY" || rc=$?
  case "$rc" in
    0) echo "==> Вход в базу с новыми настройками проверен." ;;
    3)
      echo "!!  С новыми настройками приложение НЕ ВОЙДЁТ в базу (отказ по паролю)."
      echo "    Postgres берёт POSTGRES_PASSWORD только при создании тома pgdata;"
      echo "    у старой установки пароль роли прежний (обычно voise), и работает"
      echo "    она лишь благодаря строке DATABASE_URL=… в .env. Верните эту строку"
      echo "    либо смените пароль роли — README, «Пароль базы на старой установке»."
      echo "    Ничего не собираю, контейнер НЕ перезапускаю."
      exit 1 ;;
    *) echo "!!  Вход в базу проверить не удалось (контейнер app не запущен или база"
       echo "    не отвечает) — продолжаю без этой проверки." ;;
  esac
}

# Том pgdata создавался образом postgres:16-alpine (musl), а теперь база —
# pgvector/pgvector:pg16 (Debian, glibc). Порядок строк en_US.utf8 у них
# разный, и текстовые индексы старого тома на glibc неверны: поиск по ключу
# может не найти строку, уникальность — пропустить дубль. Признак: база не
# записала версию правил сортировки (на musl её нет), а glibc её отдаёт.
# Лечение — REINDEX и запись текущей версии; на нашей базе это секунды.
reindex_if_collation_changed() {
  local state
  if ! state="$(echo "select case when d.datcollversion is null
      and d.datcollate not in ('C', 'POSIX')
      and pg_database_collation_actual_version(d.oid) is not null
      then 'reindex' else 'ok' end
      from pg_database d where d.datname = current_database();" | db_sql)"; then
    echo "!!  Не удалось проверить правила сортировки базы — продолжаю."
    return 0
  fi
  state="$(printf '%s' "$state" | tr -d '[:space:]')"
  [ "$state" = "reindex" ] || return 0
  echo "==> База создана на musl (alpine), теперь glibc — перестраиваю индексы…"
  if ! printf '%s\n' 'select current_database() as db \gset' \
      'REINDEX DATABASE :"db";' \
      'ALTER DATABASE :"db" REFRESH COLLATION VERSION;' | db_sql; then
    echo "!!  REINDEX не прошёл — приложение НЕ перезапускаю (работает старое)."
    echo "    Повторите: bash scripts/update.sh, при повторе — смотрите logs db."
    exit 1
  fi
  echo "==> Индексы перестроены."
}

# Изменённые отслеживаемые файлы: git pull --ff-only на них падает (а раньше
# скрипт трижды долбил SourceCraft, рапортовал «Репозиторий недоступен» и
# собирал СТАРЫЙ код), а если и проходит — правки молча уезжают в образ. Так
# однажды чуть не уехал compose с DATABASE_URL без хоста db (ревью 30.10,
# P0-1). Поэтому решение — за человеком, и до любых обращений к сети.
DIRTY="$(git status --porcelain --untracked-files=no 2>/dev/null || true)"
if [ -n "$DIRTY" ]; then
  echo "!!  В рабочей копии есть НЕЗАКОММИЧЕННЫЕ правки отслеживаемых файлов:"
  printf '%s\n' "$DIRTY" | sed 's/^/      /'
  if [ "$ALLOW_DIRTY" -eq 0 ]; then
    echo "    Обновление остановлено. Варианты:"
    echo "      • правка случайная:  git checkout -- <файл>"
    echo "      • отложить её:       git stash push -m \"до обновления\""
    echo "      • собрать с ней:     bash scripts/update.sh --allow-dirty"
    exit 1
  fi
  echo "    --allow-dirty: эти правки попадут в образ."
fi

refuse_if_busy "Пересборка прервёт их, и работа начнётся заново."
[ "$FORCE" -eq 0 ] && echo "==> Сервис свободен, обновляюсь."

# Сеть и слияние разделены: повторять имеет смысл только скачивание. Не
# применилось скачанное (расхождение веток, правки в тех же файлах при
# --allow-dirty) — это не «репозиторий недоступен», и собирать старый код
# молча нельзя.
echo "==> Забираю изменения (IPv4, с повторами)…"
FETCHED=0
for i in 1 2 3; do
  if GIT_SSH_COMMAND="ssh -4 -o ConnectTimeout=10" git fetch; then
    FETCHED=1
    break
  fi
  [ "$i" -lt 3 ] && { echo "    попытка $i не прошла, повтор через $RETRY_SEC с…"; sleep "$RETRY_SEC"; }
done

PULLED=0
if [ "$FETCHED" -eq 1 ]; then
  if ! git merge --ff-only '@{u}'; then
    echo "!!  Изменения скачаны, но НЕ применяются к рабочей копии (локальные"
    echo "    правки в тех же файлах или ветка разошлась с сервером):"
    git status -sb 2>/dev/null | head -1 | sed 's/^/      /' || true
    echo "    Ничего не собираю. Разберитесь с git status и запустите снова."
    exit 1
  fi
  PULLED=1
else
  # Недоступность репозитория НЕ должна отменять применение настроек: правки
  # .env (модель распознавания, число потоков) к git отношения не имеют, а
  # раньше скрипт выходил здесь с ошибкой и молча их не применял.
  echo "!!  Репозиторий недоступен — собираю ТЕКУЩИЙ код."
  echo "    Правки .env применятся, новых изменений из git не будет."
fi

# Конфиг проверяется ДО сборки: раньше проверка стояла после `compose build`,
# а сломанный конфиг роняет и сборку — под `set -e` сообщение ниже было
# недостижимо.
if ! docker compose config -q; then
  echo "!!  docker compose config не проходит — ничего не собираю, контейнер НЕ перезапускаю."
  exit 1
fi
if command -v python3 >/dev/null 2>&1; then
  if ! docker compose config --format json | python3 scripts/check_compose_config.py; then
    echo "!!  Адрес базы у app не сходится с сервисом db — контейнер НЕ перезапускаю."
    exit 1
  fi
else
  echo "!!  На сервере нет python3 — сверку адреса базы пропускаю (вход проверю ниже)."
fi
check_db_login

echo "==> Собираю образ (несколько минут)…"
# Хэш коммита вшивается в образ: после сборки видно, из чего собран контейнер.
export VTX_BUILD="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
if ! docker compose build app; then
  echo "!!  Сборка упала — работающий контейнер НЕ тронут. Смотрите вывод выше"
  echo "    (обычно npm/vite или pip)."
  exit 1
fi
# Сборка идёт минуты, и за это время могла начаться встреча — проверяем ещё
# раз прямо перед перезапуском: `up -d` убил бы и её.
refuse_if_busy "За время сборки началась работа. Образ собран — запустите обновление позже, пересборка будет быстрой (кэш)."
# Сначала база (если сменился её образ — пересоздастся), проверка правил
# сортировки, и только потом приложение: иначе оно стартовало бы на
# неверных индексах.
docker compose up -d --wait db
reindex_if_collation_changed
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
  if [ "$PULLED" -eq 1 ]; then
    echo "==> В контейнере сборка $IN_IMAGE — совпадает с git."
  else
    echo "==> В контейнере сборка $IN_IMAGE — это код ДО обновления: git недоступен."
  fi
else
  echo "!!  В контейнере сборка «${IN_IMAGE:-?}», в git — $VTX_BUILD: образ НЕ обновился."
  echo "    Смотрите вывод сборки выше (обычно падает npm/vite или pip)."
fi
