#!/usr/bin/env bash
# Куда ушёл диск. Запускать НА СЕРВЕРЕ из каталога проекта:
#     bash scripts/disk_report.sh
#
# Ничего не удаляет — только показывает. Печатает пять источников, которые
# на этом сервисе и растут: архив записей (локальное «облако»), рабочие
# записи пользователей, база, образы/кэш Docker и логи контейнеров.
set -u
cd "$(dirname "$0")/.." || exit 1
DATA="${VTX_DATA_DIR:-./data}"

hr() { printf '\n=== %s\n' "$1"; }

hr "Диск целиком"
df -h / | sed -n 1,2p

hr "Каталог data — верхний уровень"
du -sh "$DATA"/* 2>/dev/null | sort -rh | head -20

hr "Архив записей (локальное «облако», НИКОГДА не чистится сам)"
REC="$DATA/recordings"
if [ -d "$REC" ]; then
    du -sh "$REC" 2>/dev/null
    echo "файлов: $(find "$REC" -maxdepth 1 -type f | wc -l)"
    echo "самая старая / самая новая:"
    find "$REC" -maxdepth 1 -type f -printf '%T+ %10s %p\n' 2>/dev/null | sort | sed -n '1p;$p'
    echo "10 самых крупных:"
    find "$REC" -maxdepth 1 -type f -printf '%s\t%p\n' 2>/dev/null \
        | sort -rn | head -10 | awk -F'\t' '{printf "%8.1f МБ  %s\n", $1/1048576, $2}'
    echo "по месяцам (файлов / ГБ):"
    find "$REC" -maxdepth 1 -type f -printf '%TY-%Tm %s\n' 2>/dev/null \
        | awk '{n[$1]++; s[$1]+=$2} END {for (m in n) printf "  %s  %4d шт  %7.1f ГБ\n", m, n[m], s[m]/1073741824}' \
        | sort
else
    echo "нет каталога $REC — записи отдаются во внешнее облако"
fi

hr "Рабочие записи пользователей (их чистит ретеншн, 24 ч)"
du -sh "$DATA"/users/*/recordings 2>/dev/null | sort -rh | head
echo "старше суток (их быть не должно):"
find "$DATA"/users/*/recordings -type f -mtime +1 -printf '%T+ %10s %p\n' 2>/dev/null | sort | head

hr "Загрузки и результаты"
du -sh "$DATA"/uploads "$DATA"/results 2>/dev/null

hr "Docker: образы, кэш сборки, тома"
docker system df 2>/dev/null

hr "Логи контейнеров (растут без ограничения, если не задан max-size)"
sudo du -sh /var/lib/docker/containers/*/*-json.log 2>/dev/null | sort -rh | head

hr "Модель распознавания и профиль бота"
du -sh "$DATA"/models "$DATA"/browser-profile* 2>/dev/null | sort -rh | head
