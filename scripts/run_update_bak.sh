#!/bin/bash
# run_update_bak.sh — вся цепочка сразу: сбор с pk.hse.ru + метрики + заливка
# на Drive. Тонкая обёртка над двумя независимыми скриптами:
#   scripts/fetch_data_bak.sh            — трогает pk.hse.ru
#   scripts/run_process_and_sync_bak.sh  — всё остальное, сайт не трогает
#
# Использование: scripts/run_update_bak.sh
set -euo pipefail

scripts/fetch_data_bak.sh
scripts/run_process_and_sync_bak.sh
