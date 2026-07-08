#!/usr/bin/env python
"""
parse_supply_bak.py — разбор таблиц мест приёма (КЦП) бакалавриата из RTF
(docs/{moscow,nnov,perm,spb}_supply.rtf, по одному на филиал) в единую
data/supply_bak_raw.csv.

В отличие от match_supply_demand.py магистратуры (два идентичных по
структуре файла), 4 файла бакалавриата СТРУКТУРНО РАЗНЫЕ — разное число
колонок, разный порядок квот, и Пермь вообще без гиперссылок на программы:

  - Москва (8 колонок): Название(+ссылка) | Онлайн | Бюджет | Особое право |
    Целевая квота | Отдельная квота | Платные | Иностранцы
  - Нижний Новгород (7): Название(+ссылка) | Бюджет | Особое право |
    Отдельная квота | Целевая квота | Платные | Иностранцы
    (!) порядок «отдельная/целевая» ПРОТИВОПОЛОЖНЫЙ Москве
  - СПб (7): тот же порядок колонок, что у Нижнего Новгорода
  - Пермь (8, БЕЗ ссылок): Код направления+имя направления | Образовательная
    программа | Бюджет | Особое право | Отдельная квота | Целевая квота |
    Платные | Иностранцы — единственный файл, где код направления подготовки
    (ОКСО, "09.03.04") идёт explicit-колонкой в каждой строке, а не общим
    заголовком над группой строк.

Поэтому конфигурация колонок задаётся явно per-филиал (FILIAL_CONFIGS), а не
угадывается по позиции или по тексту заголовка.

Строки-заголовки направлений подготовки ("НАПРАВЛЕНИЕ ПОДГОТОВКИ 01.03.02 ...")
не являются строками программ (нет данных о местах), но несут код направления
(формат ОКСО "NN.03.NN"), который дальше пригодится для разрешения
программ-«близнецов» (см. docs/FINDINGS.md — 10 случаев, где одно и то же
имя+филиал скрывает разные trainingDirection). Код из последнего увиденного
заголовка переносится на все следующие строки программ как direction_codes_hint
— informational, не заменяет join по educationProgramId при сведении с raw/.

Итоговые числа сверяются с явными строками "Итого"/"Всего" в каждом файле —
это встроенная проверка, что парсинг не съехал по колонкам (аналог
SUSPICIOUS_BUDGET_THRESHOLD в match_supply_demand.py, только точнее: сверка
не с порогом, а с реальной опубликованной суммой).
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import pandas as pd

OKSO_CODE_RE = re.compile(r"\d{2}\.\d{2}\.\d{2}")

QUOTA_COLS = [
    "budget_places",
    "special_right_places",
    "separate_quota_places",
    "target_quota_places",
    "commercial_places",
    "foreigner_places",
]

# --------------------------------------------------------------------------
# Конфигурация по филиалам — проверено вручную на каждом из 4 файлов
# (см. докстринг). Индексы — позиция в списке <td> строки данных.
# --------------------------------------------------------------------------

FILIAL_CONFIGS = {
    "Москва": {
        "file": "moscow_supply.rtf",
        "td_count": 8,
        "has_link": True,
        "name_col": 0,
        "direction_code_col": None,  # кода в строке нет — берём из заголовка-переноса
        "col_map": {
            "budget_places": 2,
            "special_right_places": 3,
            "target_quota_places": 4,
            "separate_quota_places": 5,
            "commercial_places": 6,
            "foreigner_places": 7,
        },
    },
    "Нижний Новгород": {
        "file": "nnov_supply.rtf",
        "td_count": 7,
        "has_link": True,
        "name_col": 0,
        "direction_code_col": None,
        "col_map": {
            "budget_places": 1,
            "special_right_places": 2,
            "separate_quota_places": 3,
            "target_quota_places": 4,
            "commercial_places": 5,
            "foreigner_places": 6,
        },
    },
    "Санкт-Петербург": {
        "file": "spb_supply.rtf",
        "td_count": 7,
        "has_link": True,
        "name_col": 0,
        "direction_code_col": None,
        "col_map": {
            "budget_places": 1,
            "special_right_places": 2,
            "separate_quota_places": 3,
            "target_quota_places": 4,
            "commercial_places": 5,
            "foreigner_places": 6,
        },
    },
    "Пермь": {
        "file": "perm_supply.rtf",
        "td_count": 8,
        "has_link": False,
        "name_col": 1,  # колонка 0 — код+название направления, программа — колонка 1
        "direction_code_col": 0,
        "col_map": {
            "budget_places": 2,
            "special_right_places": 3,
            "separate_quota_places": 4,
            "target_quota_places": 5,
            "commercial_places": 6,
            "foreigner_places": 7,
        },
    },
}

TOTAL_ROW_MARKERS = ("итого", "всего")
# Строка-заголовок таблицы имеет то же число <td>, что и строки данных (не
# отсекается по td_count), и не содержит "итого"/"всего" — нужен отдельный
# маркер. Фраза "образовательная программа" встречается во всех 4 вариантах
# заголовка (разное окружение — "Направление подготовки .../образовательная
# программа", "...образовательная программа бакалавриата",
# "Специальность/образовательная программа"), но не в реальных именах
# программ. Баг найден сверкой с "Итого": заголовок Нижнего Новгорода/СПб
# содержал "...в 2026 году..." — число 2026 из года попадало в сумму как
# будто это места по отдельной/целевой квоте.
HEADER_ROW_MARKER = "образовательная программа"


def rtf_to_html(rtf_path: Path) -> Path:
    html_path = rtf_path.with_suffix(".html")
    subprocess.run(
        ["textutil", "-convert", "html", str(rtf_path), "-output", str(html_path)],
        check=True,
    )
    return html_path


def _cell_text(td_html: str) -> str:
    """Текст ячейки, ссылки-сноски (<a>...</a> с одиночной цифрой-маркером)
    НЕ вырезаются здесь — только в _cell_number, где это важно для чисел."""
    text = re.sub(r"<[^>]+>", " ", td_html)
    return re.sub(r"\s+", " ", text).strip()


def _cell_number(td_html: str) -> int:
    """Число из ячейки квоты. Сноски вида '12 <a href="...#1">1</a>' —
    убираем содержимое <a> целиком перед поиском числа, иначе маркер сноски
    (сам по себе тоже цифра) может быть принят за настоящее значение, если
    оно вдруг окажется пустым/нечисловым. У Перми встречается описательный
    текст в ячейке целевой квоты ('13 (в т.ч. 5 - Администрация...)') —
    берём первое число в тексте (это и есть итоговая величина, скобки —
    расшифровка по заказчикам, не отдельное значение)."""
    without_footnote_links = re.sub(r"<a[^>]*>.*?</a>", "", td_html, flags=re.S)
    text = _cell_text(without_footnote_links)
    if text in ("", "-", "–", "—"):
        return 0
    m = re.search(r"\d+", text)
    return int(m.group()) if m else 0


def _extract_link(td_html: str) -> tuple[str, str]:
    """Имя программы иногда переносится на вторую строку внутри той же ячейки
    (два <p><a>...</a></p> с одинаковым href — например, "Фундаментальная и
    прикладная" / "лингвистика" двумя параграфами). Берём текст ВСЕЙ ячейки
    (склеивает оба фрагмента через пробел), а не только первого <a> —
    иначе вторая строка названия молча теряется."""
    name = _cell_text(td_html)
    m = re.search(r'<a href="([^"]*)"', td_html)
    url = m.group(1) if m else ""
    return name, url


def parse_supply_rtf(rtf_path: Path, filial: str, config: dict) -> tuple[pd.DataFrame, dict]:
    html_path = rtf_to_html(rtf_path)
    html = html_path.read_text(encoding="utf-8")

    table_m = re.search(r"<table.*?</table>", html, re.S)
    assert table_m, f"{rtf_path}: не найдена <table> после конвертации в HTML"
    trs = re.findall(r"<tr.*?</tr>", table_m.group(0), re.S)

    rows = []
    totals = {}
    current_codes: list[str] = []

    for tr in trs:
        tds = re.findall(r"<td.*?</td>", tr, re.S)

        # --- строки-заголовки направлений (1 <td>, содержит код ОКСО) —
        # переносим код на последующие строки программ, сами не данные ---
        if len(tds) == 1:
            text = _cell_text(tds[0])
            codes = OKSO_CODE_RE.findall(text)
            if codes:
                current_codes = codes
            continue

        if len(tds) != config["td_count"]:
            continue  # заголовок таблицы / служебная строка с другим числом колонок

        name_col = config["name_col"]
        if config["has_link"]:
            name, url = _extract_link(tds[name_col])
        else:
            name, url = _cell_text(tds[name_col]), ""

        first_cell_text = _cell_text(tds[0]).lower()
        is_total_row = any(marker in first_cell_text for marker in TOTAL_ROW_MARKERS)
        is_header_row = HEADER_ROW_MARKER in first_cell_text or HEADER_ROW_MARKER in _cell_text(tds[name_col]).lower()

        if is_header_row:
            continue

        if is_total_row:
            totals = {
                col: _cell_number(tds[idx]) for col, idx in config["col_map"].items()
            }
            continue

        if not name:
            continue  # пустая строка / артефакт таблицы

        if config["direction_code_col"] is not None:
            direction_text = _cell_text(tds[config["direction_code_col"]])
            row_codes = OKSO_CODE_RE.findall(direction_text) or current_codes
        else:
            row_codes = current_codes

        row = {
            "filial": filial,
            "educationProgram": name,
            "program_url": url,
            "direction_codes_hint": ";".join(row_codes),
        }
        for col, idx in config["col_map"].items():
            row[col] = _cell_number(tds[idx])
        rows.append(row)

    return pd.DataFrame(rows), totals


def sanity_check(df: pd.DataFrame, totals: dict, filial: str) -> list[str]:
    problems = []
    if not totals:
        problems.append(f"[{filial}] строка «Итого/Всего» не найдена — сверить парсинг вручную")
        return problems
    for col in QUOTA_COLS:
        parsed_sum = int(df[col].sum())
        expected = totals.get(col)
        if expected is None:
            continue
        if parsed_sum != expected:
            problems.append(
                f"[{filial}] {col}: сумма по строкам = {parsed_sum}, а в «Итого» = {expected} "
                f"(расхождение {parsed_sum - expected:+d}) — возможен сдвиг колонок или пропущенная строка"
            )
    return problems


def main():
    p = argparse.ArgumentParser(description="Разбор RTF-таблиц мест приёма (КЦП) бакалавриата")
    p.add_argument("docs_dir", type=Path, help="папка с {moscow,nnov,perm,spb}_supply.rtf")
    p.add_argument("-o", "--out-dir", type=Path, default=Path("data"))
    args = p.parse_args()

    all_rows = []
    all_problems = []

    for filial, config in FILIAL_CONFIGS.items():
        rtf_path = args.docs_dir / config["file"]
        assert rtf_path.exists(), f"не найден {rtf_path}"
        df, totals = parse_supply_rtf(rtf_path, filial, config)
        problems = sanity_check(df, totals, filial)
        all_problems.extend(problems)
        print(f"{filial}: {len(df)} программ, бюджетных мест по строкам = {int(df['budget_places'].sum())}"
              + (f", по «Итого» = {totals.get('budget_places')}" if totals else " (⚠ «Итого» не найдено)"))
        all_rows.append(df)

    supply = pd.concat(all_rows, ignore_index=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "supply_bak_raw.csv"
    supply.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"\nВсего программных строк по всем 4 филиалам: {len(supply)} -> {out_path}")

    if all_problems:
        print(f"\n✗ Обнаружено расхождений с «Итого»: {len(all_problems)}")
        for msg in all_problems:
            print(f"  ⚠ {msg}")
    else:
        print("\n✓ Все 4 филиала: сумма по строкам совпадает с опубликованным «Итого»/«Всего» по каждой колонке квот.")

    # --- явный список программ-«близнецов» на этом источнике (одно имя+филиал, >1 строки) ---
    dupe = supply.groupby(["filial", "educationProgram"]).size()
    dupes = dupe[dupe > 1]
    if len(dupes):
        print(f"\n⚠ Программы с несколькими строками в supply на один филиал (проверить перед join с raw/):")
        for (filial, name), n in dupes.items():
            codes = supply[(supply["filial"] == filial) & (supply["educationProgram"] == name)]["direction_codes_hint"].tolist()
            print(f"  {name!r} @ {filial}: {n} строк, direction_codes_hint={codes}")


if __name__ == "__main__":
    main()
