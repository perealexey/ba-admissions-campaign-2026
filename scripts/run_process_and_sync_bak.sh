#!/bin/bash
# run_process_and_sync_bak.sh — всё, что идёт ПОСЛЕ сбора с pk.hse.ru: метрики
# → заливка на Google Drive. Не обращается к pk.hse.ru вообще — можно
# запускать сколько угодно раз (например, чтобы перезалить на Drive после
# правки скрипта).
#
# Предполагает, что bak_data/ уже заполнена (collector_bak.py уже отработал)
# и data/supply_bak_raw.csv существует (см. ШАГ 2а в БА_КАК_ОБНОВИТЬ_ДАННЫЕ.txt,
# если места приёма ещё не разобраны).
#
# Использование: scripts/run_process_and_sync_bak.sh [ДАТА]
#   ДАТА по умолчанию — сегодня (ГГГГ-ММ-ДД). Указывайте явно, если нужно
#   пересчитать снапшот на другую дату.
set -euo pipefail

DATE="${1:-$(date +%Y-%m-%d)}"

if [ ! -f "data/supply_bak_raw.csv" ]; then
    echo "Не найден data/supply_bak_raw.csv — сначала scripts/parse_supply_bak.py docs/ -o data/." >&2
    exit 1
fi

echo "=== Метрики (снапшот ${DATE}) ==="
python3 scripts/campaign_metrics_bak.py bak_data/ data/supply_bak_raw.csv --snapshot-date "$DATE" -o data/

echo "=== Заливка на Google Drive ==="
python3 scripts/sync_to_drive_bak.py

echo "=== Готово: снапшот $DATE обработан и залит на Drive (pk.hse.ru не трогали). ==="
