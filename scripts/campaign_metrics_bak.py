#!/usr/bin/env python
"""
campaign_metrics_bak.py — слой метрик бакалаврской приёмной кампании (M1-M9)
поверх собранных данных pk.hse.ru (bak_data/) и мест приёма (data/supply_bak_raw.csv,
см. parse_supply_bak.py). Формулы и решения — см. docs/METRICS.md, история
разведки и обоснование — docs/FINDINGS.md, docs/HANDOFF_FOR_METRICS_DESIGN.md.

Три канала на программу (НЕ budget/commercial, как в магистратуре — здесь
структура богаче, и целевая квота НЕ сопоставима по priority с бюджетом):

  - budget_family — Бюджетные места + Отдельная квота + Особое право,
    схлопнуты до одной строки на (idEpgu, educationProgramId) правилом ниже.
  - commercial — С оплатой обучения, независимая нумерация приоритета.
  - target_quota — независимая нумерация приоритета; НЕ входит в M4/M5/M6.

Правило схлопывания budget_family (разобрано поимённо на всех 47
расходящихся случаях, см. HANDOFF §6.1): для каждого (idEpgu,
educationProgramId) с несколькими строками — берём приоритет из строк со
статусом, отличным от "Отозвано поступающим"; если таких несколько и они
совпадают — общее значение; если совпадений нет (единственный случай на всю
выборку) — минимальный priority как тай-брейк.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

K_SERIOUS = 5  # решение пользователя для бакалавриата (не 2, как в магистратуре)
MIN_N_FOR_VARIANCE_ESTIMATION = 10

BUDGET_FAMILY_TRACKS = {
    "Бюджетные места",
    "Отдельная квота",
    "Квота приема лиц, имеющих особое право",
}
COMMERCIAL_TRACK = "С оплатой обучения"

# Сокращения городов для приписки к display_name программ-«дублёров» между
# филиалами (одно и то же имя программы в нескольких городах — например
# «Медиакоммуникации» в Москве и в СПб — это НЕ близнецы, а нормальная
# ситуация, см. program_base). Москва в словаре сознательно отсутствует —
# московский вариант остаётся без приписки, решение пользователя.
CITY_ABBREV = {
    "Санкт-Петербург": "СПб",
    "Нижний Новгород": "НН",
    "Пермь": "Пм",
}

# Первое приближение по смыслу названия статуса (документации от приёмной
# комиссии нет) — см. docs/METRICS.md §0. Пересмотреть, если появится
# официальное описание статусов.
INACTIVE_STATUSES = {"Отозвано поступающим", "Отклонено вузом"}


# --------------------------------------------------------------------------
# 1. Загрузка сырых данных
# --------------------------------------------------------------------------

def load_applications(bak_data_dir: Path) -> pd.DataFrame:
    rows = []

    for path in glob.glob(str(bak_data_dir / "raw" / "*.json")):
        d = json.load(open(path, encoding="utf-8"))
        meta = d["meta"]
        direction = meta.get("trainingDirection") or {}
        for a in d["applicants"]:
            rows.append({
                "idEpgu": a.get("idEpgu"),
                "educationProgramId": meta["educationProgramId"],
                "educationProgram": meta["educationProgram"],
                "filial": meta["filial"],
                "trainingDirectionCode": direction.get("code"),
                "trainingDirectionName": direction.get("name"),
                "placeTypeName": meta["placeTypeName"],
                "priority": a.get("priority"),
                "participantStatus": a.get("participantStatus"),
                "score": a.get("sumCompetitiveScore"),
            })

    for path in glob.glob(str(bak_data_dir / "raw_target_quota" / "*.json")):
        d = json.load(open(path, encoding="utf-8"))
        meta = d["meta"]
        direction = meta.get("trainingDirection") or {}
        for a in d["applicants"]:
            rows.append({
                "idEpgu": a.get("idEpgu"),
                "educationProgramId": meta["educationProgramId"],
                "educationProgram": meta["educationProgram"],
                "filial": meta["filial"],
                "trainingDirectionCode": direction.get("code"),
                "trainingDirectionName": direction.get("name"),
                "placeTypeName": "Целевая квота",
                "priority": a.get("priority"),
                "participantStatus": a.get("participantStatus"),
                "score": a.get("sumCompetitiveScoreTarget"),
            })

    df = pd.DataFrame(rows)
    df["is_active"] = ~df["participantStatus"].isin(INACTIVE_STATUSES)
    return df


def program_base(df: pd.DataFrame) -> pd.DataFrame:
    """educationProgramId -> (имя, филиал, код и название направления,
    display_name). 1:1 — проверено неявно тем, что groupby ниже используется
    только для дедупликации.

    display_name существует из-за программ-«близнецов» (10+ подтверждённых
    случаев: одно имя+филиал, разные educationProgramId, разные
    trainingDirection — см. docs/FINDINGS.md). Без разрешения дублей
    построение матрицы пересечений (program_intersections) молча создаёт
    ДУБЛИРУЮЩИЙСЯ индекс по имени программы — это ломает точечный доступ
    matrix.loc[name] у любого потребителя (например, дашборда), не только
    визуально путает. display_name = educationProgram, если имя уникально в
    каталоге; иначе — с явной припиской "(направление)".

    Отдельно — программы-«дублёры» между филиалами (одно и то же имя в
    нескольких городах, например «Медиакоммуникации» в Москве и в СПб — это
    норма, не близнецы). В списках без колонки «Филиал» рядом (например,
    st.selectbox с выбором программы по имени) такие пары неотличимы на
    глаз — решение пользователя: московский вариант оставить как есть, а
    немосковским приписать сокращённое название города (см. CITY_ABBREV)."""
    base = (
        df[["educationProgramId", "educationProgram", "filial",
            "trainingDirectionCode", "trainingDirectionName"]]
        .drop_duplicates("educationProgramId")
        .set_index("educationProgramId")
    )
    # Дубли считаем В ПРЕДЕЛАХ ФИЛИАЛА — разные филиалы и так различимы отдельной
    # колонкой «Филиал» в любом UI; приписывать направление им не нужно (иначе,
    # например, «Юриспруденция» в 4 филиалах получила бы избыточную и странную
    # приписку "(Юриспруденция)" — имя направления совпадает с именем программы).
    # Настоящие близнецы — это то самое совпадение имя+филиал, см. FINDINGS.md.
    name_counts = base.groupby(["filial", "educationProgram"]).size()
    base["display_name"] = base.apply(
        lambda r: r["educationProgram"] if name_counts.get((r["filial"], r["educationProgram"]), 0) <= 1
        else f'{r["educationProgram"]} ({r["trainingDirectionName"]})',
        axis=1,
    )

    # Приписка города для дублёров между филиалами (см. докстринг выше).
    # Дубль определяем по уже готовому display_name (после разрешения
    # близнецов), глобально по всем филиалам — иначе «Юриспруденция» (4
    # филиала) попала бы под то же правило, что и «Медиакоммуникации» (2).
    cross_filial_counts = base["display_name"].value_counts()
    base["display_name"] = base.apply(
        lambda r: r["display_name"] if (
            cross_filial_counts.get(r["display_name"], 0) <= 1 or r["filial"] not in CITY_ABBREV
        ) else f'{r["display_name"]} — {CITY_ABBREV[r["filial"]]}',
        axis=1,
    )

    # matrix_label — ОТДЕЛЬНАЯ, ГЛОБАЛЬНО уникальная метка (не только в
    # пределах филиала) для program_intersections (M8): та матрица
    # индексируется ТОЛЬКО по имени, без отдельной колонки "Филиал" рядом.
    # display_name теперь уже глобально уникален в подтверждённых случаях
    # (Москва — не более одного вхождения на имя после разрешения близнецов
    # внутри филиала, остальные города получили приписку выше), но эта
    # проверка — на случай непредвиденной комбинации (например, будущего
    # города без записи в CITY_ABBREV): .sort_values() без явного by= падает
    # с TypeError у любого потребителя при дублирующемся индексе (найдено на
    # реальном использовании дашборда, 2026-07-09, канал "Бюджет (+квоты)",
    # "Медиакоммуникации").
    global_name_counts = base["display_name"].value_counts()
    base["matrix_label"] = base.apply(
        lambda r: r["display_name"] if global_name_counts.get(r["display_name"], 0) <= 1
        else f'{r["display_name"]} — {r["filial"]}',
        axis=1,
    )
    return base


# --------------------------------------------------------------------------
# 2. Схлопывание узкой бюджетной семьи
# --------------------------------------------------------------------------

def collapse_budget_family(df: pd.DataFrame) -> pd.DataFrame:
    fam = df[df["placeTypeName"].isin(BUDGET_FAMILY_TRACKS)]

    def pick(g: pd.DataFrame) -> pd.Series:
        active = g[g["is_active"]]
        pool, is_active = (active, True) if len(active) > 0 else (g, False)
        row = pool.loc[pool["priority"].idxmin()].copy()
        row["is_active"] = is_active
        return row

    collapsed = (
        fam.groupby(["idEpgu", "educationProgramId"], group_keys=False)
        .apply(pick, include_groups=False)
        .reset_index()
    )
    return collapsed


# --------------------------------------------------------------------------
# 3. M1-M3
# --------------------------------------------------------------------------

def compute_m1_m2_m3(budget_family: pd.DataFrame, commercial: pd.DataFrame,
                      target_quota: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    def counts(channel_df: pd.DataFrame, suffix: str) -> pd.DataFrame:
        active = channel_df[channel_df["is_active"]]
        m1 = active.groupby("educationProgramId").size().rename(f"n_applications_{suffix}")
        m3 = (
            active[active["priority"] <= K_SERIOUS]
            .groupby("educationProgramId")
            .size()
            .rename(f"n_serious_{suffix}")
        )
        return pd.concat([m1, m3], axis=1)

    parts = [
        counts(budget_family, "budget_family"),
        counts(commercial, "commercial"),
    ]
    # у целевой квоты нет M3 (priority несопоставим, см. docs/METRICS.md) —
    # только M1 (счётчик заявок).
    tq_active = target_quota[target_quota["is_active"]]
    m1_tq = tq_active.groupby("educationProgramId")["idEpgu"].nunique().rename("n_applications_target_quota")
    parts.append(m1_tq.to_frame())

    m2 = pd.concat([
        budget_family[budget_family["is_active"]][["educationProgramId", "idEpgu"]],
        commercial[commercial["is_active"]][["educationProgramId", "idEpgu"]],
        tq_active[["educationProgramId", "idEpgu"]],
    ])
    m2 = m2.groupby("educationProgramId")["idEpgu"].nunique().rename("n_unique_applicants_total")

    out = base.join(parts).join(m2).fillna(0)
    count_cols = [c for c in out.columns if c not in (
        "educationProgram", "filial", "trainingDirectionCode", "trainingDirectionName",
        "display_name", "matrix_label",
    )]
    out[count_cols] = out[count_cols].astype(int)
    return out.reset_index()


# --------------------------------------------------------------------------
# 4. M4 — рейтинг интереса (эмпирический Байес), только budget_family, активные
# --------------------------------------------------------------------------

def compute_m4(budget_family: pd.DataFrame, base: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    active = budget_family[budget_family["is_active"]]
    per_program = active.groupby("educationProgramId").agg(
        cnt_p1=("priority", lambda s: (s == 1).sum()),
        n=("priority", "size"),
    )

    p0 = per_program["cnt_p1"].sum() / per_program["n"].sum()

    share_p1 = per_program["cnt_p1"] / per_program["n"]
    big = per_program[per_program["n"] >= MIN_N_FOR_VARIANCE_ESTIMATION]
    share_p1_big = share_p1.loc[big.index]

    obs_var = share_p1_big.var(ddof=1)
    samp_var = (share_p1_big * (1 - share_p1_big) / big["n"]).mean()
    true_var = max(obs_var - samp_var, 1e-6)
    if obs_var <= samp_var:
        print(f"⚠ M4: вся наблюдаемая вариация — выборочный шум (obs_var={obs_var:.5f} <= "
              f"samp_var={samp_var:.5f}); k уйдёт вверх, межпрограммного сигнала не обнаружено.")

    k = max(p0 * (1 - p0) / true_var - 1, 1.0)

    per_program["desirability"] = (per_program["cnt_p1"] + k * p0) / (per_program["n"] + k)
    per_program["low_data"] = per_program["n"] < MIN_N_FOR_VARIANCE_ESTIMATION

    # Апостериорная дисперсия Beta-Binomial модели — нужна как вес при
    # объединении интереса с другим каналом (см. compute_m4_combined).
    # a, b — параметры апостериорного Beta(a,b); var(Beta) = ab/((a+b)^2(a+b+1)).
    a = per_program["cnt_p1"] + k * p0
    b = (per_program["n"] - per_program["cnt_p1"]) + k * (1 - p0)
    per_program["posterior_var"] = a * b / ((a + b) ** 2 * (a + b + 1))

    result = base.join(per_program, how="left")
    meta = {
        "k": k, "p0": p0, "obs_var": obs_var, "samp_var": samp_var, "true_var": true_var,
        "n_programs_used_for_variance": int(len(big)),
        "n_programs_with_budget_data": int(len(per_program)),
    }
    return result.reset_index(), meta


def compute_m4_combined(m4_budget: pd.DataFrame, m4_commercial: pd.DataFrame) -> pd.DataFrame:
    """Объединённый интерес (M4) — бюджет+квоты и платное вместе, через
    inverse-variance weighting (взвешивание по обратной апостериорной
    дисперсии каждого канала, посчитанной в compute_m4). Не произвольный вес —
    прямое следствие уже существующей Beta-Binomial модели: чем больше данных
    и увереннее оценка в канале, тем больше его вклад.

    Оправдано эмпирически: корреляция независимо посчитанного интереса
    (бюджет vs платное) на срезе 2026-07-08 — 0.94 по всем 121 программе с
    данными в обоих каналах (проверено в сессии, см. чат/docs/FINDINGS.md) —
    это ДВЕ ОЦЕНКИ ОДНОЙ И ТОЙ ЖЕ величины с разным шумом, не два разных
    явления, которые опасно смешивать (в отличие, например, от целевой
    квоты — там корреляция с бюджетом всего 10.4%, поэтому её объединять
    нельзя, см. docs/METRICS.md §0).

    Если канала нет (программа целиком без бюджетных мест или целиком без
    платных) — его вес естественно обнуляется (var=NaN -> 1/var=0), и
    итоговая оценка гладко превращается в интерес оставшегося канала —
    не требует отдельного случая "нет данных" в коде."""
    b = m4_budget[["educationProgramId", "desirability", "posterior_var"]].rename(
        columns={"desirability": "desirability_budget", "posterior_var": "var_budget"})
    c = m4_commercial[["educationProgramId", "desirability", "posterior_var"]].rename(
        columns={"desirability": "desirability_commercial", "posterior_var": "var_commercial"})
    merged = b.merge(c, on="educationProgramId", how="outer")

    w_budget = 1 / merged["var_budget"]
    w_comm = 1 / merged["var_commercial"]
    w_budget = w_budget.where(np.isfinite(w_budget), 0)
    w_comm = w_comm.where(np.isfinite(w_comm), 0)

    num = w_budget * merged["desirability_budget"].fillna(0) + w_comm * merged["desirability_commercial"].fillna(0)
    den = w_budget + w_comm
    merged["desirability_combined"] = np.where(den > 0, num / den, np.nan)
    return merged[["educationProgramId", "desirability_budget", "desirability_commercial", "desirability_combined"]]


# --------------------------------------------------------------------------
# 5. Сопоставление с местами приёма (КЦП) — supply_bak_raw.csv
# --------------------------------------------------------------------------

# Курируемая карта псевдонимов: сокращение <-> полное название. Найдено при
# сверке "программ без КЦП" — не устраняется общей нормализацией, т.к. это не
# опечатка/регистр, а осознанное сокращение в одном из двух источников.
SUPPLY_TO_RAW_ALIAS = {
    "совместный бакалавриат ниу вшэ и центра педагогического мастерства":
        "совместный бакалавриат ниу вшэ и цпм",
}


def normalize_name(name: str) -> str:
    name = str(name).replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е")
    name = re.split(r"\s*/\s*", name)[0]  # "рус / eng" -> "рус"
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)  # убрать "(...)" в конце
    name = re.sub(r"[«»\"“”]", "", name)  # разные виды кавычек — не несут различающего смысла
    name = re.sub(r"\*+\s*$", "", name)  # хвостовой маркер сноски вида "Архитектура **"
    name = re.sub(r"\s+", " ", name).strip().lower()
    return SUPPLY_TO_RAW_ALIAS.get(name, name)


def match_supply(base: pd.DataFrame, supply: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Сопоставляет educationProgramId с местами приёма по (filial,
    нормализованное имя), с разрешением близнецов через direction_codes_hint
    <-> trainingDirectionCode. Возвращает (merged, ambiguous)."""
    catalog = base.reset_index()[["educationProgramId", "educationProgram", "filial", "trainingDirectionCode"]].copy()
    catalog["key"] = catalog["educationProgram"].map(normalize_name)

    supply = supply.copy()
    supply["key"] = supply["educationProgram"].map(normalize_name)
    supply["codes"] = supply["direction_codes_hint"].fillna("").apply(
        lambda s: [c for c in s.split(";") if c]
    )

    supply_by_group = supply.groupby(["filial", "key"])

    matched_rows = []
    ambiguous_rows = []

    for _, prog in catalog.iterrows():
        try:
            candidates = supply_by_group.get_group((prog["filial"], prog["key"]))
        except KeyError:
            continue  # нет пары в supply вообще — попадёт в unmatched через outer-diff ниже

        if len(candidates) == 1:
            chosen = candidates.iloc[0]
        else:
            # несколько supply-строк на (filial, key) — близнецы. Разрешаем
            # по коду направления, если он однозначно указывает на одну строку.
            code = prog["trainingDirectionCode"]
            by_code = candidates[candidates["codes"].apply(lambda cs: code in cs)]
            if len(by_code) == 1:
                chosen = by_code.iloc[0]
            else:
                ambiguous_rows.append({
                    "educationProgramId": prog["educationProgramId"],
                    "educationProgram": prog["educationProgram"],
                    "filial": prog["filial"],
                    "trainingDirectionCode": code,
                    "n_supply_candidates": len(candidates),
                })
                continue

        matched_rows.append({
            "educationProgramId": prog["educationProgramId"],
            "budget_places": chosen["budget_places"],
            "special_right_places": chosen["special_right_places"],
            "separate_quota_places": chosen["separate_quota_places"],
            "target_quota_places": chosen["target_quota_places"],
            "commercial_places": chosen["commercial_places"],
            "foreigner_places": chosen["foreigner_places"],
        })

    matched = pd.DataFrame(matched_rows).set_index("educationProgramId") if matched_rows else pd.DataFrame(
        columns=["budget_places", "special_right_places", "separate_quota_places",
                 "target_quota_places", "commercial_places", "foreigner_places"]
    )
    ambiguous = pd.DataFrame(ambiguous_rows)
    return matched, ambiguous


# --------------------------------------------------------------------------
# 6. M5, M6, диагностика по целевой квоте относительно бюджета
# --------------------------------------------------------------------------

def compute_m5_m6(m1_m2_m3: pd.DataFrame, supply_matched: pd.DataFrame) -> pd.DataFrame:
    merged = m1_m2_m3.merge(supply_matched, on="educationProgramId", how="left")

    has_places = (merged["budget_places"] > 0).fillna(False)
    merged["competition_ratio_budget_family"] = np.where(
        has_places, merged["n_applications_budget_family"] / merged["budget_places"], np.nan
    )
    merged["demand_pressure_budget_family"] = np.where(
        has_places, merged["n_serious_budget_family"] / merged["budget_places"], np.nan
    )
    merged["no_budget_places"] = ~has_places

    merged["target_quota_share_of_budget"] = np.where(
        has_places, merged["target_quota_places"] / merged["budget_places"], np.nan
    )
    return merged


# --------------------------------------------------------------------------
# 7. M7 — распределение приоритетов, три канала
# --------------------------------------------------------------------------

def compute_m7(budget_family: pd.DataFrame, commercial: pd.DataFrame,
               target_quota: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    def dist(channel_df: pd.DataFrame, suffix: str) -> pd.Series:
        active = channel_df[channel_df["is_active"]].dropna(subset=["priority"])
        return active.groupby(["educationProgramId", "priority"]).size().rename(f"n_{suffix}")

    parts = [
        dist(budget_family, "budget_family"),
        dist(commercial, "commercial"),
        dist(target_quota, "target_quota"),
    ]
    out = pd.concat(parts, axis=1).fillna(0).reset_index()
    for c in ("n_budget_family", "n_commercial", "n_target_quota"):
        out[c] = out[c].astype(int)
    out = out.merge(base.reset_index()[["educationProgramId", "educationProgram", "filial", "display_name"]],
                     on="educationProgramId", how="left")
    return out.sort_values(["filial", "educationProgram", "priority"])


# --------------------------------------------------------------------------
# 8. M8 — матрицы пересечений, три канала
# --------------------------------------------------------------------------

def program_intersections(channel_df: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    active = channel_df[channel_df["is_active"]]
    incidence = (
        active[["idEpgu", "educationProgramId"]]
        .drop_duplicates()
        .assign(flag=1)
        .pivot_table(index="idEpgu", columns="educationProgramId", values="flag", fill_value=0)
    )
    overlap = incidence.T.dot(incidence)
    # matrix_label, не display_name — у матрицы нет отдельной колонки "Филиал"
    # рядом, поэтому нужна ГЛОБАЛЬНО (не только в пределах филиала) уникальная
    # метка, иначе одноимённые программы в разных филиалах (например
    # «Медиакоммуникации» в Москве и в СПб — норма, не близнецы) дают
    # дублирующийся индекс и ломают matrix.loc[name] у потребителя.
    name_map = base["matrix_label"]
    overlap.index = overlap.index.map(name_map)
    overlap.columns = overlap.columns.map(name_map)
    return overlap


# --------------------------------------------------------------------------
# 9. M9 — диагностика (не для основного дашборда, кроме target_quota_share)
# --------------------------------------------------------------------------

def compute_m9(df_raw: pd.DataFrame, budget_family: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    active_fam = budget_family[budget_family["is_active"]]
    noise = active_fam.groupby("educationProgramId").agg(
        n_budget_family=("priority", "size"),
        n_noisy=("priority", lambda s: (s > K_SERIOUS).sum()),
    )
    noise["noise_share_budget_family"] = np.where(
        noise["n_budget_family"] > 0, noise["n_noisy"] / noise["n_budget_family"], np.nan
    )

    withdrawn = (
        df_raw.groupby(["educationProgramId", "placeTypeName"])
        .agg(n_total=("idEpgu", "size"), n_inactive=("is_active", lambda s: (~s).sum()))
        .reset_index()
    )
    withdrawn["withdrawn_share"] = withdrawn["n_inactive"] / withdrawn["n_total"]
    withdrawn_pivot = withdrawn.pivot_table(
        index="educationProgramId", columns="placeTypeName", values="withdrawn_share"
    )
    withdrawn_pivot.columns = [f"withdrawn_share__{c}" for c in withdrawn_pivot.columns]

    out = base.join(noise[["n_budget_family", "n_noisy", "noise_share_budget_family"]], how="left")
    out = out.join(withdrawn_pivot, how="left")
    return out.dropna(subset=["n_budget_family"]).reset_index()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Метрики бакалаврской приёмной кампании (M1-M9)")
    p.add_argument("bak_data_dir", type=Path)
    p.add_argument("supply_csv", type=Path)
    p.add_argument("--snapshot-date", type=str, default=None)
    p.add_argument("-o", "--out-dir", type=Path, default=Path("data"))
    args = p.parse_args()

    df = load_applications(args.bak_data_dir)
    print(f"Загружено заявок: {len(df)}, программ: {df['educationProgramId'].nunique()}")

    base = program_base(df)
    budget_family = collapse_budget_family(df)
    commercial = df[df["placeTypeName"] == COMMERCIAL_TRACK]
    target_quota = df[df["placeTypeName"] == "Целевая квота"]

    print(f"Бюджетная семья после схлопывания: {len(budget_family)} строк "
          f"(было {len(df[df['placeTypeName'].isin(BUDGET_FAMILY_TRACKS)])} до схлопывания)")

    # Сохраняем в parquet для дашборда — не парсить ~530 JSON-файлов заново
    # при каждом запуске приложения (аналог admissions_long_*.parquet
    # в магистратуре, только без Google Drive, дашборд пока локальный).
    args.out_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out_dir / "bak_applications_long.parquet", index=False)
    budget_family.to_parquet(args.out_dir / "bak_budget_family_collapsed.parquet", index=False)
    print(f"(сырые данные для дашборда) -> {args.out_dir / 'bak_applications_long.parquet'}, "
          f"{args.out_dir / 'bak_budget_family_collapsed.parquet'}")

    supply = pd.read_csv(args.supply_csv)
    supply_matched, ambiguous = match_supply(base, supply)
    print(f"КЦП сопоставлено: {len(supply_matched)}/{len(base)} программ"
          + (f", неоднозначных (близнецы, не разрешены): {len(ambiguous)}" if len(ambiguous) else ""))

    m1_m2_m3 = compute_m1_m2_m3(budget_family, commercial, target_quota, base)
    m5_m6 = compute_m5_m6(m1_m2_m3, supply_matched)
    m4, m4_meta = compute_m4(budget_family, base)
    m4_comm, m4_comm_meta = compute_m4(commercial, base)
    m4_combined = compute_m4_combined(m4, m4_comm)
    m9 = compute_m9(df, budget_family, base)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    main_table = m5_m6.merge(
        m4[["educationProgramId", "cnt_p1", "low_data"]],
        on="educationProgramId", how="left",
    ).rename(columns={"cnt_p1": "n_p1_budget_family"}).merge(
        m4_combined, on="educationProgramId", how="left",
    )

    # Витрина интереса теперь включает ЛЮБУЮ программу, для которой
    # интерес определён хоть в одном канале (бюджет и/или платное) — не
    # только программы с бюджетными местами. Раньше чисто платные программы
    # молча исключались из рейтинга; desirability_combined через
    # inverse-variance weighting корректно закрывает этот пробел (см.
    # compute_m4_combined) — оправдано эмпирически (корреляция 0.94 между
    # каналами на срезе 2026-07-08).
    showcase = main_table[main_table["desirability_combined"].notna()].copy()
    showcase = showcase.sort_values("desirability_combined", ascending=False)
    showcase["desirability_rank"] = range(1, len(showcase) + 1)
    # no_budget_places по-прежнему только про M5/M6 (нужны КЦП с реальными
    # бюджетными местами) — про интерес (M4) эти программы больше не
    # "вне рейтинга", если у них есть хоть какие-то заявки на платное.
    no_places_list = main_table[main_table["no_budget_places"]][
        ["educationProgramId", "educationProgram", "filial", "trainingDirectionName", "display_name"]
    ].copy()

    main_path = args.out_dir / "campaign_metrics_main_bak.csv"
    main_table.to_csv(main_path, index=False, encoding="utf-8-sig")
    print(f"\n(main) M1-M6 -> {main_path} ({len(main_table)} строк)")

    showcase_path = args.out_dir / "campaign_metrics_m4_desirability_ranked_bak.csv"
    showcase[["desirability_rank", "educationProgramId", "educationProgram", "filial",
              "trainingDirectionName", "display_name", "n_p1_budget_family",
              "n_applications_budget_family", "desirability_budget", "desirability_commercial",
              "desirability_combined", "low_data", "target_quota_share_of_budget"]].to_csv(
        showcase_path, index=False, encoding="utf-8-sig"
    )
    print(f"(б) Витрина объединённого интереса (бюджет+квоты И платное) -> {showcase_path} ({len(showcase)} строк)")

    no_places_path = args.out_dir / "campaign_metrics_no_budget_places_bak.csv"
    no_places_list.to_csv(no_places_path, index=False, encoding="utf-8-sig")
    print(f"    Без бюджетных мест (вне M5/M6, но не вне рейтинга интереса) -> {no_places_path} ({len(no_places_list)} программ)")

    m9_path = args.out_dir / "campaign_metrics_m9_diagnostic_bak.csv"
    m9.to_csv(m9_path, index=False, encoding="utf-8-sig")
    print(f"(M9, диагностика) -> {m9_path}")

    if len(ambiguous):
        amb_path = args.out_dir / "campaign_metrics_ambiguous_supply_match_bak.csv"
        ambiguous.to_csv(amb_path, index=False, encoding="utf-8-sig")
        print(f"⚠ Неоднозначные близнецы (КЦП не сопоставлено автоматически) -> {amb_path}")

    m7 = compute_m7(budget_family, commercial, target_quota, base)
    m7_path = args.out_dir / "metric_priority_distribution_bak.csv"
    m7.to_csv(m7_path, index=False, encoding="utf-8-sig")
    print(f"(M7) распределение приоритетов -> {m7_path} ({len(m7)} строк)")

    for channel_df, suffix in ((budget_family, "budget_family"), (commercial, "commercial"), (target_quota, "target_quota")):
        overlap = program_intersections(channel_df, base)
        overlap_path = args.out_dir / f"metric_program_intersections_bak_{suffix}.csv"
        overlap.to_csv(overlap_path, encoding="utf-8-sig")
        print(f"(M8, {suffix}) {overlap.shape[0]}x{overlap.shape[1]} -> {overlap_path}")

    corr_budget_comm = m4_combined[["desirability_budget", "desirability_commercial"]].corr().iloc[0, 1]
    meta = {
        "snapshot_date": args.snapshot_date or date.today().isoformat(),
        "n_programs": int(df["educationProgramId"].nunique()),
        "K_SERIOUS": K_SERIOUS,
        "M4_k_budget": m4_meta["k"],
        "M4_p0_budget": m4_meta["p0"],
        "M4_n_programs_used_for_variance_budget": m4_meta["n_programs_used_for_variance"],
        "M4_n_programs_with_data_budget": m4_meta["n_programs_with_budget_data"],
        "M4_k_commercial": m4_comm_meta["k"],
        "M4_p0_commercial": m4_comm_meta["p0"],
        "M4_n_programs_with_data_commercial": m4_comm_meta["n_programs_with_budget_data"],
        "M4_corr_budget_commercial": None if pd.isna(corr_budget_comm) else round(float(corr_budget_comm), 3),
        "n_supply_matched": int(len(supply_matched)),
        "n_supply_ambiguous": int(len(ambiguous)),
    }
    meta_path = args.out_dir / "campaign_metrics_meta_bak.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nМетаданные -> {meta_path}")
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    n_no_places = int(main_table["no_budget_places"].sum())
    print(f"\nПрограмм без бюджетных мест (M4/M5/M6 = NaN): {n_no_places}")


if __name__ == "__main__":
    main()
