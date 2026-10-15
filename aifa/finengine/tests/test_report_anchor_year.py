"""Якорь года решает, какие периоды считаются собственными для отчёта (#P3).

`_filter_plausible_report_periods` отбрасывает всё, что новее якоря, — чтобы
будущие годы из таблиц погашений и долговых примечаний не попали в колонки.
Ровно поэтому якорь и есть то место, где годовой отчёт теряет год, о котором он.

ПЕРЕНОС ИЗ UPSTREAM: коммит 92e48e8 (`tests/test_report_anchor_year.py`),
плюс наш собственный случай — у нас дефект жил не там, где у них.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `_max_report_anchor_year` отбор
`candidates = duration_years + matched_annual_years` (то есть выкинуть
`date_years` из голосования) →
`test_a_bare_balance_sheet_date_outvotes_a_stray_prior_year_quarter` краснеет.
"""
from __future__ import annotations

from finengine.skills.bulk_extractor.period_selection import (
    _filter_plausible_report_periods,
    _max_report_anchor_year,
)

# ВК, консолидированная отчётность за год, закончившийся 31.12.2025, как её
# видит детектор периодов.
VK_FY2025 = [
    "FY 2025",
    "31.12.2025",
    "01.10.2025",
    "29.04.2025",
    "FY 2024",
    "Q4 2024",
    "31.12.2024",
    "01.10.2024",
]

# Русагро, шесть месяцев 2025 — у промежуточного отчёта есть своя длительность.
RUSAGRO_6M2025 = [
    "6M 2025",
    "12M 30.06.2025",
    "30.06.2025",
    "FY 2024",
    "31.12.2024",
    "6M 2024",
]


def test_a_bare_balance_sheet_date_outvotes_a_stray_prior_year_quarter() -> None:
    """НАШ дефект: годовой отчёт распознан датами, без подписи `FY`.

    Воспроизведён запуском нашей же функции до правки: список отдавал якорь
    2024, и единственная дата 2025 года вылетала из отчёта —
    `_filter_plausible_report_periods` оставляла `['Q4 2024', '31.12.2024']`.
    """
    assert _max_report_anchor_year(["31.12.2025", "31.12.2024", "Q4 2024"]) == 2025
    kept = _filter_plausible_report_periods(["31.12.2025", "31.12.2024", "Q4 2024"])
    assert "31.12.2025" in kept


def test_a_stray_prior_year_quarter_does_not_outvote_the_balance_sheet_date() -> None:
    """Их случай: `Q4 2024` выигрывал, и все периоды 2025 уходили как будущий год."""
    assert _max_report_anchor_year(VK_FY2025) == 2025
    kept = _filter_plausible_report_periods(list(VK_FY2025))
    assert "FY 2025" in kept
    assert "31.12.2025" in kept


def test_interim_report_is_unchanged() -> None:
    """Раньше работало лишь по случайности: `6M 2025` сам по себе длительность."""
    assert _max_report_anchor_year(RUSAGRO_6M2025) == 2025
    kept = _filter_plausible_report_periods(list(RUSAGRO_6M2025))
    assert "6M 2025" in kept
    assert "30.06.2025" in kept


def test_future_years_from_a_maturity_schedule_are_still_dropped() -> None:
    periods = ["6M 2025", "30.06.2025", "FY 2024", "31.12.2024", "FY 2027", "FY 2029"]
    kept = _filter_plausible_report_periods(list(periods))
    assert "FY 2027" not in kept
    assert "FY 2029" not in kept
    assert "6M 2025" in kept


def test_an_annual_label_alone_still_anchors_when_no_date_backs_it() -> None:
    """Отчёт, распознанный одними подписями `FY`, держит самую позднюю."""
    assert _max_report_anchor_year(["FY 2025", "FY 2024"]) == 2025


def test_an_annual_label_defers_to_a_date_of_the_same_year() -> None:
    """`FY 2026` из примечания о годе вперёд не должна становиться якорем."""
    assert _max_report_anchor_year(["FY 2026", "FY 2025", "31.12.2025"]) == 2025


def test_a_maturity_date_from_a_note_does_not_become_the_anchor() -> None:
    """МКБ, третий квартал 2025: `31.12.2064` — срок погашения, не отчётная дата.

    Голая дата обязана голосовать, иначе годовой отчёт теряет свой год. Но
    пустить её голосовать без границы значит отдать якорь сроку погашения:
    якорь 2064 не отсекает ничего, и периодом прогона становится он сам.
    Замер на 85 golden-документах с PDF: без границы так ломаются семь —
    МКБ, ДОМ.РФ, БСПб, Самсунг, Нестле, Катерпиллер, Bank First.
    """
    periods = ["31.12.2064", "Q3 2025", "9M 2025", "30.09.2025", "FY 2024", "31.12.2024"]
    assert _max_report_anchor_year(periods) == 2025
    assert "31.12.2064" not in _filter_plausible_report_periods(list(periods))


def test_an_event_date_a_year_ahead_does_not_extend_the_anchor() -> None:
    """Bank First, третий квартал 2025: `06.08.2027` — дата события, не баланса.

    Перешагнуть горизонт отрезков и годовых подписей дата вправе только будучи
    концом квартала. `06.08` балансовой датой не бывает.
    """
    periods = ["06.08.2027", "12M 17.02.2026", "Q3 2025", "9M 2025", "30.09.2025", "FY 2024"]
    assert _max_report_anchor_year(periods) == 2026


def test_a_quarter_end_two_years_past_the_horizon_does_not_vote() -> None:
    """Форточка открыта РОВНО на один год — и закреплено это здесь.

    ⚠️ Значение «один» свидетельством не подпёрто, и делать вид, что подпёрто,
    нечестно. Замер: замена `horizon + 1` на `horizon + 2` оставляет все
    остальные тесты этого файла зелёными и не меняет НИ ОДНОГО из 85
    golden-документов с PDF — ни якоря, ни списка оставленных периодов. На
    нашей выборке ширина не решает ничего; выбрана она из осторожности —
    отчётной датой бывает конец года, о котором отчёт, а не конец следующего
    за ним.

    Тест держит контракт, а не замеренный факт: расширить форточку теперь
    можно, но только осознанно — покрасив этот сторож, а не молча.
    """
    periods = ["31.12.2027", "Q3 2025", "9M 2025", "30.09.2025", "FY 2024", "31.12.2024"]
    assert _max_report_anchor_year(periods) == 2025
    assert "31.12.2027" not in _filter_plausible_report_periods(list(periods))


def test_dates_alone_still_anchor_when_nothing_else_was_detected() -> None:
    """Ни отрезков, ни годовых подписей — горизонта нет, и границы тоже."""
    assert _max_report_anchor_year(["17.02.2026", "31.12.2025"]) == 2026

def test_deutsche_bank_2025_keeps_the_year_it_reports_on() -> None:
    """Единственный golden-документ, ответ по которому эта правка чинит.

    Годовой отчёт Дойчбанка за 2025: детектор нашёл `31.12.2025`, но подписи
    `FY 2025` в тексте нет — только `FY 2024`. До правки якорем становился
    2024, `_filter_plausible_report_periods` выбрасывала ВСЕ даты 2025, и
    период прогона выходил `FY 2024` при прошлом `FY 2023`. Весь отчёт за 2025
    возвращался бы как отчёт за 2024.

    Список снят прогоном детектора по
    `benchmarks/.../ifrs_banks_en/pdf/deutsche-bank-ag-2025.pdf` и приведён
    к виду теста, а не скопирован дословно: `12M 31.12.2024` записана как
    `FY 2024` (это её `normalize_period_label`), а два вхождения 2015 года
    (`23.12.2015`, `20.08.2015`) убраны как шум примечаний. Исход от этого не
    меняется — на сыром списке детектора якорь тоже 2025, проверено запуском.
    Разметка того же документа стоит на `31.12.2025` и `FY 2025`.

    `20.02.2026` — дата подписания, и держит её вне якоря требование КОНЦА
    КВАРТАЛА: `20.02` в `_PERIOD_END_DAYS` не входит, поэтому в голосование
    такая дата не попадает ни при какой ширине форточки. Ширина здесь ни при
    чём — при горизонте 2024 (по `FY 2024`) `20.02.2026` отсеивается и
    форточкой в один год, и форточкой в два. Про саму ширину — сторож
    `test_a_quarter_end_two_years_past_the_horizon_does_not_vote` выше.
    """
    periods = [
        "20.02.2026",
        "31.12.2025",
        "20.12.2025",
        "02.10.2025",
        "22.08.2025",
        "FY 2024",
        "31.12.2024",
        "25.01.2023",
    ]
    assert _max_report_anchor_year(periods) == 2025
    kept = _filter_plausible_report_periods(list(periods))
    assert "31.12.2025" in kept
    assert "20.02.2026" not in kept
