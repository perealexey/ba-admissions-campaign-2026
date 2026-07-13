"""
app.py — дашборд бакалаврской приёмной кампании ВШЭ. Задеплоен на Streamlit
Community Cloud, данные читаются из приватной папки Google Drive через
сервисный аккаунт (тот же, что и в магистратуре, summer_campaign_26/app.py —
новая отдельная папка на Drive, доступ и ID папки в st.secrets). Репозиторий
публичный, но данных абитуриентов в нём нет.

Запуск локально: streamlit run app.py (нужен .streamlit/secrets.toml, см.
summer_campaign_26/docs/PIPELINE.md §7 — структура секретов та же).

Данные устроены иначе, чем в магистратуре — три канала на программу (не
budget/commercial):
  - budget_family — Бюджетные места + Отдельная квота + Особое право
    (схлопнуты до одного человека на программу, см. scripts/campaign_metrics_bak.py)
  - commercial — С оплатой обучения
  - target_quota — целевая квота (не сопоставима по приоритету с бюджетной
    семьёй, см. docs/METRICS.md — отдельная колонка, не отдельный "тип места")
"""
from __future__ import annotations

import io
import json

import altair as alt
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Цвет фона страницы — по референсу от заказчика (скриншот). Таблицы
# (st.dataframe) рисуются на <canvas> и берут цвета ИЗ ТЕМЫ Streamlit
# (--theme.backgroundColor/secondaryBackgroundColor в launch.json), она же
# governs и общий фон .stApp — то есть с одной темой нельзя одновременно
# иметь "белое тело таблицы" и "синюю страницу" (одна и та же переменная).
# Решение: theme.backgroundColor = белый (нужен таблицам), а видимый синий
# фон САМОЙ СТРАНИЦЫ красится поверх через CSS — только `background-color`,
# БЕЗ `height`/`min-height` на внутренних контейнерах Streamlit. Раньше
# фон красился через CSS с принудительным height:100% на #root/
# stAppViewContainer/stMain — это ломало внутреннюю flex/grid-раскладку
# непредсказуемо по вьюпорту (растягивало по горизонтали/сжимало по
# вертикали). Чистый `background-color` без `height` не трогает layout,
# только paint — безопасно.
PAGE_BACKGROUND_COLOR = "#406BED"  # на один оттенок светлее прежнего #2657EB
TEXT_ON_BACKGROUND = "#FFFFFF"
# Тот же светло-голубой, что secondaryBackgroundColor в .streamlit/config.toml —
# используется для выпадающих блоков (st.expander), чтобы они не сливались с
# синим фоном страницы, как поле выбора программы (st.selectbox уже берёт этот
# цвет из темы автоматически, st.expander — нет, красим вручную).
WIDGET_BACKGROUND_COLOR = "#C7E4FF"
# Фон для st.info-врезок (несколько оттенков светлее PAGE_BACKGROUND_COLOR) —
# без этого их скруглённая рамка почти не отличалась от фона страницы.
# Именно светлее, а не белее как WIDGET_BACKGROUND_COLOR: контраст белого
# текста с фоном страницы и так близок к порогу WCAG AA (4.5:1) — светлее
# уже начинает падать ниже нормы для обычного текста, поэтому лёгкий сдвиг
# компенсирован дополнительной светлой левой полосой-акцентом (см. CSS),
# а не только разницей в заливке.
INFO_BOX_BACKGROUND_COLOR = "#4A72EE"

# Приоритетные программы (звёздочка везде в списках) — очные программы
# Москвы по направлениям Медиакоммуникации/Журналистика/Реклама/Кино/Актёр.
# Решение пользователя (2026-07-08): онлайн-варианты ("Глобальные цифровые
# коммуникации (онлайн)") в список НЕ включены, хотя формально закодированы
# как очные — по названию это другой формат обучения. educationProgramId
# используется как ключ (не имя) — среди совпадающих по направлению программ
# есть настоящие «близнецы» (два разных id под именем «Реклама и связи с
# общественностью», см. docs/FINDINGS.md).
PRIORITY_PROGRAM_IDS = {
    "a5aa2dd6-cbcf-11ec-b808-005056989556": "Актер",
    "a5aa2da5-cbcf-11ec-b808-005056989556": "Журналистика",
    "a5aa2dd7-cbcf-11ec-b808-005056989556": "Управление в креативных индустриях",
    "a5aa2da6-cbcf-11ec-b808-005056989556": "Медиакоммуникации",
    "a5aa2dd8-cbcf-11ec-b808-005056989556": "Кинопроизводство",
    "9493b116-d0a3-11ee-b914-0050560b0254": "Реклама и связи с общественностью (Медиакоммуникации)",
    "e0f65398-11db-11ef-9d83-0050560b40ed": "Реклама и связи с общественностью (Реклама и СО)",
    "a5aa2da4-cbcf-11ec-b808-005056989556": "Стратегия и продюсирование в коммуникациях",
}

CHANNEL_LABELS = {
    "budget_family": "Бюджет (+квоты)",
    "commercial": "Платное",
    "target_quota": "Целевая квота",
}


# --------------------------------------------------------------------------
# Google Drive — тот же механизм, что и в магистратуре (summer_campaign_26/app.py)
# --------------------------------------------------------------------------

@st.cache_resource
def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gdrive_service_account"]),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@st.cache_data(ttl=600)
def list_drive_files() -> dict[str, str]:
    """{имя файла: id файла} для всех файлов в папке (folder_id из secrets)."""
    service = get_drive_service()
    folder_id = st.secrets["gdrive_folder_id"]
    files: dict[str, str] = {}
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            files[f["name"]] = f["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


@st.cache_data(ttl=600)
def download_drive_file(file_id: str) -> bytes:
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def read_drive_csv(name: str, **kwargs) -> pd.DataFrame:
    files = list_drive_files()
    return pd.read_csv(io.BytesIO(download_drive_file(files[name])), **kwargs)


def read_drive_parquet(name: str) -> pd.DataFrame:
    files = list_drive_files()
    return pd.read_parquet(io.BytesIO(download_drive_file(files[name])))


def check_password() -> bool:
    """Тот же общий пароль на весь дашборд, что и в магистратуре — репозиторий
    публичный (данных внутри нет), пароль не про защиту данных как таковых, а
    про то, чтобы дашборд не открывался случайным прохожим."""
    if st.session_state.get("authenticated"):
        return True

    def on_submit():
        if st.session_state.get("password_input") == st.secrets.get("app_password"):
            st.session_state["authenticated"] = True
        else:
            st.session_state["authenticated"] = False

    st.text_input("Пароль", type="password", key="password_input", on_change=on_submit)
    if st.session_state.get("authenticated") is False:
        st.error("Неверный пароль.")
    return False


# --------------------------------------------------------------------------
# Загрузка данных
# --------------------------------------------------------------------------

@st.cache_data(ttl=600)
def load_long_table() -> pd.DataFrame:
    return read_drive_parquet("bak_applications_long.parquet")


@st.cache_data(ttl=600)
def load_budget_family() -> pd.DataFrame:
    return read_drive_parquet("bak_budget_family_collapsed.parquet")


@st.cache_data(ttl=600)
def load_metrics():
    main = read_drive_csv(
        "campaign_metrics_main_bak.csv",
        dtype={
            "budget_places": "Int64", "special_right_places": "Int64",
            "separate_quota_places": "Int64", "target_quota_places": "Int64",
            "commercial_places": "Int64", "foreigner_places": "Int64",
        },
    )
    ranked = read_drive_csv("campaign_metrics_m4_desirability_ranked_bak.csv")
    files = list_drive_files()
    meta = json.loads(download_drive_file(files["campaign_metrics_meta_bak.json"]).decode("utf-8"))
    priority_dist = read_drive_csv("metric_priority_distribution_bak.csv")
    m9 = read_drive_csv("campaign_metrics_m9_diagnostic_bak.csv")
    intersections = {
        ch: read_drive_csv(f"metric_program_intersections_bak_{ch}.csv", index_col=0)
        for ch in CHANNEL_LABELS
    }
    return main, ranked, meta, priority_dist, m9, intersections


# --------------------------------------------------------------------------
# Хелперы
# --------------------------------------------------------------------------

def with_priority_first(ids: list[str], id_to_name: dict[str, str]) -> list[str]:
    """Приоритетные программы — в начале списка, остальные по алфавиту."""
    present_priority = [i for i in PRIORITY_PROGRAM_IDS if i in ids]
    rest = sorted((set(ids) - set(present_priority)), key=lambda i: id_to_name.get(i, ""))
    return present_priority + rest


def star_label(pid: str, id_to_name: dict[str, str]) -> str:
    name = id_to_name.get(pid, pid)
    return f"⭐ {name}" if pid in PRIORITY_PROGRAM_IDS else name


def channel_radio(label: str, key: str) -> str:
    return st.radio(label, list(CHANNEL_LABELS), format_func=lambda x: CHANNEL_LABELS[x],
                     horizontal=True, key=key)


def inject_theme():
    """Фон страницы красится тут ТОЛЬКО через background-color (без height/
    min-height — см. комментарий у PAGE_BACKGROUND_COLOR, это принципиально:
    чистый background-color не трогает layout/раскладку, только paint).
    Таблицы (canvas, вне досягаемости CSS) — сплошной белый и шапка, и тело
    (проверено пиксельно): secondaryBackgroundColor в этой версии Streamlit
    (1.50.0) НЕ используется рендерером таблицы (glide-data-grid) для шапки
    отдельно от тела — обе берут backgroundColor. Решение пользователя
    (2026-07-08): оставить как есть (белый/белый), не тратить время на
    обходные пути через pandas Styler (вероятно, тоже не сработает для шапки
    в этом рендерере)."""
    st.markdown(
        f"""
        <style>
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background-color: {PAGE_BACKGROUND_COLOR};
        }}
        .stApp, .stApp p, .stApp span, .stApp label, .stMarkdown, h1, h2, h3 {{
            color: {TEXT_ON_BACKGROUND};
        }}
        [data-testid="stHeader"] {{
            background: transparent;
        }}
        [data-testid="stMetricValue"], [data-testid="stMetricLabel"] {{
            color: {TEXT_ON_BACKGROUND};
            font-size: calc(1em + 2px);
        }}
        div[data-baseweb="tab-list"] {{
            gap: 8px;
        }}
        div[data-baseweb="tab-list"] button[data-baseweb="tab"] {{
            height: 56px;
            padding: 0 28px;
        }}
        div[data-baseweb="tab-list"] button[data-baseweb="tab"] p {{
            font-size: 22px;
            font-weight: 700;
        }}
        div[data-baseweb="tab-highlight"] {{
            height: 4px;
        }}
        [data-testid="stExpander"] summary {{
            background-color: {WIDGET_BACKGROUND_COLOR};
            border-radius: 8px;
            padding: 0.5rem 1rem;
        }}
        [data-testid="stExpander"] summary:hover {{
            background-color: {WIDGET_BACKGROUND_COLOR};
        }}
        [data-testid="stExpander"] summary p,
        [data-testid="stExpander"] summary span,
        [data-testid="stExpander"] summary [data-testid="stIconMaterial"] {{
            color: #000000 !important;
        }}
        /* st.info-врезки ("Зачем эта страница", "Как пользоваться", "Три вида
        конкурса" и служебные st.info по ходу дашборда) — без этого их
        скруглённая рамка почти не отличалась от фона страницы. */
        [data-testid="stAlertContainer"] {{
            background-color: {INFO_BOX_BACKGROUND_COLOR} !important;
            border-left: 4px solid rgba(255, 255, 255, 0.55);
            border-radius: 8px;
        }}
        [data-testid="stAlertContainer"] p,
        [data-testid="stAlertContainer"] li,
        [data-testid="stAlertContainer"] span,
        [data-testid="stAlertContainer"] strong {{
            color: #FFFFFF !important;
        }}
        /* st.caption по умолчанию рисуется приглушённым (тёмным текстом с
        уменьшенной непрозрачностью) — на синем фоне это читалось как блёклый
        серый текст. */
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] p {{
            color: #FFFFFF !important;
            opacity: 1 !important;
        }}
        /* Единая точка увеличения размера текста (~+2px): stMarkdownContainer —
        общий внутренний контейнер, через который рендерится текст st.markdown,
        st.title/header/subheader, st.caption и st.info одновременно — поэтому
        правило только здесь, а не отдельно на p/span/li, иначе размер удвоился
        бы у вложенных элементов (caption/alert внутри тоже используют этот же
        контейнер). */
        [data-testid="stMarkdownContainer"] {{
            font-size: calc(1em + 2px);
        }}
        [data-testid="stWidgetLabel"] p {{
            font-size: calc(1em + 2px);
        }}
        .block-container {{
            background: rgba(255, 255, 255, 0.04);
            border-radius: 12px;
            padding: 2rem 2rem 3rem 2rem;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Приложение
# --------------------------------------------------------------------------

st.set_page_config(page_title="Приёмная кампания ВШЭ — бакалавриат", layout="wide")
inject_theme()

if not check_password():
    st.stop()

df = load_long_table()
budget_family = load_budget_family()
main, ranked, meta, priority_dist, m9, intersections = load_metrics()

# display_name != educationProgram для программ-«близнецов» (одно имя+филиал,
# разные educationProgramId — см. docs/FINDINGS.md, 10 подтверждённых случаев,
# например два «Реклама и связи с общественностью» под разными направлениями).
# Используется везде в UI вместо голого имени — иначе близнецы неразличимы.
id_to_name = dict(zip(main["educationProgramId"], main["display_name"]))
# matrix_label — отдельная от display_name, ГЛОБАЛЬНО уникальная метка (не
# только в пределах филиала) — нужна конкретно для intersections (M8): та
# матрица не имеет отдельной колонки "Филиал", и одноимённые программы в
# разных филиалах (например «Медиакоммуникации» в Москве и в СПб — обычная
# ситуация, не близнецы) иначе дают дублирующийся индекс, matrix.loc[name]
# вернёт DataFrame вместо Series, и .sort_values() падает с TypeError
# (найдено на реальном использовании дашборда, 2026-07-09).
id_to_matrix_label = dict(zip(main["educationProgramId"], main["matrix_label"]))
snapshot_date_short = meta["snapshot_date"].replace("-", ".")

st.title("Приёмная кампания ВШЭ — бакалавриат")
st.caption("Проект подготовлен Переясловым А.Д., ст. преподавателем, менеджером Института медиа")
st.caption(
    f'<span style="font-size:20px;">Данные актуальны на момент {snapshot_date_short} · '
    f'Программ: {meta["n_programs"]} · Порог «серьёзного» приоритета: ≤ {meta["K_SERIOUS"]}</span>',
    unsafe_allow_html=True,
)

st.info(
    "**Зачем эта страница.** Обычная сводка приёмной кампании отвечает на вопрос "
    "«сколько подано заявлений». Здесь — на более важный: **насколько абитуриенты "
    "действительно хотят каждую программу** (как часто ставят её первым приоритетом) "
    "и **какие программы соперничают за одних и тех же абитуриентов** (сколько у них "
    "общих заявителей). Так виден настоящий интерес к программе, а не только число "
    "поданных заявлений — оно бывает большим просто потому, что программу указывают "
    "запасным вариантом.",
    icon="📊",
)
st.info(
    "**Как пользоваться.** «Базовая статистика» ниже — это просто число заявлений по "
    "выбранным программам. Дальше три вкладки: «По программе» (подробные показатели и "
    "рейтинг желанности), «Сравнить программы» (две программы бок о бок) и "
    "«По абитуриенту» (все заявки одного человека). Почти у каждого показателя есть "
    "кнопка «Подробнее» — там формула и пояснение простым языком.",
    icon="🧭",
)

st.info(
    "**Три вида конкурса, за которые борются программы.**\n\n"
    "- **Бюджет (+квоты)** — бесплатные места. Объединяет сразу три категории приёма "
    "(общий конкурс, отдельная квота, особое право) в один список: один человек "
    "считается здесь только раз, даже если проходит сразу по нескольким категориям.\n"
    "- **Платное** — места с оплатой обучения.\n"
    "- **Целевая квота** — тоже бесплатные места, но отдельный конкурс: своя аудитория "
    "и своя, независимая нумерация приоритетов. Поэтому она считается сама по себе — "
    "не входит в рейтинг желанности и не прибавляется к обычному бюджету.",
    icon="ℹ️",
)

all_ids = sorted(main["educationProgramId"].unique(), key=lambda i: id_to_name.get(i, ""))

# ------------------------------------------------------------- Базовая статистика
st.header("Базовая статистика")
st.caption("Заявления по программам — как есть, без дополнительных расчётов.")

default_basic = [i for i in PRIORITY_PROGRAM_IDS if i in all_ids]
selected_basic = st.multiselect(
    "Программы", options=with_priority_first(all_ids, id_to_name), default=default_basic,
    format_func=lambda i: star_label(i, id_to_name), placeholder="Выберите программы",
)

basic_table = main[main["educationProgramId"].isin(selected_basic)][
    ["display_name", "filial", "n_applications_budget_family", "n_applications_commercial",
     "n_applications_target_quota"]
].rename(columns={
    "display_name": "Программа", "filial": "Филиал",
    "n_applications_budget_family": "Заявок на бюджет (+квоты)",
    "n_applications_commercial": "Заявок на платное",
    "n_applications_target_quota": "Заявок на целевую квоту",
})
st.dataframe(basic_table, width="stretch", hide_index=True)

st.divider()

tab_program, tab_compare, tab_applicant = st.tabs(["По программе", "Сравнить программы", "По абитуриенту"])

# ---------------------------------------------------------------- По программе
with tab_program:
    filials = sorted(main["filial"].unique())
    selected_filials = st.multiselect("Филиал", filials, default=filials, placeholder="Выберите филиал")
    filtered_main = main[main["filial"].isin(selected_filials)]

    serious_label_budget = f"Приоритет 1–{meta['K_SERIOUS']} (бюджет)"
    serious_label_commercial = f"Приоритет 1–{meta['K_SERIOUS']} (платное)"

    st.subheader("Подробные метрики по программе")

    detail_ids = sorted(filtered_main["educationProgramId"].unique(), key=lambda i: id_to_name.get(i, ""))
    default_detail = [i for i in PRIORITY_PROGRAM_IDS if i in detail_ids]
    selected_detail = st.multiselect(
        "Программы", options=with_priority_first(detail_ids, id_to_name), default=default_detail,
        format_func=lambda i: star_label(i, id_to_name), key="detail_programs", placeholder="Выберите программы",
    )

    with st.expander("Подробнее о метриках"):
        st.markdown(
            f"- **Уникальных абитуриентов** — сколько разных людей подали хотя бы одну "
            f"заявку на программу (бюджет+квоты, платное и целевая квота вместе, без "
            f"задвоения тех, кто подался несколькими способами).\n"
            f"- **«{serious_label_budget}» / «{serious_label_commercial}»** — сколько "
            f"заявителей поставили программу в число первых {meta['K_SERIOUS']} приоритетов "
            f"(порог «серьёзности», для бакалавриата выше, чем в магистратуре — распределение "
            f"приоритетов растянутее).\n"
            f"- **Конкурс на место (бюджет)** — заявок на одно бюджетное место "
            f"(Бюджетные места + Отдельная квота + Особое право вместе, без задвоения).\n"
            f"- **Приоритетных заявителей на место** — заявителей с приоритетом "
            f"1–{meta['K_SERIOUS']} на одно бюджетное место; значение <1 означает, что "
            f"серьёзных заявителей меньше, чем мест.\n"
            f"- **Доля целевой квоты от бюджета** — сколько бюджетных мест программы "
            f"отдано под целевой приём (не общий конкурс). Не входит в желанность (M4), "
            f"но искажает интуицию о «сколько на самом деле шансов» — держать перед глазами."
        )

    detailed_table = filtered_main[filtered_main["educationProgramId"].isin(selected_detail)][
        [
            "display_name", "filial", "n_unique_applicants_total",
            "n_serious_budget_family", "n_serious_commercial",
            "competition_ratio_budget_family", "demand_pressure_budget_family",
            "target_quota_share_of_budget",
        ]
    ].copy()
    for c in ("competition_ratio_budget_family", "demand_pressure_budget_family", "target_quota_share_of_budget"):
        detailed_table[c] = detailed_table[c].round(2)
    detailed_table = detailed_table.rename(columns={
        "display_name": "Программа", "filial": "Филиал",
        "n_unique_applicants_total": "Уникальных абитуриентов",
        "n_serious_budget_family": serious_label_budget,
        "n_serious_commercial": serious_label_commercial,
        "competition_ratio_budget_family": "Конкурс на место (бюджет)",
        "demand_pressure_budget_family": "Приоритетных заявителей на место",
        "target_quota_share_of_budget": "Доля целевой квоты от бюджета",
    })
    st.dataframe(
        detailed_table.sort_values("Уникальных абитуриентов", ascending=False),
        width="stretch", hide_index=True, height=400,
    )

    st.subheader("Рейтинг желанности программ")
    st.caption(
        "Насколько часто абитуриенты ставят программу первым приоритетом — бюджет+квоты "
        "и платное берутся ВМЕСТЕ (не только бюджет), взвешенно по тому, насколько "
        "уверенно посчитан каждый из них. Целевая квота не участвует (её приоритет "
        "несопоставим с остальной очередью, см. врезку выше)."
    )
    st.caption(
        "ℹ️ **Как читать верхние строчки рейтинга.** Небольшие чисто платные "
        "программы нередко оказываются наверху. У них нет бесплатной "
        "альтернативы, которая привлекала бы абитуриентов «на всякий случай» — "
        "туда подаются в основном те, кто уже определился, поэтому доля "
        "«поставили первым приоритетом» там естественно выше. Это стоит "
        "воспринимать как показатель целевой определённости абитуриентов, а "
        "не напрямую как «эта программа желаннее для всех», как у массовых "
        "конкурентных программ."
    )
    with st.expander("Подробнее о метрике"):
        st.markdown(
            "Простая доля «поставили первым приоритетом» ненадёжна для программ с малым "
            "числом заявок. Поэтому оценка каждой программы подтягивается к среднему по "
            "рынку значению тем сильнее, чем меньше у неё данных (метод — эмпирический Байес), "
            "отдельно для бюджетной очереди и для платной."
        )
        st.latex(r"\text{Желанность} = \dfrac{\text{cnt}_{p1} + k \cdot p_0}{n + k}")
        st.markdown(
            f"(считается для каждого вида конкурса отдельно) где `cnt_p1` — число "
            f"заявителей с приоритетом №1, `n` — всего заявителей этого вида конкурса "
            f"у программы. Бюджет: `p0` ≈ {meta['M4_p0_budget']:.3f}, "
            f"`k` ≈ {meta['M4_k_budget']:.2f}. Платное: `p0` ≈ {meta['M4_p0_commercial']:.3f}, "
            f"`k` ≈ {meta['M4_k_commercial']:.2f}."
        )
        st.markdown(
            "Бюджет и платное объединяются во взвешенное среднее — вес каждого обратно "
            "пропорционален его апостериорной неопределённости (чем больше данных, тем "
            "увереннее оценка и больше вес), это стандартный статистический приём "
            "(взвешивание по обратной дисперсии), не произвольно подобранная пропорция. "
            f"Это оправдано на данных: желанность по бюджету и по платному, посчитанные "
            f"полностью независимо, коррелируют на **{meta['M4_corr_budget_commercial']:.2f}** "
            f"по всем программам с данными в обоих видах конкурса — то есть это две оценки одной "
            f"и той же величины, а не два разных явления."
        )
    filtered_ranked = ranked[ranked["filial"].isin(selected_filials)]
    ranked_display = filtered_ranked[
        ["desirability_rank", "display_name", "filial", "n_p1_budget_family",
         "n_applications_budget_family", "desirability_budget", "desirability_commercial",
         "desirability_combined", "target_quota_share_of_budget"]
    ].copy()
    for c in ("desirability_budget", "desirability_commercial", "desirability_combined"):
        ranked_display[c] = ranked_display[c].round(3)
    ranked_display["target_quota_share_of_budget"] = ranked_display["target_quota_share_of_budget"].round(2)
    ranked_display = ranked_display.rename(columns={
        "desirability_rank": "Ранг", "display_name": "Программа", "filial": "Филиал",
        "n_p1_budget_family": "Заявок с приоритетом №1 (бюджет)",
        "n_applications_budget_family": "Всего заявок (бюджет+квоты)",
        "desirability_budget": "Желанность (бюджет)",
        "desirability_commercial": "Желанность (платное)",
        "desirability_combined": "Желанность (общая)",
        "target_quota_share_of_budget": "Доля целевой квоты от бюджета",
    })
    st.dataframe(
        ranked_display,
        width="stretch", hide_index=True, height=400,
        column_config={
            "Желанность (бюджет)": st.column_config.NumberColumn(format="%.3f"),
            "Желанность (платное)": st.column_config.NumberColumn(format="%.3f"),
            "Желанность (общая)": st.column_config.NumberColumn(format="%.3f"),
        },
    )

    st.divider()
    program_ids_here = sorted(filtered_main["educationProgramId"].unique(), key=lambda i: id_to_name.get(i, ""))
    selected_id = st.selectbox(
        "Программа — распределение приоритетов и пересечения",
        with_priority_first(program_ids_here, id_to_name),
        format_func=lambda i: star_label(i, id_to_name),
    )

    st.markdown("**Распределение приоритетов**")
    dist = priority_dist[priority_dist["educationProgramId"] == selected_id].set_index("priority")
    if len(dist):
        dist_display = dist[["n_budget_family", "n_commercial", "n_target_quota"]].rename(columns={
            f"n_{ch}": label for ch, label in CHANNEL_LABELS.items()
        })
        st.bar_chart(dist_display)
    else:
        st.info("Нет заявок с указанным приоритетом по этой программе.")

    st.markdown("**Топ-10 пересечений (общие абитуриенты)**")
    channel_choice = channel_radio("Вид конкурса", key="inter_channel")
    matrix = intersections[channel_choice]
    selected_name = id_to_matrix_label.get(selected_id, selected_id)
    if selected_name in matrix.index:
        row = matrix.loc[selected_name]
        if isinstance(row, pd.DataFrame):
            # Подстраховка: даже глобально уникальный matrix_label не спасает,
            # если построение матрицы (program_intersections) когда-то
            # регрессирует — лучше явный, понятный лог, чем TypeError у
            # sort_values() без объяснения причины.
            row = row.iloc[0]
        row = row.drop(selected_name, errors="ignore")
        row = row[row > 0].sort_values(ascending=False).head(10)
        if len(row):
            chart_data = row.rename("count").rename_axis("program").reset_index()
            chart = (
                alt.Chart(chart_data)
                .mark_bar()
                .encode(
                    x=alt.X("count:Q", title="Общих абитуриентов"),
                    y=alt.Y("program:N", sort="-x", title=None, axis=alt.Axis(labelLimit=400)),
                )
                .properties(height=32 * len(chart_data) + 20)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("Нет пересечений с другими программами по этому виду конкурса.")
    else:
        st.info(f"У программы нет заявок по виду конкурса «{CHANNEL_LABELS[channel_choice]}» — пересечения не определены.")

    st.markdown("**Абитуриенты этой программы**")
    bf_sub = budget_family[budget_family["educationProgramId"] == selected_id][
        ["idEpgu", "priority", "participantStatus", "is_active", "score"]
    ].rename(columns={"priority": "Приоритет (бюджет)", "participantStatus": "Статус (бюджет)",
                       "score": "score_budget"})
    comm_sub = df[(df["educationProgramId"] == selected_id) & (df["placeTypeName"] == "С оплатой обучения")][
        ["idEpgu", "priority", "participantStatus", "score"]
    ].rename(columns={"priority": "Приоритет (платное)", "participantStatus": "Статус (платное)",
                       "score": "score_commercial"})
    tq_sub = df[(df["educationProgramId"] == selected_id) & (df["placeTypeName"] == "Целевая квота")][
        ["idEpgu", "priority", "participantStatus", "score"]
    ].rename(columns={"priority": "Приоритет (целевая квота)", "participantStatus": "Статус (целевая квота)",
                       "score": "Баллы (целевая квота)"})

    applicants_table = pd.merge(
        bf_sub.drop(columns=["is_active"]), comm_sub, on="idEpgu", how="outer"
    ).merge(tq_sub, on="idEpgu", how="outer")

    if len(applicants_table):
        # score_budget/score_commercial — один и тот же балл абитуриента (не зависит от
        # канала подачи), дублируются только из-за раздельного сбора очередей;
        # схлопываем в одну колонку вместо повторного показа.
        applicants_table["Баллы"] = applicants_table["score_budget"].combine_first(
            applicants_table["score_commercial"]
        )
        applicants_table = applicants_table.drop(columns=["score_budget", "score_commercial"])
        applicants_table = applicants_table.rename(columns={"idEpgu": "ID (Госуслуги)"})
        cols = applicants_table.columns.tolist()
        cols.remove("Баллы")
        cols.insert(cols.index("ID (Госуслуги)") + 1, "Баллы")
        applicants_table = applicants_table[cols]
        for col in ("Приоритет (бюджет)", "Приоритет (платное)", "Приоритет (целевая квота)"):
            applicants_table[col] = applicants_table[col].astype("Int64")
        applicants_table = applicants_table.sort_values(
            ["Приоритет (бюджет)", "Приоритет (платное)"], na_position="last"
        )
        st.dataframe(applicants_table, width="stretch", hide_index=True, height=400)
        st.caption(
            f"Всего уникальных абитуриентов по этой программе: "
            f"{applicants_table['ID (Госуслуги)'].nunique()}. Пусто — не подавался по этому виду конкурса. "
            f"«Приоритет (бюджет)» — уже сведён к одному значению на человека (общий конкурс, "
            f"отдельная квота и особое право считаются одним списком, без повторного счёта)."
        )
    else:
        st.info("Нет заявок по этой программе.")

# -------------------------------------------------------------- Сравнить программы
with tab_compare:
    st.caption(
        "Сравните две программы рядом — как карточки товаров на маркетплейсе: "
        "места, конкурс, желанность, профиль приоритетов."
    )

    options_compare = with_priority_first(all_ids, id_to_name)
    col_a, col_b = st.columns(2)
    with col_a:
        program_a = st.selectbox(
            "Программа 1", options_compare, index=None,
            format_func=lambda i: star_label(i, id_to_name),
            placeholder="Выберите программу", key="compare_program_a",
        )
    with col_b:
        program_b = st.selectbox(
            "Программа 2", options_compare, index=None,
            format_func=lambda i: star_label(i, id_to_name),
            placeholder="Выберите программу", key="compare_program_b",
        )

    if program_a is None or program_b is None:
        st.info("Выберите обе программы для сравнения.")
    elif program_a == program_b:
        st.warning("Выберите две разные программы.")
    else:
        selected_compare = [program_a, program_b]

        cmp = main[main["educationProgramId"].isin(selected_compare)].merge(
            ranked[["educationProgramId", "desirability_rank"]],
            on="educationProgramId", how="left",
        ).set_index("educationProgramId").loc[selected_compare]
        cmp.index = [id_to_name.get(i, i) for i in cmp.index]

        students_a = set(df.loc[df["educationProgramId"] == program_a, "idEpgu"])
        students_b = set(df.loc[df["educationProgramId"] == program_b, "idEpgu"])
        n_common = len(students_a & students_b)

        labels = [id_to_name.get(program_a, program_a), id_to_name.get(program_b, program_b)]
        rows = {
            "Филиал": cmp["filial"],
            "Бюджетных мест": cmp["budget_places"],
            "  из них целевая квота": cmp["target_quota_places"],
            "Платных мест": cmp["commercial_places"],
            "Заявок на бюджет (+квоты)": cmp["n_applications_budget_family"],
            "Заявок на платное": cmp["n_applications_commercial"],
            "Заявок на целевую квоту": cmp["n_applications_target_quota"],
            "Уникальных абитуриентов": cmp["n_unique_applicants_total"],
            "Общие уникальные абитуриенты": pd.Series(n_common, index=cmp.index),
            serious_label_budget: cmp["n_serious_budget_family"],
            serious_label_commercial: cmp["n_serious_commercial"],
            "Конкурс на место (бюджет)": cmp["competition_ratio_budget_family"].round(2),
            "Приоритетных заявителей на место": cmp["demand_pressure_budget_family"].round(2),
            "Желанность (бюджет)": cmp["desirability_budget"].round(3),
            "Желанность (платное)": cmp["desirability_commercial"].round(3),
            "Желанность (общая)": cmp["desirability_combined"].round(3),
            "Ранг желанности": cmp["desirability_rank"],
        }
        compare_table = pd.DataFrame(rows).T
        compare_table.columns = labels
        compare_table = compare_table.fillna("—")
        st.dataframe(compare_table, width="stretch")
        st.caption(
            "«—» значит «не определено» (нет бюджетных мест у программы, поэтому нет "
            "бюджетных метрик) — не путать с нулевым интересом. «Общие уникальные "
            "абитуриенты» — одно число на пару программ по всем трём видам конкурса вместе."
        )

        st.divider()
        st.markdown("**Профиль приоритетов**")
        profile_channel = channel_radio("Вид конкурса", key="compare_profile_channel")
        value_col = {"budget_family": "n_budget_family", "commercial": "n_commercial",
                     "target_quota": "n_target_quota"}[profile_channel]
        profile_data = priority_dist[priority_dist["educationProgramId"].isin(selected_compare)][
            ["educationProgramId", "priority", value_col]
        ].copy()
        profile_data["Программа"] = profile_data["educationProgramId"].map(id_to_name)
        if len(profile_data):
            profile_chart = (
                alt.Chart(profile_data)
                .mark_bar()
                .encode(
                    x=alt.X("priority:O", title="Приоритет"),
                    y=alt.Y(f"{value_col}:Q", title="Заявителей"),
                )
                .properties(width=340, height=400)
                .facet(column=alt.Column("Программа:N", title=None, sort=labels))
            )
            st.altair_chart(profile_chart, use_container_width=True)
        else:
            st.info(f"Нет заявок по виду конкурса «{CHANNEL_LABELS[profile_channel]}» ни у одной из выбранных программ.")

# --------------------------------------------------------------- По абитуриенту
with tab_applicant:
    st.caption("Найдите абитуриента по ID (числовой код, соответствует Госуслугам), чтобы увидеть все его заявки.")
    query = st.text_input("ID абитуриента (idEpgu)")

    if query:
        try:
            query_value = str(int(query))
        except ValueError:
            st.error("Нужно ввести число.")
        else:
            student_rows = df[df["idEpgu"] == query_value]
            if student_rows.empty:
                st.warning("Абитуриент с таким ID не найден в текущем срезе.")
            else:
                student_rows = student_rows.copy()
                student_rows["display_name"] = student_rows["educationProgramId"].map(id_to_name)
                display_cols = ["display_name", "filial", "placeTypeName", "priority",
                                 "participantStatus", "score"]
                student_display = student_rows[display_cols].sort_values(
                    ["placeTypeName", "priority"]
                ).rename(columns={
                    "display_name": "Программа", "filial": "Филиал",
                    "placeTypeName": "Вид конкурса", "priority": "Приоритет",
                    "participantStatus": "Статус", "score": "Баллы",
                })
                st.dataframe(student_display, width="stretch", hide_index=True)
                st.caption(f"Всего заявок у абитуриента: {len(student_rows)} "
                           f"на {student_rows['educationProgramId'].nunique()} программ(-ы).")
