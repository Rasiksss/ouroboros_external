"""Колонка «Источник» заполняется, даже когда модель не назвала страницу.

ДЕФЕКТ (прогон владельца по Россельхозбанку, 19.08.2026): `page` пуст у ВСЕХ
110 строк, `source_form` — тоже. Колонка «Источник» пустая при полностью
удачном извлечении.

ПРИЧИНА НЕ В ТЕКСТЕ И НЕ В МОДЕЛИ. Текст размечен страницами (46 маркеров
`===PAGE`), значит `has_pages=True` и шаблон ответа страницу спрашивает. Но
спрашивает так:

    '{"values": {…}, "unit_text": "__RAW_UNIT_TEXT__", "page": null}'

У каждого поля плашка «сюда подставь», у страницы — литеральный `null`.
Модель возвращает то, что ей показали.

РЕШЕНИЕ: не переписывать промпт (это меняет форму ответа и результат, а
проверяется только живым прогоном), а взять то, что мы уже знаем сами. Стадия
сжатия выбрала для показателя конкретные таблицы, и страница каждой таблицы
известна нашему коду без участия модели.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `_page_from_evidence` из сборки строки в
`_extract_stream_two_stage_full_context` — покраснеет
`test_the_page_falls_back_to_the_table_the_compression_chose`.
"""
from __future__ import annotations

from finengine.skills import two_stage_extractor as ts


def _table(table_id: str, page: int) -> ts.ReportTable:
    """Настоящая таблица отчёта, а не заглушка: путь к странице идёт через
    разбор её текста, и подделка проверяла бы не тот код."""
    return ts.ReportTable(
        table_id=table_id, page=page, title="Отчёт о финансовом положении",
        text="| Денежные средства и их эквиваленты | 605 868 | 908 529 |")


def _setup():
    tables = {"t1": _table("t1", 5), "t2": _table("t2", 18)}
    compression = {"metrics": {
        "CASH_EQUIVALENTS": {"tables": [{"table_id": "t1"}], "quotes": []},
        "SECURITIES_FVPL": {"tables": [{"table_id": "t2"}], "quotes": []},
        "GOODWILL": {"tables": [], "quotes": [{"page": 23, "text": "…"}]},
        "PPE": {"tables": [], "quotes": []},
    }}
    return compression, tables


def test_the_page_falls_back_to_the_table_the_compression_chose():
    """Страница — та, на которой лежит выбранная сжатием таблица."""
    compression, tables = _setup()
    assert ts._page_from_evidence("CASH_EQUIVALENTS", compression, tables) == 5
    assert ts._page_from_evidence("SECURITIES_FVPL", compression, tables) == 18


def test_a_quote_page_is_used_when_there_is_no_table():
    """Показатель раскрыт текстом примечания — страница берётся из цитаты."""
    compression, tables = _setup()
    assert ts._page_from_evidence("GOODWILL", compression, tables) == 23


def test_no_evidence_means_no_page_not_a_guess():
    """Улик нет — страницы нет. Выдуманная ссылка хуже пустой клетки."""
    compression, tables = _setup()
    assert ts._page_from_evidence("PPE", compression, tables) is None
    assert ts._page_from_evidence("НЕИЗВЕСТНЫЙ", compression, tables) is None


def test_the_flat_row_actually_uses_the_fallback():
    """СТОРОЖ ДОСТАВКИ: запасной путь позван там, где собирается строка.

    Функция может быть безупречной и не значить ничего — ровно так колонка
    «Источник» и пустовала: страницу спрашивали, ответ не приходил, запасного
    пути не было.
    """
    import inspect

    src = inspect.getsource(ts._extract_stream_two_stage_full_context)
    assert "_page_from_evidence(enum, compression, table_by_id)" in src, (
        "строка собирается без запасного пути — колонка «Источник» снова будет "
        "пустой при полностью удачном извлечении")
