#!/usr/bin/env python
"""
sync_to_drive_bak.py — заливает содержимое локальной data/ в папку на Google
Drive, откуда его читает задеплоенный дашборд (app.py, доступ read-only).

Тот же сервисный аккаунт, что и в магистратуре (summer_campaign_26), но
ОТДЕЛЬНАЯ папка на Drive (свой gdrive_folder_id в .streamlit/secrets.toml) —
данные двух кампаний не должны пересекаться. Запрашивает более широкий scope
(read-write) — только для этого локального скрипта, права самого дашборда
(app.py, drive.readonly) не меняются.

В отличие от магистратуры, здесь нет проблемы "датированных файлов" (там
admissions_long_YYYY-MM-DD.parquet требовал переименования в latest —
см. summer_campaign_26/scripts/sync_to_drive.py) — файлы в data/ этого
проекта уже без дат в именах, заливаются как есть, всегда "update".

Запуск: python3 scripts/sync_to_drive_bak.py
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SECRETS_PATH = Path(__file__).parent.parent / ".streamlit" / "secrets.toml"
DATA_DIR = Path(__file__).parent.parent / "data"
MIME_TYPES = {".parquet": "application/octet-stream", ".csv": "text/csv", ".json": "application/json"}


def get_service():
    with SECRETS_PATH.open("rb") as f:
        secrets = tomllib.load(f)
    creds = service_account.Credentials.from_service_account_info(
        secrets["gdrive_service_account"],
        scopes=["https://www.googleapis.com/auth/drive"],  # read-write, только для этого скрипта
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False), secrets["gdrive_folder_id"]


def list_remote_files(service, folder_id: str) -> dict[str, str]:
    files, page_token = {}, None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        files.update({f["name"]: f["id"] for f in resp.get("files", [])})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def main():
    if not SECRETS_PATH.exists():
        raise SystemExit(f"Не найден {SECRETS_PATH} — сначала настройте локальные секреты (см. app.py).")

    service, folder_id = get_service()
    remote_files = list_remote_files(service, folder_id)
    local_files = sorted(p for p in DATA_DIR.iterdir() if p.is_file() and not p.name.startswith("."))

    if not local_files:
        raise SystemExit(f"В {DATA_DIR} нет файлов для заливки — сначала запустите campaign_metrics_bak.py.")

    uploaded, updated, failed = 0, 0, []
    for path in local_files:
        media = MediaFileUpload(str(path), mimetype=MIME_TYPES.get(path.suffix, "application/octet-stream"))
        try:
            if path.name in remote_files:
                service.files().update(fileId=remote_files[path.name], media_body=media).execute()
                print(f"обновлён: {path.name}")
                updated += 1
            else:
                service.files().create(
                    body={"name": path.name, "parents": [folder_id]}, media_body=media
                ).execute()
                print(f"загружен новый: {path.name}")
                uploaded += 1
        except Exception as e:
            print(f"⚠ НЕ УДАЛОСЬ залить {path.name}: {e}")
            failed.append(path.name)
            continue

    stray = sorted(set(remote_files) - {p.name for p in local_files})
    print(f"\nГотово: обновлено {updated}, загружено новых {uploaded}.")
    if failed:
        print(f"\n✗ ОШИБКИ при заливке ({len(failed)}): {', '.join(failed)}")
        print("  Если ошибка 'storageQuotaExceeded' — сервисный аккаунт не может САМ создать новый файл")
        print("  (нет своей квоты хранилища). Создайте/переименуйте файл с таким именем на Drive вручную")
        print("  один раз (через веб-интерфейс, под своим аккаунтом) — дальше скрипт сможет его обновлять.")
    if stray:
        print(f"\n⚠ На Drive есть файлы, которых нет локально в data/ (не тронуты, удалите вручную при необходимости):")
        for name in stray:
            print(f"  - {name}")


if __name__ == "__main__":
    main()
