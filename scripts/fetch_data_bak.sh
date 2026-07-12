#!/bin/bash
# fetch_data_bak.sh — обращается к pk.hse.ru (сбор статистики бакалавриата).
# В отличие от магистратуры, здесь НЕТ ограничения "раз в сутки" — можно
# запускать сколько угодно раз, задержка 2 сек между запросами уже в
# collector_bak.py. Резюмируемый: прерывать (Ctrl+C) и перезапускать можно
# смело, уже собранное (bak_data/progress.json) не перескачается.
#
# ВАЖНО: если нужно обновить УЖЕ собранные ранее группы (не только новые),
# сначала очистите прогресс, иначе повторный запуск их пропустит:
#   mv bak_data bak_data_backup_$(date +%Y-%m-%d) && mkdir -p bak_data
#
# Использование: scripts/fetch_data_bak.sh
set -euo pipefail

echo "=== Сбор данных с pk.hse.ru (collector_bak.py) ==="
python3 scripts/collector_bak.py

echo "=== Готово: сырьё в bak_data/. Дальше — scripts/run_process_and_sync_bak.sh ==="
