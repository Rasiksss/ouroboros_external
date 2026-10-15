"""Детали, перенесённые из Finengine под двухстадийное извлечение (#257).

ЗАЧЕМ ЭТОТ ФАЙЛ. `two_stage_extractor` (боевой движок Finengine с v634) берёт из
нашего пакета 33 символа. Тридцать два были у нас как есть — это и есть главный
довод переносить схему, а не переписывать. Не хватало двух вещей, и обе
перенесены сюда ДОСЛОВНО, вместе с декораторами.

⚠️ ЗАМЫКАНИЕ ОКАЗАЛОСЬ ГЛУБЖЕ, ЧЕМ Я ОЦЕНИЛ. В задаче было записано «одна
функция и один класс». На деле за `FiscalPeriodContext` тянулись ещё пять
функций — 168 строк. Посчитано обходом дерева зависимостей, а не на глаз;
запись в #257 исправлена.

ДВЕ ЛОВУШКИ ПЕРЕНОСА, ОБЕ ПОЙМАНЫ ДО КОММИТА:
  * `@dataclass(frozen=True)` едва не потерялся: у декорированного класса
    `lineno` указывает на строку `class`, а не на декоратор. Класс приехал бы
    обычным — без заморозки и без сгенерированного конструктора, и упал бы не
    здесь, а глубоко внутри переноса;
  * `_model_supported_max_report_year` зовёт три наших символа
    (`_is_forward_looking_period_header`, `_period_year`, `_parse_verbatim_value`)
    — проверено до переноса, а не после.

НИЧТО ИЗ ПЕРЕНЕСЁННОГО ПОКА НЕ ВЫЗЫВАЕТСЯ БОЕВЫМ ПУТЁМ. Это предпосылка, а не
включение: поведение существующего извлечения не менялось.
"""
from __future__ import annotations

from datetime import date

from finengine.skills.bulk_extractor import _model_supported_max_report_year
from finengine.skills.periods import FiscalPeriodContext


def test_the_fiscal_context_is_frozen():
    """ГЛАВНОЕ: `@dataclass(frozen=True)` доехал.

    Потеряй декоратор — класс собрался бы как обычный, без конструктора по полям
    и без заморозки, а падало бы это далеко от места ошибки.
    """
    ctx = FiscalPeriodContext({2024: date(2024, 9, 1)})
    assert ctx.year_end_dates == {2024: date(2024, 9, 1)}
    try:
        ctx.year_end_dates = {}
    except Exception as exc:  # FrozenInstanceError
        assert "frozen" in type(exc).__name__.lower() or "frozen" in str(exc).lower()
    else:
        raise AssertionError("контекст не заморожен — декоратор потерян при переносе")


def test_the_shifted_year_is_read_from_the_document():
    """Сдвинутый финансовый год достаётся из оговорки в начале отчёта."""
    ctx = FiscalPeriodContext.from_text(
        "The fiscal year ended September 1, 2024 and the fiscal year ended "
        "September 3, 2023.")
    assert ctx.end_date_for_year(2024) == date(2024, 9, 1)
    assert ctx.end_date_for_year(2023) == date(2023, 9, 3)
    assert ctx.has_shifted_years(), (
        "сентябрьский конец года не признан сдвигом — заголовок «2024» будет "
        "нормализован как календарный год")


def test_a_calendar_year_is_not_a_shift():
    """Отрицательный контроль: 31 декабря — не сдвиг."""
    ctx = FiscalPeriodContext({2024: date(2024, 12, 31)})
    assert not ctx.has_shifted_years()


def test_the_empty_context_is_harmless():
    """Нет оговорки в тексте — контекст пуст и ничего не сдвигает."""
    empty = FiscalPeriodContext.empty()
    assert empty.year_end_dates == {}
    assert not empty.has_shifted_years()
    assert empty.end_date_for_year(2024) is None


def test_the_year_cutoff_survives_an_empty_input():
    """Перенесённая отсечка года не падает на пустом входе и без отсечки."""
    assert _model_supported_max_report_year(
        {}, detected_max_report_year=2024, total_metrics=0)[0] == 2024
    assert _model_supported_max_report_year(
        {}, detected_max_report_year=None, total_metrics=10)[0] is None


def test_the_year_cutoff_needs_a_third_of_the_metrics():
    """Порог — 35% запрошенных показателей, округление вверх.

    Ниже порога свидетельство модели не перевешивает детерминированную отсечку:
    иначе один упомянутый в тексте квартал сдвинул бы границу отчётного года.
    Половина реестра была слишком строгой для того, что порог защищает: ошибаясь,
    отсечка режет живые отчётные периоды.
    """
    for total, expected in ((10, 4), (9, 4), (3, 2), (1, 1), (0, 1)):
        _, _, threshold = _model_supported_max_report_year(
            {}, detected_max_report_year=2024, total_metrics=total)
        assert threshold == expected, f"порог для {total} показателей: {threshold}"
