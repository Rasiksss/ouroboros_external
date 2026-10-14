"""Metrics-table helper for the analysis prompt.

Renders extracted metrics as a compact markdown bullet list. Used by the
live `fe_analyze` path (`skills/analysis/run.py`) to hand the model a
grounded reference table alongside the report text.

(Historically this module also hosted the sequential CoT chain runner —
`run_chain`/`run_full_synthesis` — removed together with the legacy
chain-analysis endpoints.)
"""
from __future__ import annotations

import re
from typing import Iterable

# Ссылка на страницу исходного PDF — ОДИН формат на весь продукт (#157).
# Пишет его сюда движок (в таблицу заземления), повторяет модель в отчёте, а
# фронт по нему делает ссылку кликабельной. Держать регулярку рядом с
# генератором строки обязательно: разъедутся формат записи и формат чтения —
# и ссылки либо перестанут работать молча, либо станут выдумываемыми.
#
# «PDF стр. N» именно так, а не «стр. N»: печатный колонтитул отчёта почти
# никогда не совпадает с физической страницей файла (обложка, разделители,
# приложения), а прыгаем мы по ФИЗИЧЕСКОЙ. Слово PDF в подписи — напоминание
# читателю, что номер файловый, а не типографский.
PAGE_REF_RE = re.compile(r"PDF\s*стр\.\s*(\d+)")


def _format_value(value, unit: str) -> str:
    """Число так, как его написал бы аналитик, а не `repr` float'а.

    ГРАБЛЯ (#146). Раньше здесь стояло `f"{value}"`, и рассчитанный кодом
    коэффициент уезжал в промпт как «0.24632214874683647»: семнадцать значащих
    цифр у величины, точность которой — два знака. Модель такую запись честно
    переписывала в отчёт, и заземляющая таблица выглядела машинным мусором.

    Единица клеится к КАЖДОМУ значению. Раньше она стояла одна на строку, в
    хвосте, и строка читалась как «предыдущий млн. руб.».
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"{value}"
    if abs(v) >= 1000:
        text = f"{v:,.0f}".replace(",", " ")
    elif abs(v) >= 1:
        text = f"{v:.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip() if unit else text


def _format_metric_row(name: str, value_current, value_prev, unit: str, sanity: str = "",
                       page=None) -> str:
    cur = _format_value(value_current, unit)
    prev = _format_value(value_prev, unit)
    row = f"- {name}: {cur} (тек.) / {prev} (пред.)"
    # Страница, на которой движок НАШЁЛ это число (#157). Раньше она доезжала
    # до таблицы извлечения и обрывалась там: в отчёте анализа сослаться было
    # не на что, и проверить число можно было только перелистав отчёт руками.
    # Страницы нет (число посчитано формулой или движок её не запомнил) —
    # молчим: пустая ссылка хуже отсутствующей, а выдуманная хуже обеих.
    if isinstance(page, int) and page > 0:
        row = f"{row} [PDF стр. {page}]"
    # Движок посчитал что-то вне диапазона смысла — говорим об этом модели, а не
    # подаём число как достоверное (#146: перевёрнутый Cost/Income подавался
    # ровно с тем же спокойствием, что и правильный).
    return f"{row} ⚠ {sanity}" if sanity else row


def build_metrics_table_str(metrics: list[dict], filter_enums: Iterable[str] | None = None) -> str:
    """Render the extracted metrics as a markdown bullet list (one row per
    enum). When `filter_enums` is non-empty, keep only those.
    """
    selected = list(filter_enums) if filter_enums else None
    rows = []
    for m in metrics:
        if selected is not None and m.get("enum") not in selected:
            continue
        name = m.get("name_ru") or m.get("enum") or ""
        if not name:
            continue
        rows.append(_format_metric_row(
            name=name,
            value_current=m.get("value_current"),
            value_prev=m.get("value_prev"),
            unit=m.get("unit", "") or "",
            sanity=m.get("sanity", "") or "",
            page=m.get("page"),
        ))
    return "\n".join(rows) if rows else "(нет данных по этой группе показателей)"


def pages_in_table(table_str: str) -> set[int]:
    """Страницы, которые движок ПОКАЗАЛ модели в таблице заземления.

    Читаем их ОБРАТНО из готовой строки таблицы, а не собираем вторым проходом
    по метрикам, и это принципиально: множество разрешённых страниц обязано
    совпадать с тем, что модель реально видела, символ в символ. Второй проход
    по данным — это вторая реализация одного правила, и разъедется она молча.
    """
    return {int(n) for n in PAGE_REF_RE.findall(table_str or "")}


def scrub_page_refs(text: str, allowed: set[int]) -> str:
    """Снять формат ссылки со страниц, которых движок не находил.

    ИНВАРИАНТ (#157): страницу находит ДВИЖОК, а не модель. Модель обязана
    брать номер из таблицы заземления, но послушание модели — не гарантия:
    она может вспомнить номер из колонтитула, из оглавления или просто
    ошибиться. Такая ссылка ведёт читателя не туда, и хуже того — выглядит
    ровно как проверенная.

    Поэтому кликабельность даётся не по доверию, а ПО ДАННЫМ: номер, которого
    нет среди показанных, теряет опознавательный формат «PDF стр. N» и
    получает честную пометку. Число из отчёта при этом не выбрасывается —
    удалить строку рассуждения мы права не имеем, а вот выдать её за
    подтверждённую ссылку — тем более.
    """
    def _sub(m: "re.Match[str]") -> str:
        n = int(m.group(1))
        return m.group(0) if n in allowed else f"стр. {n} (страница не подтверждена движком)"

    return PAGE_REF_RE.sub(_sub, text or "")
