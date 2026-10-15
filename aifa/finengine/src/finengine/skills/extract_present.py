"""Представление результата извлечения: периоды прогона, markdown-таблица, Excel.

ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ. Эти три вещи жили ВНУТРИ станционного скрипта
`station-skills/aifa-extract/scripts/extract.py` (его шаги 4–6). Пока навык был
одной программой, это было терпимо. С появлением второго исполнителя —
спека-навыка (`finengine.skills.spec`) — они бы неминуемо появились ВТОРОЙ
копией, и первая же правка формата разошлась бы между «старым скриптом» и
«спекой». Ровно этот класс двойников репозиторий уже оплачивал: #72 (навык в
двух профилях), #99 (инлайн-ветка движка рядом со станционной), #147 (вторая
сборка промпта). Поэтому логика вынута сюда ЦЕЛИКОМ, а оба пути её зовут.

По границе SKILLS_ARCHITECTURE.md это СПОСОБНОСТЬ (слой тулов), а не порядок:
спека навыка вправе решать, звать её или нет, но не переписывать.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from finengine.roles.formatter import (
    build_formatted_table,
    write_formatted_xlsx,
    write_generic_wide_xlsx,
)


def format_number(v: Any) -> str:
    """Число в вид, привычный русскому отчёту: разряды пробелом, прочерк вместо
    пустоты. Прочерк (а не пусто) — чтобы в чате было видно, что показатель
    спрашивали и не нашли, а не что строку забыли."""
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}".replace(",", " ") if float(v).is_integer() else f"{v:,}".replace(",", " ")
    return str(v)


# Заглушка для «года в ярлыке нет»: даёт пустую строку тем же `.group(1)`,
# что и настоящее совпадение, — без второй ветки на каждый вызов.
class _NoYear:
    @staticmethod
    def group(_n: int) -> str:
        return ""


_NO_YEAR = _NoYear()


def value_for_period(metric: dict, period: str) -> Any:
    """Значение показателя за указанный период.

    Сопоставление подстрокой по году, а не точным равенством ярлыка: движок
    отдаёт периоды в разных формах («FY 2024», «31.12.2024», «12M 2024»), и
    таблица обязана собираться по всем ним одинаково. Пустой `period` = «текущий»
    в терминах движка."""
    if not period:
        return metric.get("value_current")
    for pv in metric.get("period_values") or []:
        if period in str(pv.get("period") or ""):
            return pv.get("value")
    return None


def pick_periods(metrics: Iterable[dict],
                 reported: tuple[str, str] | None = None) -> tuple[str, str]:
    """Основной и предыдущий год прогона.

    🔴 СНАЧАЛА СПРАШИВАЕМ ДВИЖОК, И ТОЛЬКО ПОТОМ СЧИТАЕМ (22.08.2026).

    ЖИВОЙ ПРОГОН, С КОТОРОГО ЭТО НАПИСАНО: `severstal-Q1-2025.pdf`. Движок
    определил периоды верно и сказал об этом в событии `start`
    (`period_current: "Q1 2025"`, `period_prev: "Q1 2024"`) — а этот подсчёт
    вернул `("2024", "2025")`, ПЕРЕВЁРНУТЫМИ. У промежуточного отчёта баланс
    сравнивается с концом ПРОШЛОГО года (`31.12.2024`), и вместе со строками
    `Q1 2024` год 2024 встречается чаще, чем 2025. Правило «самый частый год»
    на квартальнике работает против себя.

    Цена: колонки markdown-таблицы и книги Excel уезжают наоборот, `metrics_found`
    считается по чужому году, а сводка уносит перевёрнутые периоды в промпт
    следующего хода. При этом шапка экрана всё это время показывает правильные —
    её рисует событие движка. То есть в продукте было ДВА решателя одного
    вопроса, и они расходились молча.

    `reported` — ярлыки, которые движок уже назвал. Берём из них ГОД, а не ярлык
    целиком: сопоставление в `value_for_period` идёт подстрокой, и «Q1 2025» не
    нашлось бы в балансовой строке `31.03.2025`. Год находит обе.

    ⚠️ ПОДСЧЁТ НИКУДА НЕ ДЕВАЕТСЯ и остаётся правилом для всех, кто событий
    движка не видит (путь спеки `skills/spec/steps.py`), и запасным здесь: если
    названного движком года в показателях нет вовсе, доверять ему нечему.
    Основной = год с НАИБОЛЬШИМ числом значений; не «самый свежий» — в отчёте
    всегда мелькают годы из примечаний и сравнительных справок, и по свежести
    основным стал бы год с двумя значениями. Предыдущий — строго «основной − 1»,
    если он в отчёте есть, иначе следующий по частоте.
    """
    # 🔴 СЧИТАЕМ ЗНАЧЕНИЯ, А НЕ МЕТКИ (прогон KBC, 26.08.2026).
    #
    # Здесь стоял подсчёт всех `period_values` подряд, включая записи со
    # значением `None`. У отчёта KBC за 1Q2026 движок назвал периодом
    # `20.12.2024` (выдуманная дата, разобрано в `skills/periods.py`), и
    # проверка «названный год обязан встречаться в показателях» его ПРОПУСТИЛА:
    # год 2024 встречался пять раз — все пять в метках без единого значения
    # (дата аудиторского заключения у `MODIFIED_AUDITORS_OPINION`).
    #
    # Итог: таблица построилась по колонкам 2024/2023 и вышла ПУСТОЙ при 73
    # найденных показателях. Ни одного отказа.
    #
    # Метка без значения ничего не доказывает: год, за который нечего показать,
    # не может быть годом прогона. Считаем только то, что реально несёт число.
    yc: Counter = Counter()
    for m in metrics:
        for pv in m.get("period_values") or []:
            if pv.get("value") is None:
                continue
            ym = re.search(r"(20\d{2})", str(pv.get("period") or ""))
            if ym:
                yc[ym.group(1)] += 1

    if reported:
        сказано = [(re.search(r"(20\d{2})", str(p or "")) or _NO_YEAR).group(1)
                   for p in reported[:2]]
        main_said, prev_said = (сказано + ["", ""])[:2]
        # Оба названных года обязаны реально встречаться в показателях: иначе
        # это не «движок знает лучше», а «движок сказал про другой документ».
        if main_said and prev_said and main_said in yc and prev_said in yc:
            return main_said, prev_said

    years = [y for y, _ in yc.most_common()]
    main_year = years[0] if years else ""
    prev_year = ""
    if main_year.isdigit():
        cand = str(int(main_year) - 1)
        prev_year = cand if cand in yc else (years[1] if len(years) > 1 else "")
    return main_year, prev_year


def render_table_markdown(
    metrics: Iterable[dict],
    *,
    name_by_enum: dict[str, str],
    main_period: str,
    prev_period: str,
    show_enums: Optional[set[str]] = None,
) -> str:
    """Markdown-таблица для вывода агентом as-is.

    `show_enums` — раскладка роли (#141). Таблица в чате это ВЫДАЧА, и её состав
    задаёт роль, а не то, сколько показателей движок читал из отчёта: при
    `--basis-full` базис специально шире роли (его ест анализ), и без этого
    фильтра пользователь роли ДИЭ получил бы в чат всю ячейку вместо своих
    13 строк. `None` = не фильтровать."""
    head = f"| Показатель | {main_period or 'Текущий'} | {prev_period or 'Пред.'} | Ед. |"
    rows = [head, "|---|---:|---:|---|"]
    for m in metrics:
        if show_enums and m.get("enum") not in show_enums:
            continue
        cur, prev = value_for_period(m, main_period), value_for_period(m, prev_period)
        if cur is None and prev is None:
            continue
        nm = name_by_enum.get(m.get("enum"), m.get("name_ru") or m.get("enum"))
        rows.append(f"| {nm} | {format_number(cur)} | {format_number(prev)} | {m.get('unit', '') or ''} |")
    return "\n".join(rows)


def write_role_xlsx(path: str | Path, *, plan, metrics: list[dict], meta: dict) -> None:
    """Excel реальным форматтером ролей: у спец-роли свой лэйаут, у остальных —
    generic wide. Второй ветки нет намеренно — лэйауты это данные
    (`roles/layouts.json`), и «своя» сборка книги увела бы правку лэйаута мимо
    выгрузки."""
    ft = build_formatted_table(plan=plan, metrics=metrics, meta=meta)
    xlsx = write_formatted_xlsx(ft) if ft else write_generic_wide_xlsx(metrics, meta)
    Path(path).write_bytes(xlsx.getvalue())
