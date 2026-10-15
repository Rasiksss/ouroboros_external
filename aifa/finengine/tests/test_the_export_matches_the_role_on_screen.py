"""Книга Excel выходит в формате той же роли, что на экране (Б2).

ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «выгрузка должна выходить в формате той же роли».

ЧТО БЫЛО. `finalize_extraction` собирал ДВЕ разные таблицы по одному прогону:
на экран — раскладку роли (`build_role_layout` → ДИЭ/CIB/Риски), а в
«Источники» — плоскую книгу всех показателей (`build_metrics_workbook`), про
роль не знавшую вовсе (`grep role src/finengine/skills/metrics_xlsx.py` — ни
одного совпадения). Человек видел на экране 22 строки своей роли, скачивал
файл и получал 122 чужих: какая из двух таблиц «настоящая», по файлу понять
было нельзя.

ЧТО СТАЛО. Есть раскладка — книга собирается ЕЮ, тем же форматтером ролей,
что рисует экран (`roles.formatter.write_formatted_xlsx`). Нет раскладки
(роль «Общая», или пара роль×отчётность без формата — ДГР и БАИФП по
нефинансовой компании: их нет и у Finengine, проверено по layouts.json) —
книгу по-прежнему собирает `build_metrics_workbook`.

ПАСПОРТ ПРОГОНА (#155) ДОПИСЫВАЕТСЯ ОТДЕЛЬНЫМ ЛИСТОМ. У форматтера ролей своей
книги с паспортом нет, а отпечаток реестра — единственное, чем объясняется
расхождение вчерашней выгрузки с сегодняшней. Потерять его на ролевом экспорте
значило бы починить одно, сломав другое.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `finalize_extraction` безусловный
`build_metrics_workbook` — красен `test_finalize_builds_the_book_from_the_role`;
убрать дописывание паспорта — `test_the_passport_survives_the_role_export`.
"""
from __future__ import annotations

import inspect
import io
import re

import pytest

from finengine.roles.formatter import build_formatted_table
from finengine.roles.scope import build_role_plan
from finengine.skills import extract_result

PASSPORT = [("Реестр показателей", "ifrs_nf_ru v1"), ("Прогон", "2026-08-20")]


def _row(enum: str, name: str, cur: float, prev: float) -> dict:
    return {
        "enum": enum, "name_ru": name, "value_current": cur, "value_prev": prev,
        "unit": "млн руб.", "is_calculated": False, "found": True,
        "period_values": [{"period": "2025", "value": cur, "unit": "млн руб."},
                          {"period": "2024", "value": prev, "unit": "млн руб."}],
    }


TABLE = [_row("SALES_REVENUE", "Выручка", 178733.0, 188706.0),
         _row("NET_INCOME", "Чистая прибыль", 21000.0, 30000.0),
         _row("TOTAL_ASSETS", "Итого активы", 900000.0, 880000.0)]
META = {"period_current": "2025", "period_prev": "2024", "unit": "млн руб.",
        "company_type": "non_financial", "company_name": "Северсталь"}


def _formatted(role: str) -> dict | None:
    plan = build_role_plan(role, "non_financial")
    if not plan.config:
        return None
    return build_formatted_table(plan=plan, metrics=TABLE, meta=META)


def _sheets(content: bytes) -> list[str]:
    import openpyxl

    return openpyxl.load_workbook(io.BytesIO(content)).sheetnames


def test_a_role_layout_becomes_the_book():
    """Роль с раскладкой — книга её формата, а не плоская."""
    table = _formatted("die")
    assert table, "предпосылка ушла: у роли ДИЭ по нефинансовой компании была раскладка"
    content = extract_result.build_role_workbook(table, passport=PASSPORT)
    assert content, (
        "раскладка роли есть, а книги по ней нет — человек снова скачает не то, "
        "что видит на экране")
    assert _sheets(content)[0] != "Показатели", (
        "первый лист называется как у плоской книги — похоже, собралась она же")


def test_the_passport_survives_the_role_export():
    """Отпечаток реестра (#155) остаётся в книге и при ролевом экспорте."""
    content = extract_result.build_role_workbook(_formatted("cib"), passport=PASSPORT)
    assert "Паспорт прогона" in _sheets(content), (
        "лист паспорта пропал: расхождение вчерашней выгрузки с сегодняшней "
        "объяснить будет нечем")


def test_no_layout_means_no_role_book():
    """Раскладки нет — книгу собирает прежний путь, а не пустышка."""
    assert extract_result.build_role_workbook(None, passport=PASSPORT) is None
    assert extract_result.build_role_workbook({"rows": []}, passport=PASSPORT) is None
    assert extract_result.build_role_workbook(
        {"rows": [{"kind": "section", "label": "Раздел"}]}, passport=PASSPORT) is None, (
        "книга собрана из одних заголовков разделов — в файле не будет ни одного числа")


def test_the_generic_role_still_gets_the_flat_book():
    """«Общая» — плоская книга по устройству: у неё нет и не может быть раскладки."""
    assert _formatted("other") is None, (
        "у роли «Общая» появилась раскладка — тогда правило выбора книги надо "
        "пересматривать, а не проверять этим тестом")


@pytest.mark.parametrize("role", ["dgr", "baifp"])
def test_a_role_without_a_layout_for_this_report_falls_back(role: str):
    """ДГР и БАИФП по нефинансовой компании — раскладки нет НИ У НАС, НИ У НИХ.

    Проверено по `roles/layouts.json`: у обеих ролей объявлен только формат
    `bank`. Это не наш пробел переноса, и «таблица не перестроилась» для них —
    правильное поведение, а не дефект.
    """
    assert _formatted(role) is None
    assert build_role_plan(role, "non_financial").format == "unsupported"


def _code(src: str) -> str:
    """Исходник без комментариев: сторож обязан читать код, а не пояснения к нему."""
    return re.sub(r"#.*", "", src)


def test_finalize_builds_the_book_from_the_role():
    """СТОРОЖ ДОСТАВКИ: функция позвана там, где книга собирается."""
    src = _code(inspect.getsource(extract_result.finalize_extraction))
    assert "build_role_workbook(formatted" in src, (
        "книга снова собирается мимо раскладки роли — на экране одна таблица, "
        "в «Источниках» другая")
    assert src.index("formatted = build_role_layout") < src.index("build_role_workbook("), (
        "раскладка считается ПОСЛЕ книги — значит книге достаётся None, и "
        "ролевой экспорт молча не работает")
    assert "build_metrics_workbook(" in src, (
        "запасной путь снесён: у роли «Общая» и у пар без формата книги не будет вовсе")
