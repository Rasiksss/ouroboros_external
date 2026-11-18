"""Первый уровень, шаги 1–2: нарезка отчёта на таблицы и паспорт каждой.

ЗАЧЕМ. Требование владельца 15.08.2026: извлечение из ОДНОГО прохода по всему
тексту становится ДВУХУРОВНЕВЫМ — сначала поиск и ранжирование таблиц, потом
извлечение из отобранных. Проект — `docs/EXTRACTION_V2_DESIGN.md`, раздел 6.
Здесь сделаны шаги 6.1 и 6.2: нарезка и паспорт. Оба — КОД БЕЗ МОДЕЛИ.

🔴 ГЛАВНОЕ ПРАВИЛО ЭТОГО МОДУЛЯ: ЗДЕСЬ НЕТ НИ ОДНОГО СУЖДЕНИЯ О СМЫСЛЕ.
Ни весов, ни ключевых слов, ни порогов «числовая плотность больше 35%», ни
«похоже на форму отчётности». Всё это — решения по смыслу, а их принимает
модель, читая паспорта (шаг 6.3). Граница код↔модель, решение владельца 09.08:
код возит и режет, модель решает.

Механика нарезки взята у `bulk_extractor._consolidated_group_candidate_tables`
(строки 2099-2144) — и взята НАМЕРЕННО без её продолжения: там блоки получают
оценку `score` по ключевым словам и сортируются по ней (`_flush_group_table_block`,
веса +3/+5/+7 и порог 7 на строках 2036-2098). Эта арифметика СНОСИТСЯ, а не
переезжает: отбор таблиц — то самое решение по смыслу.

ЧТО ДАЁТ ЭКОНОМИЮ. Сегодня в модель уходит ВЕСЬ отчёт, да ещё по числу шардов
раз. Паспорт — это заголовки, шапка и метки строк. Полный текст остаётся нужен
только тем показателям, которым таблица не нашлась (остаточный пакет, шаг 6.5),
— поэтому первый уровень не может ухудшить полноту, он может только убрать шум
там, где таблица найдена.

⚠️ ЗАМЕР НА ЖИВОМ ПРОГОНЕ (Черкизово, годовой МСФО, 15.08): 93 таблицы,
паспорта — 101 173 б из 329 129 б текста, то есть **30,7%**, а не «единицы
процентов», как я написал здесь при первой редакции. Экономия на первом уровне
есть, но она втрое скромнее ожидаемой, и планировать по ней надо от 30%.
Причина видна из числа таблиц: 93 блока — это почти весь табличный слой отчёта,
а метки строк в нём длинные. Сокращать дальше можно только отбором таблиц
(шаг 6.3), то есть моделью, — но не нарезкой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Маркер страницы, который ставит наш конвейер распознавания. На него же
# опирается цитирование страниц в готовой таблице — поэтому маркеры обязаны
# дожить до пакета уровня-2 (шаг 6.5), а не потеряться при нарезке.
_PAGE = re.compile(r"\s*===PAGE\s+(\d+)===")

# Сколько текстовых строк над блоком считаем его подписью. Четыре — как в
# механике-источнике: этого хватает на «Примечание 14. Кредиты и займы» плюс
# строку единиц измерения, и не хватает, чтобы затащить абзац прозы.
_CONTEXT_LINES = 4

# Сколько меток строк кладём в паспорт. Двенадцать — чтобы паспорт остался
# коротким (в этом вся экономия), но по нему было видно, о чём таблица.
_LABELS_IN_PASSPORT = 12


@dataclass(frozen=True)
class TableBlock:
    """Кусок текста, который выглядит таблицей. Без единой оценки его полезности."""

    id: int
    page: int | None
    lines: tuple[str, ...]
    context: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def rows(self) -> int:
        return len(self.lines)

    @property
    def chars(self) -> int:
        return len(self.text)


def split_tables(text: str) -> tuple[TableBlock, ...]:
    """Отчёт → блоки-таблицы. Два правила, ноль суждений.

    * `===PAGE N===` задаёт текущую страницу и ЗАКРЫВАЕТ открытый блок: таблица
      не может продолжаться через границу страницы, иначе в один блок склеятся
      хвост одной формы и шапка другой;
    * подряд идущие строки с `|` образуют блок; строка без `|` его закрывает.

    ⚠️ ЕСЛИ `|` В ДОКУМЕНТЕ НЕТ ВОВСЕ — блоков не будет, и это не отказ, а
    честный ответ «таблиц не видно». Решение, что делать дальше (единицей станет
    страница), принимается СНАРУЖИ: подставлять страницы здесь значило бы
    выдавать за таблицу то, что ею не является.
    """
    blocks: list[TableBlock] = []
    buf: list[str] = []
    block_page: int | None = None
    block_context: list[str] = []
    current_page: int | None = None
    recent: list[str] = []

    def close() -> None:
        nonlocal buf, block_page, block_context
        if buf:
            blocks.append(TableBlock(
                id=len(blocks) + 1,
                page=block_page,
                lines=tuple(buf),
                context=tuple(block_context),
            ))
        buf, block_page, block_context = [], None, []

    for raw in (text or "").splitlines():
        page = _PAGE.match(raw)
        if page:
            close()
            current_page = int(page.group(1))
            recent = []
            continue
        line = raw.rstrip()
        if "|" in line:
            if not buf:
                block_page = current_page
                block_context = list(recent[-_CONTEXT_LINES:])
            buf.append(line)
            continue
        close()
        stripped = line.strip()
        if stripped:
            recent.append(stripped)
            recent = recent[-8:]
    close()
    return tuple(blocks)


@dataclass(frozen=True)
class Passport:
    """Короткая выписка из блока — то, что уезжает модели вместо всей таблицы.

    Только вырезки из текста. Ни одного поля, которое было бы нашим выводом о
    том, что это за таблица и насколько она подходит: назвать форму и показатели
    — работа модели на шаге 6.3.
    """

    id: int
    page: int | None
    rows: int
    chars: int
    title_lines: tuple[str, ...] = ()
    header_row: str = ""
    labels: tuple[str, ...] = ()
    unit_lines: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "page": self.page,
            "rows": self.rows,
            "chars": self.chars,
            "title_lines": list(self.title_lines),
            "header_row": self.header_row,
            "labels": list(self.labels),
            "unit_lines": list(self.unit_lines),
        }


# Строка единиц измерения: «(в миллионах российских рублей)», «(RUB million)»,
# «тыс. руб.». Это НЕ суждение о смысле таблицы — это выписка строки, которую
# модель всё равно увидит; вынесена отдельным полем, чтобы не потеряться среди
# подписей, потому что от неё зависит масштаб числа.
_UNITS = re.compile(
    r"(тыс\.?|млн|млрд|миллион|миллиард|тысяч|thousand|million|billion|"
    r"руб|rub|USD|EUR|₽|\$|€)", re.IGNORECASE)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_label(cell: str) -> bool:
    """Метка строки — первая ячейка, если в ней есть буквы.

    Проверка на буквы — не суждение о смысле, а отсев пустых ячеек и колонок из
    одних цифр: метка, состоящая только из числа, ничего не скажет модели.
    """
    return any(ch.isalpha() for ch in cell)


def passport(block: TableBlock) -> Passport:
    """Блок → паспорт. Вырезки, и ничего кроме вырезок.

    ШАПКОЙ считается ПЕРВАЯ строка блока, у которой первая ячейка пуста или без
    букв, а дальше идут непустые ячейки, — так печатают заголовок периодов
    («| | 31 декабря 2024 | 31 декабря 2023 |»). Не нашли такой — шапки нет, и
    выдумывать её не надо: модель разберётся по меткам.
    """
    title_lines = tuple(c for c in block.context if not _UNITS.search(c))
    unit_lines = tuple(c for c in block.context if _UNITS.search(c))

    header_row = ""
    labels: list[str] = []
    for line in block.lines:
        cells = _cells(line)
        if not cells:
            continue
        first, rest = cells[0], cells[1:]
        if not header_row and not _is_label(first) and any(c for c in rest):
            header_row = line.strip()
            continue
        if _is_label(first) and len(labels) < _LABELS_IN_PASSPORT:
            labels.append(first)

    return Passport(
        id=block.id,
        page=block.page,
        rows=block.rows,
        chars=block.chars,
        title_lines=title_lines,
        header_row=header_row,
        labels=tuple(labels),
        unit_lines=unit_lines,
    )


def passports(text: str) -> tuple[Passport, ...]:
    """Отчёт → паспорта всех его таблиц. Вход шага 6.3 (единственного с моделью)."""
    return tuple(passport(b) for b in split_tables(text))
