"""Синонимы ОБЪЕДИНЯЮТСЯ при переходе на реестр ячейки, а не замещаются (#147).

ЗАЧЕМ. Файлы ячеек команды правили под свою отчётность, и подсказки «где искать»
там точнее общих — их и надо брать. Но списки синонимов в ячейках только русские.
Замер 01.08.2026 на снимке промпта показал, что переход «в лоб» терял 169
латинских синонимов у ВСЕХ 78 показателей `ifrs_bank_ru` и 192 у 75 в
`ifrs_nf_ru`: уходили «Total assets», «PP&E», «Long-term borrowings»,
«Qualified opinion».

Потеря опасна не абстрактно: русские отчёты по МСФО часто ДВУЯЗЫЧНЫЕ, половина
крупных эмитентов печатает таблицы с английскими заголовками. Обменять точные
подсказки на потерю узнавания — плохая сделка, поэтому берём и то, и другое.
"""
from __future__ import annotations

import re

import pytest

from finengine.skills.bulk_extractor import _merge_aliases_from_base
from finengine.skills.metric_registry import EXTRACTABLE_BANK, EXTRACTABLE_NON_FINANCIAL
from finengine.skills.metric_schema import registry_for_profile

CELLS = [
    ("ifrs_bank_ru", "bank", EXTRACTABLE_BANK),
    ("ifrs_nf_ru", "non_financial", EXTRACTABLE_NON_FINANCIAL),
    ("us_gaap_bank_en", "bank", EXTRACTABLE_BANK),
    ("us_gaap_nf_en", "non_financial", EXTRACTABLE_NON_FINANCIAL),
]


def _aliases(m: dict) -> list[str]:
    out: list[str] = []
    for key in ("aliases_ru", "aliases_en", "aliases"):
        out += list(m.get(key) or [])
    return out


def _fold(xs) -> set[str]:
    return {str(x).strip().casefold() for x in xs}


@pytest.mark.parametrize(("cell", "ct", "base"), CELLS, ids=[c for c, _, _ in CELLS])
def test_no_alias_is_lost(cell, ct, base):
    """Ни один синоним общего реестра не должен пропасть — главное свойство."""
    merged = _merge_aliases_from_base(registry_for_profile(cell)["extractable"], ct)
    by_enum = {m["enum"]: m for m in base}
    lost: list[str] = []
    for row in merged:
        src = by_enum.get(row["enum"])
        if src is None:
            continue
        lost += sorted(_fold(_aliases(src)) - _fold(_aliases(row)))
    assert not lost, f"{cell}: потеряно синонимов {len(lost)}, например {lost[:5]}"


@pytest.mark.parametrize(("cell", "ct", "base"), CELLS, ids=[c for c, _, _ in CELLS])
def test_latin_aliases_survive_in_russian_cells(cell, ct, base):
    """Латиница отдельной проверкой: именно она терялась, и именно она нужна
    на двуязычных отчётах."""
    merged = _merge_aliases_from_base(registry_for_profile(cell)["extractable"], ct)
    by_enum = {m["enum"]: m for m in base}
    for row in merged:
        src = by_enum.get(row["enum"])
        if src is None:
            continue
        latin = {a for a in _fold(_aliases(src)) if re.search(r"[a-z]{3}", a)}
        assert latin <= _fold(_aliases(row)), f"{cell}/{row['enum']}: латиница потерялась"


def test_cell_hints_win_over_base():
    """Подсказки и описания — из ячейки: там сознательная работа команд.

    Пример из живого диффа: у бессрочных облигаций общий реестр говорит «раздел
    КАПИТАЛ баланса или долгосрочные обязательства», а ячейка — «только
    бессрочные/AT1 без срока погашения, не используй обычные выпуски». Второе
    точнее, и объединение синонимов не должно было это затереть.
    """
    rows = registry_for_profile("ifrs_bank_ru")["extractable"]
    merged = _merge_aliases_from_base(rows, "bank")
    cell = {m["enum"]: m for m in rows}
    for row in merged:
        for field in ("hint", "description", "name_ru"):
            assert row.get(field) == cell[row["enum"]].get(field), (
                f"{row['enum']}.{field}: значение ячейки подменено общим реестром")


def test_own_aliases_come_first():
    """Порядок стабильный — иначе промпт менялся бы от запуска к запуску и
    эталон стал бы бесполезен."""
    rows = registry_for_profile("ifrs_bank_ru")["extractable"]
    merged = _merge_aliases_from_base(rows, "bank")
    cell = {m["enum"]: m for m in rows}
    for row in merged:
        own = list(cell[row["enum"]].get("aliases_ru") or [])
        if own:
            assert (row.get("aliases_ru") or [])[: len(own)] == own, (
                f"{row['enum']}: свои синонимы должны идти первыми")


def test_metric_absent_from_base_is_kept_as_is():
    """Показатель, заведённый только ячейкой, не должен потеряться."""
    rows = [{"enum": "USER_ONLY_METRIC", "name_ru": "Своё", "aliases_ru": ["моё"]}]
    got = _merge_aliases_from_base(rows, "bank")
    assert got == rows


def test_merge_is_idempotent():
    """Повторное объединение ничего не добавляет — иначе список рос бы с каждым
    прогоном и промпт пух."""
    rows = registry_for_profile("ifrs_bank_ru")["extractable"]
    once = _merge_aliases_from_base(rows, "bank")
    twice = _merge_aliases_from_base(once, "bank")
    assert [_aliases(m) for m in once] == [_aliases(m) for m in twice]


# ───────────────── проверка ПУТИ, а не функции ─────────────────
# Тесты выше зовут хелпер напрямую и поэтому НЕ ловят отключение объединения на
# живом пути — проверено откатом: снял вызов из `_extract_stream_full_context`,
# все двенадцать остались зелёными. Ровно этот промах стоил нам #137: тест читал
# файл ячейки в обход движка, был зелёным, а на проде шесть коэффициентов не
# считались. Ниже — проверка того, что латиница доезжает ДО МОДЕЛИ.


def _capture_prompt(profile_id: str, company_type: str) -> str:
    """Промпт, перехваченный на РЕАЛЬНОМ пути (модель при этом не зовётся).

    ⚠️ ПЕРЕХВАТЫВАЮТСЯ ОБА ВХОДА В МОДЕЛЬ, и это не перестраховка. Наш движок
    зовёт `llm.stream`, двухстадийная — `llm.complete_with_usage`. Пока стоял
    только `stream`, переключение схемы (#257) уронило этот сторож дважды:
    промпт не перехватывался (проверять стало нечего) И уходил НАСТОЯЩИЙ вызов
    модели с повторами — два теста висели 224 секунды на сеть.

    Сторож обязан проверять правило «справочник ячейки доезжает до промпта»
    при ЛЮБОЙ выбранной схеме: у двухстадийной промптов два, и потерять
    справочник она может ровно так же.
    """
    import asyncio  # noqa: PLC0415

    from finengine.llm import openrouter as llm  # noqa: PLC0415
    from finengine.skills import extract_text as et  # noqa: PLC0415

    seen: list[str] = []

    async def _fake_stream(prompt: str, **kw):  # noqa: ANN003
        seen.append(prompt)
        yield "{}"

    async def _fake_complete_with_usage(prompt="", system=None, **kw):  # noqa: ANN001, ANN003
        # Промптом считаем И системную часть, И пользовательскую: справочник
        # может уехать в любую из них, и проверка не должна зависеть от того,
        # в какую именно.
        seen.append(f"{system or ''}\n{prompt}")
        return "{}", {"prompt_tokens": 0, "completion_tokens": 0}

    async def _run() -> None:
        async for _ in et.extract_stream_from_text(
            "===PAGE 1===\nОтчёт о финансовом положении\nИтого активы 1 000\n",
            company_type=company_type,
            role="other",
            include_segments=False,
            include_consolidated_group=False,
            registry_override=registry_for_profile(profile_id),
        ):
            pass

    orig_stream, orig_complete = llm.stream, llm.complete_with_usage
    llm.stream = _fake_stream
    llm.complete_with_usage = _fake_complete_with_usage
    try:
        asyncio.run(_run())
    finally:
        llm.stream, llm.complete_with_usage = orig_stream, orig_complete
    # Справочник может уехать не в первом промпте: у двухстадийной первый —
    # сжатие контекста. Склеиваем всё, что ушло модели.
    return "\n".join(seen)


def test_latin_aliases_reach_the_actual_prompt():
    """Латиница обязана быть В ПРОМПТЕ, а не только в результате хелпера."""
    prompt = _capture_prompt("ifrs_bank_ru", "bank")
    assert prompt, "движок не дошёл до вызова модели — перехват не сработал"
    for alias in ("Total assets", "Qualified opinion"):
        assert alias in prompt, f"синоним «{alias}» не доехал до модели"


def test_cell_hint_reaches_the_actual_prompt():
    """И подсказка ячейки — тоже в промпте: объединение не должно её затирать."""
    prompt = _capture_prompt("ifrs_bank_ru", "bank")
    assert "бессрочные" in prompt.lower(), "уточнённая подсказка ячейки не доехала"
