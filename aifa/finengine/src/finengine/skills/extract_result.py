"""Пост-обработка состоявшегося прогона извлечения: периоды, таблица, паспорт
правил, Excel, роль-раскладка, заземление, событие фронту, журнал прогонов.

ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ (#190, коммит К1). Всё перечисленное жило внутри
хендлера тула `llm/tools.py::_h_fe_extract_metrics`. Тул уходит: комнату станции
поднимает гейтвей, результат прогона приходит бэкенду нонс-каналом, и разбирать
его будет слушатель (`skills/station_run_sink.py`, коммит К8) — у него нет ни
`ToolContext`, ни очереди прогресса тула. Пост-обработка вынута РАНЬШЕ сноса по
закону порядка: решение переселяется раньше, чем умирает его решатель.

ГРАНИЦА МОДУЛЯ. Здесь РАБОТА и её ПОРЯДОК, но не разговор с внешним миром:
отметок шага плана, эмита событий в канал и сборки ответа модели тут нет и не
будет — это разное у тула и у слушателя, и обобщение свелось бы к флагам.

`ToolContext` здесь не упоминается сознательно. Проверка владельца ресурса —
забота вызывающего, а не этого модуля.

ГЛАВНАЯ ТОЧКА ВХОДА — `finalize_extraction`: весь рецепт одним вызовом. Отдельные
шаги оставлены публичными, потому что их зовут тесты и станционный путь, но
СОБИРАТЬ ИЗ НИХ СВОЮ ПОСЛЕДОВАТЕЛЬНОСТЬ НЕ НАДО — ради этого рецепт и вынесен.

    pick_report_years → dominant_unit → render_flat_table
    → registry_passport → passport_rows
    → build_metrics_workbook → metrics_xlsx_filename → save_metrics_xlsx
    → build_role_layout → build_extract_done → log_extract_run

Порядок не косметика. Раскладка роли обязана быть собрана ДО события
`extract_done` — иначе она уедет во фронт пустой.

⚠️ Срез 13.5.4: `publish_grounding` (заземление ждущего анализа через
`grounding_bus`, Ш12) удалён вместе с публикующей половиной шины. Canonical
`analyze.py` не ждёт чужого прогона — он строит собственную `EvidenceBundleV2`
внутри своего `PlanGraph`, и второй прогон движка, ради экономии которого шина
была заведена, здесь не запускается вовсе.

ПОЛИТИКА ОТКАЗА ЖИВЁТ В РЕЦЕПТЕ (изменено в К8a). В К1 пять `try/except` были
оставлены у вызывающего: потребитель был один, а политика второго неизвестна.
Теперь она известна и совпадает — оба обязаны доставить таблицу, даже если не
собрался паспорт, книга или раскладка. Держать одну политику в двух местах
значит чинить её дважды.

ПОЧЕМУ ШАГОВ БОЛЬШЕ, ЧЕМ КАЖЕТСЯ НУЖНЫМ. Паспорт разбит на два вызова, Excel —
на три. Это не дробление ради красоты: у каждой пары РАЗНЫЕ частичные исходы.
Отпечаток правил обязан уцелеть, даже если не отрисовались строки для листа;
имя книги обязано быть известно, даже если её не приняло хранилище. Слепив их
в одну функцию, теряешь половину состояния при отказе второй половины — ровно
эту ошибку первая редакция модуля и содержала.

⚠️ РЯДОМ ЖИВЁТ ПОЧТИ ТО ЖЕ САМОЕ — `skills/extract_present.py`, его зовёт
станционный скрипт `station-skills/aifa-extract/scripts/extract.py`. Дословно
совпадает только выбор годов (`extract_present.pick_periods`). Остальные четыре
расходятся ПОВЕДЕНИЕМ, а не стилем, и подставить их вместо здешних — молча
изменить то, что видит пользователь:

  * `format_number` — «1234.5» печатает как есть, здешняя `format_amount` даёт
    «1 234.50»; дробные округляет до двух знаков только здешняя;
  * `value_for_period` — при пустом периоде отдаёт `value_current`, здешняя
    `value_for_year` отдаёт None;
  * `render_table_markdown` — четыре колонки (с единицей), выбрасывает строки,
    где оба значения пусты, и берёт имя показателя из реестра;
  * `write_role_xlsx` — собирает ДРУГУЮ книгу: без паспорта, без листа сегментов
    и без данных графиков.

Слияние двух модулей — задача #223 (грумминг), и делать его надо со сравнением
выхода, а не по похожести имён.
"""
from __future__ import annotations

import logging
import re
from io import BytesIO
from collections import Counter
from dataclasses import dataclass
from typing import Any

from finengine.config import settings
from finengine.roles import formatter
from finengine.skills import metrics_xlsx, run_logger, sources, versioning

log = logging.getLogger(__name__)

# Импорты подняты на уровень модуля намеренно (правило 3.1: lazy только для
# тяжёлых, опциональных и разрывающих цикл). Замерено 13.08.2026: циклов нет ни
# в одном порядке импорта, ни один из семи не тянет ни PyMuPDF, ни openpyxl, ни
# FastAPI на этапе импорта. Форма `import модуль` + `модуль.функция`
# ОБЯЗАТЕЛЬНА: тесты подменяют `sources.save_upload` и
# `versioning.registry_fingerprint` как атрибуты модуля, и `from … import имя`
# выключил бы подмену молча.

_XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def pick_report_years(metrics: list[dict]) -> tuple[str, str, list[str]]:
    """Основной год прогона, предыдущий и все годы по убыванию частоты.

    Основной — НЕ тот, что назвал движок: его `period_current` бывает
    проекцией. У спецотчётов с ковенантными выкладками 2025-2027 он приезжал
    как «FY 2027», и таблица в чате оказывалась про будущее. Поэтому основной
    год — тот, за который в прогоне больше всего значений; предыдущий —
    сравнительный.

    Третьим значением возвращается ТОТ ЖЕ список, из которого выбраны первые
    два: он уходит в ответ как `all_periods_in_report`. Считать его отдельно
    значило бы завести второй счётчик, способный разойтись с первым.
    """
    year_counts: Counter = Counter()
    for m in metrics:
        for pv in (m.get("period_values") or []):
            year = re.search(r"(20\d{2})", str(pv.get("period") or ""))
            if year:
                year_counts[year.group(1)] += 1
    years = [y for y, _ in year_counts.most_common()]
    main_year = years[0] if years else ""
    prev_year = ""
    if main_year.isdigit():
        cand = str(int(main_year) - 1)
        prev_year = cand if cand in year_counts else (years[1] if len(years) > 1 else "")
    return main_year, prev_year, years


def fill_row_comparative(row: dict) -> bool:
    """Заполнить `value_prev` строки с ЕЁ СОБСТВЕННОЙ оси периода.

    ДЕФЕКТ, КОТОРЫЙ ЭТО ЗАКРЫВАЕТ (найден на прогоне владельца по
    Россельхозбанку 19.08.2026). У промежуточного отчёта ДВЕ оси сравнения:

        баланс   31.03.2026  против  31.12.2025   ← конец прошлого ГОДА
        ОПУ      Q1 2026     против  Q1 2025      ← тот же квартал год назад

    Движок спрашивает `value_prev` ОДНИМ периодом на весь прогон (здесь
    «Q1 2025»), и балансовая строка честно отвечает `None`: значения за первый
    квартал прошлого года у неё нет и быть не может. В прогоне РСХБ так вышло
    у 92 строк из 110 — вторая колонка пустая, дельта не считается, хотя число
    лежит в самой строке:

        CASH_EQUIVALENTS  value_prev=None
            period_values: 31.03.2026 → 605 868
                           31.12.2025 → 908 529   ← вот оно, и оно верное

    ПРАВИЛО: сравнительное значение — ближайший ПРЕДШЕСТВУЮЩИЙ период этой же
    строки. Не «второй по списку» и не «тот, что назвали прогону»: у строки
    может быть три периода, и брать надо соседний по времени.

    Ось приводится к дате окончания тем же ключом, которым движок сводит
    duration к point (`_period_end_key`), — второй способ сравнить периоды
    однажды разошёлся бы с первым.

    Возвращает `True`, если заполнил. Строку правит НА МЕСТЕ: её же объект
    лежит в списке прогона, и через него правка доезжает и до экрана, и до
    книги Excel, и до раскладки роли.
    """
    if row.get("value_prev") is not None:
        return False
    current = row.get("value_current")
    if current is None:
        return False
    from finengine.skills.bulk_extractor.period_selection import _period_end_key

    dated = []
    for pv in row.get("period_values") or []:
        if pv.get("value") is None:
            continue
        key = _period_end_key(pv.get("period"))
        if key is not None:
            dated.append((key, pv))
    if len(dated) < 2:
        return False
    dated.sort(key=lambda item: item[0], reverse=True)
    # Текущая — та, чьё значение уже стоит в `value_current`. Не нашли (движок
    # взял значение обходным путём) — считаем текущей самую свежую.
    at = next((i for i, (_k, pv) in enumerate(dated) if pv.get("value") == current), 0)
    if at + 1 >= len(dated):
        return False
    _key, prev = dated[at + 1]
    row["value_prev"] = prev.get("value")
    # ⚠️ ПОДПИСЬ ОБЯЗАТЕЛЬНА. У разных строк сравнительный период РАЗНЫЙ, и
    # одна подпись колонки на всю таблицу соврала бы про две трети строк.
    # Пусть экран покажет, с чем именно сравнивается эта строка.
    row["period_prev_label"] = str(prev.get("period") or "")
    return True


def dominant_unit(metrics: list[dict], *, default: str = "млн руб.") -> str:
    """Единица измерения прогона — самая частая среди переданных показателей.

    ПОДАВАТЬ СЮДА ТОЛЬКО ПЛОСКИЕ СТРОКИ. У сегментной и групповой разбивки
    бывают свои единицы (штуки, проценты), и если досыпать их в счёт, сменится
    подпись ВСЕЙ таблицы — молча и на всех показателях сразу. Функция этого не
    проверяет: отличить сегментную строку она может, но тогда у вызывающего
    появился бы соблазн передавать всё подряд.
    """
    return (Counter(m.get("unit") for m in metrics if m.get("unit")).most_common(1)
            or [(default, 0)])[0][0]


def value_for_year(metric: dict, year: str) -> Any:
    """Значение показателя за год — поиск ПОДСТРОКОЙ по ярлыку периода.

    Движок отдаёт периоды в разных формах («FY 2024», «31.12.2024», «12M 2024»),
    и колонка обязана собираться по всем ним одинаково.

    Пустой `year` даёт None, а не «текущее» значение: пустым он бывает ровно
    тогда, когда годов в прогоне не нашлось вовсе, и число под пустым
    заголовком колонки было бы враньём.
    """
    if not year:
        return None
    for pv in (metric.get("period_values") or []):
        if year in str(pv.get("period") or ""):
            return pv.get("value")
    return None


def format_amount(v: Any) -> str:
    """Число в вид русского отчёта: разряды пробелом, прочерк вместо пустоты.

    `except` здесь часть алгоритма, а не обработка сбоя: значение показателя
    бывает текстовым (заключение аудитора, «н/д»), и такое печатается как есть.
    """
    if v is None:
        return "—"
    try:
        f = float(v)
        return (f"{f:,.0f}" if f == int(f) else f"{f:,.2f}").replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def render_flat_table(
    metrics: list[dict],
    *,
    main_year: str,
    prev_year: str,
    show_enums: set[str] | None = None,
) -> str:
    """Плоская markdown-таблица «Показатель | основной год | предыдущий».

    #141: состав таблицы в чате задаёт РАСКЛАДКА РОЛИ, а не то, сколько
    показателей движок читал из отчёта. При `basis_full` базис шире роли
    специально — им заземляется анализ, — и без фильтра пользователь роли ДИЭ
    получил бы в чат всю ячейку вместо своих строк.

    ПУСТОЕ МНОЖЕСТВО = фильтра нет, ровно как None. Это не небрежность: у ролей
    «Общая»/«Все» `output_enums` пуст по устройству, и «отфильтровать по
    пустому» дало бы пустую таблицу. Пустая `table_markdown` выключает
    verbatim-мост целиком (`orchestrator/agent.py::_verbatim_render`) — у
    пользователя пропадает и таблица, и кнопка Excel. Это сценарий #143.
    """
    show = show_enums or None
    return (
        f"| Показатель | {main_year or 'тек.'} | {prev_year or 'пред.'} |\n|---|---:|---:|\n"
        + "\n".join(
            f"| {m.get('name_ru') or m.get('enum')} "
            f"| {format_amount(value_for_year(m, main_year))} "
            f"| {format_amount(value_for_year(m, prev_year))} |"
            for m in metrics
            if not show or m.get("enum") in show
        )
    )


def registry_passport(profile_id: str) -> dict:
    """Отпечаток ПРАВИЛ прогона (#155) в форме паспорта: `{"registry": …}`.

    Правила извлечения — это реестр ячейки ВМЕСТЕ с синонимами общего реестра,
    с которыми он сливается по дороге в промпт (#147): формулы, подсказки,
    описания, синонимы. За одни сутки они менялись трижды (развёрнут
    Cost/Income, у стоимости риска снят ABS — сменился ЗНАК, добавлены три
    показателя), и вчерашние выгрузки перестали сходиться с сегодняшними.
    Версия КОДА на это не реагирует: правка json-реестра деплой не двигает.

    Отказ отсюда НЕ гасится — гасит его вызывающий: уронить готовое извлечение
    из-за паспорта нельзя, но и тихо подменить источник правил тоже.
    """
    return {"registry": versioning.registry_fingerprint(profile_id)}


def passport_rows(versions: dict) -> list[tuple[str, str]]:
    """Тот же паспорт строками «подпись → значение» для листа Excel.

    ОТДЕЛЬНЫМ ВЫЗОВОМ, а не внутри `registry_passport`, — и это не дробление
    ради дробления. Отпечаток и его рендер нужны РАЗНЫМ потребителям: отпечаток
    едет в журнал прогонов (по нему считается бенчмарк), строки — в книгу
    пользователя. Слитые в одну функцию, при отказе рендера они теряются ВМЕСТЕ,
    и журнал остаётся без отпечатка на ровном месте. Так и было в первой
    редакции этого модуля — поймано состязательной проверкой 13.08.
    """
    return versioning.passport_rows(versions)


def build_metrics_workbook(
    metrics: list[dict],
    segment_rows: list[dict],
    group_rows: list[dict],
    *,
    passport: list[tuple[str, str]],
    company_type: str = "",
) -> bytes:
    """Книга со ВСЕМИ периодами прогона, включая проекции.

    Отдельно от сохранения по той же причине, что и паспорт: сборка книги и её
    отправка в хранилище отказывают по разным поводам, и вызывающий должен
    различать, на каком из двух шагов он остановился.
    """
    return metrics_xlsx.build_metrics_xlsx(
        metrics, segment_rows, group_rows, passport=passport,
        company_type=company_type)


def build_role_workbook(formatted: dict | None, *,
                        passport: list[tuple[str, str]] | None = None) -> bytes | None:
    """Книга В ФОРМАТЕ РОЛИ — та же таблица, что человек видит на экране.

    ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «выгрузка должна выходить в формате той же роли».
    Экран показывал раскладку ДИЭ/CIB/Риски, а в «Источники» ложилась плоская
    книга всех показателей: две разные таблицы под одним прогоном, и какая из
    них «настоящая», по файлу не понять.

    None — раскладки нет (роль «Общая» или пара роль×отчётность без формата).
    Тогда книгу собирает `build_metrics_workbook`, как и раньше.

    Паспорт прогона (#155) дописывается ОТДЕЛЬНЫМ листом: у форматтера ролей
    своей книги с паспортом нет, а отпечаток реестра — единственное, чем
    объясняется расхождение вчерашней выгрузки с сегодняшней.
    """
    if not formatted:
        return None
    rows = [r for r in (formatted.get("rows") or [])
            if r.get("kind") not in {"section", "spacer"}]
    if not rows:
        return None

    import openpyxl

    from finengine.roles.formatter import write_formatted_xlsx

    book = write_formatted_xlsx(formatted)
    if not passport:
        return book.getvalue()
    wb = openpyxl.load_workbook(book)
    ws = wb.create_sheet("Паспорт прогона")
    for label, value in passport:
        ws.append([label, value])
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def metrics_xlsx_filename(base_name: str) -> str:
    """Имя книги, безопасное для файловой системы и понятное человеку.

    Санитайзер оставляет кириллицу — иначе «метрики_.xlsx» у каждого второго
    российского эмитента.
    """
    safe = (re.sub(r"[^0-9A-Za-zА-Яа-яЁё_.-]+", "_", base_name)[:40].strip("_")
            or "report")
    return f"метрики_{safe}.xlsx"


async def save_metrics_xlsx(
    *,
    content: bytes,
    filename: str,
    user_id: int | str,
    project_id: str,
) -> str:
    """Положить книгу в «Источники». Возвращает `file_id`.

    ПРО САНИТАЙЗЕР КЛЮЧА — чтобы никто не «починил» несуществующее. `user_id` и
    `project_id` становятся частью ключа объекта, но чистит их не эта функция и
    не вызывающий, а сам `sources.save_upload`: он строит префикс через
    `sources._префикс` → `llm/tools.py::_safe_path_segment`, а сверх того ключ
    ещё раз проверяется в `store/blobs._safe_key`.

    ⚠️ ЦЕПОЧКА ПЕРЕПИСАНА 26.08.2026 — прежняя описывала путь через
    `_sources_dir` → `project_data_dir`, которых после переезда источников в
    объектное хранилище больше нет. Описание санитайзера, ссылающееся на
    несуществующие звенья, хуже отсутствующего: по нему проводят аудит.

    Поэтому тул, который зовёт `require_user_ctx` до этого места, делает это НЕ
    ради пути, а ради owner-гейта: без `project_id` работать нельзя вовсе.
    Повторная чистка уже чистой строки ничего не меняет — `_safe_path_segment`
    идемпотентен.
    """
    meta = await sources.save_upload(
        user_id=user_id, project_id=project_id, filename=filename,
        content=content, content_type=_XLSX_CONTENT_TYPE)
    return meta.file_id


def build_role_layout(
    *,
    plan: Any,
    metrics: list[dict],
    segment_rows: list[dict],
    group_rows: list[dict],
    main_year: str,
    prev_year: str,
    unit: str,
    company_type: str,
    company_name: str,
) -> dict | None:
    """Раскладка роли (ДГР широкая, ЦИБ длинная, ДИЭ сегменты) для фронта.

    None — штатный ответ, а не сбой: у роли «Общая» `config` пуст по
    устройству, и фронт обязан рисовать плоскую таблицу.

    `plan` типизирован как `Any` намеренно: это `roles.scope.RolePlan`, но
    тянуть его сюда типом значило бы связать слой пост-обработки со слоем
    ролей ради одной аннотации.
    """
    if not (settings.role_formatters_enabled and plan.config):
        return None
    return formatter.build_formatted_table(
        plan=plan,
        metrics=[*metrics, *segment_rows, *group_rows],
        meta={
            "period_current": main_year, "period_prev": prev_year,
            "unit": unit, "company_type": company_type,
            "company_name": company_name,
        },
    )


def build_extract_done(
    *,
    metrics_count: int,
    role: str,
    plan: Any,
    role_not_applied: bool,
    role_notice: str,
    formatted: dict | None,
) -> dict:
    """Тело события `extract_done` — единственного пути таблицы на экран.

    До 09.08.2026 этот путь был НЕМЫМ в трёх местах сразу: канала нет —
    тишина; очередь не приняла — тишина; раскладка не собралась — тоже тишина.
    У владельца это выглядело так: прогон отработал, показатели извлечены, а
    таблица в рабочей области пустая, и по логам не понять, на каком из трёх
    шагов оборвалось. Поэтому поля отвечают ровно на вопрос «почему на экране
    пусто»: что послали, какая роль, собралась ли раскладка.

    Восемь ключей — контракт фронта. Переименование любого = пустой экран без
    единой ошибки в логе.
    """
    return {
        "metrics_found": metrics_count,
        "role": role,
        "role_label": plan.role_label,
        "role_format": plan.format,
        "role_not_applied": role_not_applied,
        "role_notice": role_notice,
        "table_output_enums": (
            sorted(plan.output_enums) if plan.output_enums is not None else None
        ),
        "formatted": formatted,
    }


# Поля записи вызова модели, которые едут в журнал прогона. Сокращаем сознательно:
# `cost_usd`, `generation_id` и имя модели журнал уже знает из других полей, а
# разрез стадий читают глазами — длинная запись перестаёт читаться.
#
# `started_at` (epoch, время начала вызова) добавлен 20.08 вместе с параллельным
# запуском сегментной стадии. До него разрез читался сложением: сумма `elapsed_s`
# равна времени прогона — значит стадии стояли в очередь. Теперь сумма больше
# времени прогона всегда, и одно сложение говорит лишь «что-то перекрылось».
# Отрезок каждого вызова — это `started_at` + `elapsed_s`; без начала отрезка
# следующий замер скорости мерить нечем. У прогонов со старой полки поля нет —
# приедет `None`, и это честнее выдуманного нуля.
_STAGE_FIELDS = ("stage", "started_at", "elapsed_s", "tokens_in", "tokens_out", "tokens_cached")

# Итоговые секунды, которые движок кладёт рядом со списком вызовов. У
# двухстадийной схемы есть все три, у полноконтекстной — только `calc_s`:
# отсутствующие не выдумываем, а опускаем.
_STAGE_TOTALS = ("wall_s", "llm_s", "calc_s")


def refusal_reason(metrics: list[dict], diag: dict | None) -> str:
    """Безусловный отказ извлечения — текстом причины; пустая строка = не отказ.

    ДЕФЕКТ, РАДИ КОТОРОГО ЭТО НАПИСАНО. Батч второй стадии, не прошедший разбор
    JSON, попадает в `stage2_invalid_batches`, а цикл выдачи движка всё равно
    отдаёт СТРОКУ НА КАЖДЫЙ запрошенный показатель — только с `found: False`.
    Станционный сторож проверял непустоту списка строк, а список непуст всегда:
    полностью провалившееся извлечение выходило с `metrics_found: 0,
    metrics_total: 122` и КОДОМ 0. Тело навыка учит модель, что «упала» — это
    ненулевой код возврата, значит модель обязана была прочитать провал как
    успех и доложить человеку пустую таблицу как результат.

    БЕЗУСЛОВНЫЙ СЛУЧАЙ ЗДЕСЬ ТОЛЬКО ОДИН: разобрано ноль показателей. Порог для
    ЧАСТИЧНОГО провала (провалилась половина батчей) НЕ выдумываем — это
    решение владельца, а не побочный эффект этой правки.

    `stage2_*` смотрим только чтобы НАЗВАТЬ причину: у полноконтекстной схемы
    батчей нет вовсе, и их отсутствие само по себе отказом не является.
    """
    if any(row.get("found") for row in metrics):
        return ""
    source = diag or {}
    batches = int(source.get("stage2_batch_count") or 0)
    invalid = len(source.get("stage2_invalid_batches") or ())
    if batches and invalid >= batches:
        return (f"извлечение провалилось целиком: ни один из {batches} батчей второй "
                f"стадии не разобран, значений ноль из {len(metrics)} строк")
    return f"извлечение не разобрало ни одного значения из {len(metrics)} строк"


def form_refusal_reason(form: dict) -> str:
    """Тот же безусловный порог `refusal_reason`, но на canonical `FormOutput`.

    Срез 13.5.4: `two_stage_extractor`'s `_diag`/`stage2_*` batch-диагностика
    canonical `extract.py` больше не видна (она внутренняя для graph atoms
    `read.value`/`locate`) — считать причину провала по ней здесь не из чего.
    Порог, который эта функция переносит, тот же самый, что уже был решён
    владельцем для `refusal_reason`: разобрано НОЛЬ значений — отказ;
    ЛЮБОЕ ненулевое число найденного — не отказ. Порог для частичного провала
    по-прежнему не выдумывается.
    """
    rows = form.get("rows") or []
    total = len(rows)
    if total == 0:
        return ""
    found = sum(1 for row in rows if row.get("status") == "supported")
    if found > 0:
        return ""
    return f"извлечение не нашло ни одного значения из {total} запрошенных показателей"


# ── ПОЧЕМУ СОРВАЛСЯ БАТЧ ──────────────────────────────────────────────────────
#
# ТРЕБОВАНИЕ ВЛАДЕЛЬЦА 21.08: «нужно логировать почему не смог, чтобы потом на
# этом ошибки исправлять свои». До сих пор в журнал ехало ЧИСЛО сорвавшихся —
# и по нему нельзя было отличить «модель вернула мусор» от «кончились деньги»,
# то есть нельзя было понять, чинить нам промпт или пополнить счёт.
#
# Причина уже была в диагностике (`stage2_invalid_batches[*].error`), но текстом
# провайдера — разным у каждого. Сводим к ПРИЧИНЕ, по которой можно считать
# статистику, сохраняя исходный текст рядом.
_FAILURE_CAUSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("нет_денег", ("402", "insufficient credits", "more credits", "payment required")),
    ("ключ", ("401", "unauthorized", "invalid api key")),
    ("лимит_скорости", ("429", "rate limit", "too many requests")),
    ("таймаут", ("timeout", "timed out", "deadline")),
    ("модель_молчала", ("empty response", "no content", "пустой ответ")),
    ("не_разобран_json", ("json", "expecting value", "unterminated", "decode")),
    ("обрыв", ("connection", "reset by peer", "eof", "broken pipe")),
)


def failure_cause(error_text: object) -> str:
    """Текст ошибки провайдера → причина, по которой можно считать статистику.

    Порядок проверок = приоритет: «402 … invalid json» это про деньги, а не про
    разбор. Не узнали — `иное`, и это ЧЕСТНЕЕ, чем свалить в «не_разобран_json»:
    неузнанная причина обязана быть видна, иначе она никогда не будет разобрана.
    """
    low = str(error_text or "").lower()
    if not low.strip():
        return "без_текста"
    for cause, needles in _FAILURE_CAUSES:
        if any(n in low for n in needles):
            return cause
    return "иное"


def shard_losses(diag: dict | None) -> dict:
    """Потери шардирования второй стадии; пустой словарь = схема без батчей.

    ЧТО СЛОМАЛОСЬ, КОГДА ВТОРАЯ СТАДИЯ РАЗДЕЛИЛАСЬ НА БАТЧИ (20.08.2026).
    Пока батч был ОДИН, его срыв означал пустую ячейку целиком — и это ловил
    `refusal_reason`: значений ноль, прогон объявлен отказом. Теперь батчей
    восемь, и срыв одного из них — это дыра ровно в его состав (при размере 10
    — десять показателей), а `refusal_reason` молчит: хоть одна строка
    `found=True` есть, значит формально не отказ. Проверено исполнением: 7
    провалившихся батчей из 8 и 10 найденных значений из 80 дают пустую строку
    причины, код возврата 0 и доклад «извлечение удалось».

    ПОЭТОМУ ФАКТ ПОТЕРИ ЕДЕТ В ИСХОД, А НЕ ТОЛЬКО В ДИАГНОСТИКУ. Диагностику
    (`_diag`) не читает ни модель, ни человек: она уезжает в разрез стадий и в
    журнал. Здесь же — те два числа и тот список имён, по которым видно, что
    таблица неполна НЕ ПОТОМУ, что чисел нет в отчёте:
      * `batches_failed` — сорвавшиеся батчи (JSON не разобран за все попытки);
      * `metrics_unasked` — показатели, которых во второй стадии не спрашивали
        вовсе: первая стадия не нашла им улик. В таблице они выйдут пустыми и
        неотличимо от «в отчёте нет».
      * `metrics_asked_without_evidence` — те же безулиочные, но СПРОШЕННЫЕ
        (умолчание, `FINENGINE_EXTRACT_ASK_WITHOUT_EVIDENCE`): улик первой
        стадии у них нет, и смотрели они в таблицы соседей по батчу. Это не
        потеря, а ЦЕНА обратной стороны — их значения проверяются придирчивее
        прочих, потому что выдумать модели тут легче всего. Список нужен
        ровно затем, чтобы замер мог посчитать, сколько из них подтвердилось
        по golden, а сколько оказалось выдумкой.

    ⚠️ ПОРОГ ОТКАЗА ЗДЕСЬ НЕ РЕШАЕТСЯ. Сколько сорвавшихся батчей делают прогон
    негодным — решение владельца (см. `refusal_reason`); эта функция только
    показывает, а не судит. Молча же выдавать дырявую таблицу за целую нельзя
    ни при каком пороге.

    ЧТО ДЕЛИТСЯ НА ЧТО (правка 21.08.2026, проверяющий волны R). Числа здесь
    сравнивают долями, поэтому у каждой доли обязан быть СВОЙ знаменатель:
      * `batches` / `batches_failed` / `batch_retries` — ОСНОВНАЯ стадия;
      * `segment_batches` / `segment_batch_retries` — СЕГМЕНТНАЯ, и только если
        она вообще шла.
    До этой правки `batch_retries` складывал обе стадии, а `batches` считал
    одну: при восьми основных батчах без единого переспроса и трёх переспросах
    сегментной стадии доля выходила 3/8 = 37 % на ровном месте.
    Нулевых сегментных ключей здесь нет по той же причине, по которой у схемы
    без батчей пустой словарь: «segment_batch_retries: 0» читается как
    «сегментные батчи были и не переспрашивали», а их не было вовсе.

    ⚠️ `batch_retries` И `batches_failed` ПЕРЕСЕКАЮТСЯ, И ЭТО НЕ ОШИБКА.
    Счётчик считает ЛИШНИЕ ВЫЗОВЫ, а не батчи: у насовсем сорвавшегося батча
    попыток `attempts`, и в `batch_retries` он даёт `attempts - 1`, попадая
    заодно в `batches_failed`. То есть `batch_retries` — цена во времени и
    токенах, `batches_failed` — дыра в таблице; складывать их нельзя.
    """
    source = diag or {}
    batches = int(source.get("stage2_batch_count") or 0)
    if not batches:
        return {}
    unasked = [str(enum) for enum in (source.get("stage2_skipped_empty_evidence") or ())]
    starved_asked = [str(enum) for enum in (source.get("stage2_asked_without_evidence") or ())]
    # ПЕРЕСПРОСЫ — ТОЖЕ ЦЕНА ШАРДИРОВАНИЯ, И ДО 21.08 ЕЁ НЕ ВИДЕЛ НИКТО.
    # Движок считает их (`stage2_retry_count`, `segment_stage2_retry_count`), но
    # полная диагностика в журнал не едет: `log_extract_run` кладёт в него
    # ВЫБОРКУ — ячейку, роль, единицу, разрез стадий и вот эти потери. Значит
    # счётчик оставался внутри одного хода и умирал вместе с ним, а журнал —
    # единственное место, где прогоны сравнивают между собой.
    # Отличие от `batches_failed`: там батч сорвался НАСОВСЕМ (дыра в таблице),
    # здесь он ответил со второго-третьего раза (таблица цела, но заплачено
    # временем и токенами). Переспросов при четырёх попытках может быть втрое
    # больше, чем батчей, — и на замере времени это неотличимо от медленной
    # модели, если счётчика нет.
    segment_batches = int(source.get("segment_stage2_batch_count") or 0)
    segment_losses = (
        {
            "segment_batches": segment_batches,
            "segment_batch_retries": int(source.get("segment_stage2_retry_count") or 0),
        }
        if segment_batches
        else {}
    )
    invalid = list(source.get("stage2_invalid_batches") or ())
    causes: dict[str, int] = {}
    for item in invalid:
        cause = failure_cause((item or {}).get("error"))
        causes[cause] = causes.get(cause, 0) + 1
    return {
        "batches": batches,
        "batches_failed": len(invalid),
        # ПРИЧИНЫ, а не только счёт: «два батча сорвались» не говорит, чинить
        # промпт или пополнять счёт. Текст провайдера остаётся в диагностике.
        "batches_failed_causes": causes,
        # Честная доля СПРОШЕННОГО, что доехало. Владелец 21.08: «сказать честно
        # сколько процентов выполнено от запланированного и что не смог извлечь».
        "batches_done_pct": round(100.0 * (batches - len(invalid)) / batches, 1),
        "batch_retries": int(source.get("stage2_retry_count") or 0),
        **segment_losses,
        "metrics_unasked_total": len(unasked),
        "metrics_unasked": unasked,
        "metrics_asked_without_evidence_total": len(starved_asked),
        "metrics_asked_without_evidence": starved_asked,
    }


def run_stages(diag: dict | None) -> dict:
    """Посекундный разрез прогона из диагностики движка (`done['_diag']`).

    ЦЕНА, КОТОРАЯ ЭТО КУПИЛА (разведка Б7, 19.08). Оба движка считают время
    каждого вызова модели (`_diag['llm_requests'][*]['elapsed_s']`) и кладут его
    в терминальное событие — и на боевом пути его НЕ ЗАБИРАЛ НИКТО: станционный
    скрипт до `_diag` не дотрагивался, замерщик A/B выбрасывал. Из-за этого
    вопрос «сколько стоит стадия сжатия» упирался в переизобретение замера, а
    предложения по ускорению приходилось обосновывать оценками вместо чисел.

    Разрез внутренний: он едет в журнал прогонов и в конверт, но не на экран
    пользователю и не в сводку хода (см. `_HEAVY_FOR_SUMMARY` в стоке) — иначе
    секунды попадали бы в промпт следующего хода и в пересказ модели.
    """
    source = diag or {}
    stages: dict = {
        "calls": [
            {field: record.get(field) for field in _STAGE_FIELDS}
            for record in (source.get("llm_requests") or [])
        ]
    }
    for key in _STAGE_TOTALS:
        if source.get(key) is not None:
            stages[key] = source[key]
    return stages


def log_extract_run(
    *,
    arch: str,
    doc_id: str,
    filename: str,
    model_id: str | None,
    company_type: str,
    company_name: str,
    main_year: str,
    prev_year: str,
    user_id: int | str | None,
    started_at: float,
    metrics: list[dict],
    segment_rows: list[dict],
    group_rows: list[dict],
    cell: str,
    role: str,
    unit: str,
    versions: dict,
    stages: dict | None = None,
    losses: dict | None = None,
) -> None:
    """Запись прогона в журнал `extract_runs.jsonl`.

    Журнал читает снимок бенчмарка (`benchmarks/snap_bench.py`), поэтому в него
    едут ВСЕ строки прогона — плоские, сегментные и групповые: качество
    считается по всему, что извлеклось.

    `arch` — обязательный аргумент, а не константа: журнал должен называть
    ПУТЬ, которым шёл прогон. Захардкоженное имя пережило бы смену пути и
    соврало бы бенчмарку, не покраснев нигде.

    `losses` — потери и цена шардирования второй стадии (`shard_losses`).
    ЗАЧЕМ ОНИ В ЖУРНАЛЕ. Спрошенные без улик первой стадии показатели
    (`metrics_asked_without_evidence`) — это те, у кого своих улик не было и
    кто смотрел в таблицы соседей по батчу; выдумать значение модели там легче
    всего. Отличить находку от выдумки можно ровно одним способом: взять этот
    список поимённо и сверить его строки с golden. Сверяет — снимок бенчмарка,
    а читает он ЭТОТ файл. Пока список ехал только в сводку хода и в stderr
    станции, замерять было нечем: план «отследить, не выдумывает ли модель»
    упирался бы в ручное чтение исхода.
    """
    run_logger.log_run(
        doc_id=doc_id, filename=filename,
        arch=arch, model_id=model_id,
        company_type=company_type, company_name=company_name,
        period_current=main_year, period_prev=prev_year,
        user_id=user_id, started_at=started_at,
        metrics=[*metrics, *segment_rows, *group_rows],
        diag={"cell": cell, "role": role, "unit": unit,
              "segments": len(segment_rows), "group": len(group_rows),
              # Посекундный разрез стадий. Пустой словарь В ЖУРНАЛ НЕ КЛАДЁМ:
              # запись «stages: {}» читается как «стадий не было», а означает
              # «прогон пришёл со старой полки, которая разрез не присылает».
              **({"stages": stages} if stages else {}),
              # Потери шардирования — по тому же правилу: пустого ключа нет
              # вовсе. У схемы без батчей (`full_context`) `shard_losses`
              # возвращает пустой словарь, и «losses: {}» в журнале читалось бы
              # как «батчи были, потерь не случилось».
              **({"losses": losses} if losses else {})},
        versions=versions,
    )


def document_filename(doc_id: str) -> str:
    """Имя файла для журнала прогонов. Best-effort: журнал важнее точности имени.

    По этому полю бенчмарк сопоставляет прогон с эталоном, поэтому пустым его
    оставлять нельзя; но и ронять из-за него доставку результата не за что.

    Живёт здесь, а не у вызывающего: журнал пишут ДВА пути — состоявшийся прогон
    (`log_extract_run`) и оборванный (`log_aborted_run`), — и имя им нужно по
    одному правилу. Второй копией правило разъехалось бы при первой же правке.
    """
    try:
        from finengine.ingestion.document_store import get_store

        meta = get_store().get_document(doc_id)
        return getattr(meta, "filename", "") or doc_id
    except Exception:  # noqa: BLE001
        log.warning("журнал: имя документа %s не найдено", doc_id[:8])
        return doc_id


def log_aborted_run(
    *,
    doc_id: str,
    company_name: str,
    user_id: int | str | None,
    started_at: float,
    metrics: list[dict],
    segment_rows: list[dict],
    group_rows: list[dict],
    reason: str,
) -> None:
    """Записать в журнал прогон, который НЕ доработал до терминального события.

    ЗАЧЕМ (найдено на прогоне владельца по Совкомбанку 15.08). Комната отдала
    все 110 показателей и замолчала; терминального `done` не пришло, дренаж
    закрылся по простою — и записи в журнале не появилось. При этом на экране
    таблица БЫЛА: строки рисуются по событиям на лету, а журнал пишется только
    из терминального события. Владелец видел результат, а разобрать прогон было
    нечем — ни бенчмарком, ни глазами.

    Такой прогон обязан оставлять след: журнал — источник для бенчмарка и для
    разбора инцидентов, и «не доработал» само по себе факт, который надо видеть.

    ЧЕГО ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО. Ни Excel, ни раскладки роли, ни события
    фронту, ни отметки «готово» на шине. Прогон не состоялся, и выдавать его
    итог за полный нельзя: поле `error` заполнено, `arch` тот же, а `diag`
    честно говорит, сколько строк успело приехать.

    Ячейка, роль и тип компании приезжают в `done.result` — то есть ровно в том,
    чего не было. Оставляем пустыми, а не угадываем.
    """
    rows = [*metrics, *segment_rows, *group_rows]
    if not rows:
        return
    main_year, prev_year, _all = pick_report_years(rows)
    run_logger.log_run(
        doc_id=doc_id, filename=document_filename(doc_id),
        arch="aifa_station", model_id=None,
        company_type="", company_name=company_name,
        period_current=main_year, period_prev=prev_year,
        user_id=user_id, started_at=started_at,
        metrics=rows, error=reason,
        diag={"aborted": True, "segments": len(segment_rows),
              "group": len(group_rows), "metrics": len(metrics)},
        versions={},
    )


@dataclass(frozen=True)
class ExtractionOutcome:
    """Всё, что даёт пост-обработка прогона — одним объектом.

    Кортеж из десяти значений вызывающие разбирали бы каждый по-своему и
    однажды разошлись бы в порядке. Здесь имена, а не позиции.
    """
    main_year: str
    prev_year: str
    all_periods: list[str]
    unit: str
    table_markdown: str
    excel_file_id: str | None
    excel_filename: str | None
    formatted: dict | None
    versions: dict
    extract_done: dict


async def finalize_extraction(
    *,
    metrics: list[dict],
    segment_rows: list[dict],
    group_rows: list[dict],
    plan: Any,
    cell: str,
    company_name: str,
    company_type: str,
    role: str,
    role_not_applied: bool,
    role_notice: str,
    owner_id: int | str | None,
    doc_id: str,
    user_id: int | str | None,
    project_id: str | None,
    xlsx_base_name: str,
    arch: str,
    model_id: str | None,
    filename: str,
    started_at: float,
    stages: dict | None = None,
    losses: dict | None = None,
) -> ExtractionOutcome:
    """РЕЦЕПТ пост-обработки состоявшегося прогона — целиком, одним вызовом.

    ЗАЧЕМ ОДНОЙ ФУНКЦИЕЙ (#190, К8a). Отдельные шаги вынесены ещё в К1, но
    ПОСЛЕДОВАТЕЛЬНОСТЬ их вызова оставалась в хендлере тула. Второй потребитель
    — слушатель нонса — написал бы её заново, и она разъехалась бы ровно так же,
    как разъехались два рисовальщика таблицы: тул сохраняет пустые строки,
    станция выбрасывает, и никто этого не замечал, пока не сверили.

    ПОРЯДОК ЗДЕСЬ — ЧАСТЬ КОНТРАКТА, а не деталь. Раскладка роли обязана быть
    собрана ДО события `extract_done`, иначе она уедет во фронт пустой;
    заземление публикуется до события, иначе ждущий анализ простаивает дольше
    нужного.

    ПОЛИТИКА ОТКАЗА ТЕПЕРЬ ЗДЕСЬ. В К1 пять `try/except` были намеренно
    оставлены у вызывающего: потребитель был один, а политика второго
    неизвестна. Теперь она известна и совпадает — оба обязаны доставить
    таблицу, даже если не собрался паспорт, книга или раскладка. Держать одну
    политику в двух местах значило бы чинить её дважды.

    ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ: отметок шага плана, эмита событий в канал и
    сборки ответа модели. Это разное у тула и у слушателя, и попытка обобщить
    их привела бы к флагам вместо кода.
    """
    main_year, prev_year, all_periods = pick_report_years(metrics)
    unit = dominant_unit(metrics)
    table_markdown = render_flat_table(
        metrics, main_year=main_year, prev_year=prev_year,
        show_enums=plan.output_enums)

    # Паспорт ПРАВИЛ прогона (#155). ДВА присваивания, а не одно: при отказе
    # рендера строк отпечаток обязан уцелеть — он едет в журнал, по которому
    # считается бенчмарк.
    versions: dict = {}
    passport: list[tuple[str, str]] = []
    try:
        versions = registry_passport(cell)
        passport = passport_rows(versions)
    except Exception:  # noqa: BLE001 — паспорт не смеет ронять готовое извлечение
        log.exception("%s: паспорт правил не снят, doc=%s", arch, doc_id)

    # ── Excel со ВСЕМИ периодами. ТРИ шага в этом порядке: собрать книгу →
    # назвать файл → сохранить. Имя присваивается ДО отправки в хранилище,
    # поэтому при отказе сохранения оно остаётся известно, а `file_id` — нет.
    # Это состояние наблюдаемо в ответе и менять его нельзя.
    # Раскладка роли (ДГР широкая / CIB длинная / ДИЭ сегменты / …). У роли
    # «Общая» config пуст по устройству → None, и фронт рисует плоскую.
    #
    # ⚠️ СЧИТАЕТСЯ ДО КНИГИ, И ЭТО НЕ ПЕРЕСТАНОВКА РАДИ ПОРЯДКА: по ней книга и
    # собирается (см. ниже). Раньше экран показывал раскладку роли, а выгрузка
    # приезжала плоской — «выгрузка должна выходить в формате той же роли»
    # (владелец, 19.08).
    formatted: dict | None = None
    try:
        formatted = build_role_layout(
            plan=plan, metrics=metrics, segment_rows=segment_rows,
            group_rows=group_rows, main_year=main_year, prev_year=prev_year,
            unit=unit, company_type=company_type, company_name=company_name)
    except Exception:  # noqa: BLE001 — плоская таблица всё равно работает
        log.exception("%s: раскладка роли не собралась, doc=%s", arch, doc_id)

    excel_file_id: str | None = None
    excel_filename: str | None = None
    try:
        if not user_id or not project_id:
            # Штатный исход, а не сбой: без проекта складывать книгу некуда.
            # Уровень WARNING, а не EXCEPTION со стеком, — стек тут читается
            # как дефект, которого нет.
            log.warning("%s: Excel не сохранён — нет user_id/project_id, doc=%s",
                        arch, doc_id)
        else:
            content = build_role_workbook(formatted, passport=passport) or (
                build_metrics_workbook(
                    metrics, segment_rows, group_rows, passport=passport,
                    company_type=company_type))
            excel_filename = metrics_xlsx_filename(xlsx_base_name)
            excel_file_id = await save_metrics_xlsx(
                content=content, filename=excel_filename,
                user_id=user_id, project_id=project_id)
    except Exception:  # noqa: BLE001 — книга не смеет ронять готовое извлечение
        log.exception("%s: Excel не сохранён, doc=%s", arch, doc_id)

    extract_done = build_extract_done(
        metrics_count=len(metrics), role=role, plan=plan,
        role_not_applied=role_not_applied, role_notice=role_notice,
        formatted=formatted)

    try:
        log_extract_run(
            arch=arch, doc_id=doc_id, filename=filename, model_id=model_id,
            company_type=company_type, company_name=company_name,
            main_year=main_year, prev_year=prev_year,
            # ⚠️ В ЖУРНАЛ — СЫРОЙ владелец (`owner_id`), а НЕ санированный
            # `user_id`. Санитайзер делает из числа строку, и запись в журнале
            # молча сменила бы тип поля: разбор инцидентов и бенчмарк ищут по
            # нему. `user_id`/`project_id` нужны только хранилищу.
            user_id=owner_id,
            started_at=started_at, metrics=metrics, segment_rows=segment_rows,
            group_rows=group_rows, cell=cell, role=role, unit=unit,
            versions=versions, stages=stages, losses=losses)
    except Exception:  # noqa: BLE001 — журнал не смеет ронять извлечение
        log.exception("%s: прогон не записан в журнал, doc=%s", arch, doc_id)

    return ExtractionOutcome(
        main_year=main_year, prev_year=prev_year, all_periods=all_periods,
        unit=unit, table_markdown=table_markdown, excel_file_id=excel_file_id,
        excel_filename=excel_filename, formatted=formatted, versions=versions,
        extract_done=extract_done)
