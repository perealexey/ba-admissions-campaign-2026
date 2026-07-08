#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборщик статистики бакалаврской приёмной кампании ВШЭ с pk.hse.ru.

ВАЖНО: этот скрипт нужно запускать НЕ в Cowork-песочнице (её сетевой прокси
блокирует pk.hse.ru по allowlist), а локально на вашей машине или на сервере,
откуда есть прямой доступ в интернет.

Источник данных: недокументированный JSON REST API Angular-приложения
pk.hse.ru/admissions/bak/BD. Доступ анонимный (без cookie/токенов), но
robots.txt для домена отсутствует не то же самое, что явное разрешение —
см. обсуждение с пользователем. Скрипт написан по вашему прямому указанию,
с задержкой между запросами как единственной мерой "вежливости" к серверу.

ЦЕЛЕВАЯ КВОТА (placeType == "Целевая квота", код "ЦД"): разобрано по HAR
`har/tselevoi_math_pk.hse.ru.har` (2026-07-08). Пара (setOfCompetitiveGroupId,
placeType) в каталоге и правда не уникальна — на одну и ту же пару приходится
несколько competitiveGroupId (по одному на организацию-заказчика целевого
приёма). Но это НЕ значит, что у каждой организации свой список абитуриентов:
эндпоинт /applicant, вызванный с этой парой параметров, возвращает ОДИН общий
пул абитуриентов независимо от того, через какой competitiveGroupId зашли —
привязки заявителя к конкретному заказчику в /applicant нет вообще
(customerIdRabotaVRossii/customerOfferNumber в записях абитуриентов всегда
null). Список заказчиков и их квот (число договоров, наименование) отдаётся
ОТДЕЛЬНЫМ эндпоинтом /quota?setOfCompetitiveGroupId=...&placeTypeId=...— это
про предложение (места), не про заявителей.
Следствие: собирать нужно ОДИН РАЗ на уникальную пару
(setOfCompetitiveGroupId, placeTypeId), а не на каждый competitiveGroupId —
иначе один и тот же список абитуриентов задвоится по числу заказчиков.
Подробности и проверка — см. docs/FINDINGS.md.
"""

import csv
import json
import logging
import time
import sys
from pathlib import Path
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

BASE = "https://pk.hse.ru/admissions/api"
CATALOG_URL = f"{BASE}/competitve-group/competitive-list?level=BAK"
APPLICANT_URL = f"{BASE}/applicant"
QUOTA_URL = f"{BASE}/quota"

PAGE_SIZE = 50
REQUEST_DELAY_SECONDS = 2.0   # "этика" — минимальная задержка между запросами
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5.0
REQUEST_TIMEOUT = 20

TARGET_QUOTA_PLACE_TYPE_NAME = "Целевая квота"

# Иностранцы — отдельный конкурс: целиком платный трек (проверено по каталогу,
# ни одной группы с пометкой бюджета), в фокус задачи не входит — исключён из
# сбора по решению пользователя (2026-07-08, см. docs/FINDINGS.md, §3).
SKIP_FOREIGNERS = True
FOREIGNERS_PLACE_TYPE_NAME = "Иностранцы - отдельный конкурс"

OUTPUT_DIR = Path("./bak_data")
RAW_DIR = OUTPUT_DIR / "raw"
RAW_TARGET_QUOTA_DIR = OUTPUT_DIR / "raw_target_quota"
CATALOG_SNAPSHOT_DIR = OUTPUT_DIR / "catalog_snapshots"
PROGRESS_FILE = OUTPUT_DIR / "progress.json"
SKIPPED_FOREIGNERS_FILE = OUTPUT_DIR / "skipped_foreigners.csv"
LOG_FILE = OUTPUT_DIR / "collector.log"

# Идентифицируйте себя реальным контактом — это тот минимум прозрачности,
# который стоит соблюдать при систематическом автоматическом доступе.
USER_AGENT = "HSE-BAK-stats-research/0.1 (contact: apereiaslov@hse.ru)"

# --------------------------------------------------------------------------
# Логирование
# --------------------------------------------------------------------------

def setup_logging():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# HTTP-хелпер с ретраями
# --------------------------------------------------------------------------

def get_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "Попытка %d/%d не удалась для %s params=%s: %s",
                attempt, MAX_RETRIES, url, params, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Не удалось получить {url} params={params}") from last_exc


# --------------------------------------------------------------------------
# Каталог программ / конкурсных групп
# --------------------------------------------------------------------------

def fetch_catalog(session: requests.Session) -> list[dict]:
    log.info("Загружаю каталог конкурсных групп: %s", CATALOG_URL)
    data = get_json(session, CATALOG_URL)
    time.sleep(REQUEST_DELAY_SECONDS)

    rows = []
    for fil in data.get("filials", []):
        filial_name = fil.get("name")
        for td in fil.get("trainingDirections", []):
            td_name = td.get("name")
            td_id = td.get("id")
            for ep in td.get("educationPrograms", []):
                ep_name = ep.get("name")
                ep_id = ep.get("id")
                edulevel = (ep.get("educationLevel") or {}).get("name")
                for cg in ep.get("competitiveGroups", []):
                    place_type = cg.get("placeType") or {}
                    set_group = cg.get("setOfCompetitiveGroup") or {}
                    rows.append({
                        "filial": filial_name,
                        "trainingDirection": td_name,
                        "trainingDirectionId": td_id,
                        "educationProgram": ep_name,
                        "educationProgramId": ep_id,
                        "educationLevel": edulevel,
                        "competitiveGroupId": cg.get("id"),
                        "competitiveGroupName": cg.get("name"),
                        "placeTypeId": place_type.get("id"),
                        "placeTypeCode": place_type.get("code"),
                        "placeTypeName": place_type.get("name"),
                        "setOfCompetitiveGroupId": set_group.get("id"),
                        "setOfCompetitiveGroupCode": set_group.get("code"),
                        "setOfCompetitiveGroupName": set_group.get("name"),
                    })

    log.info("Каталог: %d конкурсных групп", len(rows))

    CATALOG_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = CATALOG_SNAPSHOT_DIR / f"catalog_bak_{stamp}.json"
    with snapshot_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    log.info("Снапшот каталога сохранён: %s", snapshot_path)

    return rows


# --------------------------------------------------------------------------
# Прогресс (резюмируемость между запусками)
# --------------------------------------------------------------------------

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with PROGRESS_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    return {"done_group_ids": []}


def save_progress(progress: dict) -> None:
    with PROGRESS_FILE.open("w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Сбор абитуриентов по одной конкурсной группе
# --------------------------------------------------------------------------

def fetch_group_applicants(session: requests.Session, row: dict) -> list[dict]:
    all_records = []
    page = 0
    total_pages = None

    while total_pages is None or page < total_pages:
        params = {
            "sort": "index_number_in_reg_list",
            "level": "BAK",
            "placeType": row["placeTypeId"],
            "setOfCompetitiveGroupId": row["setOfCompetitiveGroupId"],
            "page": page,
            "size": PAGE_SIZE,
        }
        data = get_json(session, APPLICANT_URL, params=params)
        total_pages = data.get("totalPages", 0)
        content = data.get("content", [])
        all_records.extend(content)

        log.info(
            "  %s | %s (%s): страница %d/%d, записей на странице %d",
            row["educationProgram"], row["competitiveGroupName"],
            row["placeTypeName"], page + 1, total_pages, len(content),
        )

        page += 1
        if page < total_pages:
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_records


# --------------------------------------------------------------------------
# Целевая квота: список организаций-заказчиков на пару
# (setOfCompetitiveGroupId, placeTypeId) — про места, не про заявителей
# --------------------------------------------------------------------------

def fetch_quota_customers(session: requests.Session, set_of_group_id: str, place_type_id: str) -> list[dict]:
    data = get_json(session, QUOTA_URL, params={
        "setOfCompetitiveGroupId": set_of_group_id,
        "placeTypeId": place_type_id,
    })
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# Основной цикл
# --------------------------------------------------------------------------

def main():
    setup_logging()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    RAW_TARGET_QUOTA_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    catalog = fetch_catalog(session)
    progress = load_progress()
    done_ids = set(progress["done_group_ids"])

    skipped_foreigners = []
    regular_rows = []
    target_quota_rows = []

    for row in catalog:
        if row["placeTypeName"] == TARGET_QUOTA_PLACE_TYPE_NAME:
            target_quota_rows.append(row)
        elif SKIP_FOREIGNERS and row["placeTypeName"] == FOREIGNERS_PLACE_TYPE_NAME:
            skipped_foreigners.append(row)
        else:
            regular_rows.append(row)

    # --- обычные группы: один competitiveGroupId = один список абитуриентов ---
    for row in regular_rows:
        group_id = row["competitiveGroupId"]
        if group_id in done_ids:
            continue

        log.info(
            "Группа: %s / %s / %s [%s]",
            row["filial"], row["educationProgram"], row["competitiveGroupName"], group_id,
        )

        try:
            records = fetch_group_applicants(session, row)
        except Exception:
            log.exception("Не удалось собрать группу %s — пропускаю, попробуйте перезапустить позже", group_id)
            continue

        out_path = RAW_DIR / f"{group_id}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump({
                "meta": row,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "count": len(records),
                "applicants": records,
            }, f, ensure_ascii=False, indent=2)

        done_ids.add(group_id)
        progress["done_group_ids"] = sorted(done_ids)
        save_progress(progress)

        time.sleep(REQUEST_DELAY_SECONDS)

    # --- целевая квота: дедуплицируем по (setOfCompetitiveGroupId, placeTypeId) —
    # см. docs/FINDINGS.md, эндпоинт /applicant не различает заказчиков внутри
    # одной пары, поэтому качать её нужно один раз, а не на каждый competitiveGroupId ---
    pools: dict[tuple, dict] = {}
    for row in target_quota_rows:
        key = (row["setOfCompetitiveGroupId"], row["placeTypeId"])
        pools.setdefault(key, {"representative": row, "competitiveGroupIds": []})
        pools[key]["competitiveGroupIds"].append(row["competitiveGroupId"])

    log.info(
        "Целевая квота: %d строк в каталоге -> %d уникальных пулов абитуриентов",
        len(target_quota_rows), len(pools),
    )

    for (set_id, place_type_id), pool in pools.items():
        pool_id = f"tq__{set_id}__{place_type_id}"
        if pool_id in done_ids:
            continue

        rep = pool["representative"]
        log.info(
            "Целевая квота: %s / %s (%d заказчиков) [%s]",
            rep["filial"], rep["educationProgram"], len(pool["competitiveGroupIds"]), pool_id,
        )

        try:
            records = fetch_group_applicants(session, rep)
            time.sleep(REQUEST_DELAY_SECONDS)
            customers = fetch_quota_customers(session, set_id, place_type_id)
        except Exception:
            log.exception("Не удалось собрать пул целевой квоты %s — пропускаю, попробуйте перезапустить позже", pool_id)
            continue

        out_path = RAW_TARGET_QUOTA_DIR / f"{pool_id}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump({
                "meta": rep,
                "competitiveGroupIds": pool["competitiveGroupIds"],
                "customers": customers,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "count": len(records),
                "applicants": records,
            }, f, ensure_ascii=False, indent=2)

        done_ids.add(pool_id)
        progress["done_group_ids"] = sorted(done_ids)
        save_progress(progress)

        time.sleep(REQUEST_DELAY_SECONDS)

    if skipped_foreigners:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with SKIPPED_FOREIGNERS_FILE.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(skipped_foreigners[0].keys()))
            w.writeheader()
            w.writerows(skipped_foreigners)
        log.info(
            "Пропущено %d групп «Иностранцы — отдельный конкурс» (список: %s) — "
            "исключены из сбора по решению пользователя, см. docs/FINDINGS.md.",
            len(skipped_foreigners), SKIPPED_FOREIGNERS_FILE,
        )

    regular_done = sum(1 for r in regular_rows if r["competitiveGroupId"] in done_ids)
    pools_done = sum(1 for k in pools if f"tq__{k[0]}__{k[1]}" in done_ids)
    log.info(
        "Готово. Собрано: %d/%d обычных групп + %d/%d пулов целевой квоты, пропущено (иностранцы): %d",
        regular_done, len(regular_rows), pools_done, len(pools), len(skipped_foreigners),
    )


if __name__ == "__main__":
    main()
